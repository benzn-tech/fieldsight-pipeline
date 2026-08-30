"""The caller's timezone has to survive every hop, or the slot read is dead.

Three hops carry it and each one has dropped a field before:

  gateway POST /api/ask        -> ask-agent
  gateway POST /api/ask/voice  -> ask-agent
  ask-agent _voice_answer      -> _rag_answer

The third is the one worth a test of its own. The voice path rebuilds the body
from scratch rather than passing it through, so a field added to the screen
path is silently absent from the spoken one -- and voice is where "what
happened yesterday" is most likely to be asked, because there is no date picker
to fall back on.

This is the same shape as the defect being fixed: `date` was forwarded by the
gateway, documented in the handler's docstring, and read by nobody.
"""
import json

import pytest

fsapi = pytest.importorskip("lambda_fieldsight_api", reason="requires boto3 (installed in CI)")
import lambda_ask_agent as laa  # noqa: E402


CALLER = {"sub": "sub-1", "role": "admin", "display_name": "Ada", "email": "a@x.nz"}


class Recorder:
    def __init__(self, payload=None):
        self.sent = []
        self.payload = payload if payload is not None else {"answer": "ok", "citations": []}

    def invoke(self, FunctionName, InvocationType, Payload, **kw):
        self.sent.append(json.loads(Payload))

        class S:
            def read(inner):
                return json.dumps(self.payload).encode("utf-8")
        return {"Payload": S()}


def test_gateway_forwards_the_zone_to_ask(monkeypatch):
    rec = Recorder()
    monkeypatch.setattr(fsapi, "lambda_client", rec)

    fsapi.ask_question({"question": "昨天发生了什么", "tz": "Pacific/Auckland"}, CALLER)

    assert rec.sent[0]["tz"] == "Pacific/Auckland"


def test_gateway_omits_the_zone_when_the_client_sent_none(monkeypatch):
    """An absent zone must stay absent rather than become an empty string: ""
    is a value the slot reader would have to special-case, and every place that
    has to special-case a blank is a place one of them will forget to."""
    rec = Recorder()
    monkeypatch.setattr(fsapi, "lambda_client", rec)

    fsapi.ask_question({"question": "昨天发生了什么"}, CALLER)

    assert "tz" not in rec.sent[0]


def test_gateway_forwards_the_zone_to_voice_ask(monkeypatch):
    rec = Recorder(payload={"answer": "ok", "audio": None})
    monkeypatch.setattr(fsapi, "lambda_client", rec)

    fsapi.ask_voice({"audio": "AAAA", "tz": "Australia/Sydney"}, CALLER)

    assert rec.sent[0]["tz"] == "Australia/Sydney"


def test_the_voice_path_carries_the_zone_into_the_rag_answer(monkeypatch):
    """_voice_answer builds a fresh body instead of passing one through, so a
    field the screen path gained is absent here unless it is added twice."""
    seen = {}
    monkeypatch.setattr(laa, "_rag_answer", lambda body: seen.update(body) or
                        {"answer": "ok", "citations": []})
    monkeypatch.setattr(laa, "_invoke_voice_audit", lambda *a, **k: None)

    import dashscope_utils
    monkeypatch.setattr(dashscope_utils, "stt", lambda *a, **k: "昨天发生了什么")
    monkeypatch.setattr(dashscope_utils, "tts", lambda *a, **k: b"")

    laa._voice_answer({"audio": "", "caller_sub": "sub-1", "tz": "Pacific/Auckland"})

    assert seen.get("tz") == "Pacific/Auckland"


def test_the_voice_response_carries_the_basis_it_was_given(monkeypatch):
    """_voice_answer builds its response from scratch, so a field the screen path
    gains stops there unless it is listed. `_basis`'s docstring claimed the voice
    path rendered it as a spoken clause while the voice path never received it --
    the same shape as `date` being forwarded, documented, and read by nobody.

    The device does not speak it yet. This asserts it ARRIVES, so that when the
    app renders it there is nothing to plumb.
    """
    monkeypatch.setattr(laa, "_rag_answer", lambda body: {
        "answer": "On 2026-07-18 the pour moved.", "citations": [],
        "basis": {"from": "2026-07-18", "to": "2026-07-18", "widened": True,
                  "chunks": 4, "dates": ["2026-07-18"]}})
    monkeypatch.setattr(laa, "_invoke_voice_audit", lambda *a, **k: None)

    import dashscope_utils
    monkeypatch.setattr(dashscope_utils, "stt", lambda *a, **k: "what happened yesterday")
    monkeypatch.setattr(dashscope_utils, "tts", lambda *a, **k: b"\x00\x01")

    out = laa._voice_answer({"audio": "", "caller_sub": "sub-1", "tz": "Pacific/Auckland"})

    assert out["basis"]["widened"] is True
    assert out["basis"]["from"] == "2026-07-18"
