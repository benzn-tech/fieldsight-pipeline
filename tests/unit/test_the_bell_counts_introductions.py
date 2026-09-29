"""GET /api/org/name-proposals gains `introductions`; GET /api/org/name-suggestions lists
them. Mirrors `test_the_bell_can_ask_who_is_waiting.py`'s reasoning for the badge: the bell
polls this from every page, so the countless shape must stay countless -- a transcript read
here is an S3 round trip on every page load in the product.
"""
import json

import pytest

org = pytest.importorskip("lambda_org_api", reason="requires psycopg (installed in CI)")

CO = "11111111-1111-1111-1111-111111111111"
SESSION = "sid" + "a" * 32
SRC = f"Ben1_2026-09-29_09-00-00_{SESSION}_c0000.json"

CALLER = {"id": "u-1", "cognito_sub": "sub-1", "company_id": CO, "email": "a@x.nz",
          "first_name": "Ada", "last_name": "L", "folder_name": "Ada_L",
          "avatar_s3_key": None, "global_role": "site_manager", "created_at": "2026-09-29"}

SUGGESTION_ROW = {"id": "s1", "heard_name": "Petros", "company_name": "Cassidy",
                  "quote": "Hi, this is Petros from Cassidy",
                  "session_date": "2026-09-29", "user_folder": "Ben1",
                  "session_base": SESSION, "source_filename": SRC,
                  "speaker_label": "spk_0", "start_sec": 0.0, "end_sec": 6.0,
                  "created_at": "2026-09-29T09:00:06Z"}


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


def _event(path, qs=None, method="GET", body=None, sub="sub-1"):
    return {"httpMethod": method, "path": path, "queryStringParameters": qs,
            "body": json.dumps(body) if body is not None else None,
            "requestContext": {"authorizer": {"claims": {"sub": sub}}}}


def _body(resp):
    return json.loads(resp["body"])


@pytest.fixture
def wired(monkeypatch):
    reads = []
    monkeypatch.setattr(org, "SPEAKER_IDENTITY_MODE", "on")
    monkeypatch.setattr(org, "get_connection", lambda: FakeConn())
    monkeypatch.setattr(org.users, "get_user_by_sub", lambda conn, sub: dict(CALLER))
    monkeypatch.setattr(org.speaker_name_proposals, "pending_by_person", lambda conn, co: [])
    monkeypatch.setattr(org.speaker_intro_suggestions, "pending_count", lambda conn, co: 3)
    monkeypatch.setattr(org.speaker_intro_suggestions, "pending",
                        lambda conn, co, limit=20: [dict(SUGGESTION_ROW)])

    def turns(conn, folder, date, sb, with_text=False):
        reads.append((folder, date, sb))
        return []

    monkeypatch.setattr(org, "_session_turns", turns)
    return reads


def test_the_badge_gains_introductions_and_keeps_people_and_total(wired):
    b = _body(org.lambda_handler(_event("/api/org/name-proposals"), None))
    assert b["introductions"] == 3
    assert b["people"] == []
    assert b["total"] == 0


def test_the_badge_never_reads_a_transcript_for_introductions(wired):
    org.lambda_handler(_event("/api/org/name-proposals"), None)
    assert wired == [], "the introductions count must come from one grouped query"


def test_the_list_returns_suggestions_straight_off_the_rows_no_transcript_no_score(wired):
    b = _body(org.lambda_handler(_event("/api/org/name-suggestions"), None))
    assert wired == []
    s = b["suggestions"][0]
    assert s["heardName"] == "Petros"
    assert s["companyName"] == "Cassidy"
    assert s["quote"] == "Hi, this is Petros from Cassidy"
    assert s["date"] == "2026-09-29"
    assert s["userFolder"] == "Ben1"
    assert s["sessionBase"] == SESSION
    assert s["sourceFilename"] == SRC
    assert s["speakerLabel"] == "spk_0"
    assert s["startSec"] == 0.0
    assert s["endSec"] == 6.0
    assert s["createdAt"] == "2026-09-29T09:00:06Z"
    assert "score" not in json.dumps(s).lower()


def test_the_list_is_gone_when_the_feature_is_off(wired, monkeypatch):
    monkeypatch.setattr(org, "SPEAKER_IDENTITY_MODE", "off")
    assert org.lambda_handler(_event("/api/org/name-suggestions"), None)["statusCode"] == 404


def test_a_worker_cannot_see_the_list(wired, monkeypatch):
    monkeypatch.setattr(org.users, "get_user_by_sub",
                        lambda conn, sub: dict(CALLER, global_role="worker"))
    assert org.lambda_handler(_event("/api/org/name-suggestions"), None)["statusCode"] == 403
