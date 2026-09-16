"""A speaker-diarization label (spk_0, spk_1, ...) is a per-call transcription
artefact, not an identity -- the same spk_1 in two different calls is usually
two different people. This has shipped as a bug before: a `spk_1` label once
reached a customer email as "the responsible person" because it was sitting
in `action_items.responsible` and the renderer printed the Owner column
verbatim.

These tests cover the one shared helper (`_sanitize_speaker_label` in
lambda_meeting_minutes.py) and all three call sites that must use it:
  1. generate_prose_document's Owner cell
  2. generate_word_document's assembled-minutes action items and follow-ups
  3. lambda_session_report._action_items_for_prompt, so the model is never
     handed a label to echo into prose in the first place.
"""
from io import BytesIO
import zipfile

import pytest

docx = pytest.importorskip("docx", reason="python-docx layer not installed here")

import lambda_meeting_minutes as mm
from lambda_meeting_minutes import (
    _sanitize_speaker_label,
    generate_prose_document,
    generate_word_document,
)
from lambda_session_report import _action_items_for_prompt


def _text(buf):
    with zipfile.ZipFile(BytesIO(buf.getvalue())) as z:
        return z.read("word/document.xml").decode("utf-8")


# ---------------------------------------------------------------------------
# The shared helper itself
# ---------------------------------------------------------------------------

def test_helper_blanks_a_value_that_is_purely_a_speaker_label():
    assert _sanitize_speaker_label("spk_1") == ""
    assert _sanitize_speaker_label("  SPK_12  ") == ""


def test_helper_leaves_a_real_name_untouched():
    assert _sanitize_speaker_label("Daniel") == "Daniel"


def test_helper_does_not_mangle_words_that_merely_contain_the_letters():
    assert _sanitize_speaker_label("spkr") == "spkr"
    assert _sanitize_speaker_label("speaker") == "speaker"


def test_helper_replaces_an_embedded_token_with_someone_and_keeps_the_rest():
    text = "Email Chris Fellows, spk_1, Nick re: Unit 11 pipes"
    assert _sanitize_speaker_label(text) == \
        "Email Chris Fellows, someone, Nick re: Unit 11 pipes"


# ---------------------------------------------------------------------------
# 1. generate_prose_document's Owner cell
# ---------------------------------------------------------------------------

SECTIONS = [{"title": "What this was", "paragraphs": ["Notes."]}]


def test_prose_document_owner_that_is_a_speaker_label_reads_no_owner_recorded():
    actions = [{"action": "Email supplier", "owner": "spk_1", "deadline": "Fri"}]
    xml = _text(generate_prose_document("Meeting Notes", "Site", SECTIONS, actions))
    assert "spk_1" not in xml.lower()
    assert "no owner recorded" in xml


def test_prose_document_real_owner_is_untouched():
    actions = [{"action": "Email supplier", "owner": "Daniel", "deadline": "Fri"}]
    xml = _text(generate_prose_document("Meeting Notes", "Site", SECTIONS, actions))
    assert "Daniel" in xml


def test_prose_document_action_text_with_embedded_label_reads_someone():
    actions = [{"action": "Email Chris Fellows, spk_1, Nick re: Unit 11 pipes",
                "owner": "Daniel", "deadline": "Fri"}]
    xml = _text(generate_prose_document("Meeting Notes", "Site", SECTIONS, actions))
    assert "spk_1" not in xml.lower()
    assert "Email Chris Fellows, someone, Nick re: Unit 11 pipes" in xml


# ---------------------------------------------------------------------------
# 2. generate_word_document -- the assembled minutes path (today's shipping
#    behaviour, so the exposure predates the generation work).
# ---------------------------------------------------------------------------

def _minutes(topics=None, follow_ups=None):
    return {"meeting_date": "2026-09-03", "attendees": ["Ben"],
            "topics": topics or [], "follow_ups": follow_ups or []}


def test_assembled_minutes_action_item_owner_label_reads_no_owner_recorded():
    topic = {"topic_title": "Unit 11 plumbing", "category": "general",
              "summary": "Discussed pipe leak.",
              "action_items": [{"action": "Fix pipe", "owner": "spk_1",
                                "deadline": "Mon", "priority": "high"}]}
    xml = _text(generate_word_document(_minutes([topic]), "Session report"))
    assert "spk_1" not in xml.lower()
    assert "no owner recorded" in xml


