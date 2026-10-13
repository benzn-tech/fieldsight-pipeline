"""Voice-session tokens for the ElevenLabs voice agent (spec 2026-10-14 phase 2 §2).

HMAC-signed, good for one audience ("voice-llm") and five minutes. Minted by
org-api for an authenticated caller; verified by the Node endpoint, which then
uses `sub` as caller_sub. The Cognito idToken is NOT used: ElevenLabs carries
this value in every request body, and an idToken there would be a credential
for the whole API."""
import base64
import hashlib
import hmac
import json

AUD = "voice-llm"
SKEW = 30


def _b64(b: bytes) -> str:
    return base64.urlsafe_b64encode(b).rstrip(b"=").decode("ascii")


def _sig(key: str, p: str) -> str:
    return _b64(hmac.new(key.encode(), p.encode("ascii"), hashlib.sha256).digest())


def mint(sub, key, now, ttl=300):
    if not key:
        raise ValueError("voice session signing key is empty")
    exp = int(now) + int(ttl)
    p = _b64(json.dumps({"sub": sub, "aud": AUD, "exp": exp}, separators=(",", ":")).encode())
    return {"token": p + "." + _sig(key, p), "expires_at": exp}


def verify(token, key, now):
    if not key or not isinstance(token, str) or token.count(".") != 1:
        return None
    p, s = token.split(".")
    if not hmac.compare_digest(s, _sig(key, p)):
        return None
    try:
        body = json.loads(base64.urlsafe_b64decode(p + "=" * (-len(p) % 4)))
    except Exception:
        return None
    if body.get("aud") != AUD or not isinstance(body.get("exp"), int) or body["exp"] <= int(now) - SKEW:
        return None
    return body.get("sub") or None
