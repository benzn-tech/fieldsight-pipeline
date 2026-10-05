"""One rule mints a recording folder, and nothing downstream re-cleanses it.

2026-10-05, prod: Deandre's directory first name is `Deandre'`. `_free_folder_name`
stored `Deandre'_Alberts`; the upload route wrote `users/Deandre__Alberts/...` through
its own, different cleanser; item-writer then raised "folder 'Deandre__Alberts' has no
directory row" 21 times and 45 minutes of recording never reached the web.
"""
import json

import pytest

import folder_key as fk

org = pytest.importorskip("lambda_org_api", reason="requires psycopg (installed in CI)")
users_repo = pytest.importorskip("repositories.users", reason="requires psycopg")

CJK = "王伟"


@pytest.mark.parametrize("name,expected", [
    ("Deandre'_Alberts", "Deandre_Alberts"),     # the incident
    ("O'Brien", "OBrien"),
    ("O’Brien", "OBrien"),                   # typographic apostrophe
    ("José Núñez", "Jose_Nunez"),
    ("Ben Lin", "Ben_Lin"),
    ("  Ben Lin  ", "Ben_Lin"),
    ("Ben_", "Ben_"),                             # NULL last name: unchanged from today
    ("Ben_UCPK_", "Ben_UCPK_"),
    ("Deandre__Alberts", "Deandre__Alberts"),     # hand-set live folder stays valid
    ("a/b:c", "a_b_c"),
    ("Mary-Jane.Smith", "Mary-Jane.Smith"),
    (CJK, None),                                  # CJK only
    ("_foo", "foo"), ("a\n", "a"),
    ("", None), (None, None), ("___", None), ("'", None),
    (CJK + " Li", "Li"),                        # mixed: the kept characters survive
])
def test_folder_key(name, expected):
    assert fk.folder_key(name) == expected


@pytest.mark.parametrize("name", ["Deandre'_Alberts", "José Núñez", "Ben_",
                                  "Deandre__Alberts", "a b c", CJK + " Li"])
def test_folder_key_is_idempotent(name):
    once = fk.folder_key(name)
    assert fk.folder_key(once) == once


@pytest.mark.parametrize("name", ["Deandre'_Alberts", "José Ñ", "x y/z", "åø Li"])
def test_the_key_survives_the_upload_cleanser_unchanged(name):
    """The property the incident broke: _safe_seg(key) == key."""
    key = fk.folder_key(name)
    assert org._safe_seg(key) == key


# ---- _free_folder_name ------------------------------------------------------------

def _free(monkeypatch, taken=()):
    monkeypatch.setattr(org.users, "get_by_folder_name_global",
                        lambda conn, f: {"cognito_sub": "other"} if f in taken else None)


def test_free_folder_name_uses_the_key_not_the_raw_name(monkeypatch):
    _free(monkeypatch)
    assert org._free_folder_name(None, "Deandre'_Alberts", "s", "u1") == "Deandre_Alberts"


def test_free_folder_name_keeps_the_numbered_collision_logic(monkeypatch):
    _free(monkeypatch, taken={"Deandre_Alberts", "Deandre_Alberts_2"})
    assert org._free_folder_name(None, "Deandre' Alberts", "s", "u1") == "Deandre_Alberts_3"


def test_a_cjk_name_gets_the_id_fallback(monkeypatch):
    _free(monkeypatch)
    assert org._free_folder_name(None, CJK, "s", "abcdef12-0000") == "u_abcdef12"


def test_a_cjk_name_without_an_id_is_none_as_before(monkeypatch):
    _free(monkeypatch)
    assert org._free_folder_name(None, CJK, "s", None) is None


# ---- the upload route -------------------------------------------------------------

class _Ctx:
    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


class FakeConn(_Ctx):
    def transaction(self):
        return _Ctx()


class FakeS3:
    def __init__(self):
        self.presigned = []

    def generate_presigned_url(self, op, Params=None, ExpiresIn=0):
        self.presigned.append(Params)
        return "https://s3.example/" + Params["Key"]


DEANDRE = {"id": "d3a4d5e6-1111", "cognito_sub": "sub-d", "company_id": "c-1",
           "email": "d@x.nz", "first_name": "Deandre'", "last_name": "Alberts",
           "folder_name": None, "global_role": "worker", "archived_at": None}


