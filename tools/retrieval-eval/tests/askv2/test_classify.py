import json
from datetime import date
from pathlib import Path

from askv2 import funnel

GOLD = Path(__file__).resolve().parents[2] / "gold" / "v1.1.jsonl"
TODAY = date(2026, 10, 22)


def _gold():
    return [json.loads(x) for x in GOLD.read_text(encoding="utf-8").splitlines() if x.strip()]


def test_round1_shapes_of_gold_v11_today():
    """Review S6: this gold reaches one round-1 shape (a plain search), plus the
    documents search for v1-03 when PROJECT_DOCUMENTS is on."""
    out = {it["id"]: funnel.classify_question(it["question"], TODAY) for it in _gold()}
    assert len(out) == 45
    assert [i for i, c in out.items() if c["dated"]] == []
    assert [i for i, c in out.items() if c["metric_fast_path"]] == []
    assert [i for i, c in out.items() if c["latest"]] == []
    assert {i for i, c in out.items() if c["requirements"]} == {"v1-03"}
    assert {i for i, c in out.items() if not c["subject_terms"]} == {"v1-12", "v1-13", "v1-32"}
    assert {c["round1"] for c in out.values()} == {"search"}
    assert out["v1-03"]["round1_with_documents"] == "search+documents"


def test_shapes_for_other_questions():
    c = funnel.classify_question("When did we last pour concrete?", TODAY)
    assert c["latest"] and c["round1"] == "latest"
    c = funnel.classify_question("What did we do yesterday?", TODAY)
    assert c["dated"] and c["round1"] == "day+search"
    c = funnel.classify_question("How long did I record yesterday?", TODAY)
    assert c["metric_fast_path"] is True
