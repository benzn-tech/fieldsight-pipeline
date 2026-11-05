"""Unit: the assembled report gets a Locations section (Track L, Task 5).

The day is a walk: he says where he is, talks and photographs there, moves on.
`report_sections.build` reads the day's markers from `report["location_markers"]`
and, when there are any, adds a *Locations* section between Safety and Photos --
one entry per place, in the order he visited them, each listing the topics
raised there and the photographs taken there.

What these tests pin:

  * Without markers the section list is EXACTLY what it was before this change
    (a literal title list recorded from the pre-change code, not derived).
  * With markers, Locations sits between Safety and Photos.
  * A group with no place is headed by WHY: before the first announcement,
    past the carry window ("Location not stated" -- never "after <place>",
    because two expired windows can merge into one group), or no time at all.
  * The Photos section stays complete and lists each photograph exactly once.
    A Locations entry's `photos` are references in the same `{name, key}` shape
    built by `_photos` itself; neither renderer (the Word document's `entries`
    branch, the web viewer's) draws them today, so taking them OUT of Photos
    would make a marker day's photographs vanish from the report.
  * The org-api clip gate is pinned in test_org_api_locations_section.py.
"""
import json

import pytest

import report_sections as rs

DATE, USER = "2026-09-02", "Neil_Blunden"
PREFIX = "users/%s/pictures/%s/" % (USER, DATE)


def _name(hh, mm):
    return "neil_blunden_2026-09-02_%02d-%02d-00.jpg" % (hh, mm)


def _topic(title, time_range, photos=(), **kw):
    t = {"topic_title": title, "time_range": time_range, "category": "quality",
         "summary": "", "key_decisions": [], "action_items": [],
         "related_photos": list(photos)}
    t.update(kw)
    return t


def _fixture(**kw):
    """A day with something in most sections, including Safety and Photos."""
    base = {
        "report_date": DATE,
        "user_name": USER,
        "executive_summary": ["Level 2 framing checked", "Level 3 bracing short"],
        "quality_and_compliance": [{"item": "Bracing", "status": "fail",
                                    "details": "Two braces missing"}],
        "safety_observations": [{"observation": "Open edge on level 3",
                                 "risk_level": "high"}],
        "critical_dates_and_deadlines": [{"date_mentioned": "Friday",
                                          "context": "Pour"}],
        "topics": [
            _topic("Framing on level two", "17:03 - 17:04", [_name(17, 3)],
                   key_decisions=["Accept framing"],
                   action_items=[{"action": "Send framing photos",
                                  "responsible": "Neil", "priority": "high"}]),
            _topic("Bracing on level three", "17:06 - 17:07",
                   [_name(17, 6), _name(17, 7)],
                   open_questions=["Who supplies braces?"]),
        ],
    }
    base.update(kw)
    return base


MARKERS = [
    {"at": "17:02", "location": "level two"},
    {"at": "17:05", "location": "level three"},
]

#: Recorded from `build(_fixture())` BEFORE this change (HEAD 61ae197f).
BEFORE = ["Summary", "Actions", "Open Questions", "Decisions", "Key Dates",
          "Issues & Quality", "Safety", "Photos"]


def _titles(sections):
    return [s["title"] for s in sections]


def _section(sections, title):
    found = [s for s in sections if s["title"] == title]
    assert len(found) == 1, _titles(sections)
    return found[0]


# --------------------------------------------------------------------------
# No markers: unchanged

def test_without_markers_the_section_list_is_unchanged():
    assert _titles(rs.build(_fixture())) == BEFORE


@pytest.mark.parametrize("markers", [None, [], [{"location": "no time"}],
                                     [{"at": "17:00"}], "level two"])
def test_no_usable_markers_means_no_section_and_byte_identical(markers):
    plain = rs.build(_fixture())
    got = rs.build(_fixture(location_markers=markers))
    assert json.dumps(got, sort_keys=True) == json.dumps(plain, sort_keys=True)


# --------------------------------------------------------------------------
# Markers: the section

def test_with_markers_locations_sits_between_safety_and_photos():
    titles = _titles(rs.build(_fixture(location_markers=MARKERS)))
    assert titles == BEFORE[:-1] + ["Locations", "Photos"]


def test_locations_lists_places_in_visit_order_with_topics_and_photos():
    sec = _section(rs.build(_fixture(location_markers=MARKERS)), "Locations")
    assert sec["kind"] == "entries"
    assert sec["items"] == [
        {"title": "Level 2", "status": "", "note": "Framing on level two",
         "topics": ["Framing on level two"],
         "photos": [{"name": _name(17, 3), "key": PREFIX + _name(17, 3)}]},
        {"title": "Level 3", "status": "", "note": "Bracing on level three",
         "topics": ["Bracing on level three"],
         "photos": [{"name": _name(17, 6), "key": PREFIX + _name(17, 6)},
                    {"name": _name(17, 7), "key": PREFIX + _name(17, 7)}]},
    ]


