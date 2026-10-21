import check_evidence

from repositories import search_sql


def test_no_family_keeps_the_unfiltered_query():
    sql = check_evidence.chunk_sql(None)
    assert "source_family" not in sql and "%(" not in sql
    assert sql.startswith("SELECT c.chunk_type, c.chunk_text FROM report_chunks c WHERE ")


def test_family_uses_productions_predicate_bound_as_a_parameter():
    sql = check_evidence.chunk_sql(search_sql.family_predicate)
    assert "%(" not in sql and "%%" not in sql
    assert sql.count("CAST(:source_family AS text)") == \
        search_sql.family_predicate("c").count("%(source_family)s::text")
    assert "LIKE 'extractions/%'" in sql and "c.source_s3_key" in sql
    assert sql.rstrip().endswith("ORDER BY c.id LIMIT 100 OFFSET :o")


def test_params_carry_the_family_only_when_given():
    p = check_evidence.chunk_params("s", "u", "2026-09-02", 0, None)
    assert [x["name"] for x in p] == ["s", "u", "d", "o"]
    p = check_evidence.chunk_params("s", "u", "2026-09-02", 100, "session")
    assert p[-1] == {"name": "source_family", "value": {"stringValue": "session"}}
    assert p[3] == {"name": "o", "value": {"longValue": 100}}


def test_importing_the_script_does_nothing():
    assert not hasattr(check_evidence, "rds") and callable(check_evidence.main)
