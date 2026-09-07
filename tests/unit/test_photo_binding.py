"""
Tests for src/photo_binding.py.

Style mirrors tests/unit/test_lambda_item_writer.py: plain functions plus a
`_photo` fixture builder; the matcher is pure so no FakeConn/FakeS3 is needed
except for the S3 lister at the bottom of the file.

The current rule (2026-07-24 correction, user-approved -- supersedes the
2026-07-23 P2 "nearest-wins, never orphan" rule): a photo binds to a topic
only if it is inside the topic's time_range window, or overreaches either
edge by at most PHOTO_TOLERANCE_MIN (2) minutes. Beyond that it binds to
NOTHING -- there is no never-orphan fallback anymore. Among qualifying
topics, nearest wins; ties -> lowest topic index. A topic with no parseable
window never competes and always resolves to an empty list. Per-topic cap
PHOTOS_PER_TOPIC_CAP with a deterministic cascade to the next-nearest
QUALIFYING topic that still has headroom.
"""
import photo_binding as pb


def _photo(name, hhmm):
    return {"key": f"users/Ben_UCPK/pictures/2026-07-23/{name}",
            "filename": name, "hhmm": hhmm}


# ---------------------------------------------------------------------------
# photos_for_topics — the matcher table
# ---------------------------------------------------------------------------

def test_photo_inside_window_binds():
    topics = [{"time_range": "10:00 – 10:05"}]
    p = _photo("a.jpg", "10:02")
    assert pb.photos_for_topics([p], topics) == {0: [p]}


def test_photo_within_tolerance_binds():
    # The proven prod failure shape: photo 1-2 min outside a tight window.
    topics = [{"time_range": "12:13 – 12:14"}]
    p = _photo("a.jpg", "12:16")
    assert pb.photos_for_topics([p], topics) == {0: [p]}


def test_photo_at_tolerance_edge_binds():
    # Exactly PHOTO_TOLERANCE_MIN (2) minutes past the edge still qualifies.
    topics = [{"time_range": "10:00 – 10:05"}]
    p = _photo("a.jpg", "10:07")            # distance == 2
    assert pb.photos_for_topics([p], topics) == {0: [p]}


def test_photo_past_tolerance_edge_carries_forward_to_what_was_last_said():
    """Was `..._binds_to_nothing`, and the behaviour really did change.

    Three minutes past a topic that has already ENDED is the inspection shape:
    he said where he was, then went quiet and kept photographing. It no longer
    binds to nothing -- it binds to the thing he last described, because
    nothing else was said in between.

    The tolerance rule is untouched; this is the fallback that runs only when
    the tolerance rule finds no candidate at all.
    """
    topics = [{"time_range": "10:00 – 10:05"}]
    p = _photo("a.jpg", "10:08")            # distance == 3, past tolerance
    assert pb.photos_for_topics([p], topics) == {0: [p]}


def test_a_photo_taken_before_anyone_spoke_still_binds_to_nothing():
    """Carry-forward is DIRECTIONAL. A topic that had not started yet cannot
    claim a photo -- otherwise "the last thing said" would quietly become
    "the nearest thing said", which is the unbounded rule removed in
    2026-07-24 wearing a different name."""
    topics = [{"time_range": "10:00 – 10:05"}]
    p = _photo("a.jpg", "09:50")
    assert pb.photos_for_topics([p], topics) == {0: []}


def test_a_photo_past_the_carry_bound_still_binds_to_nothing():
    """The bound is the whole difference between this and the rule that was
    deliberately removed. An hour after the last word is not "still doing that
    thing"."""
    topics = [{"time_range": "10:00 – 10:05"}]
    far = _photo("a.jpg", "11:00")          # 55 min past the end, bound is 30
    assert pb.photos_for_topics([far], topics) == {0: []}


def test_a_later_topic_never_reaches_back_for_an_earlier_photo():
    """10:45 sits 6 minutes after topic 0 ended and ~87 minutes before topic 1
    begins. Topic 0 carries forward to it; topic 1 must not reach backwards,
    which is what "the last thing SAID BEFORE it" means.

    This is the real Ben_UCPK/2026-07-23 shape that started the whole binding
    story: single-instant windows and photos a few minutes later.
    """
    topics = [{"time_range": "10:39 – 10:39"}, {"time_range": "12:12 – 12:13"}]
    p = _photo("a.jpg", "10:45")
    assert pb.photos_for_topics([p], topics) == {0: [p], 1: []}


