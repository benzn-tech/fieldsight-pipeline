"""Unit: the stored names actually reach the transcript response.

The filenames below are copied VERBATIM from TEST — the transcript side ends `.json`, the
stored reference ends `.wav`, and the two differ by more than the extension. An earlier
version of this file invented both sides and they agreed with each other rather than with
the pipeline, so the read path shipped matching nothing at all.

The resolver is tested in isolation next door. This file exists because that is not enough:
the endpoint builds its segments in one place and the overlay reads them in another, and the
two agreeing is a separate fact from either of them working. Tonight already produced one
defect of exactly that shape — two features whose own tests passed while they disagreed about
a filename — so the join gets its own test rather than being assumed.
"""
import json

import pytest

org = pytest.importorskip("lambda_org_api", reason="requires psycopg (installed in CI)")

CALLER = {"id": "u-1", "cognito_sub": "sub-1", "company_id": "c-1", "email": "a@x.nz",
          "first_name": "Ada", "last_name": "L", "folder_name": "Ada_L",
          "avatar_s3_key": None, "global_role": "admin", "created_at": "2026-08-13"}

SEGS = {"text": "hello", "segments": [], "speaker_segments": [
    {"speaker": "spk_0", "text": "morning all", "start": 41400.0, "end": 41406.0,
     "time_label": "11:30:00", "duration": 6.0,
     "source_filename": "ben_ucpk2_2026-08-13_11-49-00_sid9db9293e82b94a4d9611572b1233f82d_c0000_bn4_off0.0_to114.0_srcwav.json", "chunk_start": 12.5},
    {"speaker": "spk_1", "text": "yep", "start": 41410.0, "end": 41413.0,
     "time_label": "11:30:10", "duration": 3.0,
     "source_filename": "ben_ucpk2_2026-08-13_11-49-00_sid9db9293e82b94a4d9611572b1233f82d_c0000_bn4_off0.0_to114.0_srcwav.json", "chunk_start": 22.0},
]}


class FakeConn:
    #: Rows the speaker-group lookup should return, as `(source_filename, label, group)`.
    #: Empty by default: these tests are about NAMES, and a session with no groups is the
    #: state they were all written against.
    #:
    #: It needs a cursor at all because `_apply_speaker_names` now also reads the re-bind
    #: mapping. That read fails OPEN and logs — so without this the tests would pass through
    #: the exception path, which is passing for the wrong reason: the name overlay would be
    #: exercised while the code under test never ran its normal branch.
    groups = ()

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def cursor(self, row_factory=None):
        return self

    def execute(self, sql, params=None):
        self._rows = [{"source_filename": f, "speaker_label": l, "group_label": g}
                      for f, l, g in self.groups]
        return self

    def fetchall(self):
        return getattr(self, "_rows", [])

    def fetchone(self):
        return None


@pytest.fixture
def wired(monkeypatch):
    monkeypatch.setattr(org, "SPEAKER_IDENTITY_MODE", "shadow")
    monkeypatch.setattr(org, "get_connection", lambda *a, **k: FakeConn())
    monkeypatch.setattr(org.users, "get_user_by_sub", lambda conn, sub: dict(CALLER))
    monkeypatch.setattr(org, "_resolve_org_media_folder",
                        lambda conn, caller, user, what=None: ("Ada_L", None))
    monkeypatch.setattr(org, "_read_org_transcripts",
                        # `conn=` is passed since the reader learned to consult the deletion tombstones.
                        lambda date, folder, s, e, conn=None: json.loads(json.dumps(SEGS)))
    monkeypatch.setattr(org.voiceprints, "live_turn_names", lambda conn, co, base: [
        {"turn_ref": "ben_ucpk2_2026-08-13_11-49-00_sid9db9293e82b94a4d9611572b1233f82d_c0000_bn4_off0.0_to114.0_srcwav.wav@12.5", "display_name": "Ben L",
         "state": "tentative", "source": "correction_propagation",
         "created_at": "2026-08-13T01:00:00", "cluster_ref": "C1"},
        {"turn_ref": "vanished.wav@3.0", "display_name": "Zoe", "state": "confirmed",
         "source": "correction", "created_at": "2026-08-13T01:00:00", "cluster_ref": "C2"},
    ])