def test_assembled_minutes_action_item_text_with_embedded_label_reads_someone():
    topic = {"topic_title": "Unit 11 plumbing", "category": "general",
              "summary": "Discussed pipe leak.",
              "action_items": [{
                  "action": "Email Chris Fellows, spk_1, Nick re: Unit 11 pipes",
                  "owner": "Daniel", "deadline": "Mon", "priority": "high"}]}
    xml = _text(generate_word_document(_minutes([topic]), "Session report"))
    assert "spk_1" not in xml.lower()
    assert "Email Chris Fellows, someone, Nick re: Unit 11 pipes" in xml
    assert "Daniel" in xml


def test_assembled_minutes_follow_up_owner_label_reads_no_owner_recorded():
    follow_ups = [{"item": "Confirm delivery date", "owner": "spk_0",
                   "deadline": "Fri", "priority": "medium"}]
    xml = _text(generate_word_document(_minutes(follow_ups=follow_ups), "Session report"))
    assert "spk_0" not in xml.lower()
    assert "no owner recorded" in xml


def test_assembled_minutes_real_owner_and_lookalike_words_untouched():
    topic = {"topic_title": "Unit 11 plumbing", "category": "general",
              "summary": "spkr on site reported a leak.",
              "action_items": [{"action": "Fix pipe", "owner": "Daniel",
                                "deadline": "Mon", "priority": "high"}]}
    xml = _text(generate_word_document(_minutes([topic]), "Session report"))
    assert "Daniel" in xml
    assert "spkr" in xml


# ---------------------------------------------------------------------------
# 3. _action_items_for_prompt -- the model must never be handed a label
# ---------------------------------------------------------------------------

def test_action_items_for_prompt_strips_label_from_owner_field():
    content = {"topics": [{"action_items": [
        {"action": "Fix pipe", "responsible": "spk_1", "deadline": "Mon"}]}]}
    out = _action_items_for_prompt(content)
    assert len(out) == 1
    assert "spk_1" not in (out[0]["owner"] or "").lower()
    assert out[0]["owner"] == ""


def test_action_items_for_prompt_strips_label_from_action_text():
    content = {"topics": [{"action_items": [
        {"action": "Email Chris Fellows, spk_1, Nick re: Unit 11 pipes",
         "owner": "Daniel", "deadline": "Mon"}]}]}
    out = _action_items_for_prompt(content)
    assert "spk_1" not in out[0]["action"].lower()
    assert out[0]["action"] == "Email Chris Fellows, someone, Nick re: Unit 11 pipes"
    assert out[0]["owner"] == "Daniel"


def test_action_items_for_prompt_keeps_real_owner():
    content = {"topics": [{"action_items": [
        {"action": "Fix pipe", "owner": "Daniel", "deadline": "Mon"}]}]}
    out = _action_items_for_prompt(content)
    assert out[0]["owner"] == "Daniel"


# ---------------------------------------------------------------------------
# 4. generate_word_document's Key Decisions block -- sits a few lines above
#    Action Items in the same function, missed by the original fix.
# ---------------------------------------------------------------------------

def test_assembled_minutes_decided_by_label_is_omitted_like_a_missing_one():
    """The existing representation of a missing `decided_by` is to omit the
    "(by ...)" clause entirely (`if decided_by:`). A label must collapse to
    that same omission, not a new string."""
    topic = {"topic_title": "Unit 11 plumbing", "category": "general",
              "summary": "Discussed pipe leak.",
              "key_decisions": [{"decision": "Replace the pipe", "decided_by": "spk_1"}]}
    xml = _text(generate_word_document(_minutes([topic]), "Session report"))
    assert "spk_1" not in xml.lower()
    assert "by spk_1" not in xml.lower()
    assert "Replace the pipe" in xml


def test_assembled_minutes_real_decided_by_is_kept():
    topic = {"topic_title": "Unit 11 plumbing", "category": "general",
              "summary": "Discussed pipe leak.",
              "key_decisions": [{"decision": "Replace the pipe", "decided_by": "Daniel"}]}
    xml = _text(generate_word_document(_minutes([topic]), "Session report"))
    assert "(by daniel)" in xml.lower()