def test_far_photo_binds_to_nothing():
    # Was test_far_orphan_binds_to_nearest_topic (unbounded nearest-wins).
    # Now: both windows are far beyond the 2-min cap -> no binding at all,
    # not "bind to the less-far one".
    topics = [{"time_range": "08:00 – 08:10"}, {"time_range": "15:00 – 15:10"}]
    p = _photo("a.jpg", "13:00")            # 290 min from t0's end, 120 from t1's start
    assert pb.photos_for_topics([p], topics) == {0: [], 1: []}


def test_tie_breaks_to_lowest_index():
    # Equidistant (and inside both after overlap) -> lowest index.
    topics = [{"time_range": "09:00 – 10:00"}, {"time_range": "09:30 – 11:00"}]
    p = _photo("a.jpg", "09:45")
    assert pb.photos_for_topics([p], topics) == {0: [p], 1: []}


def test_no_parseable_windows_binds_to_nothing():
    # Was test_no_parseable_windows_binds_to_first_topic (never-orphan
    # fallback). Now: no topic has a window within 2 min of anything (there
    # is no window at all), so the photo binds to nothing -- not topic 0.
    topics = [{"time_range": None}, {"time_range": "not a range"}]
    p = _photo("a.jpg", "09:30")
    assert pb.photos_for_topics([p], topics) == {0: [], 1: []}


def test_unparseable_window_topic_never_competes():
    # Port of test_lambda_item_writer.py's unparseable-range case -- same
    # expectations hold: a topic with no parseable window never joins the
    # candidate set while any other topic has one.
    topics = [{"time_range": None}, {"time_range": "not a range"},
              {"time_range": "09:00 – 10:00"}]
    p = _photo("a.jpg", "09:30")
    result = pb.photos_for_topics([p], topics)
    assert result[0] == [] and result[1] == []
    assert result[2] == [p]


def test_no_topics_returns_empty():
    assert pb.photos_for_topics([_photo("a.jpg", "09:30")], []) == {}


def test_dash_variants_parse():
    # En dash (what the LLM writes), em dash and ASCII hyphen -- normalized
    # for robustness even though prod's dash was verified U+2013.
    for dash in ("–", "—", "-"):
        topics = [{"time_range": f"10:00 {dash} 10:05"}]
        p = _photo("a.jpg", "10:02")
        assert pb.photos_for_topics([p], topics) == {0: [p]}


def test_photo_without_hhmm_is_skipped():
    topics = [{"time_range": "10:00 – 10:05"}]
    assert pb.photos_for_topics([{"key": "k", "filename": "f", "hhmm": None}], topics) == {0: []}


def test_cap_overflow_cascades_to_next_nearest():
    # The two windows touch at 10:00 (09:00-10:00 / 10:00-11:00), so a photo
    # at exactly 10:00 is inside BOTH (distance 0 for each) and both qualify
    # -- unlike a photo merely near the boundary, which would only qualify
    # under the 2-min tolerance for one side. Tie-break sends the first 10
    # (input order) to topic 0; the cap sends the overflow to topic 1, which
    # still qualifies, rather than dropping them.
    cap = pb.PHOTOS_PER_TOPIC_CAP
    topics = [{"time_range": "09:00 – 10:00"}, {"time_range": "10:00 – 11:00"}]
    photos = [_photo(f"p{i:02d}.jpg", "10:00") for i in range(cap + 2)]
    result = pb.photos_for_topics(photos, topics)
    # Derived from the cap, not written as a literal: the cap is a display
    # limit now and is expected to move again. A test that hardcodes it has to
    # be edited every time and says nothing about the cascade it is here for.
    assert len(result[0]) == cap
    assert result[0] == photos[:cap]
    assert result[1] == photos[cap:]                         # overflow cascades, not dropped


def test_all_topics_at_cap_drops_deterministically():
    """Still deterministic, and still the first N in input order -- but a
    dropped photo is no longer a lost one: the day view lists every photo
    whether or not it binds, which is why the cap could be raised at all."""
    cap = pb.PHOTOS_PER_TOPIC_CAP
    topics = [{"time_range": "09:00 – 10:00"}]
    photos = [_photo(f"p{i:02d}.jpg", "09:%02d" % (i % 60)) for i in range(cap + 2)]
    result = pb.photos_for_topics(photos, topics)
    assert result[0] == photos[:cap]                         # 2 dropped, logged, no crash


def test_result_carries_a_key_for_every_topic_index():
    # The all-indices contract callers (item-writer, ingest) rely on.
    topics = [{"time_range": "09:00 – 10:00"}, {"time_range": None},
              {"time_range": "11:00 – 11:30"}]
    assert set(pb.photos_for_topics([], topics)) == {0, 1, 2}


def test_parse_time_range_rejects_junk():
    assert pb.parse_time_range(None) is None
    assert pb.parse_time_range("") is None
    assert pb.parse_time_range("not a range") is None
    assert pb.parse_time_range("10:00 – 10:05") == (600, 605)


