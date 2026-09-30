"""Unit: Track B Task 6 -- carry-forward pass 0, which pairs retired and new child rows on
the extractor's own `item_id` declaration (Task 5) BEFORE the existing exact/fuzzy text
passes (`carry_forward.match`) ever see them.

Two things this file pins that no earlier test does:

  * `_pair_by_item_id` itself: equal, non-NULL item_id, strictly one-to-one -- a duplicated
    id on either side is refused rather than guessed at, same posture as a fuzzy tie.
  * `_carry_forward_one_table`'s new `(orphans, carried)` return and the per-method EMF line
    `_report_orphaned_human_edits` now prints when handed a `carried` dict.

The matching behaviour of the text passes themselves (exact/fuzzy) is unchanged and stays
proven in tests/unit/test_carry_forward.py; this file only proves pass 0 runs first and that
counts are attributed to the right method. End-to-end against real Postgres is
tests/integration/test_carry_forward_pass0.py.
"""
import json
import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(
    os.path.abspath(__file__)))), "src"))

cfa = pytest.importorskip("carry_forward_apply", reason="requires psycopg (installed in CI)")

KEY = "extractions/Ben_UCPK2/2026-08-17/sid9f8c1e2a4b6d47f0a1b2c3d4e5f60718.json"


def _row(id_, item_id, text="t", touched=False):
    return {"id": id_, "item_id": item_id, "text": text, "stable_id": f"s-{id_}",
            "human_touched": touched}


def _emf_lines(capsys):
    out = capsys.readouterr().out.strip()
    return [json.loads(line) for line in out.splitlines() if line.strip()]


def test_pairs_equal_non_null_item_ids_one_to_one():
    pairs = cfa._pair_by_item_id([_row("o1", "X"), _row("o2", None)],
                                 [_row("n1", "X"), _row("n2", None)])
    assert [(o["id"], n["id"]) for o, n in pairs] == [("o1", "n1")]


def test_null_item_ids_never_pair():
    assert cfa._pair_by_item_id([_row("o1", None)], [_row("n1", None)]) == []


def test_duplicate_item_id_on_either_side_drops_the_pair():
    assert cfa._pair_by_item_id([_row("o1", "X"), _row("o2", "X")], [_row("n1", "X")]) == []
    assert cfa._pair_by_item_id([_row("o1", "X")], [_row("n1", "X"), _row("n2", "X")]) == []


def test_one_table_pass0_runs_before_text_and_counts_per_method(monkeypatch):
    moved = []

    class Repo:
        @staticmethod
        def list_for_carry_forward(conn, topic_ids, site_id):
            return ([_row("o1", "X", "old words", True), _row("o2", None, "same text")]
                    if topic_ids == ["old"] else
                    [_row("n1", "X", "totally different words"), _row("n2", None, "same text")])

        @staticmethod
        def carry_identity(conn, new_id, old_row):
            moved.append((old_row["id"], new_id))

    orphans, carried = cfa._carry_forward_one_table(None, Repo, ["old"], ["new"], "site")
    assert sorted(moved) == [("o1", "n1"), ("o2", "n2")]
    assert orphans == 0 and carried == {"item_id": 1, "exact": 1, "fuzzy": 0}


def test_a_pass0_pair_is_never_re_offered_to_the_text_passes(monkeypatch):
    """The same old/new row must not appear twice: once carried by item_id, it is removed
    from the pool the text passes see -- otherwise a row already paired by item_id could
    ALSO win a fuzzy match against some other leftover row, corrupting the one-to-one
    invariant `carry_forward.match` itself guarantees."""
    seen_by_match = []
    real_match = cfa.carry_forward.match

    def _spy(old, new, **kw):
        seen_by_match.append(([o["id"] for o in old], [n["id"] for n in new]))
        return real_match(old, new, **kw)

    monkeypatch.setattr(cfa.carry_forward, "match", _spy)

    class Repo:
        @staticmethod
        def list_for_carry_forward(conn, topic_ids, site_id):
            return ([_row("o1", "shared-id", "paired by id")]
                    if topic_ids == ["old"] else
                    [_row("n1", "shared-id", "paired by id, reworded a lot")])

        @staticmethod
        def carry_identity(conn, new_id, old_row):
            pass

    cfa._carry_forward_one_table(None, Repo, ["old"], ["new"], "site")
    assert seen_by_match == [([], [])], (
        "o1/n1 already paired by item_id -- match() must see neither")


