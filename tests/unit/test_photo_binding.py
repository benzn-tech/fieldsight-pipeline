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


def test_photo_past_tolerance_edge_binds_to_nothing():
    # One minute beyond the 2-min cap: no longer qualifies for this topic.
    topics = [{"time_range": "10:00 – 10:05"}]
    p = _photo("a.jpg", "10:08")            # distance == 3
    assert pb.photos_for_topics([p], topics) == {0: []}


def test_photo_beyond_tolerance_binds_to_nothing():
    # Was test_photo_beyond_tolerance_still_binds_to_nearest (unbounded
    # nearest-wins). Under the 2026-07-24 rule a photo further than
    # PHOTO_TOLERANCE_MIN from every window binds to nothing, even though
    # one topic is still "nearest": 10:45 is 6 min from topic 0's window and
    # ~87 min from topic 1's -- neither qualifies.
    topics = [{"time_range": "10:39 – 10:39"}, {"time_range": "12:12 – 12:13"}]
    p = _photo("a.jpg", "10:45")
    assert pb.photos_for_topics([p], topics) == {0: [], 1: []}


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
    topics = [{"time_range": "09:00 – 10:00"}, {"time_range": "10:00 – 11:00"}]
    photos = [_photo(f"p{i:02d}.jpg", "10:00") for i in range(12)]
    result = pb.photos_for_topics(photos, topics)
    assert len(result[0]) == pb.PHOTOS_PER_TOPIC_CAP        # first 10 in input order
    assert result[0] == photos[:10]
    assert result[1] == photos[10:]                          # overflow cascades, not dropped


def test_all_topics_at_cap_drops_deterministically():
    topics = [{"time_range": "09:00 – 10:00"}]
    photos = [_photo(f"p{i:02d}.jpg", f"09:{i:02d}") for i in range(12)]
    result = pb.photos_for_topics(photos, topics)
    assert result[0] == photos[:10]                          # 2 dropped, logged, no crash


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
