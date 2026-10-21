import uuid
from datetime import date, datetime, timezone
from decimal import Decimal

import pytest

from askv2.render import EvalAdapterError, check_template, render

from repositories import project_docs_sql, search_sql

U = uuid.UUID("4326dbc7-14a2-4251-8381-9d0067595a85")
VEC = [0.1] * 1023 + [-0.0000001]


def test_scalars():
    sql = "SELECT %s, %s, %s, %s, %s, %s, %s, %s, %s"
    out = render(sql, ("it's", U, date(2026, 9, 2), None, True, False, 7, 0.55, Decimal("1.5")))
    assert out == ("SELECT 'it''s', '%s', '2026-09-02', NULL, TRUE, FALSE, 7, 0.55, 1.5" % U)


def test_datetime_is_quoted_iso():
    dt = datetime(2026, 9, 2, 1, 2, 3, tzinfo=timezone.utc)
    assert render("SELECT %s", [dt]) == "SELECT '2026-09-02T01:02:03+00:00'"


def test_named_placeholders_and_repeats():
    out = render("SELECT %(a)s, %(b)s, %(a)s", {"a": 1, "b": "x"})
    assert out == "SELECT 1, 'x', 1"


def test_percent_percent_only_when_params_are_given():
    assert render("SELECT 'a%%' || %s", ("b",)) == "SELECT 'a%' || 'b'"
    assert render("SELECT 'a%%'", None) == "SELECT 'a%%'"


def test_string_arrays_are_untyped_array_literals():
    assert render("SELECT %s", [["a", 'q"x', "b\\c", U]]) == (
        "SELECT '{\"a\",\"q\\\"x\",\"b\\\\c\",\"%s\"}'" % U)
    assert render("SELECT %s", [[]]) == "SELECT '{}'"
    assert render("SELECT %s", [["it's"]]) == "SELECT '{\"it''s\"}'"


def test_vector_uses_run_py_float_format():
    out = render("SELECT %s::vector", [VEC])
    assert out.startswith("SELECT '[0.1000000,0.1000000,")
    assert out.endswith(",-0.0000001]'::vector")


def test_dict_is_a_json_literal():
    assert render("SELECT %s", [{"a": "it's"}]) == "SELECT '{\"a\": \"it''s\"}'"


@pytest.mark.parametrize("bad", [{"a"}, [[1, 2]], b"x", [0.5, 0.25], "a\x00b",
                                 float("nan"), (1, 2), object()])
def test_unrenderable_values_are_loud(bad):
    with pytest.raises(EvalAdapterError):
        render("SELECT %s", [bad])


def test_placeholder_count_mismatch_is_loud():
    with pytest.raises(EvalAdapterError):
        render("SELECT %s, %s", ("a",))
    with pytest.raises(EvalAdapterError):
        render("SELECT %(a)s", {"b": 1})


def test_real_search_sql_renders_under_the_data_api_cap():
    sql = search_sql.build_search_sql()
    check_template(sql)
    out = render(sql, {"q": VEC, "q_text": "pour", "site_ids": [str(U)], "k": 16,
                       "date_from": None, "date_to": None, "author_ids": [str(U)],
                       "source_family": "report"})
    assert "%(" not in out and len(out) < 60_000


def test_real_doc_search_sql_renders_under_the_data_api_cap():
    sql = project_docs_sql.build_doc_search_sql()
    check_template(sql)
    import re
    names = set(re.findall(r"%\((\w+)\)s", sql))
    params = {n: (VEC if n == "q" else [str(U)] if "ids" in n else 8 if n == "k" else "x")
              for n in names}
    out = render(sql, params)
    assert "%(" not in out and len(out) < 60_000


def test_oversize_statement_is_refused():
    with pytest.raises(EvalAdapterError, match="60"):
        render("SELECT %s", ["x" * 60_000])


@pytest.mark.parametrize("sql", [
    "SELECT pg_advisory_lock(1)", "SELECT 1 FOR UPDATE", "select 1 for  share",
    "WITH x AS (DELETE FROM t RETURNING 1) SELECT * FROM x", "SET TRANSACTION READ WRITE",
    "INSERT INTO t VALUES (1)", "SELECT nextval('s')", "SELECT set_config('a','b',false)",
    "SELECT lo_import('/x')", "SELECT 1; DROP TABLE t", "DO $$ BEGIN END $$",
    "SELECT pg_sleep(1)", "SELECT dblink('x','y')", "SELECT * FROM t -- \n FOR UPDATE",
])
def test_deny_list(sql):
    with pytest.raises(EvalAdapterError):
        check_template(sql)


@pytest.mark.parametrize("sql", [
    "SELECT 'please DELETE this' AS x", "  with a AS (SELECT 1) SELECT * FROM a",
    "SELECT 1 /* UPDATE */", "SELECT offset_x FROM t OFFSET 3", "SELECT \"update\" FROM t",
])
def test_allowed(sql):
    check_template(sql)
