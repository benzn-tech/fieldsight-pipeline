import time

import pytest

wsauth = pytest.importorskip("lambda_ws_authorizer", reason="requires PyJWT")

jwt = pytest.importorskip("jwt", reason="requires PyJWT")
rsa = pytest.importorskip(
    "cryptography.hazmat.primitives.asymmetric.rsa", reason="requires cryptography"
)
serialization = pytest.importorskip(
    "cryptography.hazmat.primitives.serialization", reason="requires cryptography"
)


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


# --- Real-crypto coverage -------------------------------------------------
# The tests above all monkeypatch jwt.decode, so none of them exercise
# PyJWT's actual cryptographic enforcement (RS256 signature check, exp,
# iss, algorithm allow-list). A regression that swapped RS256 for HS256,
# dropped the `issuer=` kwarg, or set `verify_exp: False` would still pass
# every test above. These tests sign real tokens with a locally generated
# RSA keypair and let the REAL jwt.decode (wsauth.jwt is left untouched)
# verify them, so the crypto boundary itself is under test.

_private_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
_PRIVATE_KEY_PEM = _private_key.private_bytes(
    encoding=serialization.Encoding.PEM,
    format=serialization.PrivateFormat.PKCS8,
    encryption_algorithm=serialization.NoEncryption(),
)
_PUBLIC_KEY_PEM = _private_key.public_key().public_bytes(
    encoding=serialization.Encoding.PEM,
    format=serialization.PublicFormat.SubjectPublicKeyInfo,
)

_REAL_POOL_ID = "ap-southeast-2_q88pd6XXr"
_REAL_ISSUER = f"https://cognito-idp.{wsauth.REGION}.amazonaws.com/{_REAL_POOL_ID}"
_OTHER_ISSUER = f"https://cognito-idp.{wsauth.REGION}.amazonaws.com/ap-southeast-2_otherPool99"


class _RealSigningKey:
    def __init__(self, key):
        self.key = key


class _RealJwks:
    """Stands in for jwt.PyJWKClient: returns the test RSA public key
    regardless of the token's `kid`, so real jwt.decode() does the actual
    signature/claims verification against a key we control."""

    def __init__(self, key):
        self._key = key

    def get_signing_key_from_jwt(self, token):
        return _RealSigningKey(self._key)


@pytest.fixture
def wired_real(monkeypatch):
    monkeypatch.setattr(wsauth, "POOL_IDS", [_REAL_POOL_ID])
    monkeypatch.setattr(wsauth, "_jwks_client", lambda pool_id: _RealJwks(_PUBLIC_KEY_PEM))
    return monkeypatch


def _real_claims(**overrides):
    now = int(time.time())
    claims = {
        "sub": "user-real-1",
        "token_use": "id",
        "iss": _REAL_ISSUER,
        "iat": now,
        "exp": now + 3600,
    }
    claims.update(overrides)
    return claims


def test_real_rs256_valid_token_allows_with_sub(wired_real):
    token = jwt.encode(_real_claims(sub="user-real-1"), _PRIVATE_KEY_PEM, algorithm="RS256")
    res = wsauth.lambda_handler(_event({"Authorization": token}), None)
    assert res["policyDocument"]["Statement"][0]["Effect"] == "Allow"
    assert res["context"]["sub"] == "user-real-1"


def test_real_rs256_expired_token_rejected(wired_real):
    now = int(time.time())
    claims = _real_claims(iat=now - 7200, exp=now - 3600)
    token = jwt.encode(claims, _PRIVATE_KEY_PEM, algorithm="RS256")
    with pytest.raises(Exception, match="Unauthorized"):
        wsauth.lambda_handler(_event({"Authorization": token}), None)


def test_real_rs256_wrong_issuer_rejected(wired_real):
    claims = _real_claims(iss=_OTHER_ISSUER)
    token = jwt.encode(claims, _PRIVATE_KEY_PEM, algorithm="RS256")
    with pytest.raises(Exception, match="Unauthorized"):
        wsauth.lambda_handler(_event({"Authorization": token}), None)


def test_real_hs256_alg_confusion_rejected(wired_real):
    # Signed with a symmetric secret instead of the RSA private key. If the
    # authorizer ever dropped algorithms=["RS256"] (or widened it to include
    # HS256), this would be a classic alg-confusion forgery and would pass.
    token = jwt.encode(_real_claims(), "somesecret", algorithm="HS256")
    with pytest.raises(Exception, match="Unauthorized"):
        wsauth.lambda_handler(_event({"Authorization": token}), None)


def test_real_rs256_access_token_use_rejected(wired_real):
    # Genuinely signed and otherwise valid, but token_use == "access": the
    # id-vs-access gate must still hold for a real, well-formed token.
    claims = _real_claims(token_use="access")
    token = jwt.encode(claims, _PRIVATE_KEY_PEM, algorithm="RS256")
    with pytest.raises(Exception, match="Unauthorized"):
        wsauth.lambda_handler(_event({"Authorization": token}), None)
