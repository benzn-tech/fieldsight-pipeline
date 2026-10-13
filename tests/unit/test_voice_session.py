import base64, hashlib, hmac, json, os
import pytest
import voice_session as vs

KEY = "fixture-key-not-secret"

def test_round_trip():
    t = vs.mint("sub-1", KEY, now=1000)["token"]
    assert vs.verify(t, KEY, now=1001) == "sub-1"

def test_wire_format_is_unpadded_and_signs_the_received_payload():
    t = vs.mint("sub-1", KEY, now=1000)["token"]
    p, s = t.split(".")
    assert "=" not in t
    want = base64.urlsafe_b64encode(hmac.new(KEY.encode(), p.encode("ascii"), hashlib.sha256).digest()).rstrip(b"=").decode()
    assert s == want
    body = json.loads(base64.urlsafe_b64decode(p + "=" * (-len(p) % 4)))
    assert body == {"sub": "sub-1", "aud": "voice-llm", "exp": 1300}

@pytest.mark.parametrize("mutate", ["payload", "sig", "aud", "expired", "garbage"])
def test_tamper_is_refused(mutate):
    t = vs.mint("sub-1", KEY, now=1000)["token"]
    p, s = t.split(".")
    now = 1001
    if mutate == "payload": p = p[:-1] + ("A" if p[-1] != "A" else "B")
    if mutate == "sig": s = s[:-1] + ("A" if s[-1] != "A" else "B")
    if mutate == "aud":
        p = base64.urlsafe_b64encode(json.dumps({"sub": "sub-1", "aud": "other", "exp": 1300}).encode()).rstrip(b"=").decode()
        s = base64.urlsafe_b64encode(hmac.new(KEY.encode(), p.encode(), hashlib.sha256).digest()).rstrip(b"=").decode()
    if mutate == "expired": now = 1300 + 31
    tok = "nonsense" if mutate == "garbage" else p + "." + s
    assert vs.verify(tok, KEY, now=now) is None

def test_empty_key_cannot_mint():
    with pytest.raises(ValueError):
        vs.mint("sub-1", "", now=1)

def test_fixture_is_current():
    """The Node verifier (voice_llm/token.test.mjs) is tested against this file;
    regenerate it from mint() if the format ever changes."""
    f = json.load(open(os.path.join(os.path.dirname(__file__), "..", "fixtures", "voice_session_token.json")))
    assert vs.mint(f["sub"], f["key"], now=f["now"])["token"] == f["token"]
