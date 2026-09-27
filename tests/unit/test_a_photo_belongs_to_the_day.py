"""
A photo belongs to at most one topic PER DAY, not one per extraction.

`photos_for_topics` was never wrong: within one call it picks a single target
(photo_binding.py `target`, one append). The defect is how many times it was
CALLED. `lambda_item_writer` lists the photo prefix for the whole DAY
(`users/{folder}/pictures/{date}/`) but passes only THIS EXTRACTION's topics,
and idempotency is keyed on `source_s3_key` -- so session B's run never clears
session A's bindings, and both keep the same photo.

Measured on prod 2026-09-23: `topic_photos` holds 193 rows over 161 distinct
photos, and 22 of those photos (13.7%) hang off more than one topic. The two
cases reproduced below are real rows from that table.

The fix is not a new matcher. It is calling the existing one ONCE for the whole
day, over every topic the day has -- which makes its existing one-photo-one-
topic guarantee global instead of per-session.

Two refinements ride along, and both are separately pinned here:

  * a photo taken while session A was recording may not be claimed by a topic
    from session B (`session_spans`), because "he was holding the camera during
    A" is stronger evidence than "a B topic starts 90 seconds later";
  * a row a HUMAN placed is never touched by a machine rebind.
"""
import photo_binding as pb


def _photo(name, hhmm, folder="Ben_Lin", date="2026-09-11"):
    return {"key": f"users/{folder}/pictures/{date}/{name}",
            "filename": name, "hhmm": hhmm}


# ---------------------------------------------------------------------------
# The two prod reproductions. These are the rows the fix has to collapse.
# ---------------------------------------------------------------------------

# Ben_Lin / 2026-09-11: ONE photo under SIX topics spanning seven minutes, each
# from a different extraction artifact. Queried from prod on 2026-09-23.
_BEN_LIN_DAY = [
    {"time_range": "14:31 – 14:32", "source": "sid...63dbe5e2b.json",
     "title": "Recording Indicator Light Design"},
    {"time_range": "14:33 – 14:34", "source": "sid...9bc0d431b.json",
     "title": "App Lockdown and Connectivity"},
    {"time_range": "14:37 – 14:37", "source": "sid...24f71a8f0.json",
     "title": "Recording Setup -- NZ Accent"},
    {"time_range": "14:37 – 14:37", "source": "sid...a48ed13a5.json",
     "title": "Voice Prompt Disable Practice"},
    {"time_range": "14:37 – 14:38", "source": "sid...da29ffb4b.json",
     "title": "Indicator Lights - Red Recording S"},
    {"time_range": "14:38 – 14:39", "source": "sid...db2845fe8.json",
     "title": "Join Meeting Multi-Device Sync"},
]

# Ben_UCPK2 / 2026-09-22 -- the afternoon the defect was reported. Seven
# extraction artifacts for one afternoon; this photo landed under two of them.
_BEN_UCPK2_DAY = [
    {"time_range": "15:21 – 15:22", "source": "sid095b...727e.json",
     "title": "Recording Device Operation Demo"},
    {"time_range": "15:24 – 15:24", "source": "sid1b89...e9ec.json",
     "title": "Video Recording Preview Function"},
    {"time_range": "15:26 – 15:28", "source": "sid14eb...5099.json",
     "title": "Wearable Recorder Design Update"},
]


def _bound_topics(result):
    """Indices that ended up holding at least one photo."""
    return sorted(i for i, photos in result.items() if photos)


def test_prod_case_ben_lin_six_rows_collapse_to_one():
    """The worst row in prod: six topics, six source keys, one photo."""
    photo = _photo("ben_lin_2026-09-11_14-37-31.jpg", "14:37")
    result = pb.photos_for_topics([photo], _BEN_LIN_DAY)
    assert len(_bound_topics(result)) == 1, (
        "one photo, one topic -- got " + repr(_bound_topics(result)))


def test_prod_case_ben_lin_picks_a_window_that_contains_the_photo():
    """Containment beats proximity: 14:37:31 is INSIDE three of these windows.

    Which of the three is a tie the matcher breaks by index, and that part is
    arbitrary. What is not arbitrary is that a window CONTAINING the instant
    must beat 14:31-14:32, which is six minutes away and is the row the user
    actually complained about seeing.
    """
    photo = _photo("ben_lin_2026-09-11_14-37-31.jpg", "14:37")
    result = pb.photos_for_topics([photo], _BEN_LIN_DAY)
    winner = _bound_topics(result)[0]
    assert _BEN_LIN_DAY[winner]["time_range"] in (
        "14:37 – 14:37", "14:37 – 14:38")


