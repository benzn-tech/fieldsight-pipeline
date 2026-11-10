"""Nightly re-match agent: the repository half (migration 0102).

Spec: docs/superpowers/specs/2026-11-10-nightly-rematch-agent-design.md §3 (J1 record, J2
input, J3 markers) and §5.

* `sessions_needing_support` -- J2's input: a site's recent finals whose live items have no
  live `supported_by` link of any method. Item text leaves the VPC in J2's request, so the
  read is strict: visibility through deleted_predicates (both deletion arms plus the live
  arm, never an inlined copy), and topics._topic_not_hidden on top (no ACTIVE redaction of
  any scope, not non_work) -- the same hide rule the action-pack items tool applies before
  handing items to a model.
* `nightly_support_count_today` -- the per-company daily budget check.
* `record_reemit` -- J1's "never twice for one (N, L)" record.
* `mark_recompute_failed` / `failed_recomputes` / `claim_one_recompute` /
  `resolve_recompute` -- J3's markers for voiceprint-writer recomputes that timed out or
  failed.

Ids are returned as text: these rows go straight into JSON request artifacts.
Repositories never commit; the caller owns the transaction.
"""
import datetime as _dt

from psycopg.rows import dict_row

import lake_keys
import nz_time
from deleted_predicates import visible_topics_predicate
from repositories.event_links import KIND_SRC_COLUMN, KIND_TABLE
from repositories.item_history import _SHAPE
from repositories.topics import _topic_not_hidden

REEMIT_LINES = ("xday", "reconcile")
RECOMPUTE_REASONS = ("timed_out", "failed")
NIGHTLY_SUPPORT_KIND = "nightly_support"

_MARKER_COLS = "company_id::text AS company_id, session_base, reason, failed_at, resolved_at"


def _needing_sql(kind):
    """One kind's live rows on the site, topic written since `since`, with no supported_by
    link on that exact row. Company-checked through sites.company_id (the site's CURRENT
    owner): a moved site's old company reads nothing."""
    table, col = KIND_TABLE[kind], KIND_SRC_COLUMN[kind]
    text_col = _SHAPE[kind][0]
    return (
        f"SELECT c.id::text AS row_id, c.stable_id::text AS stable_id, "
        f"c.{text_col} AS text, c.created_at AS row_created_at, "
        f"t.id::text AS topic_id, t.source_s3_key, t.report_date, t.time_range, "
        f"t.created_at AS topic_created_at "
        f"FROM {table} c "
        f"JOIN topics t ON t.id = c.topic_id "
        f"JOIN sites s ON s.id = t.site_id "
        f"WHERE c.site_id = %(site_id)s AND t.site_id = %(site_id)s "
        f"AND s.company_id = %(company_id)s "
        f"AND t.created_at >= %(since)s "
        f"AND t.source_s3_key LIKE 'extractions/%%' "
        f"AND {visible_topics_predicate('t')} "
        f"AND {_topic_not_hidden('t')} "
        f"AND NOT EXISTS (SELECT 1 FROM event_links e WHERE e.{col} = c.id "
        f"AND e.link_type = 'supported_by')"
    )


def sessions_needing_support(conn, *, company_id, site_id, since_utc, limit=20) -> list[dict]:
    """Finals written since `since_utc` on the site, newest first (by the session's latest
    topic write), at most `limit` sessions. Each is
    `{extraction_key, session_base, user_folder, date, items: [{stable_id, row_id, kind,
    text, topic_index, time_range}]}`.

    "Final" = a live topic whose source key is an extraction key (lake_keys); topics carry
    no tier, and live/final tiers share one key, so a still-live pass of an unfinished
    session is included -- its final supersedes it and the writer then skips the row.
    `topic_index` is the topic's 0-based ordinal within the session (topic write order),
    `time_range` the topic's own string. Only live items with no supported_by link of any
    method on their own row; sessions with no such item are omitted."""
    if not company_id or not site_id:
        raise ValueError("company_id and site_id are required")
    if not isinstance(limit, int) or isinstance(limit, bool) or limit < 1:
        raise ValueError(f"limit must be a positive int, got {limit!r}")
    params = {"company_id": company_id, "site_id": site_id, "since": since_utc}
    cur = conn.cursor(row_factory=dict_row)
    by_key = {}
    for kind in KIND_TABLE:
        for row in cur.execute(_needing_sql(kind), params).fetchall():
            parsed = lake_keys.parse(row["source_s3_key"])
            if not parsed or parsed["kind"] != "extraction":
                continue
            sess = by_key.get(row["source_s3_key"])
            if sess is None:
                base = parsed.get("session_base") or (
                    f"grp{parsed['group_id']}" if parsed.get("group_id") else None)
                sess = by_key[row["source_s3_key"]] = {
                    "extraction_key": row["source_s3_key"], "session_base": base,
                    "user_folder": parsed["folder"],
                    "date": row["report_date"].isoformat(), "latest": row["topic_created_at"],
                    "topics": {}, "rows": []}
            sess["latest"] = max(sess["latest"], row["topic_created_at"])
            sess["topics"][row["topic_id"]] = row["topic_created_at"]
            sess["rows"].append((kind, row))

    sessions = sorted(by_key.values(),
                      key=lambda s: (s["latest"], s["extraction_key"]), reverse=True)[:limit]
    out = []
    for sess in sessions:
        order = sorted(sess["topics"], key=lambda tid: (sess["topics"][tid], tid))
        index = {tid: i for i, tid in enumerate(order)}
        rows = sorted(sess["rows"], key=lambda kr: (index[kr[1]["topic_id"]],
                                                    kr[1]["row_created_at"], kr[1]["row_id"]))
        out.append({
            "extraction_key": sess["extraction_key"], "session_base": sess["session_base"],
            "user_folder": sess["user_folder"], "date": sess["date"],
            "items": [{"stable_id": r["stable_id"], "row_id": r["row_id"], "kind": kind,
                       "text": r["text"], "topic_index": index[r["topic_id"]],
                       "time_range": r["time_range"]} for kind, r in rows],
        })
    return out


