"""A corrected name is the name in the report.

Owner, prod 2026-10-02: "Tikaha" was corrected to TEKAHA in the glossary and
the report still said Tikaha -- the glossary was applied to search only. The
worker now applies it to everything the report is written from, before the
model sees any of it.

THE test is `the model never sees the wrong spelling`.
"""
import datetime as dt

import lambda_session_report as sr

GLOSSARY = [{"wrong_term": "Tikaha", "right_term": "TEKAHA"}]


def test_THE_the_model_never_sees_the_wrong_spelling(monkeypatch):
    prompts = []
    monkeypatch.setattr(sr.transcript_window, "select_keys",
                        lambda *a, **k: [(dt.datetime(2026, 10, 2, 12, 44), "t/x.json")])
    monkeypatch.setattr(sr.transcript_window, "assemble", lambda *a, **k: [
        {"at": dt.datetime(2026, 10, 2, 12, 44),
         "line": "[12:44:30] ben (spk_0): One room done today, the Tikaha room inspections."}])
    monkeypatch.setattr(sr.llm_utils, "call_llm",
                        lambda prompt, **kw: (prompts.append(prompt) or
                                              "### Inspections\n- Tikaha room inspected. [t0]\n", None))
    monkeypatch.setattr(sr.llm_utils, "active_model", lambda **k: "m")
    artifact = {"requestId": "r", "folder": "Ben_Lin_Test", "date": "2026-10-02",
                "sessionId": "sid" + "a" * 32, "resultKey": "x", "title": "Daily",
                "generate": {"templateId": "u", "templateVersion": 1, "templateName": "Daily",
                             "templateBody": {"sections": [{"title": "Inspections", "purpose": "p"}],
                                              "catch_all": {"title": "Anything else", "purpose": "r"}},
                             "templateSource": "library"},
                "window": {"from": "00:00", "to": "23:59"}, "excludedTopics": [],
                "reportFacts": {"aliases": GLOSSARY},
                "content": {"date": "2026-10-02", "topics": [
                    {"topic_title": "Tikaha Room Inspections", "time_range": "12:44 - 12:45",
                     "summary": "Referenced the Tikaha room inspections.",
                     "action_items": [{"action": "Close out Tikaha snags", "responsible": "Ben"}]}]}}
    sr._generate_document(artifact)
    assert "Tikaha" not in prompts[0]
    assert "TEKAHA Room Inspections" in prompts[0] and "the TEKAHA room inspections" in prompts[0]
    assert "Close out TEKAHA snags" in prompts[0]


def test_without_a_glossary_nothing_changes():
    content = {"topics": [{"topic_title": "Tikaha"}]}
    assert sr._glossed(content, []) == content
    assert sr._glossed({"a": ["Tikaha room", 3, None]}, GLOSSARY) == {"a": ["TEKAHA room", 3, None]}
