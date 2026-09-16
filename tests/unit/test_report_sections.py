"""Unit: the report a person reads, as opposed to the report a machine indexes.

The daily report carries two shapes for two audiences and they must not be
confused. `topics` is the machine's: `chunking.py` splits RAG chunks straight
out of `report["topics"]`, so removing it would silently empty the search index.
`sections` is the person's, and it is shaped like the template Library's
sections (`{title, kind, fields}`) so that pointing reports at a user-editable
template later is a change of data source, not a rewrite.

What the device owner asked for, reading a real report on 2026-09-10:

    "作为报告，不需要知道几点几点干了什么，追溯的时候再去 query 就行，
     我希望呈现出来按 priority 排列的 action list 就行"

So the per-topic timeline stops being rendered. It is still in the file.
"""
import pytest

# Plain import, not importorskip. `report_sections` is pure -- no boto3, no
# psycopg -- so there is no legitimate reason for it to be absent, and a skip
# would mean these 27 tests go green if the module stops being packaged.
import report_sections as rs


def _topic(**kw):
    base = {
        "topic_title": "Ground floor inspection",
        "time_range": "10:00 – 10:03",
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


def _titles(sections):
    return [s["title"] for s in sections]


def _by_title(sections, title):
    for s in sections:
        if s["title"] == title:
            return s
    raise AssertionError(f"no section {title!r} in {_titles(sections)}")


# ---------------------------------------------------------------------------
# Shape
# ---------------------------------------------------------------------------

def test_every_section_is_shaped_like_a_library_template_section():
    """The Library stores {title, kind, fields, prompt_hint}. Matching that shape
    is the whole reason this is a separate structure rather than more top-level
    keys: swapping in a user-edited template later becomes a data change."""
    sections = rs.build(_report())
    assert sections, "a report with no content still has sections"
    for s in sections:
        assert set(("title", "kind")) <= set(s)
        assert s["kind"] in rs.KINDS, f"{s['title']}: unknown kind {s['kind']}"


def test_the_timeline_is_not_a_section():
    """The per-topic blocks — time range, category, participants — are what the
    device owner asked to stop seeing. They stay in `topics` for query."""
    r = _report(topics=[_topic(), _topic(topic_title="Second thing")])
    titles = " ".join(_titles(rs.build(r))).lower()
    assert "timeline" not in titles
    assert "topic" not in titles


def test_topics_are_left_untouched_for_the_indexer():
    """chunking.py reads report["topics"] directly. Building sections must not
    mutate or consume it — an empty index is a silent failure."""
    topics = [_topic()]
    r = _report(topics=topics)
    before = str(topics)
    rs.build(r)
    assert str(topics) == before


# ---------------------------------------------------------------------------
# Actions — the ranking
# ---------------------------------------------------------------------------

def test_a_dated_action_outranks_an_undated_one_whatever_its_priority():
    """Measured on 40 prod extractions, 59 action items: high 44%, medium 51%,
    low 5%. Priority sorts 59 items into two piles and orders neither. A date is
    the only field in this data that discriminates, and only 22% carry one."""
    r = _report(topics=[_topic(action_items=[
        {"action": "Undated but urgent", "priority": "high"},
        {"action": "Due Tuesday", "priority": "low", "deadline": "2026-09-15"},
    ])])
    rows = _by_title(rs.build(r), "Actions")["rows"]
    assert rows[0]["action"] == "Due Tuesday"


def test_earlier_deadlines_come_first():
    r = _report(topics=[_topic(action_items=[
        {"action": "Later", "deadline": "2026-10-01"},
        {"action": "Sooner", "deadline": "2026-09-12"},
    ])])
    rows = _by_title(rs.build(r), "Actions")["rows"]
    assert [x["action"] for x in rows] == ["Sooner", "Later"]


def test_something_raised_twice_outranks_something_raised_once():
    """Within one day, an action that surfaces in two separate topics was
    genuinely returned to. That is a stronger signal than a priority label the
    model applies to half of everything."""
    r = _report(topics=[
        _topic(action_items=[{"action": "Tidy the IT room", "priority": "medium"}]),
        _topic(topic_title="Later", action_items=[
            {"action": "Tidy the IT room", "priority": "medium"},
            {"action": "Order the brackets", "priority": "high"},
        ]),
    ])
    rows = _by_title(rs.build(r), "Actions")["rows"]
    assert rows[0]["action"] == "Tidy the IT room"
    assert rows[0]["mentions"] == 2


def test_the_same_action_appears_once():
    """The sample report the owner read listed overlapping actions twice."""
    r = _report(topics=[
        _topic(action_items=[{"action": "Prepare pitch deck"}]),
        _topic(action_items=[{"action": "prepare pitch deck."}]),
    ])
    rows = _by_title(rs.build(r), "Actions")["rows"]
    assert len(rows) == 1
    assert rows[0]["mentions"] == 2


def test_priority_still_breaks_a_tie():
    r = _report(topics=[_topic(action_items=[
        {"action": "Medium thing", "priority": "medium"},
        {"action": "High thing", "priority": "high"},
    ])])
    rows = _by_title(rs.build(r), "Actions")["rows"]
    assert [x["action"] for x in rows] == ["High thing", "Medium thing"]


def test_the_action_table_names_its_columns():
    r = _report(topics=[_topic(action_items=[
        {"action": "Do it", "responsible": "Ben", "deadline": "2026-09-12", "priority": "high"},
    ])])
    sec = _by_title(rs.build(r), "Actions")
    assert sec["kind"] == "table"
    assert sec["fields"] == ["action", "owner", "due", "priority", "status"]
    assert sec["rows"][0]["owner"] == "Ben"
    assert sec["rows"][0]["due"] == "2026-09-12"


def test_an_action_with_no_text_is_dropped_not_rendered_blank():
    r = _report(topics=[_topic(action_items=[
        {"action": "", "priority": "high"}, {"responsible": "Ben"}, {"action": "Real one"},
    ])])
    rows = _by_title(rs.build(r), "Actions")["rows"]
    assert [x["action"] for x in rows] == ["Real one"]


# ---------------------------------------------------------------------------
# Open questions
# ---------------------------------------------------------------------------

def test_open_questions_are_their_own_section():
    """They were being string-concatenated onto the end of every topic summary
    (lambda_meeting_minutes: `summary += ' Open questions: ' + '; '.join(...)`),
    which is what made the report read as a wall. They are a different kind of
    thing from an action: nobody owns them, because nobody knows the answer."""
    r = _report(topics=[_topic(open_questions=["What are the port three requirements?"])])
    sec = _by_title(rs.build(r), "Open Questions")
    assert sec["kind"] == "list"
    assert sec["items"] == ["What are the port three requirements?"]


def test_questions_are_read_under_either_name():
    """The meeting path writes `open_questions`; the extraction schema says
    `questions`. Both reach this."""
    r = _report(topics=[_topic(questions=[{"question": "Who signs this off?"}])])
    assert _by_title(rs.build(r), "Open Questions")["items"] == ["Who signs this off?"]


def test_a_question_asked_twice_is_listed_once():
    r = _report(topics=[
        _topic(open_questions=["When does the slab pour?"]),
        _topic(open_questions=["When does the slab pour?"]),
    ])
    assert len(_by_title(rs.build(r), "Open Questions")["items"]) == 1


# ---------------------------------------------------------------------------
# The rest
# ---------------------------------------------------------------------------

def test_the_summary_is_one_narrative_not_a_list_of_fragments():
    r = _report(executive_summary=["First thing happened.", "Then another."])
    sec = _by_title(rs.build(r), "Summary")
    assert sec["kind"] == "narrative"
    assert isinstance(sec["body"], str)
    assert "First thing happened." in sec["body"] and "Then another." in sec["body"]


def test_decisions_from_every_topic_land_in_one_list():
    r = _report(topics=[
        _topic(key_decisions=["Keep the red light flashing"]),
        _topic(key_decisions=["Keep the red light flashing", "Build the Teams bridge"]),
    ])
    items = _by_title(rs.build(r), "Decisions")["items"]
    assert len(items) == 2


def test_the_recording_counts_are_not_a_section():
    """They were, and they restated `recording_session` -- which the viewer
    already renders as header facts -- so the same three numbers were on one
    screen twice and disagreed about zero. The report's owner then asked for
    the counts to go entirely: a day is described by what was decided and what
    is owed, not by how many files it took to record it."""
    r = _report(recording_session={"recordings": 3, "total_duration_display": "2m",
                                   "photos": 6, "total_words": 150537})
    titles = _titles(rs.build(r))
    assert "On Site" not in titles
    for s_ in rs.build(r):
        assert s_["kind"] != "kpi", "no section restates the session counts"

def test_an_empty_section_is_dropped_rather_than_shown_empty():
    """A report for a quiet day should not be five headings over nothing."""
    titles = _titles(rs.build(_report()))
    assert "Safety" not in titles
    assert "Actions" not in titles
    assert "Summary" in titles, "summary and the on-site numbers always exist"


def test_safety_survives_when_there_is_something_to_say():
    r = _report(safety_observations=[{"observation": "Loose cable on level 2"}])
    sec = _by_title(rs.build(r), "Safety")
    assert sec["kind"] == "entries"
    assert sec["items"][0]["title"] == "Loose cable on level 2"


def test_safety_says_what_to_do_about_it():
    """A list of observations is half a safety section. The prompt has always
    asked for `recommended_action`, every real entry carries one, and this
    section dropped it -- so a report said a digger was operating inside the
    exclusion zone and did not say to stop it."""
    r = _report(safety_observations=[{
        "observation": "Entry into the falsework exclusion zone",
        "risk_level": "high",
        "location": "Cook Brothers adjoining site",
        "recommended_action": "Start a joint coordination group",
        "who_raised": "Ben",
    }])
    item = _by_title(rs.build(r), "Safety")["items"][0]
    assert item["status"] == "high"
    assert "Start a joint coordination group" in item["note"]
    assert "Cook Brothers adjoining site" in item["note"]


def test_safety_and_quality_are_the_same_shape():
    """Asked for directly: "我不关心你是用列表还是表格，我希望和上面统一". Two
    sections that both describe an observation, its state and one line about it
    must not be rendered by two different mechanisms."""
    r = _report(
        safety_observations=[{"observation": "Loose cable", "risk_level": "low",
                              "recommended_action": "Tape it"}],
        quality_and_compliance=[{"item": "PS4 outstanding", "status": "concern",
                                 "details": "Chase the engineer"}],
    )
    built = rs.build(r)
    safety = _by_title(built, "Safety")
    quality = _by_title(built, "Issues & Quality")
    assert safety["kind"] == quality["kind"] == "entries"
    assert set(safety["items"][0]) == set(quality["items"][0]) == {"title", "status", "note"}


def test_section_order_is_fixed():
    """Actions before prose: a site manager opens this to find out what they owe
    somebody, and reads the narrative only if they have time."""
    r = _report(
        topics=[_topic(action_items=[{"action": "Do it"}], key_decisions=["Decided"],
                       open_questions=["Unknown?"])],
        quality_and_compliance=[{"item": "IT room", "status": "concern"}],
        safety_observations=[{"observation": "Cable"}],
    )
    assert _titles(rs.build(r)) == [
        "Summary", "Actions", "Open Questions",
        "Decisions", "Issues & Quality", "Safety",
    ]


# ---------------------------------------------------------------------------
# Placeholders that are not values
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("placeholder", ["?", "-", "—", "N/A", "n/a", "TBD", "none", "unknown"])
def test_a_placeholder_deadline_does_not_count_as_a_date(placeholder):
    """`lambda_meeting_minutes` defaulted a missing deadline to the string "?",
    which is truthy. Ranked as a date it would put every undated action from a
    meeting above every real one."""
    r = _report(topics=[_topic(action_items=[
        {"action": "Has no date really", "deadline": placeholder, "priority": "low"},
        {"action": "Genuinely due", "deadline": "2026-09-15", "priority": "low"},
    ])])
    rows = _by_title(rs.build(r), "Actions")["rows"]
    assert rows[0]["action"] == "Genuinely due"
    assert rows[1]["due"] == "", f"{placeholder!r} should not survive as a date"


def test_a_placeholder_owner_is_shown_as_no_owner():
    r = _report(topics=[_topic(action_items=[{"action": "Do it", "responsible": "?"}])])
    assert _by_title(rs.build(r), "Actions")["rows"][0]["owner"] == ""