@pytest.fixture
def upload(monkeypatch):
    s3, state = FakeS3(), {"set": [], "inserted": []}
    monkeypatch.setattr(org, "get_connection", lambda *a, **k: FakeConn())
    monkeypatch.setattr(org, "_s3_client", s3)
    monkeypatch.setattr(org.users, "get_user_by_sub",
                        lambda conn, sub: dict(DEANDRE) if sub == "sub-d" else None)
    monkeypatch.setattr(org.users, "get_by_folder_name_global", lambda conn, f: None)
    monkeypatch.setattr(org.users, "set_folder_name",
                        lambda conn, sub, f: state["set"].append(f))
    monkeypatch.setattr(org.recordings, "get_by_client_uuid", lambda c, u, cu: None)

    def insert(conn, **kw):
        state["inserted"].append(kw)
        return {"id": "rec-1", **kw}
    monkeypatch.setattr(org.recordings, "insert_pending", insert)
    return monkeypatch, s3, state


def _post():
    return org.lambda_handler({
        "httpMethod": "POST", "path": "/api/org/recordings/upload-url",
        "queryStringParameters": None,
        "body": json.dumps({"kind": "audio", "clientUuid": "cap-1", "fileName": "a.wav",
                            "contentType": "audio/wav", "startedAt": "2026-10-05T10:00:00Z"}),
        "requestContext": {"authorizer": {"claims": {"sub": "sub-d"}}}}, None)


def test_an_invalid_stored_folder_is_refused_with_no_row_and_no_presign(upload, caplog):
    mp, s3, state = upload
    mp.setattr(org.users, "get_user_by_sub",
               lambda conn, sub: {**DEANDRE, "folder_name": "Deandre'_Alberts"})
    res = _post()
    assert res["statusCode"] == 422
    assert "folder is invalid" in json.loads(res["body"])["error"]
    assert state["inserted"] == [] and s3.presigned == []
    assert any(r.levelname == "ERROR" for r in caplog.records)


def test_the_hand_set_double_underscore_folder_still_uploads(upload):
    mp, s3, state = upload
    mp.setattr(org.users, "get_user_by_sub",
               lambda conn, sub: {**DEANDRE, "folder_name": "Deandre__Alberts"})
    res = _post()
    assert res["statusCode"] == 200
    assert json.loads(res["body"])["s3Key"].startswith("users/Deandre__Alberts/audio/")
    assert state["set"] == []                         # nothing re-derived


def test_enrolment_stores_exactly_the_folder_the_key_uses(upload):
    _, s3, state = upload
    res = _post()
    assert res["statusCode"] == 200
    assert state["set"] == ["Deandre_Alberts"]
    assert json.loads(res["body"])["s3Key"].startswith("users/Deandre_Alberts/audio/")


def test_a_null_last_name_still_enrols_the_trailing_underscore_form(upload):
    mp, _, state = upload
    mp.setattr(org.users, "get_user_by_sub",
               lambda conn, sub: {**DEANDRE, "first_name": "Ben", "last_name": None})
    assert _post()["statusCode"] == 200
    assert state["set"] == ["Ben_"]


# ---- patch_member_folder ----------------------------------------------------------

@pytest.fixture
def patch(monkeypatch):
    admin = {**DEANDRE, "cognito_sub": "sub-1", "global_role": "admin"}
    seen = {}
    monkeypatch.setattr(org, "get_connection", lambda *a, **k: FakeConn())
    monkeypatch.setattr(org.users, "get_user_by_sub",
                        lambda conn, sub: dict(admin) if sub == "sub-1"
                        else ({**DEANDRE, "folder_name": seen.get("f")} if sub == "sub-d" else None))
    monkeypatch.setattr(org.users, "get_by_folder_name_global", lambda conn, f: None)
    monkeypatch.setattr(org.users, "set_folder_name",
                        lambda conn, sub, f: seen.update(f=f))

    def call(value):
        return org.lambda_handler({
            "httpMethod": "PATCH", "path": "/api/org/members/sub-d/folder",
            "queryStringParameters": None, "body": json.dumps({"folder_name": value}),
            "requestContext": {"authorizer": {"claims": {"sub": "sub-1"}}}}, None), seen
    return call


