"""Every /sessions/{id}/… read route refuses an id that is not a session.

The routes match `([^/]+)` and build S3 keys from that segment. The day-scoped
report writes its result under `session_report_results/{folder}/{date}/day/…`,
so without this guard `GET /sessions/day/report/status` resolves to a DAY
document and checks deletion only for the literal session "day" — which is in
no mirror. Spec 2026-09-15 §11, finding F2.
"""
import pytest

org = pytest.importorskip("lambda_org_api", reason="requires psycopg (installed in CI)")
import session_scope  # noqa: E402

CALLER = {
    "id": "u-uuid-1", "cognito_sub": "sub-1", "company_id": "c-uuid-1",
    "email": "a@x.nz", "first_name": "Ada", "last_name": "L", "folder_name": "Ada_L",
    "avatar_s3_key": None, "global_role": "admin", "created_at": "2026-07-25",
}

ROUTES = [
    ("POST", "report/preview", "session_report_preview"),
    ("POST", "report", "session_report_generate"),
    ("GET", "report/status", "session_report_status"),
    ("GET", "rolling", "session_rolling"),
    ("GET", "brief", "session_brief_read"),
]

BAD = ["day", "..", "latest", "sid" + "a" * 31, "sid" + "A" * 32, "grp" + "z" * 32, "sidday"]
GOOD = ["sid" + "a" * 32, "a" * 32, "grp" + "b" * 32,
        "Benl1_2026-07-25_13-00-11", "ben_ucpk2_2026-09-02_10-55-11"]


class FakeConn:
    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        return False


def _event(method, path):
    return {"httpMethod": method, "path": path, "queryStringParameters": {},
            "body": None, "requestContext": {"authorizer": {"claims": {"sub": "sub-1"}}}}


@pytest.fixture
def routed(monkeypatch):
    calls = []
    monkeypatch.setattr(org, "get_connection", lambda *a, **k: FakeConn())
    monkeypatch.setattr(org.users, "get_user_by_sub",
                        lambda conn, sub: dict(CALLER) if sub == "sub-1" else None)
    for _, _, name in ROUTES:
        def handler(conn, caller, sid, event, _n=name):
            calls.append((_n, sid))
            return {"statusCode": 200, "body": "{}"}
        monkeypatch.setattr(org, name, handler)
    return calls


@pytest.mark.parametrize("method,suffix,name", ROUTES, ids=[r[1] for r in ROUTES])
@pytest.mark.parametrize("bad", BAD)
def test_a_non_session_id_is_refused_before_any_handler_runs(routed, method, suffix, name, bad):
    res = org.lambda_handler(_event(method, f"/api/org/sessions/{bad}/{suffix}"), None)
    assert res["statusCode"] == 400, f"{name} accepted {bad!r}"
    assert routed == [], f"{name} ran for {bad!r}"


@pytest.mark.parametrize("method,suffix,name", ROUTES, ids=[r[1] for r in ROUTES])
@pytest.mark.parametrize("good", GOOD)
def test_every_real_session_spelling_still_reaches_its_handler(routed, method, suffix, name, good):
    res = org.lambda_handler(_event(method, f"/api/org/sessions/{good}/{suffix}"), None)
    assert res["statusCode"] == 200
    assert routed == [(name, good)]


def test_is_session_base_names_the_four_spellings():
    for good in GOOD:
        assert session_scope.is_session_base(good), good
    for bad in BAD + ["", None]:
        assert not session_scope.is_session_base(bad), bad
