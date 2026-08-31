"""Portfolio rollup counts — Phase 4c leg-1 (deterministic SQL aggregation,
no LLM / no narrative / no materialization — see Global Constraints in
docs/superpowers/plans/2026-07-08-dashboard-4b-live-4c-rollup.md).

portfolio_counts(conn, site_ids) runs four GROUP BY queries — one each
against findings (safety-domain, Phase F / D8 retirement, spec §8 — see
below) and action_items, and two against topics
(a 30-day windowed count/participants query plus an all-time
MAX(report_date) for last_activity_at) — scoped to
site_ids via `WHERE site_id = ANY(%s)` (uses the idx_*_site_status /
idx_topics_site_date / idx_findings_site_domain indexes), then
merges the result sets into one dict per site.

Phase F (D8 retirement, spec §8): the safety count used to read the legacy
`safety_observations` dual-write copy, which is NOT linked to `findings`
(no finding_id column) — a content correction to a finding's text/severity
never reached this count. It now reads `findings WHERE domain='safety'`
directly (severity vocab is none/minor/major, NOT the legacy risk_level
high/medium/low — open_high_safety filters on severity='major').
safety_observations stays in place, unread here, for rollback (see
lambda_item_writer.py / _derive_safety_flags for the corresponding
dual-write-stop).

v1 deliberately aggregates only the report-extracted tables (topics /
action_items / safety_observations, all keyed by site_id uuid). Manual
observations are keyed by site_slug (text), not site_id, so folding them
in here would need a slug<->site_id identity bridge — out of scope for
leg-1 (see Global Constraints); the UI's compliance-aggregator handles
manual+live merging separately.
"""
from datetime import date as _date

import todo_collapse
from psycopg.rows import dict_row

from repositories import redactions

_ZERO_FIELDS = ("open_safety", "open_high_safety", "open_actions", "total_actions",
                 "overdue_actions", "topics_count", "participants")


def _zero() -> dict:
    d = {f: 0 for f in _ZERO_FIELDS}
    d["last_activity_at"] = None    # ISO 'YYYY-MM-DD' once the site has any topic
    return d


