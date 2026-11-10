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
  3. J2 request build. The site's finals of the last 7 days with unsupported live items
     (at most 20 sessions). Per session: drop group (`grp…`) sessions, sessions whose
     meeting_session is not final yet, items with no text or no parseable time_range, cap
     at the worker's MAX_ITEMS; skip a session whose request key is already answered or
     already exists in S3 (so a hub retry never re-PUTs a key and S3 never fires twice for
     one session); stop at the per-company daily item budget. The artifacts are written
     LAST, after every read that can raise, each stamped with the flag. In shadow the
     worker is not wired, so nothing reads them.

Returns "nothing" when there was no work, else "done". One counts line per run (ids,
counts and enums only -- never item text).

In shadow the only DB writes are the hub's own event and run rows: J3 counts, J2 writes
S3 objects only.
"""
import datetime as _dt
import hashlib
import json
import logging
import os
import re
import time

from psycopg.errors import QueryCanceled

import nz_time
import tenancy_status
from lake_put import lake_put
from nightly_turn_pick import MAX_ITEMS
from repositories import claim_type_store, nightly
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

COUNT_KEYS = ("j2_sessions", "j2_items", "j3_recomputed", "j3_failed", "j3_pending",
              "budget_paused", "skipped_no_window", "skipped_no_text", "skipped_group",
              "skipped_live", "skipped_existing", "items_over_cap", "j2_breaker_open",
              "time_paused")


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

    # Last: nothing below can be undone by a later raise for these sessions.
    for key, body in planned:
        lake_put(ctx.bucket, key, json.dumps(body, ensure_ascii=False), client=ctx.s3_client,
                 owner_hint={"site_id": str(site_id)}, ContentType="application/json")
    counts["j2_sessions"] = len(planned)
    counts["j2_items"] = sum(len(b["items"]) for _, b in planned)

    logger.info(
        "nightly site=%s j2_sessions=%d j2_items=%d j3_recomputed=%d budget_paused=%d "
        "skipped_no_window=%d flag=%s counts=%s", site_id, counts["j2_sessions"],
        counts["j2_items"], counts["j3_recomputed"], counts["budget_paused"],
        counts["skipped_no_window"], ctx.flag, json.dumps(counts, sort_keys=True))
    worked = counts["j2_sessions"] or counts["j3_recomputed"]
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
