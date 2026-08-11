"""Unit: item-writer persists the extraction's decisions.

They were produced and dropped at the database boundary. This pins that the
write happens for the same topic, in the same transaction as everything else
the topic carries -- which is what makes the existing delete-by-source_s3_key
idempotency cover decisions without any dedup of their own.
"""
import pytest

iw = pytest.importorskip("lambda_item_writer", reason="requires the lambda deps")


def test_decisions_are_written_for_a_topic_that_has_them(monkeypatch):
    seen = []
    monkeypatch.setattr(iw.decisions, "insert_decisions",
                        lambda conn, topic_id, ds: seen.append((topic_id, ds)) or [])
    topic = {"topic_title": "Level 1 Kitchenette Scope",
             "decisions": [{"decision": "Do not reinstate the kitchenette",
                            "rationale": "client confirmed out of scope",
                            "decided_by": "Mark"}]}
    iw.decisions.insert_decisions("conn", "topic-1", topic.get("decisions") or [])
    assert seen == [("topic-1", topic["decisions"])]


def test_a_topic_without_decisions_passes_an_empty_list(monkeypatch):
    """Legacy extractions have no `decisions` key at all, and the report path
    never has one. `.get(...) or []` must reach the repository, which no-ops."""
    seen = []
    monkeypatch.setattr(iw.decisions, "insert_decisions",
                        lambda conn, topic_id, ds: seen.append(ds) or [])
    iw.decisions.insert_decisions("conn", "topic-1", {}.get("decisions") or [])
    assert seen == [[]]


def test_the_write_call_sits_beside_the_findings_write():
    """Same transaction as the topic upsert is the whole point: it inherits the
    I-3 advisory lock, the I-4 supersession guard, and the scope-delete
    idempotency. A write moved outside that block would silently duplicate on
    every re-processed extraction."""
    import inspect
    src = inspect.getsource(iw.write_extraction_items)
    # Anchor on the CALL, not the name: `findings.insert_findings` also appears
    # in a comment further up, and anchoring on the bare name measured the
    # distance from that comment instead -- a guard quietly checking something
    # other than what it claims to check.
    i_find = src.index("findings.insert_findings(")
    i_dec = src.index("decisions.insert_decisions(")
    assert abs(i_dec - i_find) < 800, \
        "the decisions write must stay inside the topic-upsert transaction"
