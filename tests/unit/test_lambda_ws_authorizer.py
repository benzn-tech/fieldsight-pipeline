import pytest

wsauth = pytest.importorskip("lambda_ws_authorizer", reason="requires PyJWT")


class _FakeSigningKey:
    key = "fake-public-key"


class _FakeJwks:
    def get_signing_key_from_jwt(self, token):
        return _FakeSigningKey()


def _event(headers=None, method_arn="arn:aws:execute-api:ap-southeast-2:509194952652:abc/prod/$connect"):
    return {"headers": headers or {}, "methodArn": method_arn}


@pytest.fixture
def wired(monkeypatch):
    monkeypatch.setattr(wsauth, "POOL_IDS", ["ap-southeast-2_q88pd6XXr"])
    monkeypatch.setattr(wsauth, "_jwks_client", lambda pool_id: _FakeJwks())
    return monkeypatch


def test_valid_id_token_allows_with_sub(wired):
    wired.setattr(wsauth.jwt, "decode",
                  lambda *a, **k: {"sub": "user-123", "token_use": "id"})
    res = wsauth.lambda_handler(_event({"Authorization": "goodtoken"}), None)
    assert res["policyDocument"]["Statement"][0]["Effect"] == "Allow"
    assert res["context"]["sub"] == "user-123"
    assert res["policyDocument"]["Statement"][0]["Resource"].endswith("$connect")


def test_case_insensitive_header_and_bearer_prefix(wired):
    seen = {}
    def fake_decode(token, *a, **k):
        seen["token"] = token
        return {"sub": "u", "token_use": "id"}
    wired.setattr(wsauth.jwt, "decode", fake_decode)
    wsauth.lambda_handler(_event({"authorization": "Bearer tok"}), None)
    assert seen["token"] == "tok"       # bearer prefix stripped, lowercase header found


def test_access_token_rejected(wired):
    # token_use != "id" must not authorize.
    wired.setattr(wsauth.jwt, "decode",
                  lambda *a, **k: {"sub": "u", "token_use": "access"})
    with pytest.raises(Exception, match="Unauthorized"):
        wsauth.lambda_handler(_event({"Authorization": "t"}), None)


def test_bad_signature_rejected(wired):
    def boom(*a, **k):
        raise ValueError("signature verification failed")
    wired.setattr(wsauth.jwt, "decode", boom)
    with pytest.raises(Exception, match="Unauthorized"):
        wsauth.lambda_handler(_event({"Authorization": "t"}), None)


def test_missing_header_rejected(wired):
    with pytest.raises(Exception, match="Unauthorized"):
        wsauth.lambda_handler(_event({}), None)
