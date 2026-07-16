"""
Tests for lambda_ask_agent voice path — SP-Ask Task 5 (TDD). stt/tts/_rag_answer
are stubbed; the fake lambda client records the async audit invoke.
"""
import base64
import io
import json
import os

import pytest

os.environ.setdefault("AWS_ACCESS_KEY_ID", "testing")
os.environ.setdefault("AWS_SECRET_ACCESS_KEY", "testing")
os.environ.setdefault("AWS_DEFAULT_REGION", "ap-southeast-2")
os.environ.setdefault("ANTHROPIC_API_KEY", "sk-ant-test-dummy-key")
os.environ.setdefault("RAG_SEARCH_FUNCTION", "fieldsight-test-rag-search")

laa = pytest.importorskip("lambda_ask_agent", reason="requires boto3/urllib3 (installed in CI)")
import dashscope_utils  # noqa: E402

CLIP = b"\x00\x01\x02rawaudio"
CLIP_B64 = base64.b64encode(CLIP).decode("ascii")


class FakeLambdaClient:
    def __init__(self):
        self.calls = []

    def invoke(self, FunctionName, InvocationType, Payload):
        self.calls.append({"FunctionName": FunctionName, "InvocationType": InvocationType,
                           "Payload": json.loads(Payload)})
        return {"StatusCode": 202}


def wire(monkeypatch, *, transcript="what happened at ellesmere",
         answer="The slab pour finished.", tts_bytes=b"RIFFwav",
         audit_fn="fieldsight-test-voice-audit"):
    monkeypatch.setattr(dashscope_utils, "stt", lambda audio, fmt="m4a": transcript)
    monkeypatch.setattr(dashscope_utils, "tts", lambda text: tts_bytes)
    monkeypatch.setattr(laa, "_rag_answer",
                        lambda body: {"answer": answer, "citations": [], "grounded": True})
    fake = FakeLambdaClient()
    monkeypatch.setattr(laa, "_get_lambda_client", lambda: fake)
    if audit_fn is None:
        monkeypatch.delenv("VOICE_AUDIT_FUNCTION", raising=False)
    else:
        monkeypatch.setenv("VOICE_AUDIT_FUNCTION", audit_fn)
    return fake


def run(event):
    resp = laa.lambda_handler(event, None)
    return json.loads(resp["body"])


def voice_event(**over):
    ev = {"mode": "voice", "audio": CLIP_B64, "format": "m4a", "caller_sub": "sub-1"}
    ev.update(over)
    return ev


def test_returns_voice_contract(monkeypatch):
    wire(monkeypatch)
    body = run(voice_event())
    assert body["transcript"] == "what happened at ellesmere"
    assert body["answerText"] == "The slab pour finished."
    assert body["audioBase64"] == base64.b64encode(b"RIFFwav").decode("ascii")
    assert body["audioFormat"] == "wav"


def test_stt_receives_decoded_audio_and_format(monkeypatch):
    seen = {}
    monkeypatch.setattr(dashscope_utils, "tts", lambda text: b"w")
    monkeypatch.setattr(laa, "_rag_answer", lambda body: {"answer": "a"})
    monkeypatch.setattr(laa, "_get_lambda_client", lambda: FakeLambdaClient())
    monkeypatch.setenv("VOICE_AUDIT_FUNCTION", "fn")

    def fake_stt(audio, fmt="m4a"):
        seen["audio"] = audio
        seen["fmt"] = fmt
        return "heard"
    monkeypatch.setattr(dashscope_utils, "stt", fake_stt)

    run(voice_event(format="m4a"))
    assert seen["audio"] == CLIP
    assert seen["fmt"] == "m4a"


def test_rag_called_with_mode_voice_and_transcript(monkeypatch):
    seen = {}
    monkeypatch.setattr(dashscope_utils, "stt", lambda audio, fmt="m4a": "the question")
    monkeypatch.setattr(dashscope_utils, "tts", lambda text: b"w")
    monkeypatch.setattr(laa, "_get_lambda_client", lambda: FakeLambdaClient())
    monkeypatch.setenv("VOICE_AUDIT_FUNCTION", "fn")

    def fake_rag(body):
        seen["body"] = body
        return {"answer": "a"}
    monkeypatch.setattr(laa, "_rag_answer", fake_rag)

    run(voice_event())
    assert seen["body"]["mode"] == "voice"
    assert seen["body"]["question"] == "the question"
    assert seen["body"]["caller_sub"] == "sub-1"


def test_empty_transcript_returns_error_no_tts(monkeypatch):
    def fail_tts(text):
        raise AssertionError("tts must not run on empty transcript")
    fake = wire(monkeypatch, transcript="   ")
    monkeypatch.setattr(dashscope_utils, "tts", fail_tts)
    body = run(voice_event())
    assert "error" in body
    assert body["transcript"] == ""
    assert fake.calls == []  # no audit either


def test_tts_failure_returns_error_with_transcript(monkeypatch):
    wire(monkeypatch)
    monkeypatch.setattr(dashscope_utils, "tts",
                        lambda text: (_ for _ in ()).throw(RuntimeError("tts 503")))
    body = run(voice_event())
    assert "error" in body
    assert body["transcript"] == "what happened at ellesmere"


def test_rag_error_returns_error_with_transcript(monkeypatch):
    wire(monkeypatch)
    monkeypatch.setattr(laa, "_rag_answer", lambda body: {"answer": "", "error": "rag down"})
    body = run(voice_event())
    assert "error" in body
    assert body["transcript"] == "what happened at ellesmere"


def test_audit_invoked_async_event(monkeypatch):
    fake = wire(monkeypatch, audit_fn="fieldsight-test-voice-audit")
    run(voice_event())
    assert len(fake.calls) == 1
    call = fake.calls[0]
    assert call["FunctionName"] == "fieldsight-test-voice-audit"
    assert call["InvocationType"] == "Event"
    assert call["Payload"] == {
        "caller_sub": "sub-1", "transcript": "what happened at ellesmere",
        "answer": "The slab pour finished."}


def test_no_audit_env_no_invoke(monkeypatch):
    fake = wire(monkeypatch, audit_fn=None)
    body = run(voice_event())
    assert body["audioFormat"] == "wav"  # still succeeds
    assert fake.calls == []


def test_invalid_base64_returns_error(monkeypatch):
    wire(monkeypatch)
    body = run(voice_event(audio="!!!not base64!!!"))
    assert "error" in body


def test_audit_invoke_failure_voice_turn_still_succeeds(monkeypatch):
    class ExplodingLambdaClient:
        def invoke(self, **kwargs):
            raise RuntimeError("lambda invoke boom")

    wire(monkeypatch)
    monkeypatch.setattr(laa, "_get_lambda_client", lambda: ExplodingLambdaClient())
    body = run(voice_event())
    # Audit invoke blew up, but the voice turn itself must still succeed.
    assert "error" not in body
    assert body["transcript"] == "what happened at ellesmere"
    assert body["answerText"] == "The slab pour finished."
    assert body["audioFormat"] == "wav"


def test_prod_guard_no_rag_search_function_voice_branch_not_taken(monkeypatch):
    fake = wire(monkeypatch)
    monkeypatch.delenv("RAG_SEARCH_FUNCTION", raising=False)
    body = run(voice_event())
    # Voice branch must not run (prod has no RAG/VPC infra); falls through to
    # the legacy S3 path, which 400s since a voice event carries no 'question'.
    assert body == {"error": "Missing question"}
    assert fake.calls == []
