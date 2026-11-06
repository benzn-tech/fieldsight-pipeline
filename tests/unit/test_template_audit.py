"""The upload audit's request, its parse, and the code guards on the model's claims
(spec 2026-11-06 A2-A4). `call_llm_parts` is faked: nothing here reaches a network.
"""
import json

import pytest

lu = pytest.importorskip("llm_utils", reason="requires urllib3 (installed in CI)")
import template_audit as audit  # noqa: E402
import template_vision  # noqa: E402

SOURCE_TEXT = ("Wall Framing\n[table 1]\nNo. | Item | Yes / No / N/A | Comment\n"
               "1 | Studs at 600 centres | [ ] |\n2 | Lintels sized to drawings | [ ] |\n"
               "3 | Check roof flashing | [ ] |\nSigned: ____ | Date: ____")


def _result(*texts, status="done", **extra):
    items = [{"text": t, "flags": []} for t in texts]
    sections = [{"title": "Wall Framing", "items": items, "flags": []}] if items else []
    return {"status": status, "sections": sections,
            "counts": {"sections": len(sections), "items": len(items)},
            "route": "docx", "model": None, **extra}


def _answer(verdict="ok", source_items=2, **lists):
    out = {"verdict": verdict, "source_items": source_items, "missing": [], "extra": [],
           "reworded": [], "split_or_merged": [], "summary": "s"}
    out.update(lists)
    return out


DRAFT = _result("Studs at 600 centres", "Lintels sized to drawings")


# ---- check(): the false-alarm guard ----------------------------------------------------

def test_missing_claim_not_in_docx_source_is_dropped():
    text = SOURCE_TEXT.replace("3 | Check roof flashing | [ ] |\n", "")
    out = audit.check(_answer("problems", 3, missing=[
        {"section": "Wall Framing", "text": "Check roof flashing"}]), DRAFT, text)
    assert out["verdict"] == "ok"
    assert out["missing"] == []
    assert out["dropped_claims"][0]["kind"] == "missing"
    assert out["dropped_claims"][0]["claim"]["text"] == "Check roof flashing"
    assert out["dropped_claims"][0]["reason"]


def test_missing_claim_in_source_counts():
    out = audit.check(_answer("problems", 3, missing=[
        {"section": "Wall Framing", "text": "3. Check  ROOF flashing"}]), DRAFT, SOURCE_TEXT)
    assert out["verdict"] == "problems"
    assert len(out["missing"]) == 1 and out["dropped_claims"] == []


def test_missing_claim_matches_whole_words_only():
    # "Check roof flash" is a substring of the source but not a row of it.
    out = audit.check(_answer("problems", 3, missing=[
        {"section": "", "text": "Check roof flash"}]), DRAFT, SOURCE_TEXT)
    assert out["verdict"] == "ok" and out["missing"] == []


@pytest.mark.parametrize("text", ["Comment", "Signed:", "Date", "Wall Framing"])
def test_missing_claim_naming_a_heading_or_sign_off_is_dropped(text):
    # Each is in the source text, so only this guard stops the false alarm.
    out = audit.check(_answer("problems", 3, missing=[{"section": "", "text": text}]),
                      DRAFT, SOURCE_TEXT)
    assert out["verdict"] == "ok" and out["missing"] == []
    assert out["dropped_claims"][0]["kind"] == "missing"


def test_missing_claim_already_in_draft_is_dropped():
    out = audit.check(_answer("problems", 3, missing=[
        {"section": "Wall Framing", "text": "studs at 600 centres"}]), DRAFT, SOURCE_TEXT)
    assert out["verdict"] == "ok" and out["missing"] == []
    assert out["dropped_claims"][0]["kind"] == "missing"