def test_prod_case_ben_ucpk2_two_rows_collapse_to_one():
    photo = _photo("ben_ucpk2_2026-09-22_15-22-34.jpg", "15:22",
                   folder="Ben_UCPK2", date="2026-09-22")
    result = pb.photos_for_topics([photo], _BEN_UCPK2_DAY)
    assert len(_bound_topics(result)) == 1


def test_prod_case_ben_ucpk2_prefers_the_window_it_just_left():
    """15:22:34 is 34 seconds past 15:21-15:22 and 86 seconds before
    15:24-15:24. Both qualify under the 2-minute tolerance; the nearer one
    wins, and it is the earlier topic."""
    photo = _photo("ben_ucpk2_2026-09-22_15-22-34.jpg", "15:22",
                   folder="Ben_UCPK2", date="2026-09-22")
    result = pb.photos_for_topics([photo], _BEN_UCPK2_DAY)
    assert _bound_topics(result) == [0]


def test_a_whole_prod_day_binds_every_photo_at_most_once():
    """The invariant, stated over the day rather than over one photo."""
    photos = [
        _photo("ben_ucpk2_2026-09-22_15-22-34.jpg", "15:22"),
        _photo("ben_ucpk2_2026-09-22_15-26-57.jpg", "15:26"),
    ]
    result = pb.photos_for_topics(photos, _BEN_UCPK2_DAY)
    placed = [p["key"] for bucket in result.values() for p in bucket]
    assert len(placed) == len(set(placed)), "a photo was placed twice"
    assert len(placed) <= len(photos)


# ---------------------------------------------------------------------------
# session_spans -- a photo taken during session A is not session B's
# ---------------------------------------------------------------------------

def test_a_photo_taken_during_one_session_is_not_claimed_by_another():
    """Session A is not allowed to reach into session B's recording.

    The handover shape, which is the one that actually happens: A stops at
    14:24 having just spoken (topic 14:23-14:24), B starts immediately and its
    first topic is 14:27-14:28. A photo at 14:25 is ONE minute from A's last
    word and TWO from B's first, so both qualify under the 2-minute tolerance
    and the nearer one -- A -- wins. But the camera that was running was B's.

    This construction took two attempts. The first put B's topic BEFORE the
    photo, where carry-forward already preferred B and the rule changed
    nothing; the case where it earns its keep is the tolerance path, at a
    session boundary.
    """
    topics = [
        {"time_range": "14:23 – 14:24", "source": "A"},
        {"time_range": "14:27 – 14:28", "source": "B"},
    ]
    photo = _photo("x.jpg", "14:25")
    spans = {"A": (14 * 60 + 10, 14 * 60 + 24), "B": (14 * 60 + 24, 14 * 60 + 40)}

    loose = pb.photos_for_topics([photo], topics)
    assert _bound_topics(loose) == [0], "precondition: carry-forward picks A"

    tight = pb.photos_for_topics([photo], topics,
                                 topic_sessions={0: "A", 1: "B"},
                                 session_spans=spans)
    assert _bound_topics(tight) == [1], "the photo was taken during B"


def test_a_photo_a_recording_session_never_spoke_over_binds_to_nothing():
    """And when the session that WAS running has nothing to offer, the answer
    is no topic -- not the session that was not running.

    Written after watching the first version of this file fail. My expectation
    had been that B would take the photo anyway; it does not, because B's only
    topic starts 14 minutes LATER and carry-forward is directional on purpose
    ("he was still doing the thing he last described", never "he was about to
    describe this"). Reaching backwards into a topic not yet spoken would be a
    new rule, and it is not one the prod evidence asks for.

    Binding to nothing is a real answer here and costs nothing visible: since
    2026-09-07 the day view lists every photo whether or not it binds, so the
    photo is ungrouped rather than invisible. Giving it to a session that was
    not recording is the failure this rule exists to prevent.
    """
    topics = [
        {"time_range": "14:00 – 14:05", "source": "A"},
        {"time_range": "14:39 – 14:40", "source": "B"},
    ]
    photo = _photo("x.jpg", "14:25")
    spans = {"A": (13 * 60 + 55, 14 * 60 + 6), "B": (14 * 60 + 20, 14 * 60 + 40)}
    out = pb.photos_for_topics([photo], topics,
                               topic_sessions={0: "A", 1: "B"},
                               session_spans=spans)
    assert _bound_topics(out) == [], "A was not recording; B had not spoken yet"


