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
# Multi-device meetings were invisible to this probe entirely.
#
# A group request is `extraction_requests/group-<id>.json` carrying
# {groupId, mergedKey, members[]} and no top-level userFolder, so
# `req["userFolder"]` raised KeyError, the meeting was filed as a fault in the
# enqueuer, and it was never checked at all.
#
# That matters more than it would for a solo session, because a meeting cannot
# be re-driven: extract_group returns None on an LLM failure instead of raising,
# and the claim has already set merged_at, so nothing retries it. A failed merge
# is permanent AND was unreportable.
# ---------------------------------------------------------------------------

GROUP_ID = "0eade7a8d97a4f4e8198b543c5494eac"
GROUP_KEY = f"extraction_requests/group-{GROUP_ID}.json"
MERGED_KEY = f"extractions/Ben_UCPK2/2026-09-02/grp{GROUP_ID}.json"
MEMBER_SID = "b7bff16c1b7c46a7ab27b671f5d1a5fe"

SPOKE = {"results": {"transcripts": [
    {"transcript": "they don't meet the requirements for the bracing so we go back"}]}}
SILENT_DOC = {"results": {"transcripts": [{"transcript": "[background noise]"}]}}


def _group_req():
    return {
        "groupId": GROUP_ID,
        "leadSessionId": GROUP_ID,
        "mergedKey": MERGED_KEY,
        "members": [
            {"userFolder": "Ben_UCPK2", "date": "2026-09-02", "sessionBase": f"sid{GROUP_ID}"},
            {"userFolder": "Ben_UCPK", "date": "2026-09-02", "sessionBase": f"sid{MEMBER_SID}"},
        ],
    }


def _group_world(*, when=OLD, merged=False, spoke=True, solo_member_request=False, req=None):
    a = f"transcripts/Ben_UCPK2/2026-09-02/a_sid{GROUP_ID}_c0000.json"
    b = f"transcripts/Ben_UCPK/2026-09-02/b_sid{MEMBER_SID}_c0000.json"
    objs = {GROUP_KEY: when, a: when, b: when}
    doc = SPOKE if spoke else SILENT_DOC
    bodies = {GROUP_KEY: req if req is not None else _group_req(), a: doc, b: doc}
    if merged:
        objs[MERGED_KEY] = when
    if solo_member_request:
        k = f"extraction_requests/{MEMBER_SID}.json"
        objs[k] = when
        bodies[k] = {"userFolder": "Ben_UCPK", "date": "2026-09-02",
                     "sessionBase": f"sid{MEMBER_SID}"}
    return FakeS3(objs, bodies)


def test_a_merged_meeting_is_not_a_backlog():
    recent, everything, skipped = bl.scan(_group_world(merged=True), now=NOW)
    assert recent == [] and everything == [] and skipped == 0


def test_a_meeting_that_was_never_merged_is_a_backlog():
    recent, everything, skipped = bl.scan(_group_world(), now=NOW)
    assert skipped == 0, "a group request is readable, not a fault in the enqueuer"
    assert len(recent) == 1
    assert recent[0]["session"] == f"grp{GROUP_ID}"


def test_a_meeting_nobody_spoke_in_is_not_a_backlog():
    """Same discriminator as a solo session, applied across the members."""
    recent, everything, _ = bl.scan(_group_world(spoke=False), now=NOW)
    assert recent == [] and everything == []


def test_a_member_of_a_merged_meeting_is_not_its_own_backlog():
    """The member's words are in the meeting record, under the group's key.

    This was the second false-positive class in the 2026-09-09 alarm: the
    session it was red for was a member of a group that had already merged.
    """
    recent, everything, _ = bl.scan(
        _group_world(merged=True, solo_member_request=True), now=NOW)
    assert recent == [] and everything == []


def test_a_member_of_a_meeting_that_never_merged_is_still_reported():
    """A lost merge must not silence its members too -- that would turn one
    invisible loss into three."""
    recent, _, _ = bl.scan(_group_world(solo_member_request=True), now=NOW)
    assert sorted(i["session"] for i in recent) == [f"grp{GROUP_ID}", f"sid{MEMBER_SID}"]


def test_a_group_request_missing_its_merged_key_is_a_fault_not_a_meeting():
    bad = {"groupId": GROUP_ID, "members": _group_req()["members"]}
    recent, everything, skipped = bl.scan(_group_world(req=bad), now=NOW)
    assert skipped == 1 and recent == [] and everything == []


# A bad request object must cost one line, not the whole probe. A scan that
# dies publishes no metric, so TreatMissingData:breaching fires the alarm AND
# the real backlog stops being reported until somebody finds the object by hand.

@pytest.mark.parametrize("body", [b"null", 7, [], "text", {"userFolder": "F"}])
def test_a_request_that_is_not_a_valid_object_is_counted_not_fatal(body):
    recent, everything, skipped = bl.scan(_world(body=body), now=NOW)
    assert skipped == 1 and recent == [] and everything == []


@pytest.mark.parametrize("members", ["notalist", [None], [7], []])
def test_a_group_whose_members_are_not_objects_is_counted_not_fatal(members):
    bad = dict(_group_req(), members=members)
    recent, everything, skipped = bl.scan(_group_world(req=bad), now=NOW)
    assert skipped == 1 and recent == [] and everything == []
