"""Tests for src/session_activity.py — open + touch the meeting_session lifecycle
from the chunk stream (§8.4). The pure core takes injected conn + resolvers + now;
meeting_session.ensure_open / touch_segment are monkeypatched to capture the
orchestration (their SQL is covered in test_meeting_session_repo.py)."""
from datetime import datetime

import pytest

sa = pytest.importorskip("session_activity", reason="requires psycopg (installed in CI)")

SID = "9f8c1e2a4b6d47f0a1b2c3d4e5f60718"
CHUNK_KEY = (f"transcripts/Ben_UCPK/2026-07-28/"
             f"Benl1_2026-07-28_14-03-00_sid{SID}_c0007_off3.0_to60.0_srcwav.json")
# The VAD sidecar — written for EVERY chunk, including one dropped as silent, so
# it is the touch source that does not depend on anybody speaking.
SIDECAR_KEY = (f"audio_segments/Ben_UCPK/2026-07-28/"
               f"Benl1_2026-07-28_14-03-00_sid{SID}_c0007_vad_metadata.json")
NOW = datetime(2026, 7, 28, 14, 4, 30)


@pytest.fixture
def captured(monkeypatch):
    calls = {"open": [], "touch": []}
    monkeypatch.setattr(sa.meeting_session, "ensure_open",
                        lambda conn, *a: calls["open"].append(a) or {"session_id": a[0]})
    monkeypatch.setattr(sa.meeting_session, "touch_segment",
                        lambda conn, sid, at: calls["touch"].append((sid, at)))
    return calls


def _resolvers(company={"id": "co-1"}, user="u-1"):
    return (lambda conn, folder: company,
            lambda conn, cid, folder: user)


def test_chunk_transcript_opens_and_touches_the_session(captured):
    rc, ru = _resolvers()
    out = sa.process_transcript_key(object(), CHUNK_KEY, resolve_company=rc,
                                    resolve_user=ru, now=NOW)
    assert out == SID
    # ensure_open(conn, sid, company_id, user_id, site=None, kind, opened_at)
    sid, company_id, user_id, site, kind, opened_at = captured["open"][0]
    assert sid == SID and company_id == "co-1" and user_id == "u-1"
    assert site is None                                   # no site pick in a chunk key
    assert kind == "audio"                                # from _srcwav
    assert opened_at == datetime(2026, 7, 28, 14, 3, 0)   # the chunk's device start (T1)
    # touch at SERVER now (skew-proof idle detection), NOT the device time
    assert captured["touch"] == [(SID, NOW)]


def test_legacy_whole_file_transcript_is_ignored(captured):
    rc, ru = _resolvers()
    legacy = "transcripts/Ben_UCPK/2026-07-28/Benl1_2026-07-28_14-03-00.json"
    assert sa.process_transcript_key(object(), legacy, resolve_company=rc,
                                     resolve_user=ru, now=NOW) is None
    assert captured["open"] == [] and captured["touch"] == []


def test_unresolvable_company_skips_without_opening(captured):
    rc = lambda conn, folder: None
    ru = lambda conn, cid, folder: "u-1"
    assert sa.process_transcript_key(object(), CHUNK_KEY, resolve_company=rc,
                                     resolve_user=ru, now=NOW) is None
    assert captured["open"] == []


def test_non_transcript_key_is_ignored(captured):
    rc, ru = _resolvers()
    # carries a sid but is neither a transcript nor a VAD sidecar -> not our concern
    key = f"extractions/Ben_UCPK/2026-07-28/sid{SID}.json"
    assert sa.process_transcript_key(object(), key, resolve_company=rc,
                                     resolve_user=ru, now=NOW) is None
    assert captured["open"] == []


def test_vad_sidecar_opens_and_touches_without_any_speech(captured):
    """The regression guard for dropping silent chunks (2026-08-08).

    Silence no longer produces a transcript, so a transcript-only touch stream
    goes quiet during a lull while the sweep's INFER_IDLE_CLOSE keeps counting —
    and the confirmation email goes out mid-meeting. The sidecar is written for
    every chunk whether or not anyone spoke, so it keeps the session alive."""
    rc, ru = _resolvers()
    out = sa.process_transcript_key(object(), SIDECAR_KEY, resolve_company=rc,
                                    resolve_user=ru, now=NOW)
    assert out == SID
    sid, company_id, user_id, site, kind, opened_at = captured["open"][0]
    assert sid == SID and company_id == "co-1" and user_id == "u-1"
    assert opened_at == datetime(2026, 7, 28, 14, 3, 0)   # same device start (T1)
    # The sidecar name carries no _src{ext} token, so kind is unknowable here.
    # ensure_open must COALESCE it, or a sidecar-first session is NULL forever.
    assert kind is None
    assert captured["touch"] == [(SID, NOW)]


def test_the_vad_segment_wav_itself_is_not_a_touch(captured):
    """Only the sidecar. The segment .wav under the same prefix already leads to
    a transcript, and counting both would touch twice for one chunk."""
    rc, ru = _resolvers()
    wav = (f"audio_segments/Ben_UCPK/2026-07-28/"
           f"Benl1_2026-07-28_14-03-00_sid{SID}_c0007_off3.0_to60.0_srcwav.wav")
    assert sa.process_transcript_key(object(), wav, resolve_company=rc,
                                     resolve_user=ru, now=NOW) is None
    assert captured["open"] == []


def test_legacy_sidecar_without_a_session_is_ignored(captured):
    rc, ru = _resolvers()
    legacy = ("audio_segments/Ben_UCPK/2026-07-28/"
              "Benl1_2026-07-28_14-03-00_vad_metadata.json")
    assert sa.process_transcript_key(object(), legacy, resolve_company=rc,
                                     resolve_user=ru, now=NOW) is None
    assert captured["open"] == [] and captured["touch"] == []


def test_unmapped_recorder_still_opens_with_null_user(captured):
    # resolve_user returns None (recorder not in the org users table) -> the session
    # still opens (recipient just won't resolve at finalize; the sweep marks it
    # no_recipient there), it is NOT dropped here.
    rc, _ = _resolvers()
    ru = lambda conn, cid, folder: None
    assert sa.process_transcript_key(object(), CHUNK_KEY, resolve_company=rc,
                                     resolve_user=ru, now=NOW) == SID
    assert captured["open"][0][2] is None                 # user_id None


def test_kind_from_key():
    assert sa._kind_from_key("x_sidABC_c0001_srcwav.json") == "audio"
    assert sa._kind_from_key("x_sidABC_c0001_srcmp4.json") == "video"
    assert sa._kind_from_key("x_sidABC_c0001.json") is None


def test_keys_from_event_reads_both_shapes():
    eb = {"detail": {"object": {"key": "transcripts/a/b/c.json"}}}
    assert sa._keys_from_event(eb) == ["transcripts/a/b/c.json"]
    s3 = {"Records": [{"s3": {"object": {"key": "transcripts/a/b/c+d.json"}}}]}
    assert sa._keys_from_event(s3) == ["transcripts/a/b/c d.json"]   # unquote_plus
