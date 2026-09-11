"""Unit: a model's output cannot cost a night's reports.

`report_sections.build` runs INSIDE the per-user loop in
`lambda_report_generator`, BEFORE the S3 write, and that loop has no per-user
try/except. So an exception here does not lose one section — it loses the
report, the Word document and the item rows for that user AND for every user
sorted after them, and fails the whole nightly invocation.

Every shape below was found by driving the real module. Before these, a model
returning `"key_decisions": [["Use Teams", "for now"]]` for the third of six
users was enough.

And a bare string in `safety_observations` was iterated character by character,
producing a Safety section reading ['L', 'o', 'o', 's', 'e'].
"""
import pytest

import report_sections as rs   # plain import: if this module stops shipping,
                               # these tests must FAIL, not skip


def _report(**kw):
    base = {"report_date": "2026-09-09", "executive_summary": "A day.",
            "topics": [], "recording_session": {"recordings": 1}}
    base.update(kw)
    return base


def _titles(sections):
    return [s["title"] for s in sections]


def _by_title(sections, title):
    for s in sections:
        if s["title"] == title:
            return s
    raise AssertionError("no section " + title + " in " + str(_titles(sections)))


# ---------------------------------------------------------------------------
# Shapes that used to raise
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("topics", [
    [None],
    ["a topic, apparently"],
    [42],
    [{"key_decisions": [["Use Teams", "for now"]]}],
    [{"key_decisions": [7]}],
    [{"open_questions": [["nested"]]}],
    [{"questions": "not a list"}],
    [{"action_items": "not a list"}],
    [{"action_items": [None, "a string", 3]}],
    [{"related_photos": "one.jpg"}],
])
def test_a_strange_topic_shape_does_not_lose_the_night(topics):
    sections = rs.build(_report(topics=topics))
    assert isinstance(sections, list)
    assert _by_title(sections, "Summary")


@pytest.mark.parametrize("report", [None, "not a report", 7, []])
def test_a_report_that_is_not_a_report_still_returns_sections(report):
    assert isinstance(rs.build(report), list)


def test_a_bare_string_of_safety_is_one_observation_not_nine_letters():
    sec = _by_title(rs.build(_report(safety_observations="Loose cable")), "Safety")
    assert [r["title"] for r in sec["items"]] == ["Loose cable"]


# ---------------------------------------------------------------------------
# The ranking, on the shapes the producer actually writes
# ---------------------------------------------------------------------------

def _rows(items, report_date="2026-09-09"):
    r = _report(report_date=report_date, topics=[{"action_items": items}])
    return _by_title(rs.build(r), "Actions")["items"]


def test_tomorrow_outranks_a_date_in_october():
    """The failure this whole thing turns on: as strings, "T" > "2"."""
    rows = _rows([
        {"action": "October thing", "deadline": "2026-10-01"},
        {"action": "Tomorrow thing", "deadline": "Tomorrow 08:00"},
    ])
    assert [x["title"] for x in rows] == ["Tomorrow thing", "October thing"]


def test_end_of_day_outranks_everything_dated_later():
    rows = _rows([
        {"action": "Next week", "deadline": "2026-09-16"},
        {"action": "Right now", "deadline": "EOD"},
    ])
    assert rows[0]["title"] == "Right now"


def test_a_deadline_nobody_can_read_ranks_as_undated():
    """"when the crane arrives" is not a date. Ranked as its own text it would
    sort by "w" and sit above or below real dates by alphabet."""
    rows = _rows([
        {"action": "Vague", "deadline": "when the crane arrives", "priority": "low"},
        {"action": "Real", "deadline": "2026-12-25", "priority": "low"},
    ])
    assert rows[0]["title"] == "Real"


def test_an_unreadable_deadline_is_still_shown():
    """Ranked as undated, displayed as written -- the model's words are what the
    site manager needs to see, even when we cannot sort on them."""
    rows = _rows([{"action": "Vague", "deadline": "when the crane arrives"}])
    assert "when the crane arrives" in rows[0]["note"]


def test_an_overdue_action_comes_first():
    rows = _rows([
        {"action": "Upcoming", "deadline": "2026-09-20"},
        {"action": "Overdue", "deadline": "2026-09-01"},
    ], report_date="2026-09-09")
    assert rows[0]["title"] == "Overdue"


