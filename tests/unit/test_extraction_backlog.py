"""Words transcribed and never summarised.

The discriminator is the whole design. Over the real prod bucket:

    122 extraction requests
     69 fulfilled
     42 with NO transcripts at all   <- VAD found no speech. Not a backlog.
     11 with transcripts and no extraction

A metric counting all 53 unfulfilled requests would sit at 42 forever, and an
alarm that is always red stops being read inside a week -- which is the same
failure that left the prod alert topic with no subscriber. So
`test_a_session_with_no_transcripts_is_not_a_backlog` is not an edge case, it
is the reason this function is worth deploying.
"""
import json
from datetime import datetime, timedelta, timezone

import pytest

bl = pytest.importorskip("lambda_extraction_backlog")

NOW = datetime(2026, 9, 6, 12, 0, tzinfo=timezone.utc)
OLD = NOW - timedelta(hours=3)          # past the grace period, inside the window
ANCIENT = NOW - timedelta(days=30)      # outside the window
FRESH = NOW - timedelta(minutes=5)      # still in flight


class FakeS3:
    """Enough of the S3 client for `scan`: a paginator over prefixes and
    get_object. Keys are exact strings, so a change to the key convention
    fails these tests rather than passing them by accident."""

    def __init__(self, objects, bodies=None):
        self._objects = objects          # {key: LastModified}
        self._bodies = bodies or {}

    def get_paginator(self, name):
        assert name == "list_objects_v2"
        outer = self

        class _P:
            def paginate(self, Bucket=None, Prefix=""):
                yield {"Contents": [{"Key": k, "LastModified": v}
                                    for k, v in sorted(outer._objects.items())
                                    if k.startswith(Prefix)]}
        return _P()

    def get_object(self, Bucket=None, Key=None):
        body = self._bodies[Key]

        class _B:
            def read(self_inner):
                return body if isinstance(body, bytes) else json.dumps(body).encode()
        return {"Body": _B()}


def _req(sid, folder="Neil_Blunden", date="2026-09-02"):
    return {"userFolder": folder, "date": date, "sessionBase": f"sid{sid}"}


SID = "b5ae5db542724e1b89b48015f882f5e0"
REQ_KEY = f"extraction_requests/{SID}.json"


def _world(*, when=OLD, transcripts=True, extracted=False, body=None):
    objs = {REQ_KEY: when}
    if transcripts:
        objs[f"transcripts/Neil_Blunden/2026-09-02/x_sid{SID}_c0000.json"] = when
    if extracted:
        objs[f"extractions/Neil_Blunden/2026-09-02/sid{SID}.json"] = when
    return FakeS3(objs, {REQ_KEY: body if body is not None else _req(SID)})


# --------------------------------------------------------------------------

def test_transcribed_but_never_extracted_is_the_backlog():
    recent, everything, skipped = bl.scan(_world(), now=NOW)
    assert len(recent) == 1 and len(everything) == 1 and skipped == 0
    assert recent[0]["folder"] == "Neil_Blunden"
    assert recent[0]["session"] == f"sid{SID}"


def test_a_session_with_no_transcripts_is_not_a_backlog():
    """VAD found no speech, so there was nothing to summarise.

    42 of 53 unfulfilled requests on prod are this. Counting them would bury
    the eleven that matter and the alarm would never be believed again.
    """
    recent, everything, _ = bl.scan(_world(transcripts=False), now=NOW)
    assert recent == [] and everything == []


def test_a_fulfilled_request_is_not_a_backlog():
    recent, everything, _ = bl.scan(_world(extracted=True), now=NOW)
    assert recent == [] and everything == []


def test_a_request_inside_the_grace_period_is_in_flight_not_late():
    """Extraction of a long session takes minutes and finalize re-drives it.
    Alarming before the grace period would fire on the normal path."""
    recent, everything, _ = bl.scan(_world(when=FRESH), now=NOW)
    assert recent == [] and everything == []


def test_historical_debt_is_reported_but_does_not_hold_the_alarm_red():
    """The nine August losses found when this was written are real and must be
    visible in the logs -- but a metric they pin above zero forever is an alarm
    nobody reads, which is the failure this whole probe exists inside."""
    recent, everything, _ = bl.scan(_world(when=ANCIENT), now=NOW)
    assert recent == [] and len(everything) == 1


def test_an_unreadable_request_is_counted_not_dropped():
    """A request whose JSON will not parse is a fault in the enqueuer. Silently
    skipping it would make it indistinguishable from a healthy fulfilled one."""
    recent, everything, skipped = bl.scan(_world(body=b"{not json"), now=NOW)
    assert skipped == 1 and recent == [] and everything == []


def test_the_sid_must_actually_appear_in_the_transcript_key():
    """A different session's transcripts on the same day must not count as
    this session's. Folder and date alone are not identity -- that confusion is
    exactly why photos cannot be tied to a deleted session."""
    world = _world(transcripts=False)
    world._objects["transcripts/Neil_Blunden/2026-09-02/other_sid" + "f" * 32 + "_c0000.json"] = OLD
    recent, everything, _ = bl.scan(world, now=NOW)
    assert recent == [] and everything == []


# --------------------------------------------------------------------------

def test_the_metric_is_published_even_when_it_is_zero(monkeypatch):
    """An alarm on a metric that only appears when something is wrong cannot
    tell healthy from "the checker stopped running" -- and invisible absence is
    the failure this function was written for. The alarm is configured
    TreatMissingData: breaching, which only works if the zero is published.
    """
    sent = {}

    class _CW:
        def put_metric_data(self, Namespace=None, MetricData=None):
            sent["ns"] = Namespace
            sent["data"] = MetricData

    monkeypatch.setattr(bl, "_s3", lambda: _world(extracted=True))
    monkeypatch.setattr(bl.boto3, "client",
                        lambda name, *a, **k: _CW() if name == "cloudwatch" else None)
    out = bl.lambda_handler({}, None)
    assert out["recent"] == 0
    assert sent["ns"] == "FieldSight/Pipeline"
    assert sent["data"][0] == {"MetricName": "ExtractionBacklog",
                               "Value": 0, "Unit": "Count"}
