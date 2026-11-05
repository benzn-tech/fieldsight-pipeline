"""Track L Task 3: a day's topics and photos, grouped under the places he said,
in the order he walked them. Every example is written for the test."""
from location_grouping import group_day, place_of


def _photo(hhmmss):
    return "insp_2026-09-02_%s.jpg" % hhmmss.replace(":", "-")


MARKERS = [
    {"at": "09:00", "location": "Level one", "quote": "ok level one now"},
    {"at": "09:30", "location": "Level 2"},
    {"at": "10:00", "location": "roof"},
]

P = {k: _photo(v) for k, v in {
    "p1": "08:45:00", "p2": "09:10:00", "p3": "09:27:00", "p4": "09:35:00",
    "p5": "09:50:00", "p6": "10:10:00", "p7": "09:52:00", "p8": "10:15:00"}.items()}

TOPICS = [
    {"id": "t0", "time_range": "08:40–08:50", "related_photos": [P["p1"]]},       # before any marker
    {"id": "t1", "time_range": "09:05–09:20", "related_photos": [P["p2"], P["p3"]]},
    {"id": "t2", "time_range": "09:25-09:40",                                     # spans L1 -> L2
     "related_photos": [P["p3"], P["p4"]]},                                       # p3 double-bound
    {"id": "t3", "time_range": "09:45–09:55", "related_photos": [P["p5"]]},
    {"id": "t4", "time_range": "10:05–10:20", "related_photos": [P["p6"]]},
]
PHOTOS = list(P.values())          # p7, p8 are unbound


def _all_photos(groups):
    return [n for g in groups for n in g["photos"] + g["unbound_photos"]]


def test_hand_built_day():
    groups = group_day(MARKERS, TOPICS, PHOTOS)
    assert [g["location"] for g in groups] == [None, "Level 1", "Level 2", "roof"]
    assert [g["order"] for g in groups] == [0, 1, 2, 3]
    assert [g["topic_ids"] for g in groups] == [["t0"], ["t1", "t2"], ["t3"], ["t4"]]
    # the spanning topic stays where it started; p4 (taken on Level 2) follows it
    assert groups[1]["photos"] == [P["p2"], P["p3"], P["p4"]]
    assert groups[0]["photos"] == [P["p1"]]
    assert groups[2]["photos"] == [P["p5"]] and groups[2]["unbound_photos"] == [P["p7"]]
    assert groups[3]["photos"] == [P["p6"]] and groups[3]["unbound_photos"] == [P["p8"]]
    assert groups[0]["key"] is None and groups[0]["kind"] is None
    assert [g["reason"] for g in groups] == ["before_first", None, None, None]
    assert groups[1]["key"] == "level:level 1" and groups[1]["kind"] == "level"
    assert groups[3]["kind"] == "other"


def test_every_photo_exactly_once():
    got = _all_photos(group_day(MARKERS, TOPICS, PHOTOS))
    assert sorted(got) == sorted(PHOTOS) and len(got) == len(set(got))


def test_double_bound_photo_follows_earlier_topic_even_if_listed_later():
    topics = [TOPICS[2], TOPICS[1]]          # the later topic first in the list
    groups = group_day(MARKERS, topics, [P["p3"]])
    assert len(groups) == 1 and groups[0]["photos"] == [P["p3"]]
    assert groups[0]["topic_ids"] == ["t1", "t2"]


def test_no_markers_is_none():
    assert group_day([], TOPICS, PHOTOS) is None
    assert group_day(None, TOPICS, PHOTOS) is None


def test_level_one_and_level_1_are_one_group():
    markers = [{"at": "09:00", "location": "Level one"}, {"at": "09:20", "location": "Level 1"}]
    topics = [{"id": "a", "time_range": "09:05–09:10"}, {"id": "b", "time_range": "09:25–09:30"}]
    groups = group_day(markers, topics, [])
    assert len(groups) == 1
    assert groups[0]["location"] == "Level 1" and groups[0]["topic_ids"] == ["a", "b"]


def test_return_to_a_place_is_a_new_group():
    markers = [{"at": "09:00", "location": "Level 1"}, {"at": "09:10", "location": "Level 2"},
               {"at": "09:20", "location": "level one"}]
    topics = [{"id": "a", "time_range": "09:01–09:05"}, {"id": "b", "time_range": "09:11–09:15"},
              {"id": "c", "time_range": "09:21–09:25"}]
    groups = group_day(markers, topics, [])
    assert [g["location"] for g in groups] == ["Level 1", "Level 2", "Level 1"]
    assert [g["order"] for g in groups] == [0, 1, 2]
    assert groups[0]["key"] == groups[2]["key"]


def test_announced_place_with_nothing_gives_no_group():
    markers = [{"at": "09:00", "location": "Level 1"}, {"at": "09:10", "location": "Level 2"},
               {"at": "09:20", "location": "Level 3"}]
    topics = [{"id": "a", "time_range": "09:01–09:05"}, {"id": "c", "time_range": "09:21–09:25"}]
    groups = group_day(markers, topics, [])
    assert [g["location"] for g in groups] == ["Level 1", "Level 3"]
    assert [g["order"] for g in groups] == [0, 1]


