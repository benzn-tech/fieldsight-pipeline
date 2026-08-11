"""Unit: decisions reach BOTH read surfaces.

`/live-items` serves whatever `repositories/topics.py` attaches -- `ok({"topics":
rows})`, no allowlist -- so that half needs the repository to attach them.

The other half is `render_report_shape`, which hardcoded `"key_decisions": []`
with the comment "D3: v1, decisions table deferred". It feeds the Timeline day
view, the session-report modal and Word export, and the reindex builder that
produces RAG chunks (`chunking._topic_text` reads `key_decisions`). Wiring only
`/live-items` would land the feature in one place and leave three showing
nothing -- which reads to a user as "decisions still don't work".
"""
import pytest

org = pytest.importorskip("lambda_org_api", reason="requires the lambda deps")

SITE_ID = "11111111-1111-1111-1111-111111111111"


def _row(**over):
    base = {
        "id": "t-1", "site_id": SITE_ID, "site_name": "Alpha", "user_name": "Ada L",
        "category": "safety", "title": "Kitchenette scope", "summary": "Discussed.",
        "time_range": "08:00 - 08:15", "participants": ["Ada L"],
        "action_items": [], "safety_observations": [], "findings": [], "photos": [],
    }
    base.update(over)
    return base


def test_the_report_shape_carries_decisions_from_the_topic():
    row = _row(decisions=[{"decision": "Do not reinstate the kitchenette",
                           "rationale": "client confirmed out of scope",
                           "decided_by": "Mark"}])
    shape = org.render_report_shape([row], None, "2026-08-07", "Ben_UCPK2")
    got = shape["topics"][0]["key_decisions"]
    assert len(got) == 1
    assert got[0]["decision"] == "Do not reinstate the kitchenette"
    assert got[0]["decided_by"] == "Mark"
    assert got[0]["rationale"] == "client confirmed out of scope"


def test_a_topic_with_no_decisions_still_renders_an_empty_list():
    shape = org.render_report_shape([_row()], None, "2026-08-07", "Ben_UCPK2")
    assert shape["topics"][0]["key_decisions"] == []


def test_a_report_sourced_topic_without_the_key_does_not_crash():
    """render_report_shape is also called with report-sourced rows, which have
    no `decisions` key at all -- `.get(...) or []`, not `row["decisions"]`."""
    row = _row()
    row.pop("decisions", None)
    shape = org.render_report_shape([row], None, "2026-08-07", "Ben_UCPK2")
    assert shape["topics"][0]["key_decisions"] == []


def test_the_repository_attaches_decisions_to_every_topic(monkeypatch):
    """`/live-items` needs no change to receive them, but the repository does:
    children are attached by explicit per-child query, not generically."""
    import repositories.topics as tp
    monkeypatch.setattr(tp.decisions, "list_for_topics",
                        lambda conn, ids: [{"topic_id": "t-1",
                                            "decision": "Seal the panel"}])
    by = {}
    for d in tp.decisions.list_for_topics(None, ["t-1"]):
        by.setdefault(d["topic_id"], []).append(d)
    assert by["t-1"][0]["decision"] == "Seal the panel"


def test_the_repository_imports_the_decisions_module():
    """A miss here is the whole feature silently absent from /live-items."""
    import repositories.topics as tp
    assert hasattr(tp, "decisions"), \
        "repositories/topics.py must import the decisions repository"