def _get():
    ev = {"httpMethod": "GET", "path": "/api/org/transcripts",
          "queryStringParameters": {"date": "2026-08-13", "user": "Ada_L"},
          "body": None, "requestContext": {"authorizer": {"claims": {"sub": "sub-1"}}}}
    return json.loads(org.lambda_handler(ev, None)["body"])


def test_a_stored_name_reaches_the_segment_it_belongs_to(wired):
    segs = _get()["speaker_segments"]
    assert segs[0]["speaker_name"] == "Ben L"
    assert "speaker_name" not in segs[1], "an unnamed turn was given a name"


def test_the_state_travels_with_the_name(wired):
    """A caller holding a bare name cannot tell tentative from confirmed, and tentative must
    not leave the viewer."""
    assert _get()["speaker_segments"][0]["speaker_state"] == "tentative"


def test_a_name_with_nowhere_to_land_is_counted_in_the_response(wired):
    """`vanished.wav` matches no turn — a name the user set that is no longer being shown.
    Reported, because silence reads as 'never named'."""
    assert _get()["unmatchedNames"] == 1


def test_the_raw_diariser_label_is_still_there_and_still_not_a_name(wired):
    """`spk_0` is not a name and must never be rendered as one; the overlay adds a field
    rather than overwriting the label the viewer already knows how to hide."""
    segs = _get()["speaker_segments"]
    assert segs[0]["speaker"] == "spk_0"
    assert segs[1]["speaker"] == "spk_1"


def test_switching_the_feature_off_returns_the_old_response_exactly(wired, monkeypatch):
    """The rollback, at the read end. Not 'names stop appearing' — the response must be what
    it was before the feature existed, including the absence of the new keys."""
    monkeypatch.setattr(org, "SPEAKER_IDENTITY_MODE", "off")
    out = _get()
    assert "unmatchedNames" not in out
    assert all("speaker_name" not in s for s in out["speaker_segments"])


def test_one_assertion_does_not_confirm_a_neighbouring_turn(wired, monkeypatch):
    """The defect real data found: the user marked the turn at 12.5, and the turn at 12.3 —
    a different speaker — came back confirmed too, because it sits inside the tolerance and
    claimed the same row. A wrong CONFIRMED name is the failure this layer exists to avoid."""
    base = ("ben_ucpk2_2026-08-13_11-49-00_sid9db9293e82b94a4d9611572b1233f82d"
            "_c0000_bn4_off0.0_to114.0_srcwav.json")
    monkeypatch.setattr(org, "_read_org_transcripts", lambda d, f, a, b, **kw: {
        "text": "", "segments": [], "speaker_segments": [
            {"speaker": "spk_1", "text": "Go.", "start": 1.0, "end": 1.4,
             "time_label": "x", "duration": 0.4,
             "source_filename": base, "chunk_start": 12.3},
            {"speaker": "spk_0", "text": "hello", "start": 2.0, "end": 8.0,
             "time_label": "y", "duration": 6.0,
             "source_filename": base, "chunk_start": 12.5},
        ]})
    monkeypatch.setattr(org.voiceprints, "live_turn_names", lambda conn, co, s: [
        {"turn_ref": base[:-5] + ".wav@12.5", "display_name": "Ben L",
         "state": "confirmed", "source": "correction",
         "created_at": "2026-08-13T01:00:00", "cluster_ref": None}])
    out = _get()
    assert "speaker_segments" in out, out
    segs = out["speaker_segments"]
    assert segs[1]["speaker_name"] == "Ben L"
    assert "speaker_name" not in segs[0], (
        "the assertion bled onto the neighbouring turn and confirmed a name nobody gave")
