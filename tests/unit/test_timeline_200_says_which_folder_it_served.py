"""Unit: a /timeline 200 names the folder it served, in `user`.

The 404 envelopes already carry `user`. The 200s did not, so a client whose
caller passed no `user` (the server resolved the caller's own folder) had no way
to learn the folder. `user_name` cannot stand in: on the Aurora path it is a
display name ("Ben Lin") and deriving a folder from it yields "Ben_Lin", a
folder no directory row claims. Both success returns are covered: the
Aurora-built shape and the S3-verbatim report document.
"""
import json

import pytest

org = pytest.importorskip("lambda_org_api", reason="requires psycopg (installed in CI)")

SITE_ID = "a1a1a1a1-a1a1-a1a1-a1a1-a1a1a1a1a1a1"
DATE = "2026-09-17"
SELF_FOLDER = "Ben_Lin_test2"
CALLER = {
    "id": "u-1", "cognito_sub": "sub-1", "company_id": "c-1", "email": "a@x.nz",
    "first_name": "Ben", "last_name": "Lin", "folder_name": SELF_FOLDER,
    "avatar_s3_key": None, "global_role": "pm", "created_at": "2026-07-25",
}


class FakeConn:
    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


def body_of(res):
    return json.loads(res["body"])


def _event(user=None):
    qs = {"date": DATE}
    if user:
        qs["user"] = user
    return {"queryStringParameters": qs}


def _wire_common(mp):
    mp.setattr(org, "GRADED_ROLES", True)
    mp.setattr(org.scope, "visible_scope",
               lambda conn, caller: {"user_scope": "SELF", "self_folder": SELF_FOLDER,
                                     "cross_company": False})
    mp.setattr(org, "_can_view_folder", lambda conn, caller, folder: True)
    mp.setattr(org, "_merged_keys_for", lambda conn, target_id, date: ())
    mp.setattr(org, "_timeline_target_id", lambda conn, caller, user: "u-1")
    mp.setattr(org, "_allowed_site_ids", lambda conn, caller: {SITE_ID})
    mp.setattr(org.recordings, "photo_list_for_day",
               lambda conn, company, folder, date: [])
    mp.setattr(org.redactions, "deleted_photo_keys",
               lambda conn, company, keys=None: set())


@pytest.fixture
def aurora(monkeypatch):
    """One in-scope Aurora topic -> the Aurora-built shape."""
    _wire_common(monkeypatch)
    monkeypatch.setattr(org.topics, "has_topics_for_source_prefix",
                        lambda conn, prefix: prefix.startswith("extractions/"))
    monkeypatch.setattr(org.topics, "list_topics_for_source_prefix",
                        lambda conn, prefix: [{"site_id": SITE_ID}])
    monkeypatch.setattr(org, "_get_lake_json", lambda key: None)
    # user_name is a DISPLAY name, exactly as the real shape builds it.
    monkeypatch.setattr(org, "render_report_shape",
                        lambda rows, doc, date, user, conn=None, company_id=None, **kw:
                        {"report_date": date, "user_name": "Ben Lin",
                         "topics": [], "_report_metadata": {}})
    return monkeypatch


@pytest.fixture
def verbatim(monkeypatch):
    """No Aurora topics, a stored daily_report.json -> the S3-verbatim path."""
    _wire_common(monkeypatch)
    monkeypatch.setattr(org.topics, "has_topics_for_source_prefix",
                        lambda conn, prefix: False)
    monkeypatch.setattr(org, "_day_has_deleted_sources", lambda conn, u, d: False)
    stored = {"report_date": DATE, "user_name": SELF_FOLDER, "topics": [],
              "_report_metadata": {"model": "x", "keep": 1}}
    monkeypatch.setattr(org, "_get_lake_json", lambda key: dict(stored))
    return monkeypatch


def _get(user=None):
    return org.get_timeline_compat(FakeConn(), dict(CALLER), _event(user))


@pytest.mark.parametrize("fx", ["aurora", "verbatim"])
def test_no_user_param_reports_the_resolved_own_folder(fx, request):
    request.getfixturevalue(fx)
    res = _get()
    assert res["statusCode"] == 200
    b = body_of(res)
    assert b["user"] == SELF_FOLDER
    assert b["user"] != "Ben Lin" and b["user"] != "Ben_Lin"


def test_explicit_user_param_is_echoed_on_the_aurora_path(aurora):
    res = _get("Other_Person")
    assert res["statusCode"] == 200
    assert body_of(res)["user"] == "Other_Person"


def test_explicit_user_param_is_echoed_on_the_verbatim_path(verbatim):
    """The verbatim doc is withheld from a cross-user graded caller (CRITICAL-1),
    so an explicit other folder reaches it as an ALL-scope caller."""
    verbatim.setattr(org.scope, "visible_scope",
                     lambda conn, caller: {"user_scope": "ALL", "self_folder": SELF_FOLDER,
                                           "cross_company": False})
    verbatim.setattr(org.users, "get_by_folder_name",
                     lambda conn, company, folder: {"folder_name": folder})
    res = _get("Other_Person")
    assert res["statusCode"] == 200
    assert body_of(res)["user"] == "Other_Person"


def test_aurora_user_name_keeps_its_display_value(aurora):
    assert body_of(_get())["user_name"] == "Ben Lin"


def test_verbatim_changes_nothing_else_about_the_document(verbatim):
    b = body_of(_get())
    assert b["user_name"] == SELF_FOLDER
    assert b["_report_metadata"] == {"keep": 1}          # model still stripped
    assert set(b) == {"report_date", "user_name", "topics", "_report_metadata", "user"}
