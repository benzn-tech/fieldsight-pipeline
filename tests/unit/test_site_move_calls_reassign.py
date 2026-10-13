"""patch_org_site hands a moved site's lake objects to lake_objects.reassign_site.

The SQL is covered on a real Postgres in tests/integration/test_site_move_moves_lake_objects.py;
here the ORDER and the conditions are pinned with fakes.
"""
import json

import pytest

org = pytest.importorskip("lambda_org_api", reason="requires psycopg (installed in CI)")

C_OLD = "6a23c57c-5fa3-4ef4-a93c-88e9543272fc"
C_NEW = "7a495d8a-c88a-43ea-bf5b-a6d1c89beb92"
CALLER = {"id": "u-1", "cognito_sub": "sub-1", "company_id": C_OLD, "email": "a@x.nz",
          "first_name": "P", "last_name": "A", "avatar_s3_key": None,
          "global_role": "platform_admin", "created_at": "2026-07-04"}


class Conn:
    def __init__(self):
        self.rolled_back = False

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def rollback(self):
        self.rolled_back = True


@pytest.fixture
def world(monkeypatch):
    log, conn = [], Conn()
    monkeypatch.setattr(org, "get_connection", lambda *a, **k: conn)
    monkeypatch.setattr(org.users, "get_user_by_sub", lambda c, sub: dict(CALLER))
    monkeypatch.setattr(org.sites, "get_site",
                        lambda c, sid: {"id": sid, "company_id": C_OLD, "slug": "s"})
    monkeypatch.setattr(org.sites, "update_site",
                        lambda c, sid, cid, **kw: {"id": sid, "company_id": cid, "icon_s3_key": None})
    monkeypatch.setattr(org.companies, "get_company_by_id", lambda c, cid: {"id": cid})
    monkeypatch.setattr(org.sites, "get_company_site_by_slug", lambda *a: None)
    monkeypatch.setattr(org, "_publish_site_coords", lambda row: log.append("publish"))

    def move(c, sid, frm, to):
        log.append(("move", sid, frm, to))
        return {"id": sid, "company_id": to, "icon_s3_key": None}
    monkeypatch.setattr(org.sites, "move_site_company", move)

    def reassign(c, sid, cid):
        log.append(("reassign", sid, cid))
        return 3
    monkeypatch.setattr(org.lake_objects, "reassign_site", reassign)
    return monkeypatch, log, conn


def _patch(body):
    ev = {"httpMethod": "PATCH", "path": "/api/org/sites/s-1", "queryStringParameters": None,
          "body": json.dumps(body),
          "requestContext": {"authorizer": {"claims": {"sub": "sub-1"}}}}
    return org.lambda_handler(ev, None)


def test_reassign_runs_once_after_the_move_and_before_publish(world):
    _, log, _ = world
    assert _patch({"target_company_id": C_NEW})["statusCode"] == 200
    assert log == [("move", "s-1", C_OLD, C_NEW), ("reassign", "s-1", C_NEW), "publish"]


def test_a_name_only_edit_does_not_reassign(world):
    _, log, _ = world
    assert _patch({"name": "Renamed"})["statusCode"] == 200
    assert not any(isinstance(e, tuple) and e[0] == "reassign" for e in log)


def test_a_lost_race_rolls_back_and_does_not_reassign(world):
    mp, log, conn = world
    mp.setattr(org.sites, "move_site_company", lambda *a: None)
    assert _patch({"target_company_id": C_NEW})["statusCode"] == 404
    assert conn.rolled_back is True and log == []


def test_a_reassign_failure_rolls_the_whole_move_back(world):
    mp, log, conn = world

    def boom(*a):
        raise RuntimeError("db went away")
    mp.setattr(org.lake_objects, "reassign_site", boom)
    res = _patch({"target_company_id": C_NEW})
    assert res["statusCode"] == 500
    assert conn.rolled_back is True and "publish" not in log
