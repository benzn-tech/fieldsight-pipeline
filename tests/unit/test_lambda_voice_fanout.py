import json

import pytest

fo = pytest.importorskip("lambda_voice_fanout", reason="requires boto3 import path")


class _Gone(Exception):
    pass


class _FakeApi:
    class exceptions:
        GoneException = _Gone
    def __init__(self, gone_ids=()):
        self._gone = set(gone_ids)
        self.posted = []
    def post_to_connection(self, ConnectionId=None, Data=None):
        if ConnectionId in self._gone:
            raise _Gone()
        self.posted.append((ConnectionId, json.loads(Data.decode("utf-8"))))


class _FakeLambda:
    def __init__(self): self.calls = []
    def invoke(self, **kw):
        self.calls.append({**kw, "payload": json.loads(kw["Payload"])})


def _wire(monkeypatch, api):
    monkeypatch.setattr(fo.boto3, "client", lambda svc, **kw: api)
    monkeypatch.setattr(fo, "REAPER_FUNCTION", "fieldsight-test-voice-reaper")
    fake_lambda = _FakeLambda()
    monkeypatch.setattr(fo, "_lambda", lambda: fake_lambda)
    return fake_lambda


def test_posts_to_all_connections(monkeypatch):
    api = _FakeApi()
    fake_lambda = _wire(monkeypatch, api)
    res = fo.lambda_handler({"endpoint": "https://ws/prod",
                             "connectionIds": ["a", "b"],
                             "payload": {"s3Key": "voice/x.wav"}}, None)
    assert res == {"sent": 2, "gone": 0}
    assert [c for c, _ in api.posted] == ["a", "b"]
    assert api.posted[0][1]["s3Key"] == "voice/x.wav"
    assert fake_lambda.calls == []   # nothing gone -> no reaper invoke


def test_gone_connections_trigger_reaper(monkeypatch):
    api = _FakeApi(gone_ids=["b"])
    fake_lambda = _wire(monkeypatch, api)
    res = fo.lambda_handler({"endpoint": "https://ws/prod",
                             "connectionIds": ["a", "b", "c"],
                             "payload": {"s3Key": "voice/x.wav"}}, None)
    assert res == {"sent": 2, "gone": 1}
    assert fake_lambda.calls[0]["FunctionName"] == "fieldsight-test-voice-reaper"
    assert fake_lambda.calls[0]["InvocationType"] == "Event"
    assert fake_lambda.calls[0]["payload"] == {"connectionIds": ["b"]}


def test_empty_input_is_noop(monkeypatch):
    api = _FakeApi()
    _wire(monkeypatch, api)
    assert fo.lambda_handler({"endpoint": "https://ws/prod", "connectionIds": []}, None) == {"sent": 0, "gone": 0}
