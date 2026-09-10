"""Unit: the daily report a meeting writes for the frontend.

`convert_to_daily_report_format` exists so a meeting shows up on the same screen
as a site walk. Two things it did on the way there were wrong, and both were
visible in a real report the device owner read on 2026-09-10.

It put the MEETING TITLE in the `site` field, with the comment "shows in UI
header". The header did show it, and so the report read

    Site: FieldSight platform and hardware

which is not a site. That field is not decoration: `reports/.../daily_report.json`
triggers embed-report and `lambda_ingest.resolve_site` looks a site up BY NAME,
which is BUG-41's exact failure -- an env fallback putting a name that is not a
site into an attribution the RAG index then trusts. It also broke the weather
block, which needs a site with a coordinate and reported "the site had no
coordinate when it was generated".

And it appended open questions to the end of every topic summary with string
concatenation, which is what made the report read as a wall of prose.
"""
import pytest

mm = pytest.importorskip("lambda_meeting_minutes")


def _minutes(**kw):
    base = {
        "meeting_date": "2026-08-27",
        "meeting_title": "FieldSight platform and hardware",
        "attendees": ["Ben", "James", "Benny"],
        "executive_summary": "Aligned on platform and hardware.",
        "topics": [],
        "_report_metadata": {"version": "v1.1"},
    }
    base.update(kw)
    return base


def _topic(**kw):
    base = {
        "topic_id": 0,
        "time_range": "09:35 – 09:40",
        "topic_title": "Opening priorities",
        "category": "general",
        "summary": "Ben outlined the day's plan.",
        "participants": ["Ben"],
        "key_decisions": [],
        "action_items": [],
        "open_questions": [],
    }
    base.update(kw)
    return base


def _convert(minutes, config=None):
    report, _user = mm.convert_to_daily_report_format(
        minutes, config or {"date": "2026-08-27", "user": "Ben_UCPK2"}, [])
    return report


# ---------------------------------------------------------------------------

def test_the_meeting_title_is_never_the_site():
    """A name that is not a site is worse than no site at all: `resolve_site`
    looks it up by name and takes what it finds."""
    report = _convert(_minutes())
    assert report["site"] != "FieldSight platform and hardware"


def test_an_unknown_site_is_empty_rather_than_invented():
    """The meeting path has no site to offer -- `meeting_config` carries date,
    title, type, attendees and user, and nothing else. Empty lets ingest fall
    back to `recordings.site_id`, which is the authoritative one."""
    assert _convert(_minutes())["site"] == ""


def test_a_known_site_is_carried_through():
    report = _convert(_minutes(), config={"date": "2026-08-27", "user": "Ben_UCPK2",
                                          "site": "UC PK"})
    assert report["site"] == "UC PK"


def test_the_meeting_title_gets_its_own_field():
    """It is still what the header should say -- it just is not a site."""
    assert _convert(_minutes())["meeting_title"] == "FieldSight platform and hardware"


def test_open_questions_are_not_glued_onto_the_summary():
    report = _convert(_minutes(topics=[_topic(
        summary="Ben outlined the day's plan.",
        open_questions=["What are the port three requirements?"],
    )]))
    summary = report["topics"][0]["summary"]
    assert "Open questions" not in summary
    assert summary == "Ben outlined the day's plan."


def test_open_questions_survive_where_a_section_can_find_them():
    """Removing them from the prose must not lose them."""
    report = _convert(_minutes(topics=[_topic(
        open_questions=["What are the port three requirements?"])]))
    assert report["topics"][0]["open_questions"] == [
        "What are the port three requirements?"]


def test_a_missing_owner_or_deadline_is_blank_not_a_question_mark():
    """`'?'` is truthy. Ranked as a date it puts every undated action from a
    meeting above every genuinely dated one."""
    report = _convert(_minutes(topics=[_topic(
        action_items=[{"action": "Prepare pitch deck"}])]))
    item = report["topics"][0]["action_items"][0]
    assert item["deadline"] == ""
    assert item["responsible"] == ""


def test_the_report_carries_sections_for_the_reader():
    report = _convert(_minutes(topics=[_topic(
        action_items=[{"action": "Prepare pitch deck", "priority": "high"}],
        open_questions=["Who signs this off?"],
    )]))
    titles = [s["title"] for s in report["sections"]]
    assert "Actions" in titles and "Open Questions" in titles
    assert not any("timeline" in t.lower() for t in titles)


def test_topics_are_still_there_for_the_indexer():
    """chunking.py reads report["topics"]. Sections are additional, not a
    replacement."""
    report = _convert(_minutes(topics=[_topic()]))
    assert report["topics"], "removing topics would silently empty the RAG index"
