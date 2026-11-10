"""nightly_rematch -- the decision hub's nightly handler (in-VPC; no internet, no model).

Spec: docs/superpowers/specs/2026-11-10-nightly-rematch-agent-design.md §3 (J2, J3), §4
(when, through the hub, flag), §6 (brakes). Plan Task 6.

The tick (lambda_decision_scan, scan `nightly_tick`, hourly) emits one `nightly.due` site
event per due site (`due_sites`); the hub dispatches the `nightly.due` run-rule to `run`
with a RunContext. `run` does, for the event's site:

  1. J3 catch-up. `on`: claim the company's failed-recompute markers one at a time (at most
     J3_MAX), re-run claim_type_store.recompute_session over all the company's sites, and
     resolve the marker on success. `shadow`: count the markers only.
  2. Circuit breaker (`on` only). Last night's request artifacts for this site stamped
     flag=on that have no nightly_support record (input_key = the artifact key) are
     "unanswered"; at BREAKER_AT or more the worker is failing, so no J2 request is written
     tonight and one alert line is logged. Shadow artifacts never count.
  3. J2 request build (only items never asked since their row was written; see
     repositories.nightly.sessions_needing_support). The site's finals of the last 7 days with unsupported live items
     (at most 20 sessions). Per session: drop group (`grp…`) sessions, sessions whose
     meeting_session is not final yet, items with no text or no parseable time_range, cap
     at the worker's MAX_ITEMS; skip a session whose request key is already answered or
     already exists in S3 (so a hub retry never re-PUTs a key and S3 never fires twice for
     one session); stop at the per-company daily item budget (today's nightly_support records
     plus the items the company's other sites requested tonight, nightly_requested_items,
     which only `on` writes). The artifacts are written
     LAST, after every read that can raise, each stamped with the flag. In shadow the
     worker is not wired, so nothing reads them.

  4. J1 catch-up for late arrivals (spec §3 J1), after the J2 plan and BEFORE the J2 puts
     (the J2 artifacts stay the run's last writes). `stale_pairs`: a session L whose
     final was written in the last 72 h makes an earlier-written final N stale when L was
     RECORDED before N (continuity_xday.recorded_earlier, the #1273 rule) and inside N's
     window on a line (reconcile: same recorder and NZ date; cross-day: 28 days). `on`: per
     stale (line, N), newest N first, at most J1_MAX: record_reemit per L FIRST (a pair
     already recorded is skipped), then re-read N's final from S3, call the OWNING line's
     builder (cross-day with nonce=night; both stamped trigger "nightly") and put the
     artifact under that line's prefix as the pair's last step. Each pair runs in its own
     savepoint: a failed read or build, or a builder answering None, rolls the record back,
     so a nightly_reemits row always names an artifact that was put. A line whose own flag
     is off (CROSS_DAY_CONTINUITY / RECONCILE_STATUS, the item writer's) is never
     re-emitted, nor one in shadow (exactly "on" only). `shadow`: count the stale pairs only (no record, no S3). Nightly builds no
     prompt and writes no continues/status rows; the owning lines do.

Returns "nothing" when there was no work, else "done". One counts line per run (ids,
counts and enums only -- never item text).

In shadow the only DB writes are the hub's own event and run rows: J3 counts, J2 writes
S3 objects only, J1 counts.
"""
import datetime as _dt
import hashlib
import json
import logging
import os
import re
import time

from psycopg.errors import QueryCanceled

import continuity_xday
import continuity_xday_request
import lake_keys
import nz_time
import reconcile_request
import tenancy_status
from lake_put import lake_put
from nightly_turn_pick import MAX_ITEMS
from repositories import claim_type_store, item_history, nightly
from topic_window import _topic_window

logger = logging.getLogger(__name__)

