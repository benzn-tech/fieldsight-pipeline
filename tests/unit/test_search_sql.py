from repositories.search_sql import build_search_sql


def test_search_sql_contains_citation_columns():
    sql = build_search_sql()

    # citation columns for RAG (report_date + site attribution)
    assert "c.report_date" in sql
    assert "site_name" in sql
    assert "JOIN sites s ON s.id = c.site_id" in sql

    # existing topics LEFT JOIN preserved
    assert "LEFT JOIN topics t ON t.id = c.topic_id" in sql
    assert "t.title AS topic_title" in sql
    assert "t.summary AS topic_summary" in sql

    # embedding cast used twice: SELECT distance + ORDER BY
    assert sql.count("::vector") >= 2

    # ACL deny-by-default filter, ordering, and limit unchanged
    assert "site_id = ANY(%(site_ids)s)" in sql
    assert "ORDER BY" in sql
    assert "<=>" in sql
    assert "LIMIT %(k)s" in sql


def test_search_sql_has_date_filter():
    sql = build_search_sql()
    # optional inclusive date range (None => no filter)
    assert "%(date_from)s::date IS NULL OR c.report_date >= %(date_from)s::date" in sql
    assert "%(date_to)s::date IS NULL OR c.report_date <= %(date_to)s::date" in sql
    # unchanged essentials still present
    assert "site_id = ANY(%(site_ids)s)" in sql
    assert "LIMIT %(k)s" in sql


def test_search_sql_has_author_filter_with_null_guard():
    sql = build_search_sql()
    # None => no filter (Ask/ALL/SITE); a list => restrict by user_id.
    assert "%(author_ids)s" in sql
    assert "c.user_id = ANY(%(author_ids)s" in sql
    # IS NULL guard so passing author_ids=None is a no-op (byte-identical scope)
    assert "%(author_ids)s::uuid[] IS NULL" in sql
    # Pin the FULL clause verbatim: the OR must stay inside one parenthesized
    # group. If this were ever split into two top-level ANDs, an author_ids
    # list would silently AND against every unrelated row instead of gating
    # the author check behind the NULL guard -- a cross-site author leak.
    assert (
        "AND (%(author_ids)s::uuid[] IS NULL OR c.user_id = ANY(%(author_ids)s::uuid[]))"
        in sql
    )