def _nz_day_bounds_utc(day):
    """[start, end) of NZ calendar `day` as aware UTC datetimes, through nz_time."""
    start = nz_time.from_nz(_dt.datetime.combine(day, _dt.time()))
    end = nz_time.from_nz(_dt.datetime.combine(day + _dt.timedelta(days=1), _dt.time()))
    return start.replace(tzinfo=_dt.timezone.utc), end.replace(tzinfo=_dt.timezone.utc)


def nightly_support_count_today(conn, company_id, today_nz) -> int:
    """`decision_records` of kind nightly_support created on NZ date `today_nz` for the
    company. Every record counts, whatever its subject's later visibility: each one cost a
    model call, and this is the budget check."""
    start, end = _nz_day_bounds_utc(today_nz)
    return conn.execute(
        "SELECT count(*) FROM decision_records WHERE company_id = %s AND kind = %s "
        "AND created_at >= %s AND created_at < %s",
        (company_id, NIGHTLY_SUPPORT_KIND, start, end)).fetchone()[0]


def record_reemit(conn, *, site_id, line, n_key, l_key, artifact_key, night) -> bool:
    """Record one J1 re-emit. False when (line, N, L) -- or, for a dropped request with
    `l_key` None, (line, N, night) -- is already recorded: never twice."""
    if line not in REEMIT_LINES:
        raise ValueError(f"line must be one of {REEMIT_LINES}, got {line!r}")
    row = conn.execute(
        "INSERT INTO nightly_reemits (site_id, line, n_extraction_key, l_extraction_key, "
        "artifact_key, night) VALUES (%s,%s,%s,%s,%s,%s) ON CONFLICT DO NOTHING RETURNING id",
        (site_id, line, n_key, l_key, artifact_key, night)).fetchone()
    return row is not None


def mark_recompute_failed(conn, company_id, session_base, reason):
    """Upsert the J3 marker for one session. A new failure reopens a resolved marker
    (resolved_at NULL) and takes the new reason and time."""
    if reason not in RECOMPUTE_REASONS:
        raise ValueError(f"reason must be one of {RECOMPUTE_REASONS}, got {reason!r}")
    conn.execute(
        "INSERT INTO claim_recompute_failures (company_id, session_base, reason) "
        "VALUES (%s,%s,%s) ON CONFLICT (company_id, session_base) DO UPDATE SET "
        "reason = EXCLUDED.reason, failed_at = now(), resolved_at = NULL",
        (company_id, session_base, reason))


def failed_recomputes(conn, *, company_id, limit=50) -> list[dict]:
    """The company's unresolved markers, oldest failure first."""
    return conn.cursor(row_factory=dict_row).execute(
        f"SELECT {_MARKER_COLS} FROM claim_recompute_failures "
        "WHERE company_id = %s AND resolved_at IS NULL "
        "ORDER BY failed_at, session_base LIMIT %s", (company_id, limit)).fetchall()


def claim_one_recompute(conn, company_id, *, exclude=()) -> dict | None:
    """Lock one unresolved marker of the company (`FOR UPDATE SKIP LOCKED`), or None.

    The caller holds the transaction: one site's run takes a marker and the others skip it.
    Resolve it in the SAME transaction; the row lock also makes a concurrent writer's
    re-mark wait, so it lands after the resolve and reopens the marker. A transaction does
    not skip its own locks, so a caller looping after a failed recompute passes the
    session bases it already took in `exclude`."""
    return conn.cursor(row_factory=dict_row).execute(
        f"SELECT {_MARKER_COLS} FROM claim_recompute_failures "
        "WHERE company_id = %s AND resolved_at IS NULL "
        "AND NOT (session_base = ANY(%s::text[])) "
        "ORDER BY failed_at, session_base LIMIT 1 FOR UPDATE SKIP LOCKED",
        (company_id, list(exclude or ()))).fetchone()


def resolve_recompute(conn, company_id, session_base):
    conn.execute(
        "UPDATE claim_recompute_failures SET resolved_at = now() "
        "WHERE company_id = %s AND session_base = %s AND resolved_at IS NULL",
        (company_id, session_base))


def answered_input_keys(conn, company_id, site_id, keys) -> set:
    """The subset of request `keys` that already have at least one `nightly_support`
    record of this company and site (the writer stamps the request key as input_key).
    Serves the handler's "never re-PUT an answered request" skip and the circuit breaker."""
    keys = [k for k in (keys or []) if k]
    if not keys:
        return set()
    rows = conn.execute(
        "SELECT DISTINCT input_key FROM decision_records WHERE company_id = %s "
        "AND site_id = %s AND kind = %s AND input_key = ANY(%s)",
        (company_id, site_id, NIGHTLY_SUPPORT_KIND, keys)).fetchall()
    return {r[0] for r in rows}


FINAL_MEETING_STATUSES = ("sent", "failed")


def live_session_ids(conn, session_ids) -> set:
    """The subset of meeting_session ids (32 hex, no `sid` prefix) whose row exists and is
    not final yet (status not in sent/failed): the soft "final" gate. A session with no
    meeting_session row is not live (legacy and whole-file sessions have none)."""
    ids = [s for s in (session_ids or []) if s]
    if not ids:
        return set()
    rows = conn.execute(
        "SELECT session_id FROM meeting_session WHERE session_id = ANY(%s) "
        "AND status <> ALL(%s)", (ids, list(FINAL_MEETING_STATUSES))).fetchall()
    return {r[0] for r in rows}
