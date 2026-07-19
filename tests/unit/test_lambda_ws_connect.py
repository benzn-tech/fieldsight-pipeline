import pytest

conn_mod = pytest.importorskip("lambda_ws_connect", reason="requires psycopg import path")
disc_mod = pytest.importorskip("lambda_ws_disconnect", reason="requires psycopg import path")


class _FakeConn:
    def __enter__(self): return self
    def __exit__(self, *a): return False


def _event(connection_id="conn-1", sub="sub-1"):
    return {"requestContext": {"connectionId": connection_id,
                               "authorizer": {"sub": sub} if sub else {}}}


def test_connect_upserts_for_provisioned_user(monkeypatch):
    monkeypatch.setattr(conn_mod, "get_connection", lambda *a, **k: _FakeConn())
    monkeypatch.setattr(conn_mod.users, "get_user_by_sub",
                        lambda c, sub: {"id": "u-1", "company_id": "c-1"})
    captured = {}
    monkeypatch.setattr(conn_mod.ws_connections, "upsert_connection",
                        lambda c, cid, uid, coid: captured.update(cid=cid, uid=uid, coid=coid))
    res = conn_mod.lambda_handler(_event(), None)
    assert res["statusCode"] == 200
    assert captured == {"cid": "conn-1", "uid": "u-1", "coid": "c-1"}


def test_connect_refuses_unprovisioned(monkeypatch):
    monkeypatch.setattr(conn_mod, "get_connection", lambda *a, **k: _FakeConn())
    monkeypatch.setattr(conn_mod.users, "get_user_by_sub", lambda c, sub: None)
    called = {"upsert": False}
    monkeypatch.setattr(conn_mod.ws_connections, "upsert_connection",
                        lambda *a, **k: called.__setitem__("upsert", True))
    res = conn_mod.lambda_handler(_event(), None)
    assert res["statusCode"] == 403 and called["upsert"] is False


def test_connect_missing_sub_401(monkeypatch):
    monkeypatch.setattr(conn_mod, "get_connection", lambda *a, **k: _FakeConn())
    res = conn_mod.lambda_handler(_event(sub=None), None)
    assert res["statusCode"] == 401


def test_disconnect_deletes_row(monkeypatch):
    monkeypatch.setattr(disc_mod, "get_connection", lambda *a, **k: _FakeConn())
    captured = {}
    monkeypatch.setattr(disc_mod.ws_connections, "delete_connection",
                        lambda c, cid: captured.update(cid=cid))
    res = disc_mod.lambda_handler({"requestContext": {"connectionId": "conn-9"}}, None)
    assert res["statusCode"] == 200 and captured == {"cid": "conn-9"}


def test_disconnect_never_fails(monkeypatch):
    monkeypatch.setattr(disc_mod, "get_connection",
                        lambda *a, **k: (_ for _ in ()).throw(RuntimeError("db down")))
    res = disc_mod.lambda_handler({"requestContext": {"connectionId": "conn-9"}}, None)
    assert res["statusCode"] == 200