def test_pdf_missing_needs_count_gap():
    claim = {"section": "Wall Framing", "text": "Check roof flashing"}
    out = audit.check(_answer("problems", 2, missing=[claim]), DRAFT, None)
    assert out["verdict"] == "ok" and out["missing"] == []
    assert out["dropped_claims"][0]["kind"] == "missing"
    out = audit.check(_answer("problems", 3, missing=[claim]), DRAFT, None)
    assert out["verdict"] == "problems" and out["missing"] == [claim]


def test_pdf_missing_claims_beyond_the_count_gap_are_dropped():
    claims = [{"section": "", "text": "Check roof flashing"}, {"section": "", "text": "Gate locked"}]
    out = audit.check(_answer("problems", 3, missing=claims), DRAFT, None)
    assert out["missing"] == claims[:1]
    assert [d["claim"] for d in out["dropped_claims"]] == claims[1:]


def test_extra_with_bad_n_dropped():
    out = audit.check(_answer("problems", 1, extra=[
        {"n": 7, "text": "Signed", "why": "signature"},
        {"n": True, "text": "x", "why": "y"},
        {"n": "2", "text": "x", "why": "y"}]), DRAFT, SOURCE_TEXT)
    assert out["verdict"] == "ok" and out["extra"] == []
    assert len(out["dropped_claims"]) == 3
    out = audit.check(_answer("problems", 1, extra=[
        {"n": 2, "text": "whatever the model says", "why": "a footnote"}]), DRAFT, SOURCE_TEXT)
    assert out["verdict"] == "problems"
    # the email shows the draft's own item, not the model's paraphrase of it
    assert out["extra"] == [{"n": 2, "text": "Lintels sized to drawings", "why": "a footnote"}]


def test_reworded_must_match_draft_text():
    wrong = {"n": 1, "source_text": "Studs at 600mm centres", "draft_text": "Studs at 400 centres"}
    out = audit.check(_answer("problems", 2, reworded=[wrong]), DRAFT, None)
    assert out["verdict"] == "ok" and out["reworded"] == []
    right = {"n": 1, "source_text": "Studs at 600mm centres", "draft_text": "Studs at 600 centres"}
    out = audit.check(_answer("problems", 2, reworded=[right]), DRAFT, None)
    assert out["verdict"] == "problems" and out["reworded"] == [right]


def test_reworded_with_no_difference_or_source_text_not_in_docx_is_dropped():
    same = {"n": 1, "source_text": "studs at 600  CENTRES", "draft_text": "Studs at 600 centres"}
    invented = {"n": 1, "source_text": "Studs at 600mm centres", "draft_text": "Studs at 600 centres"}
    out = audit.check(_answer("problems", 2, reworded=[same, invented]), DRAFT, SOURCE_TEXT)
    assert out["verdict"] == "ok" and out["reworded"] == []
    assert len(out["dropped_claims"]) == 2


def test_split_or_merged_is_reported_but_never_decides_the_verdict():
    out = audit.check(_answer("problems", 2, split_or_merged=[
        {"n": 1, "note": "two rows joined"}, {"n": 9, "note": "no such item"}]), DRAFT, SOURCE_TEXT)
    assert out["verdict"] == "ok"
    assert out["split_or_merged"] == [{"n": 1, "note": "two rows joined"}]
    assert out["dropped_claims"][0]["kind"] == "split_or_merged"


def test_model_ok_with_checked_findings_is_problems():
    out = audit.check(_answer("ok", 3, missing=[
        {"section": "Wall Framing", "text": "Check roof flashing"}]), DRAFT, SOURCE_TEXT)
    assert out["verdict"] == "problems"


def test_model_problems_with_nothing_checked_is_ok():
    out = audit.check(_answer("problems", 2), DRAFT, SOURCE_TEXT)
    assert out["verdict"] == "ok"