PREFIX = "nightly_requests/"
EVENT_TYPE = "nightly.due"
SOURCE = "nightly_scan"
LOCAL_HOUR = 1          # 01:00 site-local: after quiet hours, before the 04:00/05:00 reports
LAST_HOUR = 4           # the range absorbs EventBridge jitter; the dedupe key keeps it to one
LOOKBACK_DAYS = 7
MAX_SESSIONS = 20
J3_MAX = 20
BREAKER_AT = 5
BUDGET_ENV = "NIGHTLY_MAX_ITEMS_PER_COMPANY_DAY"
BUDGET_DEFAULT = 2000
RECOMPUTE_STATEMENT_TIMEOUT = "15s"
# The scan Lambda's Timeout is 120 s. A run past its budget starts no further work (counted
# time_paused) so it finishes and commits rather than being killed and redone forever.
RUN_BUDGET_S = 45.0
_SID = re.compile(r"^sid([0-9a-f]{32})$")
# J1 (spec §3, §6): re-emits per site per night, look-back for late finals, and the oldest
# recording a late arrival may be (cross-day's own window).
J1_MAX = 10
# 72 h, not 24: pairs over the cap (and drift between run times, the 25 h DST night) wait
# for the next night (spec §6); the unique key and the detection-time exclusion of recorded
# pairs make the overlap safe.
J1_LATE_HOURS = 72
J1_WINDOW_DAYS = continuity_xday.WINDOW_DAYS

COUNT_KEYS = ("j2_sessions", "j2_items", "j3_recomputed", "j3_failed", "j3_pending",
              "budget_paused", "skipped_no_window", "skipped_no_text", "skipped_group",
              "skipped_live", "skipped_existing", "items_over_cap", "j2_breaker_open",
              "time_paused", "j1_stale", "j1_reemitted", "j1_over_cap", "j1_time_paused",
              "j1_line_off", "j1_already", "j1_built_none", "j1_failed", "j1_skipped_group")


# --------------------------------------------------------------------------- when

def site_local_now(site, now_utc):
    """The site's local wall clock (naive). v1: every site is Pacific/Auckland through
    nz_time; this is the one seam for a later sites.timezone column."""
    return nz_time.to_nz(now_utc)


def due_sites(conn, now_utc, local_hour=LOCAL_HOUR, last_hour=LAST_HOUR):
    """[{site_id, company_id, night}] for every site whose local hour is in
    [local_hour, last_hour] and whose night (the previous local date) has no nightly.due
    event yet. Sites of a company that is not active are left out: the hub would block
    their event anyway (tenancy_status.company_status, independent of
    COMPANY_STATUS_ENFORCE), so there is no point emitting it."""
    inactive = {str(c) for c in tenancy_status.inactive_company_ids(conn)}
    candidates = []
    for site_id, company_id in conn.execute(
            "SELECT id, company_id FROM sites WHERE company_id IS NOT NULL ORDER BY id"
    ).fetchall():
        if str(company_id) in inactive:
            continue
        local = site_local_now({"id": site_id}, now_utc)
        if not (local_hour <= local.hour <= last_hour):
            continue
        candidates.append({"site_id": site_id, "company_id": company_id,
                           "night": local.date() - _dt.timedelta(days=1)})
    if not candidates:
        return []
    seen = {(str(r[0]), r[1]) for r in conn.execute(
        "SELECT site_id, bucket FROM site_events WHERE type = %s AND status <> 'duplicate' "
        "AND site_id = ANY(%s) AND bucket = ANY(%s)",
        (EVENT_TYPE, [c["site_id"] for c in candidates],
         sorted({c["night"].isoformat() for c in candidates}))).fetchall()}
    return [c for c in candidates if (str(c["site_id"]), c["night"].isoformat()) not in seen]


def request_key(site_id, night, extraction_key):
    """nightly_requests/{site_id}/{night}/{hash}.json -- deterministic per session and night,
    so a hub retry finds its own artifact."""
    night = night.isoformat() if hasattr(night, "isoformat") else str(night)
    digest = hashlib.sha256(str(extraction_key).encode("utf-8")).hexdigest()[:20]
    return f"{PREFIX}{site_id}/{night}/{digest}.json"


# --------------------------------------------------------------------------- run

def run(conn, event, ctx):
    """The nightly.due run-rule (decision_rules). Returns "done" or "nothing"."""
    return run_counts(conn, event, ctx)[0]


