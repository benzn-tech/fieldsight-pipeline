"""Unit: the brief finally has a reader.

`lambda_session_finalize` has written `session_brief/{folder}/{date}/sid{id}/latest.json`
since the brief shipped, and nothing in `src/` has ever read one. The whole narrative — the
sections with timestamps and verbatim quotes, the entities with the spellings the transcriber
actually produced, the tasks with the reason each exists — went to S3 and stopped.

Shaped deliberately like `session_rolling` beside it, so the tests below are mostly about the
two things that shape shares: the key is rebuilt server-side, and "not written yet" is a state
rather than an error.
"""
import json

import pytest

org = pytest.importorskip("lambda_org_api")

BRIEF = {"headline": "h", "sections": [{"title": "s", "bullets": []}],
         "entities": [{"name": "PB Tech", "aliases": ["PV Tech"]}],
         "tasks": [{"text": "t", "why": "because"}], "stats": {"turns": 3}}
SID = "be4190877c1d47f9b848a25cf4ca729a"


class _Body:
    def __init__(self, payload):
        self.payload = payload

    def read(self):
        return json.dumps(self.payload).encode("utf-8")


class _S3:
    def __init__(self, store):
        self.store, self.asked = store, []

    def get_object(self, Bucket, Key):
        self.asked.append(Key)
        if Key not in self.store:
            from botocore.exceptions import ClientError
            raise ClientError({"Error": {"Code": "NoSuchKey"}}, "GetObject")
        return {"Body": _Body(self.store[Key])}


def _event(**q):
    return {"queryStringParameters": q}


@pytest.fixture
def wired(monkeypatch):
    store = {f"session_brief/Ben_UCPK2/2026-08-27/sid{SID}/latest.json": BRIEF}
    s3 = _S3(store)
    monkeypatch.setattr(org, "s3", lambda: s3)
    monkeypatch.setattr(org, "_resolve_org_media_folder",
                        lambda conn, caller, user, what=None: ("Ben_UCPK2", None))
    return s3


def test_it_returns_the_whole_brief(wired):
    res = org.session_brief_read(None, {}, SID, _event(date="2026-08-27", user="Ben_UCPK2"))
    body = json.loads(res["body"])
    assert body["status"] == "ready"
    # Every field, because each one exists for a reader and trimming here would repeat the
    # mistake that made this endpoint necessary.
    for key in ("headline", "sections", "entities", "tasks", "stats"):
        assert key in body, key
    assert body["entities"][0]["aliases"] == ["PV Tech"], (
        "the aliases are the answer to the original failure and must survive the hop")
    assert body["tasks"][0]["why"] == "because"


def test_a_session_id_that_already_carries_sid_is_not_double_prefixed(wired):
    """The caller may hold either spelling. Prefixing an already-prefixed id reads
    `sidsid...`, a key that cannot exist — which surfaces as `pending` and looks like "no
    brief yet" rather than like a mistake."""
    org.session_brief_read(None, {}, "sid" + SID, _event(date="2026-08-27", user="Ben_UCPK2"))
    assert wired.asked[-1] == f"session_brief/Ben_UCPK2/2026-08-27/sid{SID}/latest.json"


def test_a_missing_brief_is_pending_not_an_error(wired):
    res = org.session_brief_read(None, {}, "0" * 32, _event(date="2026-08-27", user="x"))
    assert res["statusCode"] == 200
    assert json.loads(res["body"]) == {"status": "pending"}


def test_the_folder_comes_from_the_resolver_never_from_the_query(monkeypatch):
    """The key is rebuilt server-side. A folder taken from the request would be a reader for
    somebody else's meetings — the same rule `session_rolling` states beside it."""
    s3 = _S3({})
    monkeypatch.setattr(org, "s3", lambda: s3)
    monkeypatch.setattr(org, "_resolve_org_media_folder",
                        lambda conn, caller, user, what=None: ("Authorised_Folder", None))
    org.session_brief_read(None, {}, SID, _event(date="2026-08-27", user="Someone_Else"))
    assert "Authorised_Folder" in s3.asked[0]
    assert "Someone_Else" not in s3.asked[0]


def test_a_refusal_from_the_resolver_is_returned_unchanged(monkeypatch):
    monkeypatch.setattr(org, "_resolve_org_media_folder",
                        lambda conn, caller, user, what=None: (None, org.error("nope", 403)))
    res = org.session_brief_read(None, {}, SID, _event(date="2026-08-27", user="x"))
    assert res["statusCode"] == 403


@pytest.mark.parametrize("date", [None, "", "27-08-2026", "2026-8-27", "yesterday"])
def test_a_bad_date_is_refused_before_any_read(monkeypatch, date):
    s3 = _S3({})
    monkeypatch.setattr(org, "s3", lambda: s3)
    res = org.session_brief_read(None, {}, SID, _event(**({"date": date} if date else {})))
    assert res["statusCode"] == 400
    assert not s3.asked, "the endpoint reached S3 with an unvalidated date in the key"


def test_the_route_is_wired():
    """A handler nothing dispatches to is the shape this session keeps removing."""
    src = open("src/lambda_org_api.py", encoding="utf-8").read()
    assert r'^/sessions/([^/]+)/brief$' in src
    assert "return session_brief_read(conn, caller, m_sbr.group(1), event)" in src
