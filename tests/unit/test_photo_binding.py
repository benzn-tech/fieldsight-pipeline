"""
Tests for src/photo_binding.py — P2, 2026-07-23 prod-media-binding plan (TDD).

Style mirrors tests/unit/test_lambda_item_writer.py: plain functions plus a
`_photo` fixture builder; the matcher is pure so no FakeConn/FakeS3 is needed
except for the S3 lister at the bottom of the file.

The decided rule (user-approved): ±5 min tolerance around each topic window
PLUS a nearest-topic fallback for otherwise-orphaned photos — a photo must
never end up orphaned when the day has at least one topic. Both clauses
collapse into one deterministic rule: the nearest window wins (distance 0
when inside the window), ties -> lowest topic index; no parseable window on
any topic -> topic 0; per-topic cap PHOTOS_PER_TOPIC_CAP with a deterministic
cascade to the next-nearest topic that still has headroom.
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


def test_photo_beyond_tolerance_still_binds_to_nearest():
    # Orphan fallback: 10:40 photo vs a 10:39-10:39 window 1 min away and a
    # 12:12-12:13 window ~92 min away -> nearest wins.
    topics = [{"time_range": "10:39 – 10:39"}, {"time_range": "12:12 – 12:13"}]
    p = _photo("a.jpg", "10:40")
    assert pb.photos_for_topics([p], topics) == {0: [p], 1: []}


def test_far_orphan_binds_to_nearest_topic():
    topics = [{"time_range": "08:00 – 08:10"}, {"time_range": "15:00 – 15:10"}]
    p = _photo("a.jpg", "13:00")            # 290 min from t0's end, 120 from t1's start
    assert pb.photos_for_topics([p], topics) == {0: [], 1: [p]}


def test_tie_breaks_to_lowest_index():
    # Equidistant (and inside both after overlap) -> lowest index.
    topics = [{"time_range": "09:00 – 10:00"}, {"time_range": "09:30 – 11:00"}]
    p = _photo("a.jpg", "09:45")
    assert pb.photos_for_topics([p], topics) == {0: [p], 1: []}


def test_no_parseable_windows_binds_to_first_topic():
    topics = [{"time_range": None}, {"time_range": "not a range"}]
    p = _photo("a.jpg", "09:30")
    assert pb.photos_for_topics([p], topics) == {0: [p], 1: []}


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
    topics = [{"time_range": "09:00 – 10:00"}, {"time_range": "10:00 – 11:00"}]
    photos = [_photo(f"p{i:02d}.jpg", f"09:{i:02d}") for i in range(12)]
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
