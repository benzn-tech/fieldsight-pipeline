"""Unit: where he said he was, as a state that persists between announcements.

The shape this exists for, read verbatim off prod (Neil / 2026-09-02) -- 27
seconds of speech across 13 minutes and 53 photos:

    17:02:58  "Photos of level two progress"
    17:05:23  "Following photos are level three progress"
    17:09:05  "The following is level four progress photos"
    17:11:33  "The following is level five progress photos"
    17:14:05  "The following, uh, level six progress photos"

Five announcements, each followed by silence. Topic binding cannot group these:
a photo taken while nobody is talking has no topic to belong to, and the whole
day collapsed into ONE topic called "Progress Photos -- Levels 2-6".

The load-bearing property is NOT that a marker groups photos. It is that a
CONVERSATION DOES NOT MOVE HIM: if he announces Room 101 and then talks to
someone about a level-three delay, he is still in Room 101. Only another
announcement moves him. Everything else here follows from that.
"""
import pytest

lm = pytest.importorskip("repositories.location_markers")

NEIL = [
    {"at": "17:02", "location": "level two", "quote": "Photos of level two progress"},
    {"at": "17:05", "location": "level three", "quote": "...level three progress"},
    {"at": "17:09", "location": "level four", "quote": "...level four progress photos"},
    {"at": "17:11", "location": "level five", "quote": "...level five progress photos"},
    {"at": "17:14", "location": "level six", "quote": "...level six progress photos"},
]


def test_a_marker_owns_the_time_until_the_next_one():
    assert lm.locate(NEIL, "17:03") == "level two"
    assert lm.locate(NEIL, "17:04") == "level two"
    assert lm.locate(NEIL, "17:06") == "level three"
    assert lm.locate(NEIL, "17:13") == "level five"


def test_the_boundary_topic_binding_gets_wrong_is_the_one_this_gets_right():
    """17:04 is one minute before "level three" and two after "level two".

    Photo binding's tolerance rule reaches FORWARD and picks the nearer window,
    so it files that photo under level three -- he was still on level two. That
    is a known, asserted limit of binding (see test_photo_binding.py). The
    location layer does not have it, because "the last thing SAID BEFORE it" is
    not a distance question, and this is precisely why markers are the real
    answer rather than a wider tolerance.
    """
    assert lm.locate(NEIL, "17:04") == "level two"


def test_a_conversation_does_not_move_him():
    """THE property. A topic is not a marker, so a discussion that starts while
    he is in a room cannot relocate him -- only another announcement can.

    Modelled here as what it is: the marker list simply does not contain the
    conversation, so every photo through it stays in the announced room. If
    topics ever get folded into this list, this test is what breaks.
    """
    markers = [{"at": "10:00", "location": "Room 101"},
               {"at": "10:20", "location": "Room 102"}]
    # He announces 101, talks to someone at 10:05 about something else entirely,
    # keeps photographing until 10:19.
    for t in ("10:01", "10:06", "10:12", "10:19"):
        assert lm.locate(markers, t) == "Room 101", t
    assert lm.locate(markers, "10:21") == "Room 102"


def test_before_the_first_announcement_the_location_is_unknown():
    """None is a real answer and must not become "the nearest marker". Reaching
    backwards is the unbounded-nearest rule that was deliberately removed from
    photo binding on 2026-07-24, rebuilt one layer up."""
    assert lm.locate(NEIL, "16:59") is None


def test_a_marker_does_not_own_the_rest_of_the_day():
    """He said "level six" at 17:14 and went home. A photo at 19:00 is not
    evidence about level six; an unknown location is honest and a wrong one
    looks like evidence."""
    assert lm.locate(NEIL, "17:40") == "level six"      # inside the carry
    assert lm.locate(NEIL, "17:50") is None             # past it
    assert lm.locate(NEIL, "19:00") is None


def test_an_unreadable_time_is_unknown_not_a_crash():
    """These come from a model and from filenames. Neither is guaranteed."""
    assert lm.locate(NEIL, None) is None
    assert lm.locate(NEIL, "half past five") is None
    assert lm.locate([{"at": "nonsense", "location": "x"}], "17:03") is None


def test_the_last_announcement_wins_when_two_share_a_minute():
    """Deterministic, because HH:MM collisions are real -- 2026-08-13 has three
    topics sharing one time_range. Later in the list wins, so a correction
    ("...sorry, 102") supersedes rather than being ignored."""
    markers = [{"at": "10:00", "location": "Room 101"},
               {"at": "10:00", "location": "Room 102"}]
    assert lm.locate(markers, "10:05") == "Room 102"


# --------------------------------------------------------------------------
# What the model is allowed to send
# --------------------------------------------------------------------------

ex = pytest.importorskip("lambda_extract_session")


def test_a_marker_needs_both_a_time_and_a_place():
    """Either alone is useless: no time cannot place a photo, no place has
    nothing to say. Dropped rather than repaired -- a guessed marker puts a room
    heading over photos that may have been taken elsewhere, which is the exact
    misattribution this feature exists to prevent, arriving through its own
    front door."""
    got = ex.clean_location_markers([
        {"at": "17:02", "location": "level two", "quote": "q"},
        {"at": "17:05"},                       # no place
        {"location": "level three"},           # no time
        {"at": "", "location": "level four"},  # empty is not a time
    ])
    assert [m["location"] for m in got] == ["level two"]


def test_prose_where_markers_go_is_the_same_as_no_markers():
    """Models answer the question instead of filling the shape often enough
    that this has to be a rule, not a hope. 'none' must not become a marker."""
    for junk in (None, [], "none", ["level two"], [None], [{"at": "x"}]):
        assert ex.clean_location_markers(junk) == [], junk


def test_a_paragraph_does_not_become_a_paragraph_sized_heading():
    got = ex.clean_location_markers([
        {"at": "17:02:58 and then some", "location": "L" * 400, "quote": "Q" * 900}])
    assert got[0]["at"] == "17:02"
    assert len(got[0]["location"]) == 120
    assert len(got[0]["quote"]) == 300