def test_with_no_report_date_nothing_pretends_to_be_dated():
    """Relative deadlines are meaningless without the day they were said on."""
    r = {"topics": [{"action_items": [
        {"action": "A", "deadline": "Tomorrow"},
        {"action": "B", "deadline": "EOD", "priority": "high"},
    ]}]}
    rows = _by_title(rs.build(r), "Actions")["items"]
    assert rows[0]["title"] == "B", "falls back to priority, not to alphabet"


# ---------------------------------------------------------------------------
# Merging
# ---------------------------------------------------------------------------

def test_two_owners_for_one_action_are_both_named():
    """Keeping only the first silently reassigns somebody else's job."""
    r = _report(topics=[
        {"action_items": [{"action": "Order the brackets", "responsible": "Ben"}]},
        {"action_items": [{"action": "Order the brackets", "responsible": "James"}]},
    ])
    row = _by_title(rs.build(r), "Actions")["items"][0]
    assert "Ben" in row["note"] and "James" in row["note"]


def test_an_action_whose_text_is_a_placeholder_is_not_an_action():
    rows = _rows([{"action": "N/A"}, {"action": "TBD"}, {"action": "Real work"}])
    assert [x["title"] for x in rows] == ["Real work"]


# ---------------------------------------------------------------------------
# Sections that appear and disappear honestly
# ---------------------------------------------------------------------------

def test_a_meeting_gets_no_on_site_panel_of_zeros():
    """A meeting's compat report has no recording_session. Rendering 0 recordings
    and 0 photos states something false about a real day."""
    r = {"executive_summary": "A meeting.", "topics": []}
    assert "On Site" not in _titles(rs.build(r))


def test_the_dates_block_reads_the_keys_the_model_actually_writes():
    """THE SHAPE THE PRODUCER WRITES, not one this test invented.

    The daily prompt asks for `date_mentioned / context / who_mentioned /
    urgency / type`. This section read `item|description|event|detail` and
    `date|deadline` -- none of them -- so every real report produced an empty
    Key Dates and `build` dropped the section entirely. Measured on a TEST
    report from the deployed code: 17 entries in the field, zero readable, no
    section.

    The old test fed `{"item": ..., "date": ...}` and passed. A test that
    supplies its own input can confirm any reading of it; the only thing that
    settles which keys are real is the prompt that asks for them.
    """
    r = _report(critical_dates_and_deadlines=[
        {"date_mentioned": "28th", "context": "Backfill Zone 1",
         "who_mentioned": "Ben", "urgency": "high", "type": "deadline"},
    ])
    sec = _by_title(rs.build(r), "Key Dates")
    assert sec["kind"] == "table"
    assert sec["rows"][0] == {"when": "28th", "what": "Backfill Zone 1", "who": "Ben"}


def test_the_older_key_names_still_read():
    """The meeting path and pre-existing reports are free to use them, and a
    reader is not helped by this being strict."""
    r = _report(critical_dates_and_deadlines=[
        {"item": "Crane off-hire", "date": "2026-09-20"}, "Slab pour Friday",
    ])
    rows = _by_title(rs.build(r), "Key Dates")["rows"]
    assert {"when": "2026-09-20", "what": "Crane off-hire", "who": ""} in rows
    assert {"when": "", "what": "Slab pour Friday", "who": ""} in rows


def test_key_dates_are_in_date_order():
    """A list of dates that is not in date order is a list, not a schedule.
    `date_mentioned` is free text, so the ones that cannot be read keep their
    stated order at the end rather than sorting by first character."""
    r = _report(report_date="2026-09-09", critical_dates_and_deadlines=[
        {"date_mentioned": "next week", "context": "Later thing"},
        {"date_mentioned": "when the crane arrives", "context": "Unreadable"},
        {"date_mentioned": "tomorrow", "context": "Sooner thing"},
    ])
    rows = _by_title(rs.build(r), "Key Dates")["rows"]
    assert [x["what"] for x in rows] == ["Sooner thing", "Later thing", "Unreadable"]


# ---------------------------------------------------------------------------
# Photos: a filename is not something you can fetch
# ---------------------------------------------------------------------------

