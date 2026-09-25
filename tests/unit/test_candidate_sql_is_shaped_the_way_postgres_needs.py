"""Assertions on the candidate query's TEXT, because no double will ever run it.

`FakeConn` records SQL and never parses it. That is fine for logic and useless for the
class of defect that only Postgres can see, and this repository has paid for that twice:
a `CASE WHEN %s IS NULL` returned 500 in production with 3082 tests green, because the
planner cannot infer the parameter's type and a double never tries to.

`candidates_for_person` aggregates, so it has the commonest member of that class: every
non-aggregated column in its SELECT must also appear in its GROUP BY, or the statement is
rejected outright. Adding one column to the SELECT and forgetting the other list is a
one-line change that passes every behavioural test and fails on the first real call. It
was made here while writing 0067.

These read the shipped SQL out of the function rather than restating it, for the reason
`score_session.py` learned the hard way: a copy of a query drifts from the query, and the
copy is the one that looks right.
"""
import re

import pytest

lgc = pytest.importorskip("repositories.label_group_candidates",
                          reason="requires psycopg (installed in CI)")


def _statements():
    """Every SQL string literal block in the module, as flattened text."""
    import inspect
    src = inspect.getsource(lgc)
    # Adjacent string literals concatenated the way the module builds them.
    joined = " ".join(re.findall(r'"([^"]*)"', src))
    return re.sub(r"\s+", " ", joined)


def test_every_selected_column_is_grouped_or_aggregated():
    sql = _statements()
    m = re.search(r"SELECT (g\.session_base.*?) FROM speaker_label_groups", sql)
    assert m, ("could not find the candidate SELECT; if its shape changed, fix this test "
               "rather than deleting it -- the check it performs is one Postgres does and "
               "no test double can")
    g = re.search(r"GROUP BY (.*?) ORDER BY", sql)
    assert g, "the candidate query lost its GROUP BY"

    selected = [c.strip() for c in m.group(1).split(",")
                if c.strip() and "AVG(" not in c and " AS " not in c]
    grouped = [c.strip() for c in g.group(1).split(",")]
    missing = [c for c in selected if c not in grouped]
    assert not missing, (
        f"{missing} are selected without being aggregated or grouped. Postgres rejects the "
        f"whole statement; the unit suite cannot see it because a connection double does "
        f"not parse SQL, so this surfaces as a 500 on the first real call")


def test_the_time_window_is_a_parameter_and_not_interpolated():
    """An interpolated interval is both an injection surface and a plan-cache miss per
    distinct value. It is also the easy thing to write."""
    sql = _statements()
    assert "interval '1 hour'" in sql, "the window's unit disappeared from the query"
    assert not re.search(r"INTERVAL\s+'\s*%s", sql, re.I), "the interval is interpolated"
    assert "%s * interval" in sql, "the window multiplier is not a bound parameter"


def test_a_candidate_carries_everything_a_proposal_requires():
    """`speaker_name_proposals` has `user_folder` and `session_date` NOT NULL. A candidate
    missing either cannot become a proposal, and the failure is an insert error at the far
    end of the chain rather than anything this module reports."""
    sql = _statements()
    for column in ("g.user_folder", "g.session_date"):
        assert column in sql, (
            f"{column} is not selected; a candidate built from this row cannot be turned "
            f"into a proposal, because the correction path addresses a session as "
            f"(folder, date, session_base)")


def test_rows_without_a_centroid_are_excluded_rather_than_scored_as_distant():
    """NULL means "not cached", never "sounds like nobody". Scoring it would rank a passage
    nobody has measured below one that was measured and disagreed."""
    assert "centroid IS NOT NULL" in _statements()


def test_no_admission_threshold_hides_in_the_module():
    """Ranking and a top-N cut are not decisions about whether a candidate IS a match. A
    bare float constant here would be the absolute cut this system has refused to invent,
    arriving through a side door."""
    import inspect
    src = inspect.getsource(lgc)
    # Strip comments and docstrings before looking: the reasoning above cites measured
    # numbers (0.445, 0.574) and those are prose, not thresholds.
    code = re.sub(r"#.*", "", src)
    code = re.sub(r'"""[\s\S]*?"""', "", code)
    floats = re.findall(r"(?<![\w.])\d+\.\d+", code)
    assert not floats, (
        f"bare float constant(s) {floats} in the candidate module. If one of these is a "
        f"similarity cut, it is the thing this line has refused to invent without measured "
        f"support; if it is something else, name it and exclude it here deliberately")
