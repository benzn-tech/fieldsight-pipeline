"""Repository for findings (migration 0010) -- Task 1 of the
programme-impact-link plan; see
docs/superpowers/plans/2026-07-13-programme-impact-link.md (Task 1) and
docs/superpowers/specs/2026-07-13-unified-extraction-labeling-design.md
(S4/S5).

A `findings` row is a rich per-topic extraction item (observation/domain/
severity/entity/recommended_action) PLUS the programme-impact link as
columns on the same row (programme_task_id/impact_severity/impact_note/
impact_task_name/impact_evidence/impact_matched_at) -- deliberately not a
second link table (spec S9: one link table stays
programme_progress_suggestions, 0008).

Style mirrors src/repositories/observations.py / programme_suggestions.py
(module-level SQL, conn.cursor(row_factory=dict_row).execute(...)
.fetchone()/.fetchall()). jsonb binding follows src/repositories/chunks.py's
Jsonb() convention.
"""
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb

from deleted_predicates import visible_topics_predicate

_COLS = ("id, topic_id, site_id, observation, domain, severity, entity_name, "
         "entity_trade, recommended_action, programme_task_id, impact_severity, "
         "impact_note, impact_task_name, impact_evidence, impact_matched_at, "
         "status, created_at")

_VALID_DOMAINS = {"safety", "quality", "progress"}
_VALID_SEVERITIES = {"none", "minor", "major"}


def _clean_enum(value, valid) -> str | None:
    """Passes value through only if it matches the DB CHECK enum, else NULL.
    Never raises -- the extractor's Claude output can't be trusted to only
    emit the values it was told to (fail-open, same posture as the
    extractor's own _derive_safety_flags bridge)."""
    return value if value in valid else None


def insert_findings(conn, topic_id, site_id, findings: list[dict]) -> list[dict]:
    """Batch-insert one topic's rich extraction findings and return the new
    rows (RETURNING all cols, so callers get generated id/status/created_at
    back). Input dicts use the extractor's field names
    (lambda_extract_session.py EXTRACTION_SCHEMA findings[]: observation/
    domain/severity/entity{name,trade}/recommended_action) -- the nested
    entity dict is flattened HERE into entity_name/entity_trade columns.
    Defensive .get everywhere: this is Claude output, never trust its shape
    (a missing/non-dict entity degrades to {None, None}, not a KeyError/
    AttributeError). domain/severity values outside the CHECK enum are
    passed as NULL rather than raising -- one malformed finding must never
    abort the whole topic's insert.

    Impact columns (programme_task_id, impact_*) are left NULL here --
    they're filled later by apply_impact, downstream of the matcher/writer
    hop (D2 of the plan). Empty findings -> [] with no query executed."""
    if not findings:
        return []
    cur = conn.cursor(row_factory=dict_row)
    rows = []
    for f in findings:
        entity = f.get("entity")
        if not isinstance(entity, dict):
            entity = {}
        rows.append(cur.execute(
            f"INSERT INTO findings (topic_id, site_id, observation, domain, severity, "
            f"entity_name, entity_trade, recommended_action) "
            f"VALUES (%s,%s,%s,%s,%s,%s,%s,%s) RETURNING {_COLS}",
            (topic_id, site_id, f.get("observation"),
             _clean_enum(f.get("domain"), _VALID_DOMAINS),
             _clean_enum(f.get("severity"), _VALID_SEVERITIES),
             entity.get("name"), entity.get("trade"), f.get("recommended_action")),
        ).fetchone())
    return rows


def apply_impact(conn, finding_id, *, task_id, impact_severity, impact_note,
                 impact_task_name, impact_evidence: dict) -> dict | None:
    """Applies one matcher verdict to a finding row as an UPDATE (the
    in-VPC writer hop -- BUG-36: the matcher itself stays non-VPC and never
    touches Aurora directly). rowcount 0 is a NORMAL skip, not an error: the
    finding row may have vanished between the matcher's read and this write
    because of nightly supersession or a re-extraction racing in (D4/D5 of
    the plan) -- returns None, never raises. impact_evidence is wrapped in
    Jsonb (chunks.py convention)."""
    return conn.cursor(row_factory=dict_row).execute(
        f"UPDATE findings SET programme_task_id=%s, impact_severity=%s, "
        f"impact_note=%s, impact_task_name=%s, impact_evidence=%s, "
        f"impact_matched_at=now() WHERE id=%s RETURNING {_COLS}",
        (task_id, impact_severity, impact_note, impact_task_name,
         Jsonb(impact_evidence or {}), finding_id),
    ).fetchone()