def test_a_photo_carries_a_key_not_only_a_name():
    """`related_photos` holds BARE FILENAMES -- both writers store
    `s3_key.rsplit("/", 1)[-1]`. A filename cannot be presigned and cannot be
    read out of S3, so the viewer could only print the name of a photograph it
    was standing next to, and the Word document wrote a comma-separated run of
    them. Asked why photos are not in the report, that was the whole answer."""
    r = _report(user_name="Ben_UCPK2", report_date="2026-09-03",
                topics=[{"topic_title": "T", "related_photos": ["ben_20260903_101500.jpg"]}])
    items = _by_title(rs.build(r), "Photos")["items"]
    assert items == [{"name": "ben_20260903_101500.jpg",
                      "key": "users/Ben_UCPK2/pictures/2026-09-03/ben_20260903_101500.jpg"}]


def test_a_photo_that_already_has_a_path_is_not_prefixed_twice():
    """A future writer storing full keys must not produce
    users/X/pictures/D/users/X/pictures/D/f.jpg."""
    key = "users/Ben_UCPK2/pictures/2026-09-03/a.jpg"
    r = _report(user_name="Ben_UCPK2", report_date="2026-09-03",
                topics=[{"topic_title": "T", "related_photos": [key]}])
    assert _by_title(rs.build(r), "Photos")["items"][0]["key"] == key


def test_the_same_photo_twice_is_one_photo():
    """Two topics inside one minute bind the same shutter."""
    r = _report(user_name="Ben_UCPK2", report_date="2026-09-03", topics=[
        {"topic_title": "T", "related_photos": ["a.jpg"]},
        {"topic_title": "U", "related_photos": ["a.jpg", "b.jpg"]},
    ])
    assert [i["name"] for i in _by_title(rs.build(r), "Photos")["items"]] == ["a.jpg", "b.jpg"]


def test_a_photo_with_no_user_or_date_keeps_the_name_and_an_empty_key():
    """Absent is better than a key that resolves to somebody else's folder."""
    r = _report(user_name="", report_date="", topics=[{"topic_title": "T", "related_photos": ["a.jpg"]}])
    r.pop("date", None)
    items = _by_title(rs.build(r), "Photos")["items"]
    assert items[0]["name"] == "a.jpg" and items[0]["key"] == ""


# ---------------------------------------------------------------------------
# Weather, and how much it could have got in the way
# ---------------------------------------------------------------------------

def _weather(**kw):
    base = {"condition_label": "Slight rain", "temp_min_c": 11.2,
            "temp_max_c": 13.5, "precip_mm": 5.9, "windspeed_kmh": 22.2}
    base.update(kw)
    return _report(weather=base)


def test_weather_reads_as_the_lines_a_person_asked_for():
    items = _by_title(rs.build(_weather()), "Weather")["items"]
    assert [i["title"] for i in items] == [
        "Slight rain",
        "Temperature: 11.2°C – 13.5°C",
        "Rainfall: 5.9mm",
        "Maximum wind speed: 22.2 km/h",
    ]


def test_the_impact_level_names_what_drove_it():
    """A colour with no reason beside it is a mood ring: the reader cannot tell
    whether to move the pour or the crew."""
    head = _by_title(rs.build(_weather()), "Weather")["items"][0]
    assert head["status"] == "moderate"
    assert "rain" in head["note"]


@pytest.mark.parametrize("mm,level", [
    (0.0, ""), (0.4, "low"), (6.0, "moderate"), (18.0, "high"), (44.0, "severe"),
])
def test_rain_drives_the_level(mm, level):
    head = _by_title(rs.build(_weather(precip_mm=mm, windspeed_kmh=5,
                                       temp_min_c=14, temp_max_c=20)),
                     "Weather")["items"][0]
    assert head["status"] == level, mm


@pytest.mark.parametrize("kmh,level", [
    (10.0, ""), (28.0, "low"), (40.0, "moderate"), (55.0, "high"), (80.0, "severe"),
])
def test_wind_drives_the_level(kmh, level):
    head = _by_title(rs.build(_weather(precip_mm=0, windspeed_kmh=kmh,
                                       temp_min_c=14, temp_max_c=20)),
                     "Weather")["items"][0]
    assert head["status"] == level, kmh


