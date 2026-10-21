"""run.py's typed-parameter spelling of the production search SQL for the Data API,
shared with the Ask v2 parity smoke. Side-effect free on import.

Every rewrite is asserted, so if production SQL changes shape this fails loudly
instead of quietly measuring another search."""

REWRITES = [
    ("%(q)s::vector", "CAST(:q AS vector)"),
    ("ANY(%(site_ids)s)", "ANY(CAST(:site_ids AS uuid[]))"),
    ("%(author_ids)s::uuid[]", "CAST(:author_ids AS uuid[])"),
    ("%(date_from)s::date", "CAST(:date_from AS date)"),
    ("%(date_to)s::date", "CAST(:date_to AS date)"),
    ("%(q_text)s", ":q_text"),
    ("%(k)s", ":k"),
    ("%%", "%"),
]


OPTIONAL_REWRITES = [
    ("EXISTS (SELECT 1 FROM topics t WHERE t.id = c.topic_id AND t.superseded_at IS NULL)",
     "EXISTS (SELECT 1 FROM topics t WHERE t.id = c.topic_id)"),
]


def typed_search_sql(sql, family, optional_rewrites=True):
    """`build_search_sql()` text -> Data API SQL with :named parameters.

    `optional_rewrites`: schema compatibility for measuring newer source against a
    database that has not migrated yet (topics.superseded_at). Off for the Ask v2
    parity smoke, which compares bindings of the SAME statement (spec §2.4).
    `family`: develop's source_family switch; None = no filter, as run.py has always
    modelled when --family is not given."""
    if optional_rewrites:
        for old, new in OPTIONAL_REWRITES:
            sql = sql.replace(old, new)
    if family is not None and "%(source_family)s" not in sql:
        raise SystemExit("--family given but the imported search SQL has no source_family switch")
    sql = sql.replace("%(source_family)s::text",
                      "CAST(NULL AS text)" if family is None else "CAST(:source_family AS text)")
    for old, new in REWRITES:
        if old not in sql:
            raise SystemExit("production search SQL no longer contains %r -- "
                             "update REWRITES before trusting any number" % old)
        sql = sql.replace(old, new)
    if "%(" in sql:
        raise SystemExit("unrewritten parameter left in search SQL")
    return sql
