"""Unit: the legacy gateway must not serve a recording the customer deleted.

`fieldsight-prod-api` is the OLD gateway and it is still live — 44 invocations in the 24h
before this was written. Every content endpoint in it lists S3 objects directly and none
of them knew anything about the customer-facing delete, so a deleted recording was still
readable there: its transcripts, its audio, its video, a presigned URL for any of them,
and the pre-deletion `daily_report.json` served byte for byte.

Nothing had *called* those endpoints recently — the live traffic is `/api/actions`, which
returns only check-off state and no content — so this was **reachable** rather than
actively leaking. Reachable is enough: the promise made to the customer was that it is gone.

`/api/search` and `/api/ask` need nothing: they proxy to the ask agent and rag-search,
which filter on their own side.

The mirror rather than the database, because this lambda has no Aurora connection — the
same position the report generator and the ask agent are in.
"""
import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(
    os.path.abspath(__file__)))), "src"))

api = pytest.importorskip("lambda_fieldsight_api")

FOLDER, DATE = "Ben_UCPK2", "2026-08-17"
GONE = "sid-gone"
KEPT = "sid-keep"


@pytest.fixture(autouse=True)
def _mirror(monkeypatch):
    """One deleted session for (FOLDER, DATE); nothing anywhere else."""
    def _sessions(s3, bucket, folder, date):
        return {GONE} if (folder == FOLDER and date == DATE) else set()

    monkeypatch.setattr(api.deletion_mirror, "deleted_sessions", _sessions)


def test_the_helper_matches_on_the_session_id_inside_the_filename(monkeypatch):
    """The id sits INSIDE the filename (`…_sid{32hex}_c0001.json`), not as a path
    component — same rule as deletion_mirror.drop_deleted, deliberately."""
    bases = api._deleted_bases(FOLDER, DATE)
    keys = [f"transcripts/{FOLDER}/{DATE}/a_{GONE}_c0001.json",
            f"transcripts/{FOLDER}/{DATE}/a_{KEPT}_c0001.json"]
    kept = api._drop_deleted_keys(keys, bases)
    assert kept == [keys[1]]


def test_an_unreadable_mirror_serves_rather_than_breaks(monkeypatch):
    """Fails OPEN, like every guard on this path: an unreadable mirror must not take the
    timeline down for everyone. It still logs — 'nothing was deleted' and 'could not
    check' are otherwise indistinguishable afterwards."""
    def _boom(*a, **k):
        raise RuntimeError("s3 down")

    monkeypatch.setattr(api.deletion_mirror, "deleted_sessions", _boom)
    assert api._deleted_bases(FOLDER, DATE) == set()


def test_a_missing_folder_or_date_asks_nothing(monkeypatch):
    called = []
    monkeypatch.setattr(api.deletion_mirror, "deleted_sessions",
                        lambda *a, **k: called.append(a) or set())
    assert api._deleted_bases(None, DATE) == set()
    assert api._deleted_bases(FOLDER, None) == set()
    assert called == []


# ---- presign: the key is all there is to go on ------------------------

@pytest.mark.parametrize("key,expected", [
    (f"audio_segments/{FOLDER}/{DATE}/x_{GONE}_c1.wav", True),
    (f"audio_segments/{FOLDER}/{DATE}/x_{KEPT}_c1.wav", False),
    (f"transcripts/{FOLDER}/{DATE}/x_{GONE}_c1.json", True),
    (f"web_video/{FOLDER}/{DATE}/x_{GONE}.mp4", True),
    # users/{folder}/{kind}/{date}/… — the date is the FOURTH segment, not the third.
    (f"users/{FOLDER}/audio/{DATE}/x_{GONE}.wav", True),
    # reports/{date}/{folder}/… — reversed. Reading these in the other order takes the
    # date as the folder and every lookup silently misses.
    (f"reports/{DATE}/{FOLDER}/x_{GONE}.json", True),
    ("config/user_mapping.json", False),
    (f"audio_segments/{FOLDER}/not-a-date/x_{GONE}.wav", False),
])
def test_presign_recognises_a_deleted_recordings_key(key, expected):
    assert api._presign_key_is_deleted(key) is expected


def test_presign_refuses_with_404_not_403(monkeypatch):
    """403 confirms the object exists. org-api made the same choice, for the same reason."""
    monkeypatch.setattr(api, "can_access_user_data", lambda *a, **k: True)
    res = api.get_presigned_url(
        {"key": f"audio_segments/{FOLDER}/{DATE}/x_{GONE}_c1.wav"},
        {"role": "admin", "sub": "s"})
    assert res["statusCode"] == 404


def test_presign_still_serves_a_kept_recording(monkeypatch):
    """The guard must not take the feature down for the recordings nobody deleted."""
    class _S3:
        def generate_presigned_url(self, *a, **k):
            return "https://signed"

    monkeypatch.setattr(api, "s3_client", _S3())
    monkeypatch.setattr(api, "can_access_user_data", lambda *a, **k: True)
    res = api.get_presigned_url(
        {"key": f"audio_segments/{FOLDER}/{DATE}/x_{KEPT}_c1.wav"},
        {"role": "admin", "sub": "s"})
    assert res["statusCode"] == 200


# ---- the call sites are wired, not just the helpers -------------------

def test_every_content_endpoint_consults_the_guard():
    """Helpers that exist and are never called is a shape this repo has shipped. Pinned by
    reading the source, because deleting a call site broke no test the last time."""
    src = open(os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(
        os.path.abspath(__file__)))), "src", "lambda_fieldsight_api.py"),
        encoding="utf-8").read()
    for fn in ("get_timeline", "get_transcripts", "get_audio_segments",
               "get_video_segments"):
        body = src.split(f"def {fn}(", 1)[1].split("\ndef ", 1)[0]
        assert "_deleted_bases(" in body, f"{fn} never asks about deletions"
    presign = src.split("def get_presigned_url(", 1)[1].split("\ndef ", 1)[0]
    assert "_presign_key_is_deleted(" in presign
