"""lambda_report_generator.py feeds report_sections.py but also prompts and
renders directly on its own, in paths report_sections's fixes never touch:

1. build_weekly_prompt() writes a raw `responsible` into the text handed to
   the model for the weekly/monthly rollup -- same shape as the original bug
   (a label the model can echo into prose where nothing downstream filters
   it).
2. generate_word_document()'s legacy fallback (no `sections` key on
   report_data -- this is what weekly, monthly, site-summary, and any
   pre-`sections` daily report still use) renders who_raised, who_mentioned,
   and responsible raw, plus a generic dict renderer that dumps every field
   verbatim.
3. render_sections_into()'s table branch has no filter of its own -- pure
   defence in depth, since `sections` here is already sanitized by
   report_sections.build().

Same shared helper (transcript_utils.sanitize_speaker_label) throughout.
"""
import os

import pytest

os.environ.setdefault("AWS_ACCESS_KEY_ID", "testing")
os.environ.setdefault("AWS_SECRET_ACCESS_KEY", "testing")
os.environ.setdefault("AWS_DEFAULT_REGION", "ap-southeast-2")
os.environ.setdefault("ANTHROPIC_API_KEY", "sk-ant-test-dummy-key")

rg = pytest.importorskip("lambda_report_generator",
                         reason="requires boto3/urllib3 (installed in CI)")
docx = pytest.importorskip("docx", reason="python-docx layer not installed here")

from io import BytesIO
import zipfile


def _text(buf):
    with zipfile.ZipFile(BytesIO(buf.getvalue())) as z:
        return z.read("word/document.xml").decode("utf-8")


# ---------------------------------------------------------------------------
# 1. build_weekly_prompt() -- the rollup model prompt
# ---------------------------------------------------------------------------

def _daily_report(**kw):
    base = {"report_date": "2026-09-09", "user_name": "Ben_UCPK2",
            "executive_summary": "A day on site.", "topics": []}
    base.update(kw)
    return base


def test_weekly_prompt_responsible_label_reads_the_missing_placeholder():
    """This site's own existing representation of a missing `responsible` is
    the literal "?" (`a.get('responsible', '?')`) -- a label must collapse to
    that same placeholder, not a new string."""
    report = _daily_report(topics=[{
        "category": "progress", "topic_title": "Plumbing",
        "action_items": [{"action": "Fix pipe", "responsible": "spk_1",
                          "deadline": "Mon"}],
    }])
    prompt = rg.build_weekly_prompt([report], "UC PK", "2026-09-08", "2026-09-14")
    assert "spk_1" not in prompt.lower()
    assert "→ ? by Mon" in prompt


def test_weekly_prompt_real_responsible_is_untouched():
    report = _daily_report(topics=[{
        "category": "progress", "topic_title": "Plumbing",
        "action_items": [{"action": "Fix pipe", "responsible": "Daniel",
                          "deadline": "Mon"}],
    }])
    prompt = rg.build_weekly_prompt([report], "UC PK", "2026-09-08", "2026-09-14")
    assert "→ Daniel by Mon" in prompt


def test_weekly_prompt_embedded_label_in_action_text_reads_someone():
    report = _daily_report(topics=[{
        "category": "progress", "topic_title": "Plumbing",
        "action_items": [{
            "action": "Email Chris Fellows, spk_1, Nick re: Unit 11 pipes",
            "responsible": "Daniel", "deadline": "Mon"}],
    }])
    prompt = rg.build_weekly_prompt([report], "UC PK", "2026-09-08", "2026-09-14")
    assert "spk_1" not in prompt.lower()
    assert "Email Chris Fellows, someone, Nick re: Unit 11 pipes" in prompt


# ---------------------------------------------------------------------------
# 2. generate_word_document()'s legacy fallback (no `sections` key)
# ---------------------------------------------------------------------------

def _legacy_report(**kw):
    base = {"executive_summary": "A day on site.", "topics": []}
    base.update(kw)
    return base


def test_legacy_safety_observation_who_raised_label_is_omitted_like_a_missing_one():
    """The existing representation of a missing `who_raised` is to omit the
    "(raised by ...)" clause entirely (`if who:`). A label must collapse to
    that same omission, not a new string."""
    report = _legacy_report(safety_observations=[
        {"observation": "Loose scaffold board", "who_raised": "spk_1"}])
    xml = _text(rg.generate_word_document(report, "Daily report"))
    assert "spk_1" not in xml.lower()
    assert "raised by" not in xml.lower()
    assert "Loose scaffold board" in xml


def test_legacy_safety_observation_real_who_raised_is_kept():
    report = _legacy_report(safety_observations=[
        {"observation": "Loose scaffold board", "who_raised": "Daniel"}])
    xml = _text(rg.generate_word_document(report, "Daily report"))
    assert "raised by daniel" in xml.lower()