def test_an_admin_typed_apostrophe_is_minted_not_stored(patch):
    res, seen = patch("Deandre'_Alberts")
    assert res["statusCode"] == 200
    assert seen["f"] == "Deandre_Alberts"
    assert json.loads(res["body"])["folder_name"] == "Deandre_Alberts"   # the stored value


def test_an_admin_typed_value_with_no_usable_characters_is_a_400(patch):
    res, seen = patch(CJK)
    assert res["statusCode"] == 400
    assert "letter or digit" in json.loads(res["body"])["error"]
    assert "f" not in seen


# ---- the directory writer ---------------------------------------------------------

def test_the_field_only_writer_refuses_a_bad_key_before_touching_the_database():
    with pytest.raises(ValueError):
        users_repo.upsert_field_only_user(None, "c", "Deandre'_Alberts", "D", "A", "worker")


# ---- review fix round 1 ------------------------------------------------------------

def test_a_trailing_newline_is_not_a_folder_key():
    assert fk.KEY_RE.fullmatch("abc\n") is None
    with pytest.raises(ValueError):
        users_repo.upsert_field_only_user(None, "c", "abc\n", "D", "A", "worker")


def test_a_cjk_first_name_does_not_leave_leading_underscores(monkeypatch):
    _free(monkeypatch)
    assert org._free_folder_name(None, CJK + "_Li", "s", "abcdef12-0") == "Li"


def test_a_name_that_is_only_underscores_after_stripping_takes_the_id_fallback(monkeypatch):
    _free(monkeypatch)
    assert fk.folder_key(CJK + "_" + CJK) is None
    assert org._free_folder_name(None, CJK + "_" + CJK, "s", "abcdef12-0") == "u_abcdef12"


def test_a_resend_for_an_invalid_stored_folder_returns_the_existing_key(upload):
    mp, s3, state = upload
    mp.setattr(org.users, "get_user_by_sub",
               lambda conn, sub: {**DEANDRE, "folder_name": "Deandre'_Alberts"})
    mp.setattr(org.recordings, "get_by_client_uuid",
               lambda c, u, cu: {"id": "rec-0", "s3_key": "users/Deandre__Alberts/audio/x/a.wav"})
    res = _post()
    assert res["statusCode"] == 200
    assert json.loads(res["body"])["s3Key"] == "users/Deandre__Alberts/audio/x/a.wav"
    assert state["inserted"] == []


def _old_rule(name):
    import re
    return re.sub(r'[<>:"/\\|?*\s]', "_", (name or "").strip())


CORPUS = ["Ben Lin", "Ben_", "Ben_UCPK_", "Neil Blunden", "Amy  Rose", "Mary-Jane Smith",
          "J.R. Smith", "Deandre__Alberts", "Ada_L", "Al\tBo", "a<b>c", 'q"r', "x|y?z*w",
          "_Hidden", "__Two", "-", "...", "Bob_2", "u_abcdef12", "Li Wei", "ABC123"]


@pytest.mark.parametrize("name", CORPUS)
def test_folder_key_equals_the_old_rule_wherever_the_old_output_was_valid(name):
    """Exceptions (deliberate, all NEW-user-only): a leading underscore is stripped, and a
    name with no letter/digit ("-", "...") now gets the id fallback (None here)."""
    import re
    old = _old_rule(name)
    if not re.fullmatch(r"[A-Za-z0-9._-]+", old):
        pytest.skip("old output was already invalid")
    new = fk.folder_key(name)
    if not re.search(r"[A-Za-z0-9]", old):
        assert new is None
    else:
        assert new == old.lstrip("_")


def _enrol_caller(**kw):
    return {**DEANDRE, **kw}


def test_enrol_fallback_when_the_write_fails_keeps_a_minted_name_plus_id(monkeypatch):
    _free(monkeypatch)

    def boom(conn, sub, f):
        raise RuntimeError("x")
    monkeypatch.setattr(org.users, "set_folder_name", boom)
    out = org._enrol_folder_on_upload(FakeConn(), _enrol_caller())
    assert out == "Deandre_Alberts_d3a4d5e6"


def test_enrol_fallback_for_a_cjk_name_with_no_id_is_u_unknown(monkeypatch):
    _free(monkeypatch)
    out = org._enrol_folder_on_upload(
        FakeConn(), _enrol_caller(id=None, first_name=CJK, last_name=CJK))
    assert out == "u_unknown"
    assert org._safe_seg(out) == out
