"""The day's report photographs, and the person's choice of what to leave out.

Owner, 2026-10-01: past 120 photographs the person chooses what to leave out
of the day's reports; the choice is kept for the day (S3) and both report
paths follow it. Only the person who took them, or a company admin, chooses.

THE test is `a choice is saved for the day and read back by the same rule the
reports use`.
"""
import io
import json

import pytest

import lambda_org_api as org
import report_photos

DATE = "2026-10-01"
FOLDER = "Ben_Lin_test2"
PHOTOS = [{"s3_key": "users/%s/pictures/%s/%s" % (FOLDER, DATE, n)}
          for n in ("ben_lin_test2_2026-10-01_13-25-06.jpg", "ben_lin_test2_2026-10-01_13-28-15.jpg")]


class FakeConn:
    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def transaction(self):
        return self


class NoSuchKey(Exception):
    pass


class S3:
    exceptions = type("E", (), {"NoSuchKey": NoSuchKey})

    def __init__(self):
        self.objects = {}

    def get_object(self, Bucket, Key):
        if Key not in self.objects:
            raise NoSuchKey(Key)
        return {"Body": io.BytesIO(self.objects[Key])}

    def put_object(self, Bucket, Key, Body, **kw):
        self.objects[Key] = Body

    def generate_presigned_url(self, op, Params, ExpiresIn):
        return "https://signed/" + Params["Key"].rsplit("/", 1)[-1]


@pytest.fixture
def api(monkeypatch):
    store = S3()
    caller = {"id": "u1", "company_id": "c1", "global_role": "worker", "cognito_sub": "s1"}
    monkeypatch.setattr(org, "get_connection", lambda *a, **k: FakeConn())
    monkeypatch.setattr(org.users, "get_user_by_sub", lambda conn, sub: dict(caller))
    monkeypatch.setattr(org, "_resolve_org_media_folder", lambda conn, c, user, what="": (FOLDER, None))
    monkeypatch.setattr(org.scope, "visible_scope", lambda conn, c: {"self_folder": caller.get("self", FOLDER)})
    monkeypatch.setattr(org, "_day_photo_block", lambda conn, c, f, d: list(PHOTOS))
    monkeypatch.setattr(org.location_markers, "for_day", lambda *a: [
        {"at": "13:24", "location": "Ground floor"}, {"at": "13:26", "location": "Level 1"}])
    monkeypatch.setattr(org, "s3", lambda: store)
    monkeypatch.setattr(org, "LAKE_BUCKET", "lake")

    def call(method, body=None, **who):
        caller.update(who)
        ev = {"httpMethod": method, "path": "/api/org/days/%s/photos/selection" % DATE,
              "queryStringParameters": {"user": FOLDER},
              "body": json.dumps(body) if body is not None else None,
              "requestContext": {"authorizer": {"claims": {"sub": "s1"}}}}
        res = org.lambda_handler(ev, None)
        return res["statusCode"], json.loads(res["body"])
    return call, store


def test_THE_a_choice_is_saved_for_the_day_and_read_back_by_the_reports_rule(api):
    call, store = api
    code, body = call("PUT", {"excluded": ["ben_lin_test2_2026-10-01_13-28-15.jpg"]})
    assert code == 200 and body["included"] == 1
    assert report_photos.read_excluded(store, "lake", FOLDER, DATE) == \
        {"ben_lin_test2_2026-10-01_13-28-15.jpg"}
    code, body = call("GET")
    assert [(p["time"], p["place"], p["excluded"]) for p in body["photos"]] == [
        ("13:25", "ground floor", False), ("13:28", "level 1", True)]
    assert body["photos"][0]["url"] == "https://signed/ben_lin_test2_2026-10-01_13-25-06.jpg"


def test_the_body_says_when_photos_will_be_shrunk_or_must_be_chosen(api, monkeypatch):
    call, _ = api
    many = [{"s3_key": "users/%s/pictures/%s/p%03d.jpg" % (FOLDER, DATE, i)} for i in range(130)]
    monkeypatch.setattr(org, "_day_photo_block", lambda conn, c, f, d: many)
    code, body = call("GET")
    assert body["limits"] == {"pageSize": 60, "max": 120}
    assert body["shrunk"] is True and body["mustChoose"] is True
    code, body = call("PUT", {"excluded": ["p%03d.jpg" % i for i in range(15)]})
    assert body["included"] == 115 and body["mustChoose"] is False


def test_a_name_that_is_not_the_days_photograph_is_refused(api):
    call, store = api
    code, body = call("PUT", {"excluded": ["someone_else.jpg"]})
    assert code == 400 and "not a photograph of this day" in body["error"]
    assert store.objects == {}


def test_only_the_photographer_or_an_admin_chooses(api):
    call, store = api
    code, body = call("GET", self="Someone_Else", global_role="site_manager")
    assert code == 403
    code, body = call("PUT", {"excluded": []}, self="Someone_Else", global_role="pm")
    assert code == 403 and store.objects == {}
    code, _ = call("PUT", {"excluded": []}, self="Someone_Else", global_role="admin")
    assert code == 200
