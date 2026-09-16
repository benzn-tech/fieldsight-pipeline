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