def test_empty_draft_missed_needs_checked_missing_for_docx():
    empty = _result(status="unreadable", reason="No checklist rows found")
    out = audit.check(_answer("missed", 3, missing=[
        {"section": "", "text": "Something never in the file"}]), empty, SOURCE_TEXT)
    assert out["verdict"] == "unsure" and out["error"]
    out = audit.check(_answer("missed", 3, missing=[
        {"section": "", "text": "Check roof flashing"}]), empty, SOURCE_TEXT)
    assert out["verdict"] == "missed" and len(out["missing"]) == 1


def test_empty_draft_pdf_missed_needs_a_source_count():
    empty = _result(status="unreadable", reason="No checklist rows found")
    claim = {"section": "", "text": "Check roof flashing"}
    assert audit.check(_answer("missed", 0, missing=[claim]), empty, None)["verdict"] == "unsure"
    assert audit.check(_answer("missed", None, missing=[claim]), empty, None)["verdict"] == "unsure"
    assert audit.check(_answer("missed", 4, missing=[claim]), empty, None)["verdict"] == "missed"


def test_empty_draft_not_a_checklist():
    empty = _result(status="unreadable", reason="No checklist rows found")
    out = audit.check(_answer("not_a_checklist", 0), empty, "Dear Sir, thank you for")
    assert out["verdict"] == "not_a_checklist"


def test_empty_draft_contradictions_are_unsure():
    empty = _result(status="unreadable", reason="No checklist rows found")
    # says not a checklist, yet names a row that is in the source
    out = audit.check(_answer("not_a_checklist", 3, missing=[
        {"section": "", "text": "Check roof flashing"}]), empty, SOURCE_TEXT)
    assert out["verdict"] == "unsure"


def test_findings_lists_are_capped_at_20():
    many = [{"n": 1, "note": str(i)} for i in range(30)]
    out = audit.check(_answer("problems", 2, split_or_merged=many), DRAFT, SOURCE_TEXT)
    assert len(out["split_or_merged"]) == audit.MAX_FINDINGS == 20


def test_normalise():
    assert audit.normalise("  3.  Check☐ ROOF\n flashing [x] ") == "check roof flashing"
    assert audit.normalise("a) Gate [ ] locked") == "gate locked"
    assert audit.normalise("1.5 m high barrier") == "1.5 m high barrier"


# ---- the request ----------------------------------------------------------------------

def test_prompt_names_the_verdicts_the_non_items_and_the_cap():
    for word in ("ok", "problems", "missed", "not_a_checklist", "Reinspection required?",
                 "signature", "project", "footnote", "column heading", "20", "DATA"):
        assert word.lower() in audit.PROMPT.lower(), word


def test_build_parts_docx_numbers_items_across_sections():
    result = _result("A", "B")
    result["sections"].append({"title": "Roof", "items": [{"text": "C", "flags": []}], "flags": []})
    parts = audit.build_parts({"kind": "docx", "text": "the text", "pages": None}, result)
    assert parts[0] == {"type": "text", "text": audit.PROMPT}
    assert parts[1]["type"] == "text" and parts[1]["text"].endswith("the text")
    draft = json.loads(parts[2]["text"].split("\n", 1)[1])
    assert draft["sections"] == [
        {"title": "Wall Framing", "items": [{"n": 1, "text": "A"}, {"n": 2, "text": "B"}]},
        {"title": "Roof", "items": [{"n": 3, "text": "C"}]}]


def test_build_parts_refused_result_carries_status_and_message():
    empty = _result(status="unreadable", reason="No checklist rows found")
    parts = audit.build_parts({"kind": "docx", "text": "t", "pages": None}, empty)
    draft = json.loads(parts[2]["text"].split("\n", 1)[1])
    assert draft == {"status": "unreadable", "message": "No checklist rows found", "sections": []}