def test_a_photo_between_sessions_is_restricted_by_nothing():
    """Outside every span there is no session evidence, so the ordinary rule
    applies unchanged. Fail OPEN: a missing span must not start dropping
    photos that bind today."""
    topics = [{"time_range": "14:00 – 14:05", "source": "A"}]
    photo = _photo("x.jpg", "14:10")
    spans = {"A": (13 * 60 + 55, 14 * 60 + 6)}
    out = pb.photos_for_topics([photo], topics,
                               topic_sessions={0: "A"}, session_spans=spans)
    assert _bound_topics(out) == [0]


def test_a_topic_whose_session_span_is_unknown_still_competes():
    """`session_span` returns None for RealPTT days, days predating migration
    0009 and lake-fed files -- "a real answer, not an error" in its own words.
    A topic we cannot place in time must not be silently excluded.

    The photo is INSIDE session A's span, so the rule is switched on and A is
    the only session that covers it. Topic 1 belongs to no known session and is
    strictly nearer, so it wins if and only if it was allowed to compete.
    """
    topics = [
        {"time_range": "14:20 – 14:21", "source": "A"},
        {"time_range": "14:24 – 14:26", "source": "UNKNOWN"},
    ]
    photo = _photo("x.jpg", "14:25")
    spans = {"A": (14 * 60 + 15, 14 * 60 + 45)}
    out = pb.photos_for_topics([photo], topics,
                               topic_sessions={0: "A", 1: None},
                               session_spans=spans)
    assert _bound_topics(out) == [1], "the unplaceable topic was excluded"


def test_a_photo_outside_every_known_span_is_judged_against_everything():
    """The rule only switches on for a photo some session was recording over.
    Between sessions there is no evidence, so nothing is removed -- including
    when the only span we know about is hours away."""
    topics = [
        {"time_range": "14:24 – 14:26", "source": "A"},
    ]
    photo = _photo("x.jpg", "14:25")
    spans = {"A": (10 * 60, 11 * 60)}          # A was recording in the morning
    out = pb.photos_for_topics([photo], topics,
                               topic_sessions={0: "A"}, session_spans=spans)
    assert _bound_topics(out) == [0]


def test_overlapping_sessions_both_stay_eligible():
    """Two devices in one room record the same stretch (multi-device merge).
    Both sessions contain the photo, so both may claim it and the ordinary
    distance rule decides."""
    topics = [
        {"time_range": "14:40 – 14:41", "source": "A"},
        {"time_range": "14:25 – 14:26", "source": "B"},
    ]
    photo = _photo("x.jpg", "14:25")
    spans = {"A": (14 * 60 + 20, 14 * 60 + 45), "B": (14 * 60 + 22, 14 * 60 + 44)}
    out = pb.photos_for_topics([photo], topics,
                               topic_sessions={0: "A", 1: "B"},
                               session_spans=spans)
    assert _bound_topics(out) == [1], "nearest in time, both eligible"


def test_no_spans_at_all_is_exactly_todays_behaviour():
    """The whole feature is opt-in. Passing nothing must produce byte-identical
    results to the call signature that exists today, or every caller that has
    not been updated changes behaviour silently."""
    photos = [_photo("a.jpg", "14:37"), _photo("b.jpg", "14:31")]
    assert (pb.photos_for_topics(photos, _BEN_LIN_DAY)
            == pb.photos_for_topics(photos, _BEN_LIN_DAY,
                                    topic_sessions=None, session_spans=None))


# ---------------------------------------------------------------------------
# The writer. This is where the defect actually lives.
#
# Every assertion above is about the pure matcher, and every one of them
# passed BEFORE this fix existed -- because the matcher was never wrong. It
# binds one photo to one topic within a single call. The bug is that
# lambda_item_writer calls it once per EXTRACTION while listing the photos of
# a whole DAY, and clears only its own source key. So a green matcher test
# proves nothing about the reported defect; these do.
# ---------------------------------------------------------------------------
import io
import json

import pytest

