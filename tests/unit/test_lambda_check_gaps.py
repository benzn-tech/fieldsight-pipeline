"""Check done (step A), the non-VPC handler: its wiring to the DB hop, the Ask
STT key and the lazy import of the extraction module.

Spec: docs/superpowers/specs/2026-10-20-check-done-gaps-design.md
"""
import io
import json
import os
import subprocess
import sys

import pytest

import lambda_check_gaps as h

SRC = os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))), "src")


class FakeLambda:
    def __init__(self, payload=None, function_error=None):
        self.payload, self.function_error, self.calls = payload, function_error, []

    def invoke(self, **kw):
        self.calls.append(kw)
        out = {"Payload": io.BytesIO(json.dumps(self.payload or {}).encode())}
        if self.function_error:
            out["FunctionError"] = self.function_error
        return out


def test_a_db_hop_function_error_is_an_error_answer(monkeypatch):
    monkeypatch.setattr(h, "CHECK_GAPS_DB_FUNCTION", "db-fn")
    fake = FakeLambda({"errorMessage": "boom"}, function_error="Unhandled")
    monkeypatch.setattr(h, "_get_lambda", lambda: fake)
    out = h.lambda_handler({"caller_sub": "s", "check_name": "pre-pour done", "pressed_at": 1}, None)
    assert out["status"] == "error" and out["say"]
    assert fake.calls[0]["FunctionName"] == "db-fn"
    assert fake.calls[0]["InvocationType"] == "RequestResponse"
    assert json.loads(fake.calls[0]["Payload"]) == {"caller_sub": "s", "pressed_at": 1}


def test_no_db_function_configured_is_an_error_not_a_raise(monkeypatch):
    monkeypatch.setattr(h, "CHECK_GAPS_DB_FUNCTION", "")
    out = h.lambda_handler({"caller_sub": "s", "pressed_at": 1}, None)
    assert out["status"] == "error"


def test_no_ask_key_hands_run_no_stt(monkeypatch):
    seen = {}
    monkeypatch.setattr(h.check_done, "run", lambda event, **kw: seen.update(kw) or {"status": "ok"})
    monkeypatch.setattr(h.elevenlabs_utils, "ELEVENLABS_ASK_API_KEY", "")
    h.lambda_handler({"caller_sub": "s"}, None)
    assert seen["stt"] is None
    monkeypatch.setattr(h.elevenlabs_utils, "ELEVENLABS_ASK_API_KEY", "k")
    h.lambda_handler({"caller_sub": "s"}, None)
    assert seen["stt"] is h.stt_chunk


class FakeHttp:
    def __init__(self, status=200, data=b'{"text": " hello "}'):
        self.status, self.data, self.calls = status, data, []

    def request(self, method, url, **kw):
        self.calls.append((method, url, kw))
        return type("R", (), {"status": self.status, "data": self.data})()


def test_stt_is_one_bounded_post_on_the_ask_key_without_diarisation(monkeypatch):
    http = FakeHttp()
    monkeypatch.setattr(h.urllib3, "PoolManager", lambda: http)
    monkeypatch.setattr(h.elevenlabs_utils, "ELEVENLABS_ASK_API_KEY", "ask-key")
    monkeypatch.setattr(h.elevenlabs_utils, "ELEVENLABS_API_KEY", "pipeline-key")
    assert h.stt_chunk(b"wav", "c.wav") == "hello"
    (method, url, kw), = http.calls
    assert kw["headers"] == {"xi-api-key": "ask-key"}
    assert kw["timeout"] == h.check_done.STT_TIMEOUT_S
    assert "diarize" not in [f[0] for f in kw["fields"]]


def test_stt_failure_raises_without_retrying(monkeypatch):
    http = FakeHttp(status=503, data=b"busy")
    monkeypatch.setattr(h.urllib3, "PoolManager", lambda: http)
    monkeypatch.setattr(h.elevenlabs_utils, "ELEVENLABS_ASK_API_KEY", "ask-key")
    with pytest.raises(RuntimeError):
        h.stt_chunk(b"wav", "c.wav")
    assert len(http.calls) == 1


def test_importing_the_handler_does_not_import_the_extraction_module():
    env = dict(os.environ, PYTHONPATH=SRC, AWS_DEFAULT_REGION="ap-southeast-2",
               AWS_ACCESS_KEY_ID="x", AWS_SECRET_ACCESS_KEY="x")
    code = "import sys, lambda_check_gaps; print('lambda_extract_session' in sys.modules)"
    out = subprocess.run([sys.executable, "-c", code], env=env, capture_output=True, text=True, cwd=SRC)
    assert out.returncode == 0, out.stderr
    assert out.stdout.strip() == "False"


def test_the_clients_do_not_retry_behind_the_persons_back(monkeypatch):
    # botocore legacy mode counts RETRIES: max_attempts 1 is two attempts. A
    # retried DB hop is a second cold start the person waits for.
    monkeypatch.setattr(h, "_lambda", None)
    monkeypatch.setattr(h, "_s3", None)
    lam, s3 = h._get_lambda()._client_config, h._get_s3()._client_config
    assert lam.retries["total_max_attempts"] == 1
    assert s3.retries["total_max_attempts"] <= 2
    assert lam.read_timeout == h.DB_HOP_READ_TIMEOUT_S