def run_counts(conn, event, ctx, *, clock=time.monotonic):
    """(outcome, counts). Raises -- a failed hub attempt, retried with backoff -- on missing
    storage, a bad night, a DB error or an S3 error other than "not found"."""
    if ctx.s3_client is None or not ctx.bucket:
        raise RuntimeError("nightly run needs s3_client and bucket (LAKE_BUCKET)")
    night = _dt.date.fromisoformat(str((event.payload or {}).get("night"))[:10])
    started = clock()
    counts = dict.fromkeys(COUNT_KEYS, 0)
    company_id, site_id = ctx.company_id, ctx.site_id

    if ctx.flag == "on":
        _j3_catch_up(conn, company_id, counts, started, clock)
    else:
        counts["j3_pending"] = len(nightly.failed_recomputes(
            conn, company_id=company_id, limit=J3_MAX))

    planned = []
    if ctx.flag == "on" and _breaker_open(conn, ctx, night, counts):
        pass
    else:
        planned = _plan_j2(conn, ctx, night, counts, started, clock)

    if planned and ctx.flag == "on":
        # The budget across sites (in the run's transaction; shadow writes nothing extra).
        nightly.add_requested_items(conn, site_id=site_id, company_id=company_id,
                                    night=night, items=sum(len(b["items"]) for _, b in planned))

    # J1 BEFORE the J2 puts: its detection reads can raise (a failed hub attempt), and must
    # do so while no J2 artifact exists yet. Its re-emits each run in their own savepoint
    # (put last) and never raise out of the run.
    groups = _stale_groups(conn, company_id=company_id, site_id=site_id,
                           now_utc=ctx.now_utc)
    _j1_catch_up(conn, ctx, night, groups, counts, started, clock)

    # Last: the J2 artifacts. Nothing below can be undone by a later raise.
    for key, body in planned:
        lake_put(ctx.bucket, key, json.dumps(body, ensure_ascii=False), client=ctx.s3_client,
                 owner_hint={"site_id": str(site_id)}, ContentType="application/json")
    counts["j2_sessions"] = len(planned)
    counts["j2_items"] = sum(len(b["items"]) for _, b in planned)

    logger.info(
        "nightly site=%s j2_sessions=%d j2_items=%d j3_recomputed=%d budget_paused=%d "
        "skipped_no_window=%d j1_stale=%d j1_reemitted=%d flag=%s counts=%s", site_id,
        counts["j2_sessions"], counts["j2_items"], counts["j3_recomputed"],
        counts["budget_paused"], counts["skipped_no_window"], counts["j1_stale"],
        counts["j1_reemitted"], ctx.flag, json.dumps(counts, sort_keys=True))
    worked = counts["j2_sessions"] or counts["j3_recomputed"] or counts["j1_reemitted"]
    return ("done" if worked else "nothing"), counts


def _over_budget(started, clock, counts):
    if clock() - started > RUN_BUDGET_S:
        counts["time_paused"] += 1
        return True
    return False


def _j3_catch_up(conn, company_id, counts, started, clock):
    """Each marker in its own savepoint: claim (row lock), recompute, resolve on success.
    A failed or timed-out recompute leaves the marker for the next night. recompute_session
    sets statement_timeout LOCAL; the previous value is restored for the rest of the hub's
    transaction."""
    prev_timeout = conn.execute("SELECT current_setting('statement_timeout')").fetchone()[0]
    tried = []
    try:
        for _ in range(J3_MAX):
            if _over_budget(started, clock, counts):
                break
            try:
                with conn.transaction():
                    marker = nightly.claim_one_recompute(conn, company_id, exclude=tried)
                    if marker is None:
                        break
                    base = marker["session_base"]
                    tried.append(base)
                    res = claim_type_store.recompute_session(
                        conn, company_id=company_id, site_id=None, session_base=base,
                        statement_timeout=RECOMPUTE_STATEMENT_TIMEOUT)
                    if res.get("timed_out") or res.get("failed"):
                        counts["j3_failed"] += 1
                    else:
                        nightly.resolve_recompute(conn, company_id, base)
                        counts["j3_recomputed"] += 1
            except QueryCanceled:
                counts["j3_failed"] += 1
                break
            except Exception as exc:  # noqa: BLE001 - one marker never stops the night
                counts["j3_failed"] += 1
                logger.warning("nightly j3: recompute failed company=%s: %s", company_id,
                               type(exc).__name__)
    finally:
        conn.execute("SELECT set_config('statement_timeout', %s, true)", (prev_timeout,))


def _list_keys(s3, bucket, prefix):
    keys, token = [], None
    while True:
        kw = {"Bucket": bucket, "Prefix": prefix}
        if token:
            kw["ContinuationToken"] = token
        page = s3.list_objects_v2(**kw)
        keys.extend(o["Key"] for o in page.get("Contents") or [])
        if not page.get("IsTruncated"):
            return keys
        token = page.get("NextContinuationToken")