def list_for_topics(conn, topic_ids) -> list[dict]:
    """Batched read of findings for a set of topic ids -- mirrors
    topics.list_topics_for_date's action_items/safety_observations children
    pattern (topics.py:143-156): ONE query scoped with ANY(%s), regardless
    of how many topic_ids are passed, never N+1."""
    return conn.cursor(row_factory=dict_row).execute(
        f"SELECT {_COLS} FROM findings WHERE topic_id = ANY(%s) ORDER BY created_at",
        (list(topic_ids),),
    ).fetchall()


def count_by_domain(conn, company_id, domain, date_from, date_to,
                    site_ids=None, author_ids=None) -> dict:
    """How many safety- or quality-domain items in a date range.

    FINDINGS FIRST, `safety_observations` SECOND, PER TOPIC -- the same rule the
    shipped dashboard read already uses, not a third opinion about which table is
    true. `topics.list_topics_for_date` does exactly this in Python:

        t["safety_observations"] = (_findings_as_safety_rows(t_findings)
                                    or safety_by_topic.get(t["id"], []))

    and its left-hand side is already filtered to `domain == "safety"`, so the
    fallback fires when a topic has no findings IN THIS DOMAIN -- not when it has
    no findings at all. This function reproduces that condition, in SQL.

    The fallback is load-bearing, not defensive. Measured on the live database:
    139 topics carry findings and no `safety_observations`, 15 carry
    `safety_observations` and no findings, and ZERO carry both. The two paths are
    disjoint, so a findings-only count does not under-report by a margin -- it
    reports nothing at all for the second kind. (Those are the nightly-report
    topics, which exist only for zero-extraction days, because `AUTHORITY_FLIP`
    makes a day with extraction topics defer.)

    SAFETY ONLY. There is no legacy quality table, so `quality` never falls back;
    an arm that reused `n_legacy` for both would report safety rows as quality
    ones.

    THE TENANT COMES THROUGH `site_id`. `topics.site_id` and `sites.company_id`
    are both NOT NULL -- one hop that loses nothing. `topics.user_id` is
    nullable, so reaching the tenant through `users` instead would drop every
    NULL-author row from EVERY caller's count, including an ALL-scoped admin's,
    and the number would look like an answer.

    `author_ids` narrows to a set of authors and `null_author` is what that scope
    cannot see by construction: findings on topics nobody is recorded as having
    made. It is reported rather than subtracted silently, so a smaller number
    arrives with its reason -- and ONLY when an author filter is active. With
    `author_ids=None` those rows are IN the count, and telling an admin "2 items
    sit on notes with no recorded author" beside a complete number implies it is
    short when it is not. The docstring said "what that scope cannot see" while
    the SQL counted them for every caller. `unlabelled` is findings whose `domain` is NULL --
    measured 0 of 189 live, so it is almost always zero and the caller does not
    print a zero.

    Both deletion arms, via `visible_topics_predicate`. The topic arm covers the
    rows that exist now; the source arm covers the ones tomorrow's re-ingest
    rebuilds with new uuids that no topic-keyed tombstone names. A count with
    only the first passes every test and leaks overnight.
    """
    row = conn.cursor(row_factory=dict_row).execute(
        "WITH scoped AS ("
        "  SELECT t.id AS topic_id, t.user_id"
        "  FROM topics t JOIN sites s ON s.id = t.site_id"
        # SITE FIRST, COMPANY ONLY WHEN THERE IS NO SITE SET. `site_ids` IS
        # the ACL and is already inside the caller's company for every
        # ordinary role, so this is not a widening -- but for a
        # cross-company caller it spans companies, and ANDing the caller's
        # OWN company on top matched nothing. `has_topics_in_range` scopes
        # by site alone, so the two disagreed and a platform_admin was told
        # there were notes and no items.
        "  WHERE (CASE WHEN %(site_ids)s::uuid[] IS NULL"
        "              THEN s.company_id = %(company)s"
        "              ELSE t.site_id = ANY(%(site_ids)s::uuid[]) END)"
        "    AND t.report_date BETWEEN %(from)s AND %(to)s"
        "    AND " + visible_topics_predicate("t") +
        "), per_topic AS ("
        "  SELECT sc.topic_id, sc.user_id,"
        "    (SELECT count(*) FROM findings f"
        "      WHERE f.topic_id = sc.topic_id AND f.domain = %(domain)s) AS n_findings,"
        "    (SELECT count(*) FROM findings f"
        "      WHERE f.topic_id = sc.topic_id AND f.domain IS NULL) AS n_unlabelled,"
        "    (SELECT count(*) FROM safety_observations so"
        "      WHERE so.topic_id = sc.topic_id) AS n_legacy"
        "  FROM scoped sc"
        "), counted AS ("
        "  SELECT user_id, n_unlabelled,"
        "    CASE WHEN n_findings > 0 THEN n_findings"
        "         WHEN %(domain)s = 'safety' THEN n_legacy ELSE 0 END AS n,"
        "    CASE WHEN n_findings = 0 AND %(domain)s = 'safety' THEN n_legacy"
        "         ELSE 0 END AS n_fb"
        "  FROM per_topic"
        ")"
        "SELECT"
        "  COALESCE(SUM(n) FILTER (WHERE %(authors)s::uuid[] IS NULL"
        "                          OR user_id = ANY(%(authors)s::uuid[])), 0) AS count,"
        "  COALESCE(SUM(n_unlabelled), 0) AS unlabelled,"
        "  COALESCE(SUM(n) FILTER (WHERE user_id IS NULL""                          AND %(authors)s::uuid[] IS NOT NULL), 0) AS null_author,"
        "  COALESCE(SUM(n_fb) FILTER (WHERE %(authors)s::uuid[] IS NULL"
        "                             OR user_id = ANY(%(authors)s::uuid[])), 0) AS from_fallback"
        " FROM counted",
        {"company": company_id, "domain": domain, "from": date_from, "to": date_to,
         "site_ids": list(site_ids) if site_ids is not None else None,
         "authors": list(author_ids) if author_ids is not None else None},
    ).fetchone()
    # SUM() over bigint returns numeric, which psycopg hands back as Decimal, and
    # json.dumps has no encoder for Decimal. These numbers go straight into an
    # HTTP response body, so the cast happens here rather than at every caller.
    return {k: int(row[k] or 0) for k in ("count", "unlabelled", "null_author",
                                          "from_fallback")}


