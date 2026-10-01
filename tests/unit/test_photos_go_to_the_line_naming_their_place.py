"""Within a topic written as one line per place, each photograph goes to the
line naming where it was taken.

Owner, 2026-10-01 (TEST, Ben_Lin_test2): the inspection walk is one topic and
the report writes it as a Ground floor row and a Level 1 row -- and every
photograph went under the Ground floor row. The day's location markers say
where each was taken; the binding already uses them (photo_binding), and now
the report places by the same answer.

THE test is `a Level 1 photograph goes in the Level 1 row`.
"""
import io

import lambda_session_report as sr
import photo_binding as pb

MARKERS = [{"at": "13:24", "location": "Ground floor"},
           {"at": "13:26", "location": "Level 1"},
           {"at": "13:27", "location": "Level 1"}]
CHECKS = ("### Checklist\nItem | Answer | Comment\n---|---|---\n"
          "Is the Level 0 inspected? | Yes | Ground floor inspection completed [t2]\n"
          "Is the Level 1 inspected? | Yes | Level one inspection continued [t2]\n")


def test_THE_a_level_1_photograph_goes_in_the_level_1_row():
    secs = sr._prose_sections(CHECKS)
    sr._place_photos(secs, {"t2": ["g1", "g2", "l1"]},
                     places={"t2": ["ground floor", "ground floor", "level 1"]})
    assert secs[0]["photos_after"] == {2: ["g1", "g2"], 3: ["l1"]}


def test_a_photograph_of_no_named_place_goes_with_the_first_line():
    secs = sr._prose_sections(CHECKS)
    sr._place_photos(secs, {"t2": ["x", "y"]}, places={"t2": [None, "roof"]})
    assert secs[0]["photos_after"] == {2: ["x", "y"]}


def test_without_places_the_answer_is_unchanged():
    secs = sr._prose_sections(CHECKS)
    sr._place_photos(secs, {"t2": ["a", "b"]})
    assert secs[0]["photos_after"] == {2: ["a", "b"]}


def test_place_by_time_uses_the_same_stays_as_the_binding():
    assert pb.place_at(MARKERS, "13:25") == "ground floor"
    assert pb.place_at(MARKERS, "13:28") == "level 1"
    assert pb.place_at(MARKERS, "13:10") is None
    assert pb.photo_hhmm("ben_lin_test2_2026-10-01_13-27-54.jpg") == "13:27"


def test_a_line_names_a_place_in_the_words_people_use():
    assert pb.names_place("Level one inspection continued", "level 1")
    assert pb.names_place("Is the Level 0 inspected?", "ground floor")
    assert pb.names_place("Ground floor walk", "Level 0")
    assert not pb.names_place("Level 10 slab poured", "level 1")
    assert not pb.names_place("anything", None)


def test_a_photograph_that_cannot_be_read_shifts_no_other_name(monkeypatch):
    """The fetch skips an unreadable file; the names must stay with their own
    pictures, or every later photograph would be placed by the wrong time."""
    class S3:
        def get_object(self, Bucket, Key):
            if Key.endswith("bad.jpg"):
                raise RuntimeError("gone")
            return {"Body": io.BytesIO(Key.encode())}
    monkeypatch.setattr(sr, "s3", lambda: S3())
    monkeypatch.setattr(sr, "_shrink", lambda b: b)
    import datetime as dt
    artifact = {"folder": "F", "date": "2026-10-01", "content": {"topics": [
        {"topic_title": "Walk", "time_range": "13:24 - 13:29",
         "related_photos": ["a_13-25-06.jpg", "bad.jpg", "c_13-28-15.jpg"]}]}}
    offer, streams = sr._offered_topics(artifact, [10 ** 9], dt.datetime(2026, 10, 1),
                                        dt.datetime(2026, 10, 1, 23, 59))
    assert offer[0]["photo_names"] == ["a_13-25-06.jpg", "c_13-28-15.jpg"]
    assert [s.getvalue().decode().rsplit("/", 1)[-1] for s in streams["t0"]] == offer[0]["photo_names"]