def test_legacy_critical_date_who_mentioned_label_is_omitted_like_a_missing_one():
    report = _legacy_report(critical_dates_and_deadlines=[
        {"date_mentioned": "28th", "context": "Backfill Zone 1",
         "who_mentioned": "spk_0"}])
    xml = _text(rg.generate_word_document(report, "Daily report"))
    assert "spk_0" not in xml.lower()
    assert "mentioned by" not in xml.lower()


def test_legacy_critical_date_real_who_mentioned_is_kept():
    report = _legacy_report(critical_dates_and_deadlines=[
        {"date_mentioned": "28th", "context": "Backfill Zone 1",
         "who_mentioned": "Ben"}])
    xml = _text(rg.generate_word_document(report, "Daily report"))
    assert "mentioned by ben" in xml.lower()


def test_legacy_timeline_action_item_responsible_label_reads_missing_placeholder():
    report = _legacy_report(topics=[{
        "category": "progress", "topic_title": "Plumbing", "time_range": "10:00",
        "action_items": [{"action": "Fix pipe", "responsible": "spk_1",
                          "deadline": "Mon", "priority": "high"}],
    }])
    xml = _text(rg.generate_word_document(report, "Daily report"))
    assert "spk_1" not in xml.lower()
    assert "→ ? by mon" in xml.lower()


def test_legacy_timeline_action_item_embedded_label_reads_someone():
    report = _legacy_report(topics=[{
        "category": "progress", "topic_title": "Plumbing", "time_range": "10:00",
        "action_items": [{
            "action": "Email Chris Fellows, spk_1, Nick re: Unit 11 pipes",
            "responsible": "Daniel", "deadline": "Mon", "priority": "high"}],
    }])
    xml = _text(rg.generate_word_document(report, "Daily report"))
    assert "spk_1" not in xml.lower()
    assert "Email Chris Fellows, someone, Nick re: Unit 11 pipes" in xml


def test_legacy_generic_renderer_drops_a_label_only_field_like_a_missing_one():
    """outstanding_actions carries `responsible` per the weekly schema. The
    generic renderer dumps every truthy field value, so a label-only value
    must drop out of the joined line exactly like an originally-missing
    (falsy) field already does -- no key-name special-casing."""
    report = _legacy_report(outstanding_actions=[
        {"action": "Fix pipe", "responsible": "spk_1", "status": "open"}])
    xml = _text(rg.generate_word_document(report, "Daily report"))
    assert "spk_1" not in xml.lower()
    assert "Fix pipe" in xml and "open" in xml


def test_legacy_generic_renderer_keeps_a_real_name():
    report = _legacy_report(outstanding_actions=[
        {"action": "Fix pipe", "responsible": "Daniel", "status": "open"}])
    xml = _text(rg.generate_word_document(report, "Daily report"))
    assert "Daniel" in xml


def test_legacy_generic_renderer_scrubs_an_embedded_label_in_any_field():
    """The filter works on the emitted VALUE, not a named key -- an embedded
    label anywhere in the dict (here, the action text itself) is caught."""
    report = _legacy_report(outstanding_actions=[
        {"action": "Email Chris Fellows, spk_1, Nick re: Unit 11 pipes",
         "responsible": "Daniel", "status": "open"}])
    xml = _text(rg.generate_word_document(report, "Daily report"))
    assert "spk_1" not in xml.lower()
    assert "Email Chris Fellows, someone, Nick re: Unit 11 pipes" in xml


# ---------------------------------------------------------------------------
# 3. render_sections_into()'s table branch -- defence in depth
# ---------------------------------------------------------------------------

def test_sections_table_drops_a_label_only_cell_like_a_missing_one():
    """A missing field in this generic table already renders as "" (`''` if
    value is None). A label-only cell must collapse to that same "", not a
    new string -- and with no special-casing of the "owner" column name."""
    report = {"sections": [
        {"title": "Actions", "kind": "table", "fields": ["action", "owner"],
         "rows": [{"action": "Fix pipe", "owner": "spk_1"}]},
    ]}
    xml = _text(rg.generate_word_document(report, "Daily report"))
    assert "spk_1" not in xml.lower()
    assert "Fix pipe" in xml


def test_sections_table_keeps_a_real_name():
    report = {"sections": [
        {"title": "Actions", "kind": "table", "fields": ["action", "owner"],
         "rows": [{"action": "Fix pipe", "owner": "Daniel"}]},
    ]}
    xml = _text(rg.generate_word_document(report, "Daily report"))
    assert "Daniel" in xml


def test_sections_table_scrubs_an_embedded_label_in_any_column():
    report = {"sections": [
        {"title": "Actions", "kind": "table", "fields": ["action", "owner"],
         "rows": [{"action": "Email Chris Fellows, spk_1, Nick re: Unit 11 pipes",
                   "owner": "Daniel"}]},
    ]}
    xml = _text(rg.generate_word_document(report, "Daily report"))
    assert "spk_1" not in xml.lower()
    assert "Email Chris Fellows, someone, Nick re: Unit 11 pipes" in xml
