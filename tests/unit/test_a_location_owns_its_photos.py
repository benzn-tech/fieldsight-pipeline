"""A photo taken where a topic's place had been announced belongs to that topic.

Owner, 2026-10-01 (TEST, Ben_Lin_test2): an inspection walk -- "Ground floor
inspection" at 13:24, "Level one inspection" at 13:26 -- had three of its
Level 1 photos taken by members of the public asking the way, because their
chat was the topic overlapping 13:28 and binding went by the clock alone. The
location markers had it right; the inspection should not lose its photos to
whatever conversation interrupts it.

THE test is `the interruption does not take the inspection's photos`, built
from that day's real markers and topic windows.
"""
import photo_binding as pb
import photo_rebind

TOPICS = [{"time_range": "13:22 – 13:24"},      # 0 ductwork call
          {"time_range": "13:24 – 13:27"},      # 1 the inspection walk
          {"time_range": "13:27 – 13:28"},      # 2 members of the public
          {"time_range": "13:28 – 13:28"}]      # 3 concrete pour note
MARKERS = [{"at": "13:24", "location": "Ground floor"},
           {"at": "13:26", "location": "Level 1"},
           {"at": "13:27", "location": "Level 1"}]


def photos(*hhmm):
    return [{"key": "k%d" % i, "filename": "f%d.jpg" % i, "hhmm": h} for i, h in enumerate(hhmm)]


DAY = photos("13:25", "13:25", "13:25", "13:27", "13:28", "13:28", "13:28")


def counts(result):
    return {i: len(v) for i, v in result.items()}


def test_THE_the_interruption_does_not_take_the_inspections_photos():
    assert counts(pb.photos_for_topics(DAY, TOPICS)) == {0: 0, 1: 4, 2: 3, 3: 0}, \
        "the old answer, by the clock"
    assert counts(pb.photos_for_topics(DAY, TOPICS, markers=MARKERS)) == {0: 0, 1: 7, 2: 0, 3: 0}


def test_split_topics_get_their_own_floors_photos():
    """When extraction splits the walk per floor, each floor keeps its own."""
    split = [{"time_range": "13:24 – 13:26"}, {"time_range": "13:26 – 13:29"},
             {"time_range": "13:27 – 13:28"}]
    assert counts(pb.photos_for_topics(DAY, split, markers=MARKERS)) == {0: 3, 1: 4, 2: 0}


def test_an_announcement_opens_what_follows_it():
    """13:24 sits on the edge of 13:22-13:24 and 13:24-13:27: the one that goes on."""
    windows = {0: (13 * 60 + 22, 13 * 60 + 24), 1: (13 * 60 + 24, 13 * 60 + 27)}
    assert pb._stay_owner(13 * 60 + 24, windows) == 1


def test_a_repeated_place_is_one_stay():
    assert [s[1] for s in pb._stays(MARKERS)] == ["ground floor", "level 1"]


def test_without_markers_nothing_changes():
    assert pb.photos_for_topics(DAY, TOPICS, markers=[]) == pb.photos_for_topics(DAY, TOPICS)


def test_a_stay_ends_after_the_carry_window():
    late = photos("14:10")                      # 43 min after the last Level 1 mention
    topics = TOPICS + [{"time_range": "14:09 – 14:11"}]
    assert counts(pb.photos_for_topics(late, topics, markers=MARKERS))[4] == 1


def test_a_place_announced_outside_every_topic_owns_nothing():
    markers = [{"at": "12:00", "location": "Car park"}]
    assert pb.photos_for_topics(DAY, TOPICS, markers=markers) == pb.photos_for_topics(DAY, TOPICS)


# ---- the rebind reads the markers, and survives not having them -------------------

class Conn:
    def __init__(self):
        self.sql = []

    def execute(self, sql, params=None):
        self.sql.append(sql)

    def transaction(self):
        class T:
            def __enter__(self_inner):
                return self_inner

            def __exit__(self_inner, *a):
                return False
        return T()


def _rebind(monkeypatch, markers_fn):
    written = {}
    monkeypatch.setattr(photo_rebind.topics, "list_day_topics_for_binding",
                        lambda conn, f, d: [dict(t, id="t%d" % i, source_s3_key=None)
                                            for i, t in enumerate(TOPICS)])
    monkeypatch.setattr(photo_rebind.topics, "replace_day_photo_bindings",
                        lambda conn, f, d, rows: written.setdefault("rows", rows) and 0)
    monkeypatch.setattr(photo_rebind.location_markers, "for_day", markers_fn)
    photo_rebind.rebind_day_photos(Conn(), "c", "Ben_Lin_test2", "2026-10-01", DAY)
    by = {}
    for r in written["rows"]:
        by[r["topic_id"]] = by.get(r["topic_id"], 0) + 1
    return by


def test_the_day_rebind_binds_by_location(monkeypatch):
    assert _rebind(monkeypatch, lambda *a: MARKERS) == {"t1": 7}


def test_unreadable_markers_fall_back_to_the_clock(monkeypatch):
    def boom(*a):
        raise RuntimeError("no table")
    assert _rebind(monkeypatch, boom) == {"t1": 4, "t2": 3}