def test_cold_and_heat_are_both_an_impact():
    cold = _by_title(rs.build(_weather(precip_mm=0, windspeed_kmh=5,
                                       temp_min_c=3, temp_max_c=9)),
                     "Weather")["items"][0]
    assert cold["status"] == "moderate" and "concrete" in cold["note"]
    hot = _by_title(rs.build(_weather(precip_mm=0, windspeed_kmh=5,
                                      temp_min_c=22, temp_max_c=36)),
                    "Weather")["items"][0]
    assert hot["status"] == "high" and "crew" in hot["note"]


def test_the_worst_factor_sets_the_level_and_every_reason_is_named():
    head = _by_title(rs.build(_weather(precip_mm=40, windspeed_kmh=40,
                                       temp_min_c=2, temp_max_c=9)),
                     "Weather")["items"][0]
    assert head["status"] == "severe"
    for word in ("rain", "wind", "concrete"):
        assert word in head["note"], word


def test_a_missing_number_does_not_invent_a_level():
    """Absent stays absent. A level derived from a number nobody measured is a
    claim about a day this system did not observe."""
    head = _by_title(rs.build(_weather(precip_mm=None, windspeed_kmh=None,
                                       temp_min_c=None, temp_max_c=None)),
                     "Weather")["items"][0]
    assert head["status"] == ""


def test_a_whole_number_is_not_rendered_with_a_trailing_zero():
    items = _by_title(rs.build(_weather(precip_mm=12.0)), "Weather")["items"]
    assert "Rainfall: 12mm" in [i["title"] for i in items]


def test_no_weather_means_no_section():
    """Null on every production report so far, and not because the fetch
    failed: the generator reads the site coordinate from user_mapping.json,
    where the sites have null lat/lng, while the coordinates people type into
    the web UI live in Aurora. An empty "Weather" heading would read as a claim
    that the day was unremarkable."""
    for value in (None, {}, "sunny", 7):
        assert "Weather" not in _titles(rs.build(_report(weather=value))), value


# ---------------------------------------------------------------------------
# What was done, apart from what was said
# ---------------------------------------------------------------------------

def test_work_completed_is_its_own_section():
    """"这样把完成的内容分离". Until now the only account of a day's work was the
    executive summary, which mixes what happened with what was agreed and what
    is still owed."""
    r = _report(work_completed=["Continued groundworks despite slight rain",
                                "Progressed with foundation preparations"])
    sec = _by_title(rs.build(r), "Work Completed")
    assert sec["kind"] == "list"
    assert sec["items"] == ["Continued groundworks despite slight rain",
                            "Progressed with foundation preparations"]


def test_work_completed_is_absent_on_a_day_of_meetings():
    for value in (None, [], ["", "  ", "N/A"]):
        assert "Work Completed" not in _titles(rs.build(_report(work_completed=value)))


def test_the_prompt_asks_for_work_not_talk():
    """The section is only as good as the field, and the field is only as good
    as the instruction. A topic's summary describes a CONVERSATION, and a
    conversation about backfilling is not backfilling."""
    import lambda_report_generator as rg
    schema = rg.DAILY_REPORT_SCHEMA
    assert '"work_completed"' in schema
    assert "physically DONE" in schema
    assert "only discussed" in schema


def test_the_report_carries_every_field_it_asks_for():
    """THE ALLOWLIST AND THE SCHEMA MUST NOT DRIFT.

    `lambda_report_generator` builds the report dict from a fixed list of
    `claude_output.get(...)` calls. Every key the schema asks the model for has
    to appear there too, or the field is requested, produced, paid for and
    thrown away -- silently, because nothing downstream knows to miss it.

    That is exactly what happened to `work_completed`: the prompt asked for it,
    the debug record shows the model returned it, and the report did not carry
    it, for one missing line. org-api has the same shape of serializer and lost
    `mention_count` the same way.
    """
    import re
    import lambda_report_generator as rg
    src = open(rg.__file__, encoding="utf-8").read()
    copied = set(re.findall(r"claude_output\.get\('([a-z_]+)'", src))
    asked = set(re.findall(r'"([a-z_]+)":', rg.DAILY_REPORT_SCHEMA))
    # Only the TOP-LEVEL keys of the schema; nested item fields travel inside
    # their parent and are not copied one by one.
    top = {"executive_summary", "work_completed", "quality_and_compliance",
           "safety_observations", "critical_dates_and_deadlines", "topics"}
    assert top <= asked, "the schema stopped asking for %s" % (top - asked)
    missing = top - copied
    assert not missing, "asked for but never copied into the report: %s" % sorted(missing)