def _breaker_open(conn, ctx, night, counts):
    """True when last night's flag=on requests for this site left BREAKER_AT or more
    without a nightly_support record."""
    prev = night - _dt.timedelta(days=1)
    on_keys = []
    for key in _list_keys(ctx.s3_client, ctx.bucket, f"{PREFIX}{ctx.site_id}/{prev}/"):
        body = json.loads(ctx.s3_client.get_object(Bucket=ctx.bucket, Key=key)["Body"].read())
        if isinstance(body, dict) and body.get("flag") == "on":
            on_keys.append(key)
    if len(on_keys) < BREAKER_AT:
        return False
    missing = len(set(on_keys) - nightly.answered_input_keys(
        conn, ctx.company_id, ctx.site_id, on_keys))
    if missing < BREAKER_AT:
        return False
    counts["j2_breaker_open"] = 1
    logger.warning("nightly breaker site=%s missing=%d", ctx.site_id, missing)
    return True


def _exists(s3, bucket, key):
    """head_object; only a real "not found" is False. A 403 (e.g. a missing ListBucket) is
    raised: read as "missing" it would re-PUT on every retry."""
    from botocore.exceptions import ClientError
    try:
        s3.head_object(Bucket=bucket, Key=key)
        return True
    except ClientError as exc:
        code = str((exc.response or {}).get("Error", {}).get("Code", ""))
        if code in ("404", "NoSuchKey", "NotFound"):
            return False
        raise


def _request_items(items, date, counts):
    """The items the worker can ask about: text and a parseable time_range, at most
    MAX_ITEMS (the worker's cap), in the repository's order."""
    out = []
    for it in items:
        if not (it.get("text") or "").strip():
            counts["skipped_no_text"] += 1
            continue
        if _topic_window(it.get("time_range"), date, []) is None:
            counts["skipped_no_window"] += 1
            continue
        if len(out) >= MAX_ITEMS:
            counts["items_over_cap"] += 1
            continue
        out.append({k: it.get(k) for k in ("stable_id", "row_id", "kind", "text",
                                           "topic_index", "time_range")})
    return out


def _budget():
    try:
        return int(os.environ.get(BUDGET_ENV) or BUDGET_DEFAULT)
    except ValueError:
        return BUDGET_DEFAULT


def _plan_j2(conn, ctx, night, counts, started, clock):
    """[(key, body)] to write, after every read that can raise."""
    sessions = nightly.sessions_needing_support(
        conn, company_id=ctx.company_id, site_id=ctx.site_id,
        since_utc=ctx.now_utc - _dt.timedelta(days=LOOKBACK_DAYS), limit=MAX_SESSIONS)
    candidates = []
    for sess in sessions:
        base = sess.get("session_base") or ""
        if not base or base.startswith("grp"):
            counts["skipped_group"] += 1
            continue
        items = _request_items(sess["items"], sess["date"], counts)
        if items:
            candidates.append((sess, items))
    sids = {sess["session_base"]: m.group(1) for sess, _ in candidates
            for m in [_SID.match(sess["session_base"])] if m}
    live = nightly.live_session_ids(conn, list(sids.values()))
    keyed = []
    for sess, items in candidates:
        if sids.get(sess["session_base"]) in live:
            counts["skipped_live"] += 1
            continue
        keyed.append((request_key(ctx.site_id, night, sess["extraction_key"]), sess, items))
    answered = nightly.answered_input_keys(conn, ctx.company_id, ctx.site_id,
                                           [k for k, _, _ in keyed])
    budget = _budget()
    used = nightly.nightly_support_count_today(
        conn, ctx.company_id, nz_time.to_nz(ctx.now_utc).date())
    used += nightly.requested_items_other_sites(conn, ctx.company_id, ctx.site_id, night)
    planned = []
    for i, (key, sess, items) in enumerate(keyed):
        if key in answered or _exists(ctx.s3_client, ctx.bucket, key):
            counts["skipped_existing"] += 1
            continue
        if used + len(items) > budget:
            counts["budget_paused"] += len(keyed) - i
            break
        if _over_budget(started, clock, counts):
            break
        used += len(items)
        planned.append((key, {
            "version": 1, "flag": ctx.flag, "run_id": str(ctx.run_id),
            "site_id": str(ctx.site_id), "night": night.isoformat(),
            "extraction_key": sess["extraction_key"], "session_base": sess["session_base"],
            "user_folder": sess["user_folder"], "date": sess["date"], "items": items}))
    return planned


# --------------------------------------------------------------------------- J1