def test_unparseable_time_range_goes_to_a_none_group_at_the_end():
    topics = [{"id": "a", "time_range": "09:05–09:10"},
              {"id": "x", "time_range": "sometime", "related_photos": [P["p2"]]},
              {"id": "y", "time_range": 42}, {"id": "z"}]
    groups = group_day(MARKERS, topics, [P["p2"], "no_time_here.jpg"])
    assert [g["location"] for g in groups] == ["Level 1", None]
    tail = groups[-1]
    assert tail["topic_ids"] == ["x", "y", "z"] and tail["key"] is None
    assert tail["reason"] == "no_time"
    # bound to an untimed topic: still "bound", so in photos, not unbound_photos
    assert tail["photos"] == [P["p2"]] and tail["unbound_photos"] == ["no_time_here.jpg"]


def test_untimed_items_do_not_merge_into_an_expired_none_group():
    topics = [{"id": "late", "time_range": "11:00–11:05"}, {"id": "x", "time_range": ""},
              {"id": "y", "time_range": "junk"}]
    groups = group_day(MARKERS, topics, [])
    assert [(g["location"], g["reason"], g["topic_ids"]) for g in groups] == [
        (None, "expired", ["late"]), (None, "no_time", ["x", "y"])]
    assert [g["order"] for g in groups] == [0, 1]


def test_before_first_and_expired_none_groups_do_not_merge():
    markers = [{"at": "09:00", "location": "Level 1"}]
    topics = [{"id": "early", "time_range": "08:50–08:55"},
              {"id": "early2", "time_range": "08:56–08:58"},
              {"id": "late", "time_range": "09:40–09:45"}]
    groups = group_day(markers, topics, [])
    assert [(g["reason"], g["topic_ids"]) for g in groups] == [
        ("before_first", ["early", "early2"]), ("expired", ["late"])]


def test_before_first_is_judged_in_seconds_like_locate():
    # the marker is said at 09:00:30; a photo at 09:00:10 is before it,
    # and one at 09:00:40 is on Level 1
    markers = [{"at": "09:00", "at_s": "09:00:30", "location": "Level 1"}]
    groups = group_day(markers, [], [_photo("09:00:10"), _photo("09:00:40")])
    assert [(g["location"], g["reason"], g["unbound_photos"]) for g in groups] == [
        (None, "before_first", [_photo("09:00:10")]),
        ("Level 1", None, [_photo("09:00:40")])]


def test_carry_window_expires_after_30_minutes():
    markers = [{"at": "09:00", "location": "Level 1"}]
    topics = [{"id": "in", "time_range": "09:30–09:35"}, {"id": "out", "time_range": "09:31–09:40"}]
    groups = group_day(markers, topics, [])
    assert [(g["location"], g["topic_ids"]) for g in groups] == [("Level 1", ["in"]), (None, ["out"])]
    assert [g["reason"] for g in groups] == [None, "expired"]
    assert place_of(markers, "09:31") is None and place_of(markers, "09:30") == "Level 1"


def test_place_of_normalises_and_aliases():
    assert place_of(MARKERS, "09:05") == "Level 1"
    assert place_of(MARKERS, "08:59") is None
    aliased = [{"at": "09:00", "location": "the new block"}]
    assert place_of(aliased, "09:01", aliases={"the new block": "Block C"}) == "Block C"


def test_topic_identity_fallbacks():
    topics = [{"topic_id": 7, "time_range": "09:05–09:10"}, {"time_range": "09:06–09:10"}]
    assert group_day(MARKERS, topics, [])[0]["topic_ids"] == [7, 1]


def test_bad_data_never_raises():
    markers = [{"at": "09:00", "location": ""}, {"at": "09:00", "location": None}, "junk",
               {"location": "Level 9"}, {"at": "09:00", "location": "Level 1"}]
    topics = ["not a dict", {"id": "a", "time_range": "09:05–09:10", "related_photos": "oops"},
              {"id": "b", "time_range": "09:06–09:10", "related_photos": [None, 3, "ghost.jpg"]}]
    photos = [None, {"s3_key": "u/2026-09-02/" + P["p2"]}, P["p2"], {"nothing": 1}]
    groups = group_day(markers, topics, photos)
    assert [g["topic_ids"] for g in groups] == [["a", "b"]]
    assert _all_photos(groups) == [P["p2"]]           # deduped by basename; ghost not invented


def test_only_unusable_markers_is_none():
    topics = [{"id": "a", "time_range": "09:05–09:10"}]
    assert group_day([{"at": "09:00", "location": "  "}], topics, []) is None
    assert group_day([{"at": "09:00", "location": ""}, {"location": "Level 1"},
                      {"at": "", "location": "Level 2"}, "junk"], topics, []) is None


def test_photo_dict_takes_first_usable_name():
    name = "x_2026-09-02_09-06-00.jpg"
    for photo in ({"filename": 3, "s3_key": name}, {"filename": "  ", "s3_key": "u/d/" + name},
                  {"filename": name, "s3_key": 3}):
        groups = group_day(MARKERS, [], [photo])
        assert _all_photos(groups) == [name], photo
        assert groups[0]["location"] == "Level 1"


def test_group_shape():
    groups = group_day(MARKERS, [{"id": "a", "time_range": "09:05–09:10"}], [])
    assert groups == [{"location": "Level 1", "kind": "level", "key": "level:level 1",
                       "reason": None, "order": 0, "topic_ids": ["a"], "photos": [],
                       "unbound_photos": []}]