def test_build_does_not_mutate_the_report():
    report = _fixture(location_markers=MARKERS)
    before = json.dumps(report, sort_keys=True)
    rs.build(report)
    assert json.dumps(report, sort_keys=True) == before


def test_a_return_to_a_place_is_a_new_entry():
    report = _fixture(topics=[
        _topic("A", "17:03 - 17:03"), _topic("B", "17:06 - 17:06"),
        _topic("C", "17:09 - 17:09")])
    markers = MARKERS + [{"at": "17:08", "location": "Level 2"}]
    sec = _section(rs.build(dict(report, location_markers=markers)), "Locations")
    assert [(e["title"], e["topics"]) for e in sec["items"]] == [
        ("Level 2", ["A"]), ("Level 3", ["B"]), ("Level 2", ["C"])]


def test_topics_with_ids_are_matched_by_id_not_position():
    report = _fixture(topics=[
        _topic("Later", "17:06 - 17:06", id="t-9"),
        _topic("Earlier", "17:03 - 17:03", id="t-1")])
    sec = _section(rs.build(dict(report, location_markers=MARKERS)), "Locations")
    assert [(e["title"], e["topics"]) for e in sec["items"]] == [
        ("Level 2", ["Earlier"]), ("Level 3", ["Later"])]


# --------------------------------------------------------------------------
# Null groups: headed by reason

def test_null_group_headings_by_reason():
    markers = [{"at": "10:00", "location": "Level 1"}]
    report = _fixture(topics=[
        _topic("Before anything", "09:30 - 09:31"),
        _topic("On level one", "10:01 - 10:02"),
        _topic("Long after", "13:00 - 13:01"),
        _topic("Undated", "sometime"),
    ])
    sec = _section(rs.build(dict(report, location_markers=markers)), "Locations")
    assert [(e["title"], e["topics"]) for e in sec["items"]] == [
        ("Before any location was announced", ["Before anything"]),
        ("Level 1", ["On level one"]),
        ("Location not stated", ["Long after"]),
        ("Time not recorded", ["Undated"]),
    ]
    assert not any(e["title"].lower().startswith("after") for e in sec["items"])


def test_two_expired_windows_merge_under_one_neutral_heading():
    """Level 1 expires, then Level 2 is announced and expires: the topics after
    each window are two items with no place between them -> one group. A
    heading naming either place would be wrong for the other."""
    markers = [{"at": "10:00", "location": "Level 1"},
               {"at": "13:00", "location": "Level 2"}]
    report = _fixture(topics=[
        _topic("On one", "10:01 - 10:01"),
        _topic("Gap after one", "12:00 - 12:00"),
        _topic("On two", "13:01 - 13:01"),
        _topic("Gap after two", "16:00 - 16:00"),
    ])
    sec = _section(rs.build(dict(report, location_markers=markers)), "Locations")
    titles = [e["title"] for e in sec["items"]]
    assert titles == ["Level 1", "Location not stated", "Level 2",
                      "Location not stated"]


# --------------------------------------------------------------------------
# Every photograph once

def test_every_photo_appears_once_in_photos_and_at_most_once_under_locations():
    report = _fixture(location_markers=MARKERS, topics=[
        _topic("Framing", "17:03 - 17:04", [_name(17, 3), _name(17, 6)]),
        # The same photograph bound to a second topic too.
        _topic("Bracing", "17:06 - 17:07", [_name(17, 6), _name(17, 7)]),
        _topic("Untimed", "", [_name(18, 0)]),
    ])
    sections = rs.build(report)
    photos = _section(sections, "Photos")["items"]
    names = [p["name"] for p in photos]
    assert sorted(names) == sorted({_name(17, 3), _name(17, 6), _name(17, 7),
                                    _name(18, 0)})
    assert len(names) == len(set(names)), "Photos lists each photograph once"

    located = [p for e in _section(sections, "Locations")["items"]
               for p in e["photos"]]
    located_names = [p["name"] for p in located]
    assert len(located_names) == len(set(located_names)), \
        "a photograph sits under one place"
    # Same objects' shape, built by `_photos`, so a renderer can fetch them.
    assert all(p in photos for p in located)
    # The shared photo follows its EARLIEST topic.
    first = _section(sections, "Locations")["items"][0]
    assert _name(17, 6) in [p["name"] for p in first["photos"]]


def test_photos_section_is_the_same_with_and_without_markers():
    """Neither renderer draws an entry's `photos` today (the Word document's
    `entries` branch and the web viewer read title/status/note only), so the
    Photos section must stay complete: removing the located photographs from
    it would make them vanish from the report."""
    plain = _section(rs.build(_fixture()), "Photos")
    located = _section(rs.build(_fixture(location_markers=MARKERS)), "Photos")
    assert located == plain


def test_a_grouping_failure_drops_only_the_locations_section(monkeypatch):
    import location_grouping

    def _boom(*a, **k):
        raise RuntimeError("grouping blew up")

    monkeypatch.setattr(location_grouping, "group_day", _boom)
    assert _titles(rs.build(_fixture(location_markers=MARKERS))) == BEFORE