def _iso(value):
    if value is None:
        return None
    if isinstance(value, _dt.datetime):
        return value.astimezone(_dt.timezone.utc).isoformat()
    return value.isoformat() if hasattr(value, "isoformat") else str(value)


def _finals(conn, company_id, site_id, now_utc):
    """The site's finals recorded within the window, as dicts with the parsed key, the
    recording date and instant (item_history's rule: meeting_session.opened_at by sid, else
    the transcript stem; None = unknown) and the final's write time. Group passes and
    sessions whose meeting_session is not final yet are left out."""
    today = site_local_now({"id": site_id}, now_utc).date()
    since = today - _dt.timedelta(days=J1_WINDOW_DAYS + 1)
    out = []
    for row in nightly.site_finals(conn, company_id=company_id, site_id=site_id,
                                   since_date=since):
        parsed = lake_keys.parse(row["extraction_key"])
        if (not parsed or parsed["kind"] != "extraction" or parsed.get("group_id")
                or not parsed.get("session_base")):
            continue
        if (row["session_status"] is not None
                and row["session_status"] not in nightly.FINAL_MEETING_STATUSES):
            continue
        recorded_at = item_history._recorded_at(row["extraction_key"], row["opened_at"])
        out.append({"key": row["extraction_key"], "folder": parsed["folder"],
                    "date": row["date"], "recorded_at": recorded_at,
                    "written_at": row["written_at"],
                    "order": (row["date"].isoformat(), _iso(recorded_at))})
    return out


def _stale_groups(conn, *, company_id, site_id, now_utc):
    """Every stale (line, N) with its late arrivals, newest N first (N's recording date,
    then its recording instant, unknown last, then its key -- the cross-day sort order);
    pairs already in nightly_reemits are left out. Each group:
    {line, n: final, l_keys: [L keys, latest-written first]}."""
    finals = _finals(conn, company_id, site_id, now_utc)
    late_since = now_utc - _dt.timedelta(hours=J1_LATE_HOURS)
    lates = [f for f in finals if late_since <= f["written_at"] <= now_utc]
    if not lates:
        return []
    done = nightly.recorded_reemit_pairs(conn, site_id)
    groups = {}
    for late in lates:
        for n in finals:
            if n["key"] == late["key"] or not n["written_at"] < late["written_at"]:
                continue
            # The #1273 rule: both instants -> compare; else the date strictly earlier; a
            # same-date pair without both instants is unordered and skipped.
            if not continuity_xday.recorded_earlier(late["order"], n["order"]):
                continue
            lines = []
            if late["date"] >= n["date"] - _dt.timedelta(days=J1_WINDOW_DAYS):
                lines.append("xday")
            if late["date"] == n["date"] and late["folder"] == n["folder"]:
                lines.append("reconcile")
            for line in lines:
                if (line, n["key"], late["key"]) in done:
                    continue
                g = groups.setdefault((line, n["key"]), {"line": line, "n": n, "lates": []})
                g["lates"].append(late)
    out = []
    for g in groups.values():
        g["l_keys"] = [x["key"] for x in sorted(
            g.pop("lates"), key=lambda x: (x["written_at"], x["key"]), reverse=True)]
        out.append(g)
    out.sort(key=lambda g: (g["n"]["order"][0], g["n"]["order"][1] or "", g["n"]["key"],
                            g["line"]), reverse=True)
    return out


def stale_pairs(conn, *, company_id, site_id, now_utc, limit=J1_MAX) -> list[dict]:
    """The stale pairs J1 would re-emit tonight: `{line, n_extraction_key,
    l_extraction_key}` per (line, N, L), newest N first. `limit` caps the (line, N) groups
    (one re-emit each; J1_MAX per site per night), None for all of them."""
    groups = _stale_groups(conn, company_id=company_id, site_id=site_id, now_utc=now_utc)
    if limit is not None:
        groups = groups[:limit]
    return [{"line": g["line"], "n_extraction_key": g["n"]["key"], "l_extraction_key": lk}
            for g in groups for lk in g["l_keys"]]


def _line_mode(line):
    """"on" when the owning line's own flag (as the item writer reads it) is exactly on,
    else None. A line in shadow is not re-emitted: the pair would be consumed (recorded)
    while the line acts on nothing."""
    if line == "xday":
        mode = (os.environ.get("CROSS_DAY_CONTINUITY") or "off").strip().lower()
        return "on" if mode == "on" else None
    on = (os.environ.get("RECONCILE_STATUS") or "false").strip().lower() == "true"
    return "on" if on else None