iw = pytest.importorskip("lambda_item_writer", reason="requires psycopg")


# Local doubles rather than an import from test_lambda_item_writer: pytest does
# not put tests/unit on sys.path, and a cross-test import would also make this
# file's meaning depend on edits made for a different feature.
class _FakeCursor:
    def __init__(self, row):
        self._row = row

    def fetchone(self):
        return self._row


class _FakeTransaction:
    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        return False


class FakeConn:
    def __init__(self):
        self.executed = []

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        return False

    def execute(self, sql, params=None):
        self.executed.append((sql, params))
        return _FakeCursor(None)

    def transaction(self):
        return _FakeTransaction()


class _FakePaginator:
    def __init__(self, objects):
        self.objects = objects

    def paginate(self, Bucket, Prefix):
        yield {"Contents": [{"Key": k} for k in self.objects if k.startswith(Prefix)]}


class FakeS3:
    def __init__(self, objects=None):
        self.objects = objects or {}

    def get_object(self, Bucket, Key):
        body = self.objects[Key]
        raw = body.encode("utf-8") if isinstance(body, str) else body
        return {"Body": io.BytesIO(raw)}

    def get_paginator(self, op):
        assert op == "list_objects_v2"
        return _FakePaginator(self.objects)


EXTRACTION_KEY = f"extractions/Jarley_Trainor/2026-07-06/Benl1_2026-07-06_10-00-00.json"


def make_extraction():
    return {
        "schema_version": 1,
        "user_folder": "Jarley_Trainor",
        "date": "2026-07-06",
        "session_base": "Benl1_2026-07-06_10-00-00",
        "source_transcripts": ["Benl1_2026-07-06_10-00-00.json"],
        "extracted_at": "2026-07-06T10:05:00Z",
        "declared_site": None,
        "topics": [{
            "topic_title": "Safety Briefing",
            "category": "safety",
            "summary": "Discussed PPE requirements.",
            "time_range": "10:00 – 10:05",
            "participants": ["Jarley Trainor"],
            "action_items": [],
        }],
    }

_DAY = "2026-07-06"
_FOLDER = "Jarley_Trainor"
_PHOTO_KEY = f"users/{_FOLDER}/pictures/{_DAY}/Benl1_{_DAY}_10-02-00.jpg"

# The day already holds a topic from an EARLIER session, and that session
# already claimed the photo. This is the prod shape: seven artifacts for one
# afternoon, each one having listed the same day-wide picture prefix.
_EARLIER_SESSION_TOPIC = {
    "id": "topic-from-session-A",
    "time_range": "10:01 – 10:01",
    "source_s3_key": f"extractions/{_FOLDER}/{_DAY}/Benl1_{_DAY}_09-58-00.json",
}


@pytest.fixture
def day_with_an_earlier_session(monkeypatch):
    """This extraction (10:00-10:05) runs on a day that already has a topic
    from a session that ended minutes earlier -- and one photo at 10:02."""
    conn = FakeConn()
    monkeypatch.setattr(iw, "get_connection", lambda *a, **k: conn)
    monkeypatch.setattr(iw, "_s3_client", FakeS3({
        EXTRACTION_KEY: json.dumps(make_extraction()),
        _PHOTO_KEY: b"",
    }))
    monkeypatch.setattr(iw.companies, "get_company_by_name",
                        lambda conn, name: {"id": "co-1", "name": name})
    monkeypatch.setattr(iw.lambda_ingest, "resolve_site",
                        lambda conn, cid, report, user_folder: {"id": "site-1", "name": "Test Site"})
    monkeypatch.setattr(iw.lambda_ingest, "resolve_user", lambda conn, cid, user_folder: None)
    monkeypatch.setattr(iw.recordings, "site_for_media", lambda *a, **k: None)
    monkeypatch.setattr(iw.recordings, "site_for_day", lambda *a, **k: None)
    monkeypatch.setattr(iw.topics, "delete_topics_for_source", lambda *a, **k: 0)
    monkeypatch.setattr(iw.topics, "upsert_topic",
                        lambda *a, **k: {"id": "topic-from-session-B"})
    monkeypatch.setattr(iw.findings, "insert_findings", lambda *a, **k: [])
    monkeypatch.setattr(iw.match_request, "emit", lambda *a, **k: None)
    monkeypatch.setattr(iw.keyframe_request, "emit", lambda *a, **k: None)
    monkeypatch.setattr(iw, "EMIT_KEYFRAME_REQUESTS", False)

    # The day, as the database holds it once this extraction's own topic is in.
    monkeypatch.setattr(iw.topics, "list_day_topics_for_binding",
                        lambda conn, folder, date: [
                            _EARLIER_SESSION_TOPIC,
                            {"id": "topic-from-session-B",
                             "time_range": "10:00 – 10:05",
                             "source_s3_key": EXTRACTION_KEY},
                        ])
    monkeypatch.setattr(iw.recordings, "session_local_span", lambda *a, **k: None)

    written = []
    monkeypatch.setattr(iw.topics, "replace_day_photo_bindings",
                        lambda conn, folder, date, rows: written.append(
                            {"folder": folder, "date": date, "rows": list(rows)}))
    return {"conn": conn, "written": written, "monkeypatch": monkeypatch}