def test_assembled_minutes_decision_text_and_rationale_scrub_embedded_labels():
    topic = {"topic_title": "Unit 11 plumbing", "category": "general",
              "summary": "Discussed pipe leak.",
              "key_decisions": [{
                  "decision": "Ask spk_1 to replace the pipe",
                  "rationale": "Confirmed with Chris Fellows, spk_1, Nick",
                  "decided_by": "Daniel"}]}
    xml = _text(generate_word_document(_minutes([topic]), "Session report"))
    assert "spk_1" not in xml.lower()
    assert "Ask someone to replace the pipe" in xml
    assert "Confirmed with Chris Fellows, someone, Nick" in xml


def test_assembled_minutes_bare_string_decision_scrubs_an_embedded_label():
    topic = {"topic_title": "Unit 11 plumbing", "category": "general",
              "summary": "Discussed pipe leak.",
              "key_decisions": ["Ask spk_1 to replace the pipe"]}
    xml = _text(generate_word_document(_minutes([topic]), "Session report"))
    assert "spk_1" not in xml.lower()
    assert "Ask someone to replace the pipe" in xml


# ---------------------------------------------------------------------------
# 5. convert_to_daily_report_format's decision flattening -- bakes
#    decided_by into a string before report_sections.build() runs on this
#    same compat report, so no downstream filter can reach it.
# ---------------------------------------------------------------------------

def _minutes_input(**kw):
    base = {
        "meeting_date": "2026-08-27",
        "meeting_title": "Site meeting",
        "attendees": ["Ben"],
        "executive_summary": "A meeting happened.",
        "topics": [],
        "_report_metadata": {"version": "v1.1"},
    }
    base.update(kw)
    return base


def _convert(minutes):
    report, _user = mm.convert_to_daily_report_format(
        minutes, {"date": "2026-08-27", "user": "Ben_UCPK2"}, [])
    return report


def test_compat_decision_flattening_omits_a_label_only_decided_by():
    topic = {"topic_id": 0, "time_range": "09:00", "topic_title": "Plumbing",
              "category": "general", "summary": "Discussed pipe leak.",
              "participants": ["Ben"],
              "key_decisions": [{"decision": "Replace the pipe", "decided_by": "spk_1"}],
              "action_items": [], "open_questions": []}
    report = _convert(_minutes_input(topics=[topic]))
    flat = report["topics"][0]["key_decisions"]
    assert flat == ["Replace the pipe"]


def test_compat_decision_flattening_keeps_a_real_decided_by():
    topic = {"topic_id": 0, "time_range": "09:00", "topic_title": "Plumbing",
              "category": "general", "summary": "Discussed pipe leak.",
              "participants": ["Ben"],
              "key_decisions": [{"decision": "Replace the pipe", "decided_by": "Daniel"}],
              "action_items": [], "open_questions": []}
    report = _convert(_minutes_input(topics=[topic]))
    assert report["topics"][0]["key_decisions"] == ["Replace the pipe (by Daniel)"]


def test_compat_decision_flattening_scrubs_an_embedded_label_in_decision_text():
    topic = {"topic_id": 0, "time_range": "09:00", "topic_title": "Plumbing",
              "category": "general", "summary": "Discussed pipe leak.",
              "participants": ["Ben"],
              "key_decisions": ["Ask spk_1 to replace the pipe"],
              "action_items": [], "open_questions": []}
    report = _convert(_minutes_input(topics=[topic]))
    assert report["topics"][0]["key_decisions"] == ["Ask someone to replace the pipe"]


def test_compat_decision_flattening_reaches_the_sections_the_frontend_reads():
    """The point of fixing this here: `sections` is built from this same
    flattened `topics` a few lines later in `convert_to_daily_report_format`,
    and that is what the frontend actually renders."""
    topic = {"topic_id": 0, "time_range": "09:00", "topic_title": "Plumbing",
              "category": "general", "summary": "Discussed pipe leak.",
              "participants": ["Ben"],
              "key_decisions": [{"decision": "Replace the pipe", "decided_by": "spk_1"}],
              "action_items": [], "open_questions": []}
    report = _convert(_minutes_input(topics=[topic]))
    decisions_section = next(
        (s for s in report["sections"] if s["title"] == "Decisions"), None)
    assert decisions_section is not None
    assert "spk_1" not in " ".join(decisions_section["items"]).lower()