class _NothingToEmit(Exception):
    """Raised inside a pair's savepoint to roll its record back."""

    def __init__(self, reason):
        super().__init__(reason)
        self.reason = reason


def _j1_catch_up(conn, ctx, night, groups, counts, started, clock):
    """`groups` from _stale_groups, read by the caller before any J2 put."""
    counts["j1_stale"] = sum(len(g["l_keys"]) for g in groups)
    if ctx.flag != "on" or not groups:
        return
    emitted = 0
    for i, g in enumerate(groups):
        if emitted >= J1_MAX:
            counts["j1_over_cap"] = len(groups) - i
            break
        if _over_budget(started, clock, counts):
            counts["j1_time_paused"] = len(groups) - i
            break
        mode = _line_mode(g["line"])
        if mode is None:
            counts["j1_line_off"] += 1
            continue
        try:
            with conn.transaction():
                outcome = _reemit(conn, ctx, night, g, mode, started, clock)
        except _NothingToEmit as stop:
            outcome = stop.reason
        except Exception as exc:  # noqa: BLE001 - one pair never stops the night
            outcome = "failed"
            logger.warning("nightly j1: re-emit failed site=%s line=%s: %s", ctx.site_id,
                           g["line"], type(exc).__name__)
        if outcome == "reemitted":
            emitted += 1
        counts[f"j1_{outcome}"] += 1


def _reemit(conn, ctx, night, g, mode, started, clock):
    """One stale (line, N), inside the caller's savepoint. Returns "reemitted" or
    "already"; raises _NothingToEmit (or anything else) to roll the record back."""
    import lambda_item_writer   # lazy: a large module, needed only when there is work

    line, n = g["line"], g["n"]
    site_id = str(ctx.site_id)
    folder, date, base = lambda_item_writer._parse_extraction_key(n["key"])
    if line == "reconcile":
        provisional = reconcile_request.request_key(site_id, date, n["key"])
    else:
        # Cross-day's run_hash folds in N's extracted_at, read from S3 only after the
        # record; the row is pointed at the real key before the put.
        provisional = continuity_xday_request.key_for(site_id, date, "pending")
    fresh = [nightly.record_reemit(conn, site_id=ctx.site_id, line=line, n_key=n["key"],
                                   l_key=lk, artifact_key=provisional, night=night)
             for lk in g["l_keys"]]
    if not any(fresh):
        return "already"
    l_keys = [lk for lk, ok in zip(g["l_keys"], fresh) if ok]

    # The item writer's own gate for reconcile: a group member is not reconciled in R0.
    if line == "reconcile" and lambda_item_writer._in_a_group(conn, base):
        raise _NothingToEmit("skipped_group")
    raw = ctx.s3_client.get_object(Bucket=ctx.bucket, Key=n["key"])["Body"].read()
    extraction = json.loads(raw.decode("utf-8") if isinstance(raw, bytes) else raw)
    site = {"id": site_id, "company_id": str(ctx.company_id)}
    if line == "xday":
        left = max(0.0, RUN_BUDGET_S - (clock() - started))
        body = lambda_item_writer.build_xday_request(
            conn, site=site, company_id=str(ctx.company_id), extraction=extraction,
            extraction_key=n["key"], date=date, nonce=night.isoformat(), trigger="nightly",
            deadline=time.monotonic() + left)
        if body is None:
            raise _NothingToEmit("built_none")
        body["mode"] = mode     # the linker acts only on mode "on", as for the writer's own
        key = continuity_xday_request.key_for(body["site_id"], body["date"], body["run_hash"])
    else:
        body = reconcile_request.build(conn, extraction, n["key"], site, ctx.company_id,
                                       user_folder=folder, date=date, session_base=base)
        if not body:
            raise _NothingToEmit("built_none")
        body["trigger"] = "nightly"
        key = provisional
    if key != provisional:
        nightly.set_reemit_artifact(conn, line=line, n_key=n["key"], l_keys=l_keys,
                                    night=night, artifact_key=key)
    # Last: the put. A raise above rolls the record back; nothing after it can raise.
    if line == "xday":
        continuity_xday_request.emit(ctx.s3_client, ctx.bucket, body)
    else:
        reconcile_request.emit(ctx.s3_client, ctx.bucket, body)
    return "reemitted"