def test_build_parts_pdf_and_images_reuse_the_readers_parts(monkeypatch):
    monkeypatch.setattr(template_vision, "_image_part", lambda page: {"type": "image_url", "p": page["name"]})
    pdf = {"body": b"%PDF", "content_type": "application/pdf", "name": "f.pdf"}
    parts = audit.build_parts({"kind": "pdf", "text": None, "pages": [pdf]}, DRAFT)
    assert parts[1] == template_vision._pdf_part(pdf)
    imgs = [{"body": b"1", "content_type": "image/jpeg", "name": "a.jpg"},
            {"body": b"2", "content_type": "image/jpeg", "name": "b.jpg"}]
    parts = audit.build_parts({"kind": "images", "text": None, "pages": imgs}, DRAFT)
    assert parts[1:3] == [{"type": "image_url", "p": "a.jpg"}, {"type": "image_url", "p": "b.jpg"}]


# ---- which model ----------------------------------------------------------------------

def test_audit_model_default_for_rule_read(monkeypatch):
    monkeypatch.delenv("TEMPLATE_DIGITISE_AUDIT_MODEL", raising=False)
    assert audit.audit_model(None) == ("google/gemini-3.8-flash", None)
    assert audit.audit_model("meta/muse-spark-1.3-contributor") == ("google/gemini-3.8-flash", None)


def test_audit_model_switches_to_alt_when_reader_is_default(monkeypatch):
    monkeypatch.delenv("TEMPLATE_DIGITISE_AUDIT_MODEL", raising=False)
    monkeypatch.delenv("TEMPLATE_DIGITISE_AUDIT_ALT_MODEL", raising=False)
    assert audit.audit_model("google/gemini-3.8-flash") == (
        "meta/muse-spark-1.3-contributor", {"effort": "low"})
    monkeypatch.setenv("TEMPLATE_DIGITISE_AUDIT_MODEL", "x/reader")
    monkeypatch.setenv("TEMPLATE_DIGITISE_AUDIT_ALT_MODEL", "y/alt")
    assert audit.audit_model("x/reader") == ("y/alt", {"effort": "low"})


def test_audit_model_never_the_reader_even_when_alt_is_it_too(monkeypatch):
    monkeypatch.setenv("TEMPLATE_DIGITISE_AUDIT_MODEL", "x/reader")
    monkeypatch.setenv("TEMPLATE_DIGITISE_AUDIT_ALT_MODEL", "x/reader")
    assert audit.audit_model("x/reader") == (None, None)


# ---- run(): one call, one retry, unsure on failure ------------------------------------

def _fake_llm(monkeypatch, answers):
    calls, seq = [], list(answers)

    def fake(parts, **kw):
        calls.append({"parts": parts, **kw})
        return seq.pop(0)

    monkeypatch.setattr(lu, "call_llm_parts", fake)
    return calls


DOCX_SOURCE = {"kind": "docx", "text": SOURCE_TEXT, "pages": None}


def test_run_sends_the_audit_request_and_checks_the_answer(monkeypatch):
    monkeypatch.delenv("TEMPLATE_DIGITISE_AUDIT_MODEL", raising=False)
    monkeypatch.setenv("TEMPLATE_DIGITISE_REASONING", "high")   # the READER's, never sent
    monkeypatch.setenv("TEMPLATE_DIGITISE_PROVIDER", "coreweave/fp8")
    calls = _fake_llm(monkeypatch, [(json.dumps(_answer("problems", 3, missing=[
        {"section": "Wall Framing", "text": "Check roof flashing"},
        {"section": "Wall Framing", "text": "Invented row"}])), None)])
    rec = audit.run(DOCX_SOURCE, DRAFT)
    assert len(calls) == 1
    kw = calls[0]
    assert kw["model"] == "google/gemini-3.8-flash" and kw["max_tokens"] == 32000
    assert kw["caller"] == "template_audit"
    assert kw["fallback_env"] == "TEMPLATE_DIGITISE_AUDIT_FALLBACK_MODELS"
    assert kw.get("reasoning") is None and kw.get("provider") is None
    assert kw["parts"] == audit.build_parts(DOCX_SOURCE, DRAFT)
    assert rec["verdict"] == "problems" and rec["source_items"] == 3
    assert [m["text"] for m in rec["missing"]] == ["Check roof flashing"]
    assert len(rec["dropped_claims"]) == 1
    assert rec["model"] == "google/gemini-3.8-flash" and rec["error"] is None
    assert isinstance(rec["elapsed_s"], float)
    assert set(rec) >= {"verdict", "source_items", "missing", "extra", "reworded",
                        "split_or_merged", "summary", "dropped_claims", "model",
                        "elapsed_s", "error"}


