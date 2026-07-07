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
