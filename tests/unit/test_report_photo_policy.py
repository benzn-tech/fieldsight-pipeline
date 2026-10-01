"""The photographs a report carries: 60 at page size, up to 120 shrunk
automatically, past that the person's choice or a fair share per topic
(owner, 2026-10-01). One module for every report path (report_photos).

THE test is `a person's exclusions come off before anything is counted`.
"""
import io
import json

import report_photos as rp


def test_THE_a_persons_exclusions_come_off_before_anything_is_counted():
    groups = [("t0", ["a%d" % i for i in range(70)])]
    chosen, edge, left = rp.plan(groups, excluded={"a%d" % i for i in range(15)})
    assert len(chosen["t0"]) == 55 and edge == rp.STANDARD_EDGE and left == 0


def test_a_photograph_counts_once_in_its_first_group():
    chosen, _, _ = rp.plan([("t0", ["a", "b"]), ("t1", ["b", "c"])])
    assert chosen == {"t0": ["a", "b"], "t1": ["c"]}


def test_the_size_follows_the_count():
    assert rp.plan([("t0", ["x%d" % i for i in range(60)])])[1] == rp.STANDARD_EDGE
    assert rp.plan([("t0", ["x%d" % i for i in range(61)])])[1] == rp.COMPACT_EDGE


def test_past_the_limit_every_group_keeps_a_share_in_its_own_order():
    groups = [("walk", ["w%03d" % i for i in range(200)]), ("pour", ["p1", "p2"]),
              ("pad", ["d1"])]
    chosen, edge, left = rp.plan(groups)
    assert chosen["pour"] == ["p1", "p2"] and chosen["pad"] == ["d1"]
    assert len(chosen["walk"]) == 117 and chosen["walk"] == sorted(chosen["walk"])
    assert left == 203 - 120 and edge == rp.COMPACT_EDGE


class NoSuchKey(Exception):
    pass


class S3:
    exceptions = type("E", (), {"NoSuchKey": NoSuchKey})

    def __init__(self, body=None, boom=False):
        self.body, self.boom = body, boom

    def get_object(self, Bucket, Key):
        assert Key == "report_photo_selection/Ben_Lin_test2/2026-10-01.json"
        if self.boom:
            raise RuntimeError("denied")
        if self.body is None:
            raise NoSuchKey(Key)
        return {"Body": io.BytesIO(self.body)}


def test_the_days_choice_is_read_from_s3():
    body = json.dumps({"excluded": ["a.jpg", "b.jpg"]}).encode()
    assert rp.read_excluded(S3(body), "b", "Ben_Lin_test2", "2026-10-01") == {"a.jpg", "b.jpg"}


def test_no_choice_or_an_unreadable_one_leaves_nothing_out():
    assert rp.read_excluded(S3(), "b", "Ben_Lin_test2", "2026-10-01") == set()
    assert rp.read_excluded(S3(boom=True), "b", "Ben_Lin_test2", "2026-10-01") == set()
    assert rp.read_excluded(S3(b"not json"), "b", "Ben_Lin_test2", "2026-10-01") == set()