def test_run_alt_model_gets_low_reasoning(monkeypatch):
    monkeypatch.delenv("TEMPLATE_DIGITISE_AUDIT_MODEL", raising=False)
    monkeypatch.delenv("TEMPLATE_DIGITISE_AUDIT_ALT_MODEL", raising=False)
    calls = _fake_llm(monkeypatch, [(json.dumps(_answer("ok", 2)), None)])
    rec = audit.run(DOCX_SOURCE, dict(DRAFT, route="docx_model", model="google/gemini-3.8-flash"))
    assert calls[0]["model"] == "meta/muse-spark-1.3-contributor"
    assert calls[0]["reasoning"] == {"effort": "low"}
    assert rec["verdict"] == "ok" and rec["model"] == "meta/muse-spark-1.3-contributor"


def test_unparseable_once_is_retried(monkeypatch):
    calls = _fake_llm(monkeypatch, [("not json", None), (json.dumps(_answer("ok", 2)), None)])
    assert audit.run(DOCX_SOURCE, DRAFT)["verdict"] == "ok"
    assert len(calls) == 2


def test_unparseable_twice_is_unsure(monkeypatch):
    calls = _fake_llm(monkeypatch, [("not json", None), ('{"verdict": "great"}', None)])
    rec = audit.run(DOCX_SOURCE, DRAFT)
    assert rec["verdict"] == "unsure" and rec["error"]
    assert rec["missing"] == [] and rec["dropped_claims"] == []
    assert len(calls) == 2


def test_no_retry_without_30s_left(monkeypatch):
    calls = _fake_llm(monkeypatch, [("not json", None), (json.dumps(_answer("ok", 2)), None)])
    assert audit.run(DOCX_SOURCE, DRAFT, deadline=20)["verdict"] == "unsure"
    assert len(calls) == 1


def test_model_error_is_unsure(monkeypatch):
    calls = _fake_llm(monkeypatch, [(None, "all models failed: x 503")])
    rec = audit.run(DOCX_SOURCE, DRAFT)
    assert rec["verdict"] == "unsure" and "503" in rec["error"]
    assert len(calls) == 1


def test_model_raising_is_unsure(monkeypatch):
    def boom(parts, **kw):
        raise RuntimeError("socket closed")
    monkeypatch.setattr(lu, "call_llm_parts", boom)
    rec = audit.run(DOCX_SOURCE, DRAFT)
    assert rec["verdict"] == "unsure" and "socket closed" in rec["error"]


def test_no_independent_model_is_unsure_without_a_call(monkeypatch):
    monkeypatch.setenv("TEMPLATE_DIGITISE_AUDIT_MODEL", "x/reader")
    monkeypatch.setenv("TEMPLATE_DIGITISE_AUDIT_ALT_MODEL", "x/reader")
    calls = _fake_llm(monkeypatch, [])
    rec = audit.run(DOCX_SOURCE, dict(DRAFT, model="x/reader"))
    assert rec["verdict"] == "unsure" and calls == []


def test_run_never_changes_the_result(monkeypatch):
    _fake_llm(monkeypatch, [(json.dumps(_answer("problems", 3, extra=[
        {"n": 1, "text": "x", "why": "y"}])), None)])
    result = _result("Studs at 600 centres", "Lintels sized to drawings")
    before = json.dumps(result, sort_keys=True)
    audit.run(DOCX_SOURCE, result)
    assert json.dumps(result, sort_keys=True) == before
