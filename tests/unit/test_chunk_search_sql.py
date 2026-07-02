from repositories.search_sql import build_search_sql


def test_search_sql_always_enforces_acl_and_cosine():
    sql = build_search_sql().lower()
    assert "site_id = any(" in sql, "ACL site filter must be present (deny-by-default)"
    assert "<=>" in sql, "must order by cosine distance operator"
    assert "left join topics" in sql, "small-to-big: must join topic for rollup"
    assert "%(q)s::vector" in sql, "query param must be cast to vector for the <=> operator"
