"""The nightly daily report -- the one a person actually reads, and the one
this project already shipped a spk_1 into as "the responsible person" -- must
never render a raw speaker-diarization label as an owner or leave one sitting
in an action's own text. Same rule as the generated-report path
(tests/unit/test_speaker_label_not_owner.py), same shared helper
(transcript_utils.sanitize_speaker_label), applied here to:
  - report_sections._actions() (both the first-seen owner and the
    later-mention merge branch)
  - report_template._action_lines() (defence in depth for the model prompt,
    not exposed today because its only caller already sanitizes upstream)
"""
import report_sections as rs
import report_template


def _topic(**kw):
    base = {
        "topic_title": "Ground floor inspection",
        "time_range": "10:00 - 10:03",
        "category": "quality",
        "participants": ["Ben"],
        "summary": "Checked the IT room.",
        "key_decisions": [],
        "action_items": [],
        "safety_flags": [],
        "related_photos": [],
    }
    base.update(kw)
    return base


def _report(**kw):
    base = {
        "report_date": "2026-09-09",
        "user_name": "Ben_UCPK2",
        "site": "UC PK",
        "executive_summary": "A day on site.",
        "quality_and_compliance": [],
        "safety_observations": [],
        "topics": [],
        "recording_session": {"recordings": 3, "total_duration_display": "2m 52s", "photos": 6},
    }
    base.update(kw)
    return base


def _by_title(sections, title):
    for s in sections:
        if s["title"] == title:
            return s
    return None


# ---------------------------------------------------------------------------
# report_sections._actions()
# ---------------------------------------------------------------------------

def test_a_speaker_label_owner_collapses_to_the_same_no_owner_representation():
    """A missing owner already renders as "" in this section (see
    test_the_action_table_names_its_columns's sibling tests) -- a label must
    collapse to that same representation, not a new string."""
    with_missing = _report(topics=[_topic(action_items=[{"action": "Do it"}])])
    with_label = _report(topics=[_topic(action_items=[
        {"action": "Do it", "responsible": "spk_1"}])])
    missing_owner = _by_title(rs.build(with_missing), "Actions")["rows"][0]["owner"]
    label_owner = _by_title(rs.build(with_label), "Actions")["rows"][0]["owner"]
    assert label_owner == missing_owner == ""


def test_a_real_owner_is_untouched():
    r = _report(topics=[_topic(action_items=[
        {"action": "Do it", "responsible": "Daniel", "deadline": "2026-09-12"}])])
    row = _by_title(rs.build(r), "Actions")["rows"][0]
    assert row["owner"] == "Daniel"


def test_a_speaker_label_owner_filled_in_on_a_later_mention_also_collapses():
    """The merge branch (a second topic naming an owner for an action already
    seen) has its own `_clean(item.get("responsible"))` call and must not
    let a label through there either."""
    r = _report(topics=[
        _topic(action_items=[{"action": "Tidy the IT room"}]),
        _topic(topic_title="Later", action_items=[
            {"action": "Tidy the IT room", "responsible": "spk_0"}]),
    ])
    row = _by_title(rs.build(r), "Actions")["rows"][0]
    assert row["owner"] == ""


def test_a_real_owner_filled_in_on_a_later_mention_is_kept():
    r = _report(topics=[
        _topic(action_items=[{"action": "Tidy the IT room"}]),
        _topic(topic_title="Later", action_items=[
            {"action": "Tidy the IT room", "responsible": "Daniel"}]),
    ])
    row = _by_title(rs.build(r), "Actions")["rows"][0]
    assert row["owner"] == "Daniel"


def test_an_embedded_label_in_the_action_text_reads_someone():
    r = _report(topics=[_topic(action_items=[
        {"action": "Email Chris Fellows, spk_1, Nick re: Unit 11 pipes",
         "responsible": "Daniel"}])])
    row = _by_title(rs.build(r), "Actions")["rows"][0]
    assert row["action"] == "Email Chris Fellows, someone, Nick re: Unit 11 pipes"
    assert row["owner"] == "Daniel"


def test_lookalike_words_in_action_text_are_untouched():
    r = _report(topics=[_topic(action_items=[
        {"action": "Ask the spkr on site about the speaker system", "responsible": "Daniel"}])])
    row = _by_title(rs.build(r), "Actions")["rows"][0]
    assert row["action"] == "Ask the spkr on site about the speaker system"


# ---------------------------------------------------------------------------
# report_template._action_lines() -- defence in depth
# ---------------------------------------------------------------------------

TEMPLATE = {
    "template_id": "personal-meeting",
    "version": 3,
    "name": "Personal Meeting Notes",
    "sections": [{"key": "actions", "title": "Actions", "purpose": "One line per action."}],
    "catch_all": {"key": "other", "title": "Anything else", "purpose": "What still matters."},
    "excluded_subjects": [],
    "style": [],
}
SCOPE = {"folder": "Ben_UCPK2", "date": "2026-09-10", "from": "09:00", "to": "11:30",
         "recordings": 70}


def test_action_lines_owner_label_reads_no_owner_recorded():
    items = [{"action": "Fix pipe", "owner": "spk_1", "deadline": "Mon"}]
    p = report_template.render_prompt(TEMPLATE, SCOPE, items, "[09:00:00] Ben: morning")
    assert "spk_1" not in p.lower()
    assert "owner: no owner recorded" in p


def test_action_lines_embedded_label_reads_someone():
    items = [{"action": "Email Chris Fellows, spk_1, Nick re: Unit 11 pipes",
              "owner": "Daniel", "deadline": "Mon"}]
    p = report_template.render_prompt(TEMPLATE, SCOPE, items, "[09:00:00] Ben: morning")
    assert "spk_1" not in p.lower()
    assert "Email Chris Fellows, someone, Nick re: Unit 11 pipes" in p
    assert "owner: Daniel" in p


def test_action_lines_real_owner_untouched():
    items = [{"action": "Fix pipe", "owner": "Daniel", "deadline": "Mon"}]
    p = report_template.render_prompt(TEMPLATE, SCOPE, items, "[09:00:00] Ben: morning")
    assert "owner: Daniel" in p
