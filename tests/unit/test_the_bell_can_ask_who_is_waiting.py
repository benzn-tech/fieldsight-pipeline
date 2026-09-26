"""GET /api/org/name-proposals — the two shapes, and why they are two.

The bell polls this from every page. If the badge's query read transcripts, every page load
in the product would pay for a handful of S3 reads to render a number — so the countless
shape must stay countless, and a test is the only thing that keeps it that way once somebody
adds "just the first line of text" to the summary.

The second shape exists for the opposite reason. A row that says `spk_1 @ 2026-09-22 / 41.2s`
is an identifier, not a question: nobody can answer it without listening, and most people
will not. The words place the passage well enough to decide whether it is worth playing.
"""
import json

import pytest

org = pytest.importorskip("lambda_org_api", reason="requires psycopg (installed in CI)")

CO = "11111111-1111-1111-1111-111111111111"
VP = "22222222-2222-2222-2222-222222222222"
SRC = "Benl1_2026-08-13_11-49-00_off0.0_to60.0_srcwav.json"
SESSION = "Benl1_2026-08-13_11-49-00_sid9db9293e82b94a4d9611572b1233f82d"

CALLER = {"id": "u-1", "cognito_sub": "sub-1", "company_id": CO, "email": "a@x.nz",
          "first_name": "Ada", "last_name": "L", "folder_name": "Ada_L",
          "avatar_s3_key": None, "global_role": "site_manager", "created_at": "2026-08-13"}


class FakeConn:
    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def cursor(self, row_factory=None):
        return self

    def execute(self, sql, params=None):
        return self

    def fetchone(self):
        return None


def _event(qs=None, sub="sub-1"):
    return {"httpMethod": "GET", "path": "/api/org/name-proposals",
            "queryStringParameters": qs, "body": None,
            "requestContext": {"authorizer": {"claims": {"sub": sub}}}}


def _body(resp):
    return json.loads(resp["body"])


@pytest.fixture
def wired(monkeypatch):
    reads = []
    monkeypatch.setattr(org, "SPEAKER_IDENTITY_MODE", "on")
    monkeypatch.setattr(org, "get_connection", lambda: FakeConn())
    monkeypatch.setattr(org.users, "get_user_by_sub", lambda conn, sub: dict(CALLER))
    monkeypatch.setattr(org.speaker_name_proposals, "pending_by_person",
                        lambda conn, co: [
                            {"voiceprint_id": VP, "display_name": "Ben Lin",
                             "pending": 4, "newest": "2026-09-25T10:00:00Z"},
                            {"voiceprint_id": "vp-2", "display_name": "Sam Yu",
                             "pending": 1, "newest": "2026-09-24T09:00:00Z"}])
    monkeypatch.setattr(org.speaker_name_proposals, "pending_for_person",
                        lambda conn, co, vp, limit=5: [
                            {"id": "p1", "session_base": SESSION, "source_filename": SRC,
                             "speaker_label": "spk_0", "user_folder": "Ben_UCPK",
                             "session_date": "2026-08-13", "score": 0.5123},
                            {"id": "p2", "session_base": SESSION, "source_filename": SRC,
                             "speaker_label": "spk_1", "user_folder": "Ben_UCPK",
                             "session_date": "2026-08-13", "score": None}])

    def turns(conn, folder, date, sb):
        reads.append((folder, date, sb))
        return [
            {"source_filename": SRC, "speaker_label": "spk_0",
             "start_sec": 0.0, "end_sec": 4.0, "text": "short one"},
            {"source_filename": SRC, "speaker_label": "spk_0",
             "start_sec": 9.0, "end_sec": 31.0, "text": "the long one, which is the passage"},
            {"source_filename": SRC, "speaker_label": "spk_1",
             "start_sec": 40.0, "end_sec": 55.0, "text": "somebody else entirely"},
        ]

    monkeypatch.setattr(org, "_session_turns", turns)
    return reads


def test_the_badge_shape_counts_people_not_clips(wired):
    b = _body(org.lambda_handler(_event(), None))
    assert b["total"] == 5
    assert [p["displayName"] for p in b["people"]] == ["Ben Lin", "Sam Yu"]
    assert b["people"][0]["pending"] == 4


def test_the_badge_shape_never_reads_a_transcript(wired):
    """The bell polls this from every page. A transcript read here is an S3 round trip on
    every page load in the product, to render a number."""
    org.lambda_handler(_event(), None)
    assert wired == [], (
        f"the summary read {len(wired)} transcript(s); it must answer from one grouped "
        f"query, or the badge costs an S3 fetch per page view")


def test_asking_about_one_person_carries_the_words(wired):
    b = _body(org.lambda_handler(_event({"voiceprint": VP}), None))
    assert b["voiceprintId"] == VP
    texts = [p["text"] for p in b["proposals"]]
    assert "the long one, which is the passage" in texts, (
        "without the words, a proposal row is an identifier and not a question")


def test_the_words_are_the_longest_turn_of_that_cluster(wired):
    """The same passage a confirmation would mark. Showing a different one would put the
    decision on words the decision does not land on."""
    p = _body(org.lambda_handler(_event({"voiceprint": VP}), None))["proposals"][0]
    assert (p["startSec"], p["endSec"]) == (9.0, 31.0)
    # And the folder, or the passage has words but no voice: the dialog builds the audio
    # key from it, and the voice is the thing being judged.
    assert p["userFolder"] == "Ben_UCPK"
    assert p["text"] == "the long one, which is the passage"


def test_one_transcript_is_read_once_however_many_candidates_share_it(wired):
    """Candidates cluster in sessions by construction — several of a person's passages come
    from the same recording — so a naive loop fetches the same transcript repeatedly."""
    org.lambda_handler(_event({"voiceprint": VP}), None)
    assert len(wired) == 1, f"read the same session {len(wired)} times"


def test_a_missing_score_stays_absent_rather_than_becoming_zero(wired):
    """Zero is a similarity somebody could act on. Absent is the truth."""
    ps = _body(org.lambda_handler(_event({"voiceprint": VP}), None))["proposals"]
    assert ps[0]["score"] == 0.512
    assert ps[1]["score"] is None


def test_a_worker_cannot_see_who_is_waiting(wired, monkeypatch):
    monkeypatch.setattr(org.users, "get_user_by_sub",
                        lambda conn, sub: dict(CALLER, global_role="worker"))
    assert org.lambda_handler(_event(), None)["statusCode"] == 403


def test_the_route_is_gone_when_the_feature_is_off(wired, monkeypatch):
    monkeypatch.setattr(org, "SPEAKER_IDENTITY_MODE", "off")
    assert org.lambda_handler(_event(), None)["statusCode"] == 404