# ---------------------------------------------------------------------------
# list_pictures — paginated S3 lister, BUG-01-safe timestamps
# ---------------------------------------------------------------------------

class _FakePaginator:
    def __init__(self, keys):
        self.keys = keys

    def paginate(self, Bucket, Prefix):
        yield {"Contents": [{"Key": k} for k in self.keys if k.startswith(Prefix)]}


class _FakeS3:
    def __init__(self, keys):
        self.keys = keys

    def get_paginator(self, op):
        assert op == "list_objects_v2"
        return _FakePaginator(self.keys)


def test_list_pictures_derives_hhmm_and_skips_untimed_names():
    prefix = "users/Ben_UCPK/pictures/2026-07-23/"
    timed = prefix + "Benl1_2026-07-23_10-40-00.jpg"
    untimed = prefix + "screenshot.jpg"
    fake = _FakeS3([timed, untimed, "users/Other/pictures/2026-07-23/x.jpg"])

    photos = pb.list_pictures(fake, "bucket", prefix)

    assert photos == [{"key": timed, "filename": "Benl1_2026-07-23_10-40-00.jpg",
                       "hhmm": "10:40"}]


def test_list_pictures_empty_prefix_is_noop():
    assert pb.list_pictures(_FakeS3([]), "bucket", "users/Nobody/pictures/2026-07-23/") == []


# --------------------------------------------------------------------------
# The inspection shape, taken from real days
# --------------------------------------------------------------------------

def test_the_inspection_shape_binds_every_photo():
    """Neil / 2026-08-18, read off prod: ONE utterance at 14:32, then twelve
    silent minutes of photography. 25 photos, of which 6 bound under the
    tolerance rule alone -- the topic's window is a single instant, so anything
    after 14:34 fell outside it.

    This is not an edge case, it is the whole inspection workflow: say where
    you are, then photograph in silence. 37% of topic windows in this repo are
    a single instant and 76% are narrower than PHOTO_TOLERANCE_MIN.
    """
    topics = [{"time_range": "14:32 – 14:32"}]
    photos = [_photo("p%02d.jpg" % i, "14:%02d" % (32 + i)) for i in range(13)]
    result = pb.photos_for_topics(photos, topics)
    assert result[0] == photos, "a silent walkthrough must not lose its photos"


def test_a_second_announcement_takes_over_from_the_first():
    """Each announcement owns the time after it -- what makes per-location
    grouping possible at all.

    KNOWN LIMIT, asserted rather than hidden. The tolerance rule runs FIRST and
    it reaches FORWARD: a photo one minute before the next announcement is
    nearer to that one than to the announcement two minutes behind it, so 17:04
    lands on "level three" even though he was still on level two.
    Carry-forward never sees it, because something already qualified.

    That is wrong for an inspection and right for a meeting -- someone
    photographs a thing and then talks about it -- and nothing in the data
    tells the two apart yet. Widening this into the tolerance rule would change
    every meeting-shaped day in order to fix a walkthrough, so it is left alone
    and written down instead. The location markers in
    AI/spec-location-is-a-state-not-an-event-2026-09-07.md are what resolve it:
    a marker says where he WAS, independent of which topic is nearest.

    The blast radius is one tolerance-width before each announcement, and no
    photo is lost either way -- the day view lists them all.
    """
    topics = [{"time_range": "17:02 – 17:02"}, {"time_range": "17:05 – 17:05"}]
    clearly_two = _photo("early.jpg", "17:03")
    boundary = _photo("boundary.jpg", "17:04")
    clearly_three = _photo("late.jpg", "17:06")
    result = pb.photos_for_topics([clearly_two, boundary, clearly_three], topics)
    assert result[0] == [clearly_two]
    assert result[1] == [boundary, clearly_three], (
        "the boundary photo goes to the LATER topic -- see the docstring")


def test_the_photos_of_a_whole_real_day_all_land_somewhere():
    """2026-09-02 end to end: 53 photos across 13 minutes, five announcements.
    Measured before this change: 10 of 53 reachable. The count is the point --
    every photo lands, and none lands twice."""
    topics = [{"time_range": t} for t in
              ("17:02 – 17:02", "17:05 – 17:05", "17:09 – 17:09",
               "17:11 – 17:11", "17:14 – 17:14")]
    photos = []
    for minute in range(3, 17):
        for n in range(4):
            photos.append(_photo("p%02d_%d.jpg" % (minute, n), "17:%02d" % minute))
    result = pb.photos_for_topics(photos, topics)
    bound = [p for v in result.values() for p in v]
    assert len(bound) == len(photos), "every photo of the day binds"
    assert len({id(p) for p in bound}) == len(photos), "and none binds twice"