def test_a_row_with_no_item_id_still_falls_through_to_the_text_passes(monkeypatch):
    """A row the extractor never stamped (an older artifact, or a row it could not assign a
    stable item_id to) must not be silently dropped -- pass 0 leaves it alone and the
    existing exact/fuzzy passes get their normal chance at it."""
    class Repo:
        @staticmethod
        def list_for_carry_forward(conn, topic_ids, site_id):
            return ([_row("o1", None, "Order rebar for Monday")]
                    if topic_ids == ["old"] else
                    [_row("n1", None, "Order rebar for Monday")])

        @staticmethod
        def carry_identity(conn, new_id, old_row):
            pass

    orphans, carried = cfa._carry_forward_one_table(None, Repo, ["old"], ["new"], "site")
    assert orphans == 0
    assert carried == {"item_id": 0, "exact": 1, "fuzzy": 0}


def test_emf_line_carries_carried_by_method(capsys):
    carried = {"item_id": 2, "exact": 1, "fuzzy": 0}
    cfa._report_orphaned_human_edits(KEY, 0, carried)

    lines = _emf_lines(capsys)
    assert len(lines) == 4, lines   # 1 OrphanedHumanEdits + 3 CarriedByMethod (one per method)

    orphan_line = lines[0]
    assert orphan_line["OrphanedHumanEdits"] == 0
    assert orphan_line["_aws"]["CloudWatchMetrics"][0]["Dimensions"] == [["Stage"]]

    method_lines = {l["Method"]: l for l in lines[1:]}
    assert set(method_lines) == {"item_id", "exact", "fuzzy"}
    for method, n in carried.items():
        line = method_lines[method]
        assert line["CarriedByMethod"] == n
        assert line["key"] == KEY
        assert line["_aws"]["CloudWatchMetrics"][0]["Namespace"] == "FieldSight/Pipeline"
        assert line["_aws"]["CloudWatchMetrics"][0]["Dimensions"] == [["Stage", "Method"]]
        assert line["_aws"]["CloudWatchMetrics"][0]["Metrics"][0]["Name"] == "CarriedByMethod"


def test_no_carried_by_method_line_when_carried_is_not_given(capsys):
    """Unchanged callers (or the crash-fallback path) that pass no `carried` must keep
    emitting exactly the OrphanedHumanEdits line -- no empty/zeroed CarriedByMethod noise."""
    cfa._report_orphaned_human_edits(KEY, 3)
    lines = _emf_lines(capsys)
    assert len(lines) == 1, lines
    assert lines[0]["OrphanedHumanEdits"] == 3


def test_carry_forward_children_sums_carried_across_all_four_tables_and_emits_it(
        monkeypatch, capsys):
    """`_carry_forward_children` must add up each table's `carried` dict, not just its
    orphan int, and pass the sum through to the report -- proven here against a fake conn
    with all four repos stubbed, one method each, so the sum can only be right if every
    table's contribution actually lands."""
    class _FakeTxn:
        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):
            return False

    class _FakeConn:
        def transaction(self):
            return _FakeTxn()

    def _one_table(method, n):
        def _f(conn, repo, old_topic_ids, new_topic_ids, site_id):
            return 0, {"item_id": 0, "exact": 0, "fuzzy": 0, method: n}
        return _f

    calls = iter([
        _one_table("item_id", 2),
        _one_table("exact", 1),
        _one_table("fuzzy", 1),
        _one_table("item_id", 1),
    ])
    monkeypatch.setattr(cfa, "_carry_forward_one_table",
                        lambda *a, **k: next(calls)(*a, **k))

    cfa._carry_forward_children(_FakeConn(), ["old"], ["new"], "site-1", KEY)

    lines = _emf_lines(capsys)
    method_lines = {l["Method"]: l for l in lines[1:]}
    assert method_lines["item_id"]["CarriedByMethod"] == 3
    assert method_lines["exact"]["CarriedByMethod"] == 1
    assert method_lines["fuzzy"]["CarriedByMethod"] == 1


def test_carry_forward_children_crash_fallback_emits_no_per_method_line(monkeypatch, capsys):
    """On the crash fallback, `carried` is None -- nothing was actually carried, so there is
    no per-method breakdown to report, only OrphanedHumanEdits (unchanged from before Task 6)."""
    def _boom(*a, **k):
        raise RuntimeError("matcher exploded")

    class _FakeTxn:
        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):
            return False

    class _FakeConn:
        def transaction(self):
            return _FakeTxn()

    monkeypatch.setattr(cfa, "_carry_forward_one_table", _boom)
    monkeypatch.setattr(cfa, "_count_human_touched_old", lambda *a, **k: 2)

    cfa._carry_forward_children(_FakeConn(), ["old"], ["new"], "site-1", KEY)

    lines = _emf_lines(capsys)
    assert len(lines) == 1, lines
    assert lines[0]["OrphanedHumanEdits"] == 2