def trade_heard_for(conn, company_id, entity_name, site_id=None) -> str | None:
    """The trade this name has been heard as on site, for DISPLAY beside an employer field.

    Never the answer to "who employs them". `entity_name` is the entity a FINDING is about,
    and prod shows what that means: "Jerry / PK Building", "Troy and Jay", "facade subbie",
    "Zoe | Rebar". People, groups, roles. **"Zoe | Rebar" says Zoe does rebar** -- putting that
    behind "please confirm Zoe is from Rebar?" would turn a trade into a company with one
    click, and stamp the result `employer_source: 'suggested'`.

    So this is grey helper text and nothing else. It is returned under its own key, never
    pre-filled into the field, and the endpoint's contract says so.

    Tenant path is `findings.site_id -> sites.company_id` -- ONE hop, NOT NULL on both sides.
    `findings` has no user column, so the "reach the tenant through users" rule that `topics`
    needs does not apply and would also drop every NULL-author row silently. Measured on prod
    2026-08-30: 0 of 189 findings have a NULL site_id, so the join loses nothing.
    """
    name = (entity_name or "").strip()
    if not name:
        return None
    sql = ("SELECT f.entity_trade, count(*) AS n "
           "FROM findings f JOIN sites s ON s.id = f.site_id "
           "WHERE s.company_id = %s AND lower(f.entity_name) = lower(%s) "
           "  AND f.entity_trade IS NOT NULL ")
    params = [company_id, name]
    if site_id:
        sql += "  AND f.site_id = %s "
        params.append(site_id)
    sql += "GROUP BY f.entity_trade ORDER BY n DESC, f.entity_trade LIMIT 1"
    row = conn.cursor(row_factory=dict_row).execute(sql, tuple(params)).fetchone()
    return row["entity_trade"] if row else None
