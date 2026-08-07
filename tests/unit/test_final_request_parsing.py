"""Unit: parsing the final-extraction request artifact.

`extraction_requests/{session}.json` is how the in-VPC finalize sweep asks for
the authoritative extraction — an in-VPC Lambda cannot invoke another Lambda
(BUG-36), so the request rides S3.

A malformed artifact used to return None in silence. The trigger fired, the
session was never extracted, and the only trace in CloudWatch was a ~100ms
Duration line with no application logging whatsoever. The visible symptom is
"the trigger is broken", which sends you to debug S3 notifications — an hour
spent in the wrong layer, which is how this test came to exist.
"""
import json

import pytest

extract = pytest.importorskip("lambda_extract_session")

GOOD = {"userFolder": "Ben_UCPK", "date": "2026-08-06", "sessionBase": "sid" + "a" * 32}


class _Body:
    def __init__(self, payload):
        self._payload = payload

    def read(self):
        return self._payload


class _S3:
    def __init__(self, payload):
        self._payload = payload

    def get_object(self, **_):
        if isinstance(self._payload, Exception):
            raise self._payload
        return {"Body": _Body(self._payload)}


def _parse(monkeypatch, payload):
    monkeypatch.setattr(extract, "s3", lambda: _S3(payload))
    return extract.parse_final_request("bucket", "extraction_requests/x.json")


def test_a_complete_request_parses(monkeypatch):
    # The trailing 0 is `generation`: absent on every artifact the finalize
    # sweep writes, which means "first round" (see _rerun_if_the_session_grew).
    assert _parse(monkeypatch, json.dumps(GOOD).encode()) == (
        "Ben_UCPK", "2026-08-06", "sid" + "a" * 32, 0)


@pytest.mark.parametrize("missing", ["userFolder", "date", "sessionBase"])
def test_any_missing_field_refuses_and_says_which(monkeypatch, caplog, missing):
    payload = {k: v for k, v in GOOD.items() if k != missing}
    with caplog.at_level("WARNING"):
        assert _parse(monkeypatch, json.dumps(payload).encode()) is None
    assert missing in caplog.text, "the log must name the field that is absent"


def test_an_empty_request_is_refused_loudly(monkeypatch, caplog):
    """The exact artifact that produced a silent 116ms no-op."""
    with caplog.at_level("WARNING"):
        assert _parse(monkeypatch, b"{}") is None
    assert caplog.text.strip(), "an empty request must not pass in silence"


def test_an_unreadable_request_is_refused_not_raised(monkeypatch, caplog):
    """Raising would retry-storm a dead artifact: S3 keeps redelivering, and the
    artifact is never going to become valid."""
    with caplog.at_level("WARNING"):
        assert _parse(monkeypatch, RuntimeError("no such key")) is None
    assert "Unreadable" in caplog.text


def test_malformed_json_is_refused_not_raised(monkeypatch, caplog):
    with caplog.at_level("WARNING"):
        assert _parse(monkeypatch, b"not json at all") is None
    assert caplog.text.strip()
