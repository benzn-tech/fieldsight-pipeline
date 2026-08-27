"""Unit: collapsing one commitment that was said in several recordings.

Spec: docs/superpowers/specs/2026-08-17-todo-dedupe-and-threading.md

Measured on prod 2026-08-17: on 2026-08-10 three separate recordings each
produced a byte-identical `Scaffolding -- inspect before Monday`, in three
different topics. Ben said it once per recording; the extraction is faithful.
Nothing is wrong upstream, and the todo list shows it three times.

Two things these pin, and both are the reason this is a RENDER-time collapse
rather than a stored merge:

  * **the duplicates live in DIFFERENT topics.** Collapsing within a topic
    catches none of them, which is the shape a first implementation reaches for.
  * **nothing may be written.** `action_items` cascade-delete with their topic
    and the live tier rewrites the same source key every ~90 s while recording
    (BUG-43), so a stored pointer between two todo rows is either a foreign-key
    error inside the prod write path or a merge that undoes itself within the
    hour. Recomputing on read is the only shape that survives the store.
"""
import pytest

tc = pytest.importorskip("todo_collapse")


def item(text, **kw):
    row = {"id": kw.get("id", text), "text": text, "status": kw.get("status", "open"),
           "topic_id": kw.get("topic_id", "t1"), "created_at": kw.get("created_at", 1)}
    row.update({k: v for k, v in kw.items() if k not in row})
    return row


# ---- the normalisation rule ---------------------------------------------


def test_normalisation_is_case_whitespace_and_trailing_punctuation_only():
    n = tc.normalise
    assert n("Scaffolding -- inspect before Monday") == n(
        "scaffolding --   inspect before monday")
    assert n("Scaffolding -- inspect before Monday.") == n(
        "Scaffolding -- inspect before Monday")
    assert n("  padded  ") == n("padded")


def test_normalisation_never_strips_to_ascii():
    """This codebase has shipped `[^a-z0-9]`-style normalisation three times, and
    each time every CJK character collapsed to nothing, which makes unrelated
    Chinese todos compare EQUAL (memory: fieldsight-ascii-norm-erased-chinese).
    Two different Chinese commitments must stay different."""
    a = tc.normalise("周一前检查脚手架")
    b = tc.normalise("周五前订购木材")
    assert a and b, "normalisation emptied a Chinese todo"
    assert a != b, "two different Chinese todos normalised to the same value"
    # And a Chinese todo is not equal to an empty one either.
    assert a != tc.normalise("")


def test_normalisation_does_not_merge_different_commitments():
    n = tc.normalise
    assert n("Scaffolding -- inspect before Monday") != n(
        "Scaffolding -- inspect before Friday")
    assert n("Order timber") != n("Order steel")


# ---- the collapse --------------------------------------------------------


def test_duplicates_across_topics_collapse_to_one_with_a_count():
    """The real shape: three recordings, three topics, one commitment."""
    rows = [
        item("Scaffolding -- inspect before Monday", id="a", topic_id="t1", created_at=1),
        item("Scaffolding -- inspect before Monday", id="b", topic_id="t2", created_at=2),
        item("Scaffolding -- inspect before Monday", id="c", topic_id="t3", created_at=3),
    ]
    out = tc.collapse(rows)
    assert [r["id"] for r in out] == ["a"], "the earliest mention survives"
    assert out[0]["mention_count"] == 3
    assert out[0]["collapsed_ids"] == ["b", "c"]


def test_a_single_mention_is_not_dressed_up_as_a_collapse():
    """A count of 1 on every ordinary row would read as "raised once" everywhere
    and make the marked ones invisible."""
    out = tc.collapse([item("Order timber", id="a")])
    assert out[0]["mention_count"] == 1
    assert out[0]["collapsed_ids"] == []


def test_the_survivor_is_deterministic_when_created_at_ties():
    """`occurred_at` is NULL on every extraction topic — no writer passes it —
    and `created_at` is reassigned on every re-extraction, so rows written in one
    pass share a timestamp. Without a final tiebreak the survivor flips between
    runs and the collapse is not idempotent."""
    rows = [item("Same", id="zzz", created_at=5), item("Same", id="aaa", created_at=5)]
    assert [r["id"] for r in tc.collapse(rows)] == ["aaa"]
    assert [r["id"] for r in tc.collapse(list(reversed(rows)))] == ["aaa"]


def test_only_open_items_collapse():
    """A closed item and an open one with the same words are not the same
    commitment: one was dealt with. Merging them would hide the open one behind
    a tick."""
    rows = [item("Inspect scaffold", id="a", status="closed"),
            item("Inspect scaffold", id="b", status="open")]
    out = tc.collapse(rows)
    assert sorted(r["id"] for r in out) == ["a", "b"]


def test_collapse_never_mutates_its_input():
    """The same rows are handed to several consumers in one request."""
    rows = [item("X", id="a"), item("X", id="b")]
    before = [dict(r) for r in rows]
    tc.collapse(rows)
    assert rows == before


def test_empty_and_blank_text_is_left_alone():
    """Two todos with no text are not evidence of one todo said twice."""
    rows = [item("", id="a"), item("   ", id="b")]
    assert len(tc.collapse(rows)) == 2


def test_a_row_with_no_status_is_open():
    """The rows that carry no status are the ones read straight out of an
    extraction artifact — the shape the stop-recording email builds its list
    from. Reading a missing field as "not open" would have quietly excluded that
    surface while every database-side test stayed green."""
    rows = [{"text": "Scaffolding -- inspect before Monday", "id": "a"},
            {"text": "Scaffolding -- inspect before Monday", "id": "b"}]
    out = tc.collapse(rows)
    assert [r["id"] for r in out] == ["a"]
    assert out[0]["mention_count"] == 2


def test_the_switch_is_off_unless_it_says_true(monkeypatch):
    """Unset, misspelled, or dropped from one of the two workflows must all mean
    the lists stay exactly as they are today."""
    monkeypatch.delenv("ENABLE_TODO_COLLAPSE", raising=False)
    rows = [item("Same", id="a"), item("Same", id="b")]
    assert len(tc.collapse_if_enabled(rows)) == 2
    for bad in ("", "false", "False", "1", "yes", "TRUE ", " true"):
        monkeypatch.setenv("ENABLE_TODO_COLLAPSE", bad)
        expected = 1 if bad.strip().lower() == "true" else 2
        assert len(tc.collapse_if_enabled(rows)) == expected, bad
