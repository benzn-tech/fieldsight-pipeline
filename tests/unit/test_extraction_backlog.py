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


def _world(*, when=OLD, transcripts=True, extracted=False, body=None,
           skipped_marker=False, marker_when=None, transcript_when=None):
    objs = {REQ_KEY: when}
    if transcripts:
        objs[f"transcripts/Neil_Blunden/2026-09-02/x_sid{SID}_c0000.json"] = (
            transcript_when or when)
    if extracted:
        objs[f"extractions/Neil_Blunden/2026-09-02/sid{SID}.json"] = when
    if skipped_marker:
        objs[f"extractions/Neil_Blunden/2026-09-02/sid{SID}.skipped"] = (
            marker_when or when)
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


def test_the_metric_carries_the_stage_that_published_it(monkeypatch):
    """Both stacks publish into one namespace. Without a dimension they write
    the SAME series, and the failure that creates is not "test pages prod" --
    it is that a dead PROD probe stops being detectable, because a live test
    publisher keeps the series populated and `TreatMissingData: breaching`
    never trips. That is the exact scenario this function exists to catch.
    """
    sent = {}

    class _CW:
        def put_metric_data(self, Namespace=None, MetricData=None):
            sent["data"] = MetricData

    monkeypatch.setattr(bl, "METRIC_STAGE", "prod")
    monkeypatch.setattr(bl, "_s3", lambda: _world(extracted=True))
    monkeypatch.setattr(bl.boto3, "client",
                        lambda name, *a, **k: _CW() if name == "cloudwatch" else None)
    bl.lambda_handler({}, None)
    assert sent["data"][0]["Dimensions"] == [{"Name": "Stage", "Value": "prod"}]


def test_an_unset_stage_publishes_no_dimension_rather_than_an_empty_one(monkeypatch):
    """A dimension whose value is the empty string is a DIFFERENT series from
    no dimension at all, and from a real stage. Emitting one would quietly
    orphan the datapoints where no alarm is watching."""
    sent = {}

    class _CW:
        def put_metric_data(self, Namespace=None, MetricData=None):
            sent["data"] = MetricData

    monkeypatch.setattr(bl, "METRIC_STAGE", "")
    monkeypatch.setattr(bl, "_s3", lambda: _world(extracted=True))
    monkeypatch.setattr(bl.boto3, "client",
                        lambda name, *a, **k: _CW() if name == "cloudwatch" else None)
    bl.lambda_handler({}, None)
    assert "Dimensions" not in sent["data"][0]


# ---------------------------------------------------------------------------
# A transcript file is not evidence that anybody spoke.
#
# Measured on prod 2026-09-09: ten requests were being reported as backlog and
# nine of them had transcripts containing only "[background noise]" or the
# device saying "Recording started" -- 77 to 81 bytes with zero items. The
# tenth was real and had never been summarised. So the transcripts test, which
# was the whole point of this probe, still let nine false positives through and
# the alarm was red for a session where nothing was lost.
#
# The fix is not a better guess in here. `extract_session` already decides this
# -- it filters announcements, finds no usable turns, and skips -- and now
# records that decision as a marker object. This module reads the decision
# instead of re-deriving it, so the two can never disagree.
# ---------------------------------------------------------------------------

def test_a_session_extraction_deliberately_skipped_is_not_a_backlog():
    recent, everything, skipped = bl.scan(_world(skipped_marker=True), now=NOW)
    assert recent == [] and everything == [] and skipped == 0


def test_the_marker_does_not_excuse_a_different_session():
    """The marker is keyed on the session, not the day. A skipped session must
    not silence a real loss recorded beside it."""
    world = _world()
    world._objects["extractions/Neil_Blunden/2026-09-02/sid" + "e" * 32 + ".skipped"] = OLD
    recent, everything, _ = bl.scan(world, now=NOW)
    assert len(recent) == 1 and recent[0]["session"] == f"sid{SID}"


def test_the_marker_suffix_is_not_dot_json():
    """item-writer is wired to `extractions/` with suffix `.json`. A marker
    ending in .json would invoke it with a file it cannot parse, on every
    silent session. The suffix is load-bearing, so it is pinned here."""
    assert bl.SKIP_MARKER_SUFFIX == ".skipped"
    assert not bl.SKIP_MARKER_SUFFIX.endswith(".json")


def test_backlog_and_extractor_agree_on_the_marker_key():
    """Two lambdas, two deployment units, one convention. They cannot share a
    module -- the backlog probe is deliberately dependency-free so it can run
    outside the VPC -- so this is what stops them drifting apart."""
    les = pytest.importorskip("lambda_extract_session")
    assert les.skip_marker_key("Neil_Blunden", "2026-09-02", f"sid{SID}") == (
        f"extractions/Neil_Blunden/2026-09-02/sid{SID}{bl.SKIP_MARKER_SUFFIX}")


# ---------------------------------------------------------------------------
# A marker only speaks for the transcripts that existed when it was written.
#
# extract_session runs on EVERY transcript chunk, and the first chunk of a
# session is very often just the device saying "Recording started" -- filtered,
# no usable turns, marker written, all before any LLM call. Speech arrives in a
# later chunk. If the passes for those chunks then fail (the 2026-09-02 arrears
# outage is the case this whole probe was built for), no extraction is ever
# published but the marker is already there.
#
# Measured on prod while reviewing this: since 2026-08-20, 2 of 17 sessions
# have a sub-200-byte first chunk followed by a real-speech chunk. Honouring
# the marker unconditionally would hide roughly one session in eight, exactly
# when extraction is broken.
# ---------------------------------------------------------------------------

def test_a_transcript_newer_than_the_marker_reopens_the_backlog():
    world = _world(skipped_marker=True,
                   marker_when=OLD - timedelta(hours=2),
                   transcript_when=OLD)
    recent, everything, _ = bl.scan(world, now=NOW)
    assert len(recent) == 1 and recent[0]["session"] == f"sid{SID}"


def test_a_marker_written_after_the_last_transcript_still_silences_it():
    """The ordinary case: nobody spoke in any chunk, extraction looked at the
    finished session and passed over it."""
    world = _world(skipped_marker=True,
                   transcript_when=OLD - timedelta(hours=2),
                   marker_when=OLD)
    recent, everything, _ = bl.scan(world, now=NOW)
    assert recent == [] and everything == []


def test_a_marker_exactly_as_old_as_the_transcript_is_honoured():
    """Same second is the common case -- the pass that wrote the marker is the
    one the transcript triggered. A strict > would reopen every silent
    session and put the alarm right back where it started."""
    world = _world(skipped_marker=True, marker_when=OLD, transcript_when=OLD)
    recent, everything, _ = bl.scan(world, now=NOW)
    assert recent == [] and everything == []