def _run(handler_event=None):
    return iw.lambda_handler(handler_event or {"Records": [
        {"s3": {"object": {"key": EXTRACTION_KEY}}}]}, None)


def test_the_writer_rebinds_the_whole_day_not_just_its_own_extraction(
        day_with_an_earlier_session):
    """The fix, stated as the thing the writer must DO.

    Before it, the writer handed `photos=` to upsert_topic for its own topics
    only and never looked at the day -- so the earlier session's row survived
    untouched and the photo ended up under two topics."""
    _run()
    written = day_with_an_earlier_session["written"]
    assert written, "the day was never rebound"
    assert written[0]["folder"] == _FOLDER
    assert written[0]["date"] == _DAY


def test_one_photo_reaches_exactly_one_topic_across_two_sessions(
        day_with_an_earlier_session):
    """The reported defect, at the layer it lives on."""
    _run()
    rows = day_with_an_earlier_session["written"][0]["rows"]
    keys = [r["s3_key"] for r in rows]
    assert keys.count(_PHOTO_KEY) == 1, (
        "the photo was bound %d times: %r" % (keys.count(_PHOTO_KEY), rows))


def test_the_earlier_sessions_topic_is_a_candidate_in_the_rebind(
        day_with_an_earlier_session):
    """The rebind considers the whole day. If it only ever saw this
    extraction's topics it would still produce one row here -- and still leave
    the other session's row in place, which is the bug."""
    _run()
    rows = day_with_an_earlier_session["written"][0]["rows"]
    assert all("topic_id" in r and "s3_key" in r for r in rows)
    # The photo at 10:02 is inside 10:00-10:05 and a minute from 10:01-10:01;
    # containment wins, so session B takes it -- and A's stale row is gone
    # because the whole day was replaced, not appended to.
    assert rows == [{"topic_id": "topic-from-session-B", "s3_key": _PHOTO_KEY,
                     "caption_text": None}]


def test_the_day_is_locked_before_it_is_rebound(day_with_an_earlier_session):
    """Two sessions of the SAME day could not race before -- each cleared and
    wrote only its own source key. A day-wide replace can, so it needs a
    day-wide lock. Asserted on the SQL because an advisory lock has no other
    observable effect, and a lock that is never taken looks exactly like one
    that is."""
    _run()
    locks = [sql for sql, params in day_with_an_earlier_session["conn"].executed
             if "pg_advisory_xact_lock" in sql]
    day_locks = [params for sql, params in day_with_an_earlier_session["conn"].executed
                 if "pg_advisory_xact_lock" in sql
                 and params and _FOLDER in str(params[0]) and _DAY in str(params[0])]
    assert locks, "no advisory lock at all"
    assert day_locks, (
        "the extraction key is locked but the DAY is not -- two sessions of "
        "one day can interleave their replaces")


def test_upsert_topic_no_longer_writes_photos_itself(day_with_an_earlier_session):
    """Two writers for one table is how the rows diverged in the first place.
    The day rebind owns topic_photos now; the per-topic upsert must not also
    insert, or a rebind would be racing the very rows it is replacing."""
    seen = {}
    day_with_an_earlier_session["monkeypatch"].setattr(
        iw.topics, "upsert_topic",
        lambda *a, **k: (seen.update(k), {"id": "topic-from-session-B"})[1])
    _run()
    assert not seen.get("photos"), (
        "upsert_topic was still handed photos=%r" % (seen.get("photos"),))