def portfolio_counts(conn, site_ids) -> dict:
    """Return `{str(site_id): {open_safety, open_high_safety, open_actions,
    total_actions, overdue_actions, topics_count, participants,
    last_activity_at}}` for every
    id in site_ids (a caller-computed ACL list/set — mirrors
    lambda_org_api._allowed_site_ids / list_live_items's site_ids param).

    Every requested site_id gets an entry, even ones with zero matching
    rows across all three tables (so the UI can render a green/zero row
    instead of the site silently disappearing).

    Every key is str()'d: psycopg returns uuid.UUID objects for uuid
    columns (site_id here), but callers building JSON responses or doing
    `sid in counts` membership checks work with strings — a uuid.UUID-vs-str
    key mismatch here would silently drop every site's counts. This is the
    exact bug that once 403'd every /programme request (see
    lambda_org_api._allowed_site_ids) — don't repeat it.

    Empty site_ids -> {} without a round-trip (mirrors sites.list_sites_by_ids
    / topics.list_topics_for_date's empty-ACL fast path).
    """
    if not site_ids:
        return {}

    ids = list(site_ids)
    merged = {str(sid): _zero() for sid in ids}
    excluded = list(redactions.company_excluded_topic_ids(conn, ids))

    # Safety count = safety-domain findings (source of truth) PLUS a legacy
    # fallback: safety_observations rows for report-only topics that have NO
    # safety finding (zero-extraction days never emit findings). This mirrors
    # the per-topic findings-first fallback in topics.list_topics_for_date, so
    # the rollup count matches what the SAFETY view shows and mixed-source
    # sites are never under-counted after the D8 dual-write is retired.
    # findings severity='major' and safety_observations risk_level='high' both
    # map to the "high" tier.
    safety_rows = conn.cursor(row_factory=dict_row).execute(
        "SELECT site_id, "
        "count(*) FILTER (WHERE status='open') AS open_safety, "
        "count(*) FILTER (WHERE status='open' AND is_high) AS open_high_safety "
        "FROM ("
        "  SELECT site_id, status, (severity='major') AS is_high "
        "  FROM findings WHERE site_id = ANY(%s) AND domain='safety' "
        "    AND topic_id != ALL(%s::uuid[]) "
        "  UNION ALL "
        "  SELECT so.site_id, so.status, (lower(so.risk_level)='high') AS is_high "
        "  FROM safety_observations so "
        "  WHERE so.site_id = ANY(%s) AND so.topic_id != ALL(%s::uuid[]) "
        "    AND NOT EXISTS (SELECT 1 FROM findings f "
        "                    WHERE f.topic_id = so.topic_id AND f.domain='safety')"
        ") u GROUP BY site_id",
        (ids, excluded, ids, excluded),
    ).fetchall()
    for r in safety_rows:
        b = merged.setdefault(str(r["site_id"]), _zero())
        b["open_safety"] = r["open_safety"]
        b["open_high_safety"] = r["open_high_safety"]

    # Counted from ROWS rather than by a SQL aggregate, because the collapse is
    # a Python rule and a second copy of it written in SQL is how one definition
    # becomes two that drift. The tile a customer reads has to agree with the
    # list on the next screen: a list showing one commitment beside a counter
    # saying three tells the reader the product is broken rather than that the
    # work was said twice.
    #
    # The collapse is scoped per (site, DAY) — the same day the duplicates were
    # said in — so the counters sum per-day distinct commitments rather than
    # deduplicating a whole quarter down to one.
    #
    # Cost: this reads the rows instead of counting them in the engine. At
    # today's volume (a few hundred open items per site) that is nothing; if a
    # site ever carries tens of thousands of open actions this is the line that
    # will need a windowed SQL form, and it will need the normalisation to move
    # with it rather than be re-typed.
    action_rows = conn.cursor(row_factory=dict_row).execute(
        "SELECT a.site_id, a.text, a.status, a.deadline, a.id, a.created_at, "
        "       t.report_date "
        "FROM action_items a JOIN topics t ON t.id = a.topic_id "
        "WHERE a.site_id = ANY(%s) AND a.topic_id != ALL(%s::uuid[])",
        (ids, excluded),
    ).fetchall()

    by_site_day = {}
    for r in action_rows:
        by_site_day.setdefault((str(r["site_id"]), r["report_date"]), []).append(r)

    for (site_key, _day), rows in by_site_day.items():
        kept = todo_collapse.collapse_if_enabled(rows)
        b = merged.setdefault(site_key, _zero())
        b["total_actions"] += len(kept)
        b["open_actions"] += sum(1 for r in kept if r["status"] == "open")
        b["overdue_actions"] += sum(
            1 for r in kept
            if r["status"] == "open" and r["deadline"] is not None
            and r["deadline"] < _date.today())

    topic_rows = conn.cursor(row_factory=dict_row).execute(
        "SELECT site_id, "
        "count(*) AS topics_count, "
        "count(DISTINCT user_id) AS participants "
        "FROM topics WHERE site_id = ANY(%s) AND report_date >= CURRENT_DATE - 30 "
        "AND id != ALL(%s::uuid[]) "
        "GROUP BY site_id",
        (ids, excluded),
    ).fetchall()
    for r in topic_rows:
        b = merged.setdefault(str(r["site_id"]), _zero())
        b["topics_count"] = r["topics_count"]
        b["participants"] = r["participants"]

    # Last activity = ALL-TIME MAX(report_date) — the Sites cards' "Last
    # activity" KPI. Deliberately NOT folded into the 30-day topics query
    # above: its WHERE would clamp the max to the window and any site idle
    # for >30 days would show no date at all. Separate index-friendly
    # aggregate instead (idx_topics_site_date).
    activity_rows = conn.cursor(row_factory=dict_row).execute(
        "SELECT site_id, MAX(report_date) AS last_activity_at "
        "FROM topics WHERE site_id = ANY(%s) AND id != ALL(%s::uuid[]) GROUP BY site_id",
        (ids, excluded),
    ).fetchall()
    for r in activity_rows:
        b = merged.setdefault(str(r["site_id"]), _zero())
        la = r["last_activity_at"]
        if la is not None:
            # psycopg returns datetime.date for a date column; normalise to
            # ISO here so JSON never sees a date object (ok()'s
            # json.dumps(default=str) would str() it anyway, but the repo
            # contract stays explicit and unit-testable). hasattr-guard
            # mirrors lambda_org_api's created_at serialisation.
            b["last_activity_at"] = la.isoformat() if hasattr(la, "isoformat") else str(la)

    return merged
