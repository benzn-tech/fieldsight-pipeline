"""A template report is written from the record -- the topics -- not from a
second reading of the transcript.

Owner, 2026-10-01: the page and the report split the same afternoon
differently (TEST, Ben_Lin_test2), and every regeneration redrew it. The topics
are what people see, correct and delete on the page, so each topic's own
content now goes to the model as the record, the transcript stays for exact
detail and checklist evidence, and the lines that name no topic are counted
outside the model.

THE test is `the prompt carries each topic's content and says to report only it`.
"""
import lambda_session_report as sr
import report_template as rt

TOPIC = {"topic_title": "Daily Progress Photo Inspection", "time_range": "13:24 – 13:27",
         "category": "progress",
         "summary": "Ben walked the Ground floor\nthen Level 1, photographing progress.",
         "key_decisions": [{"decision": "Hold the Level 1 ceiling close-up"}],
         "open_questions": ["Who signs off the riser?"],
         "safety_flags": [{"description": "Cable across the main walkway"}],
         "findings": [], "action_items": [{"action": "Tag the cable", "responsible": "Dom"}]}


def test_a_topics_content_is_what_the_page_shows_for_it():
    assert sr._topic_content(TOPIC) == [
        "Summary: Ben walked the Ground floor then Level 1, photographing progress.",
        "Decided: Hold the Level 1 ceiling close-up",
        "Open question: Who signs off the riser?",
        "Safety: Cable across the main walkway",
        "Action: Tag the cable (Dom)"]


def test_THE_the_prompt_carries_each_topics_content_and_says_to_report_only_it():
    offer = [{"ref": "t2", "title": TOPIC["topic_title"], "time_range": TOPIC["time_range"],
              "category": "progress", "content": sr._topic_content(TOPIC), "photos": 7}]
    tpl = {"sections": [{"title": "Quality", "purpose": "p"}],
           "catch_all": {"title": "Anything else", "purpose": "Rest."}}
    scope = {"folder": "F", "date": "2026-10-01", "from": "00:00", "to": "23:59", "recordings": 1}
    p = rt.render_prompt(tpl, scope, [], "the transcript", topics=offer)
    assert "## The record" in p
    assert "- t2  13:24 – 13:27  Daily Progress Photo Inspection  (progress; 7 photographs)" in p
    assert "    Summary: Ben walked the Ground floor then Level 1" in p
    assert "Report nothing the record does not\ncontain" in p
    assert "two floors walked in one inspection" in p
    assert p.index("## The record") < p.index("## Transcript"), "the transcript is still there"


def test_topics_without_content_keep_the_old_block_exactly():
    offer = [{"ref": "t0", "title": "A", "time_range": "09:00 – 09:10", "photos": 0}]
    block = rt._covers_block(offer)
    assert "## What was recorded, and where the photographs sit" in block
    assert "## The record" not in block


# ---- the lines that report no topic ---------------------------------------------------

def test_lines_naming_no_topic_are_counted_but_tables_and_nothing_here_are_not():
    prose = sr._prose_sections(
        "### Quality\n- Ground floor inspected. [t2]\n- A general remark.\n\n"
        "### Decisions\nItem | Owner\n---|---\nHold the close-up | Ben [t2]\nLoose row | Ben\n\n"
        "### Safety\nNothing here.\n\n"
        "### Checklist\nItem | Answer\n---|---\nIs Level 0 inspected? | Yes\n")
    out = sr._unanchored(prose, skip_titles=["Checklist"])
    assert out == [{"section": "Quality", "text": "- A general remark."},
                   {"section": "Decisions", "text": "Loose row | Ben"}]


def test_the_offer_the_worker_builds_carries_the_content():
    """The wiring: what _offered_topics hands render_prompt has the record in it."""
    import datetime as dt
    artifact = {"folder": "F", "date": "2026-10-01", "content": {"topics": [TOPIC]}}
    offer, _ = sr._offered_topics(artifact, [0], dt.datetime(2026, 10, 1, 0, 0),
                                  dt.datetime(2026, 10, 1, 23, 59))
    assert offer[0]["content"] == sr._topic_content(TOPIC) and offer[0]["category"] == "progress"
