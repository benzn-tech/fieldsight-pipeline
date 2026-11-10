"""Unit: carry_forward_apply.number_new_items never aborts a write pass (action packs Task 1).

The real-SQL half (savepoint, missing table, ordering, carry) is in
tests/integration/test_task_numbers*.py; this pins the Python degrade contract only.
"""
import logging

import pytest

carry_forward_apply = pytest.importorskip(
    "carry_forward_apply", reason="requires psycopg (installed in CI)")


def test_no_topics_is_a_no_op(monkeypatch):
    def boom(*a, **k):
        raise AssertionError("must not be called")
    monkeypatch.setattr(carry_forward_apply.action_items, "assign_task_nos_for_topics", boom)
    assert carry_forward_apply.number_new_items(object(), [], "site", "key") == 0


def test_any_failure_is_logged_and_swallowed(monkeypatch, caplog):
    def boom(*a, **k):
        raise RuntimeError("lock timeout")
    monkeypatch.setattr(carry_forward_apply.action_items, "assign_task_nos_for_topics", boom)
    with caplog.at_level(logging.WARNING):
        assert carry_forward_apply.number_new_items(object(), ["t1"], "site", "k1") is None
    assert "task numbers not assigned for k1" in caplog.text


def test_not_migrated_is_reported_as_skipped(monkeypatch, caplog):
    monkeypatch.setattr(carry_forward_apply.action_items, "assign_task_nos_for_topics",
                        lambda *a, **k: None)
    with caplog.at_level(logging.INFO):
        assert carry_forward_apply.number_new_items(object(), ["t1"], "site", "k1") is None
    assert "table not migrated yet" in caplog.text


def test_count_is_returned(monkeypatch):
    monkeypatch.setattr(carry_forward_apply.action_items, "assign_task_nos_for_topics",
                        lambda conn, topic_ids, site_id: len(topic_ids))
    assert carry_forward_apply.number_new_items(object(), ["t1", "t2"], "site", "k") == 2
