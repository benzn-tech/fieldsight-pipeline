"""Spec 2026-10-07 D6 (backend): GET /sessions/pending, POST /sessions/{id}/expedite, the
backlog lambda's expedite notice, and the time range the marker now carries."""
import io
import json
from datetime import datetime, timedelta, timezone

import pytest
from botocore.exceptions import ClientError

import extraction_pending as ep
import lambda_extraction_backlog as bl

oa = pytest.importorskip("lambda_org_api", reason="requires psycopg (installed in CI)")

BUCKET = "lake"
FOLDER, OTHER = "Ben_Lin_test2", "Someone_Else"
DATE = "2026-10-07"
BASE = "sid" + "a" * 32
OTHER_BASE = "sid" + "b" * 32
NOW = datetime(2026, 10, 7, 3, 0, tzinfo=timezone.utc)
RANGE = "14:11–14:14"
CALLER = {"id": "u1", "company_id": "c1", "cognito_sub": "sub-1", "folder_name": FOLDER,
          "first_name": "Ben", "last_name": "Lin", "global_role": "worker"}


def Missing():
    return ClientError({"Error": {"Code": "NoSuchKey"}}, "GetObject")


class FakeS3:
    def __init__(self, objects=None):
        self.objects = {k: json.dumps(v).encode() for k, v in (objects or {}).items()}
        self.puts = []

    def get_object(self, Bucket, Key):
        if Key not in self.objects:
            raise Missing()
        return {"Body": io.BytesIO(self.objects[Key]), "ETag": '"e1"'}

    def put_object(self, Bucket, Key, Body, **kw):
        self.puts.append((Key, kw))
        self.objects[Key] = Body if isinstance(Body, bytes) else Body.encode()

    def list_objects_v2(self, Bucket, Prefix, **kw):
        return {"Contents": [{"Key": k} for k in sorted(self.objects) if k.startswith(Prefix)],
                "IsTruncated": False}

    def marker(self, base=BASE):
        return json.loads(self.objects[ep.marker_key(base)])


def _marker(base=BASE, folder=FOLDER, **kw):
    m = {"userFolder": folder, "date": DATE, "sessionBase": base, "request_key": "r.json",
         "attempts": 2, "first_failed_at": "2026-10-07T01:00:00Z", "expedite": False,
         "next_attempt_at": "2026-10-07T09:00:00Z", "last_error": "HTTP 502",
         "time_range": RANGE}
    m.update(kw)
    return m


@pytest.fixture
def web(monkeypatch):
    s3 = FakeS3({ep.marker_key(BASE): _marker(),
                 ep.marker_key(OTHER_BASE): _marker(OTHER_BASE, OTHER)})
    monkeypatch.setattr(oa, "s3", lambda: s3)
    monkeypatch.setattr(oa, "LAKE_BUCKET", BUCKET)
    monkeypatch.setattr(oa.companies, "get_company_by_id", lambda conn, cid: {"name": "UCPK"})
    return s3


def _body(resp):
    return json.loads(resp["body"])


def _pending(date=DATE, caller=CALLER):
    return oa.sessions_pending(None, caller, {"queryStringParameters": {"date": date}})


# ---- POST expedite ----------------------------------------------------------

def test_the_owner_can_expedite_and_the_marker_carries_who_and_when(web):
    r = oa.session_expedite(None, CALLER, BASE)
    assert r["statusCode"] == 200 and _body(r)["ok"] is True and _body(r)["expedited_at"]
    m = web.marker()
    assert m["expedite"] is True and m["expedite_by"] == "sub-1"
    assert (m["expedite_by_name"], m["expedite_by_company"]) == ("Ben Lin", "UCPK")
    assert m["expedite_requested_at"] == m["next_attempt_at"] == _body(r)["expedited_at"]
    assert web.puts[-1][1].get("IfMatch") == '"e1"'      # conditional write


def test_a_bare_hex_id_without_the_sid_prefix_finds_the_same_marker(web):
    assert oa.session_expedite(None, CALLER, "a" * 32)["statusCode"] == 200


def test_another_users_session_is_404_and_untouched(web):
    r = oa.session_expedite(None, CALLER, OTHER_BASE)
    assert r["statusCode"] == 404
    assert web.marker(OTHER_BASE)["expedite"] is False and not web.puts


def test_no_marker_and_promised_only_are_404(web):
    assert oa.session_expedite(None, CALLER, "sid" + "c" * 32)["statusCode"] == 404
    web.objects[ep.marker_key(BASE)] = json.dumps(
        {"sessionBase": BASE, "promised_only": True, "userFolder": FOLDER}).encode()
    assert oa.session_expedite(None, CALLER, BASE)["statusCode"] == 404


def test_a_second_request_within_ten_minutes_is_429_with_retry_after(web):
    first = datetime.now(timezone.utc) - timedelta(minutes=4)
    web.objects[ep.marker_key(BASE)] = json.dumps(
        _marker(expedite_requested_at=ep.iso(first))).encode()
    r = oa.session_expedite(None, CALLER, BASE)
    assert r["statusCode"] == 429
    assert _body(r)["error"] == "already expedited" and 300 <= _body(r)["retry_after_s"] <= 361
    assert not web.puts


def test_after_ten_minutes_it_can_be_asked_again_and_the_notice_is_re_armed(web):
    old = datetime.now(timezone.utc) - timedelta(minutes=11)
    web.objects[ep.marker_key(BASE)] = json.dumps(
        _marker(expedite_requested_at=ep.iso(old), expedite_notified_at=ep.iso(old))).encode()
    assert oa.session_expedite(None, CALLER, BASE)["statusCode"] == 200
    assert "expedite_notified_at" not in web.marker()


def test_an_sdk_without_if_match_falls_back_to_a_plain_write(web):
    real = web.put_object

    def put(Bucket, Key, Body, **kw):
        if "IfMatch" in kw:
            raise oa.ParamValidationError(report="Unknown parameter IfMatch")
        real(Bucket, Key, Body, **kw)
    web.put_object = put
    assert oa.session_expedite(None, CALLER, BASE)["statusCode"] == 200
    assert web.marker()["expedite"] is True


# ---- GET pending ------------------------------------------------------------

def test_pending_lists_only_the_callers_folder_and_date(web):
    web.objects[ep.marker_key("sidday2")] = json.dumps(
        _marker("sidday2", date="2026-10-06")).encode()
    body = _body(_pending())
    assert [s["session_id"] for s in body["sessions"]] == [BASE]
    s = body["sessions"][0]
    assert (s["date"], s["time_range"], s["attempts"], s["expedited_at"]) == (
        DATE, RANGE, 2, None)
    assert s["first_failed_at"] == "2026-10-07T01:00:00Z"


def test_pending_excludes_promised_only_and_keeps_a_null_range(web):
    web.objects[ep.marker_key("sidprom")] = json.dumps(
        {"sessionBase": "sidprom", "promised_only": True, "userFolder": FOLDER,
         "date": DATE}).encode()
    m = _marker()
    m.pop("time_range")
    web.objects[ep.marker_key(BASE)] = json.dumps(m).encode()
    sessions = _body(_pending())["sessions"]
    assert [s["session_id"] for s in sessions] == [BASE] and sessions[0]["time_range"] is None


def test_pending_shows_when_it_was_expedited(web):
    web.objects[ep.marker_key(BASE)] = json.dumps(
        _marker(expedite_requested_at="2026-10-07T02:00:00Z")).encode()
    assert _body(_pending())["sessions"][0]["expedited_at"] == "2026-10-07T02:00:00Z"


@pytest.mark.parametrize("bad", ["", "yesterday", "2026-13-40", "2026-10-7"])
def test_pending_validates_the_date(web, bad):
    assert _pending(bad)["statusCode"] == 400


def test_pending_without_a_folder_is_empty_not_everyones(web):
    assert _body(_pending(caller={**CALLER, "folder_name": None})) == {"sessions": []}


def test_the_routes_are_registered_in_the_dispatcher():
    src = open(oa.__file__, encoding="utf-8").read()
    assert 'route == "/sessions/pending"' in src and "/expedite$" in src


# ---- backlog notice ---------------------------------------------------------

class FakeSns:
    def __init__(self, fail=False):
        self.sent, self.fail = [], fail

    def publish(self, **kw):
        if self.fail:
            raise RuntimeError("sns down")
        self.sent.append(kw)


@pytest.fixture
def bk(monkeypatch):
    monkeypatch.setattr(bl, "S3_BUCKET", BUCKET)
    monkeypatch.setattr(bl, "ALERT_TOPIC_ARN", "arn:aws:sns:x:1:alerts")
    sns = FakeSns()
    monkeypatch.setattr(bl, "_sns", lambda: sns)
    s3 = FakeS3({"r.json": {"userFolder": FOLDER, "date": DATE, "sessionBase": BASE}})
    s3.objects[ep.marker_key(BASE)] = json.dumps(_marker(
        expedite=True, expedite_requested_at="2026-10-07T02:59:00Z",
        expedite_by_name="Ben Lin", expedite_by_company="UCPK")).encode()
    return s3, sns


def test_an_expedite_publishes_once_stamps_and_re_drives(bk):
    s3, sns = bk
    redriven, _ = bl.redrive(s3, now=NOW)
    assert redriven == [BASE] and len(sns.sent) == 1
    assert sns.sent[0]["Message"] == (
        "Customer Ben Lin (UCPK) asked to expedite their recording " + BASE +
        " (2026-10-07 " + RANGE + "). Attempts so far: 2. Last error: HTTP 502.")
    assert s3.marker()["expedite_notified_at"]
    bl.redrive(s3, now=NOW + timedelta(minutes=5))
    assert len(sns.sent) == 1                                   # once per expedite


def test_a_marker_not_due_and_not_expedited_publishes_nothing(bk):
    s3, sns = bk
    s3.objects[ep.marker_key(BASE)] = json.dumps(_marker()).encode()
    bl.redrive(s3, now=NOW)
    assert not sns.sent


def test_no_alert_topic_is_a_quiet_no_op(bk, monkeypatch):
    s3, sns = bk
    monkeypatch.setattr(bl, "ALERT_TOPIC_ARN", "")
    redriven, _ = bl.redrive(s3, now=NOW)
    assert redriven == [BASE] and not sns.sent
    assert "expedite_notified_at" not in s3.marker()


def test_a_failed_publish_does_not_stamp_so_the_next_run_retries(bk, monkeypatch):
    s3, _ = bk
    monkeypatch.setattr(bl, "_sns", lambda: FakeSns(fail=True))
    bl.redrive(s3, now=NOW)                                    # must not raise
    assert "expedite_notified_at" not in s3.marker()
    good = FakeSns()
    monkeypatch.setattr(bl, "_sns", lambda: good)
    bl.redrive(s3, now=NOW + timedelta(minutes=5))
    assert len(good.sent) == 1


def test_a_notice_is_stamped_even_when_the_marker_is_not_due(bk):
    s3, sns = bk
    s3.objects[ep.marker_key(BASE)] = json.dumps(_marker(
        expedite=False, expedite_requested_at="2026-10-07T02:59:00Z")).encode()
    bl.redrive(s3, now=NOW)
    assert len(sns.sent) == 1 and s3.marker()["expedite_notified_at"]


# ---- time range on the marker -----------------------------------------------

def test_time_range_from_transcript_names():
    keys = [f"transcripts/{FOLDER}/{DATE}/{FOLDER}_{DATE}_14-11-05_off0.0_to30.0_{BASE}_c0001.json",
            f"transcripts/{FOLDER}/{DATE}/{FOLDER}_{DATE}_14-13-40_off0.0_to50.0_{BASE}_c0003.json"]
    assert ep.time_range_from_keys(keys) == (
        "2026-10-07T14:11:05", "2026-10-07T14:14:30", RANGE)


def test_one_short_chunk_reads_as_a_single_time_and_no_names_as_none():
    one = [f"transcripts/f/{DATE}/f_{DATE}_09-00-10_{BASE}.json"]
    assert ep.time_range_from_keys(one)[2] == "09:00"
    assert ep.time_range_from_keys([]) == (None, None, None)
    assert ep.time_range_from_keys(["transcripts/f/d/nothing.json"]) == (None, None, None)


def test_record_failure_stores_the_range_and_keeps_it_on_a_repeat():
    s3 = FakeS3()
    key = f"transcripts/{FOLDER}/{DATE}/{FOLDER}_{DATE}_14-11-05_{BASE}.json"
    m = ep.record_failure(s3, BUCKET, user_folder=FOLDER, date=DATE, session_base=BASE,
                          request_key="r", error="x", now=NOW, segment_keys=[key])
    assert m["time_range"] == "14:11" and s3.marker()["started_at"] == "2026-10-07T14:11:05"
    again = ep.record_failure(s3, BUCKET, user_folder=FOLDER, date=DATE, session_base=BASE,
                              request_key="r", error="y", now=NOW, segment_keys=None)
    assert again["time_range"] == "14:11"


def test_extract_session_writes_the_range_into_the_marker(monkeypatch):
    import lambda_extract_session as les
    seg = (f"transcripts/{FOLDER}/{DATE}/{FOLDER}_{DATE}_14-11-05_off0.0_to30.0_"
           f"srcwav_{BASE}_c0001.json")

    class S3(FakeS3):
        def get_paginator(self, op):
            outer = self

            class _P:
                def paginate(self, Bucket, Prefix):
                    yield {"Contents": [{"Key": k} for k in outer.objects
                                        if k.startswith(Prefix)]}
            return _P()

    s3 = S3({seg: {}})
    monkeypatch.setattr(les, "s3", lambda: s3)
    les._record_pending(BUCKET, "r.json", FOLDER, DATE, BASE, "HTTP 502")
    assert s3.marker()["time_range"] == "14:11"


def test_the_template_wires_org_api_and_the_backlog_notice():
    text = open("src/template.yaml", encoding="utf-8").read()
    backlog = text[text.index("  ExtractionBacklogFunction:"):text.index("  ExtractionBacklogAlarm:")]
    assert "ALERT_TOPIC_ARN: !If [ShouldCreateAlerts, !Ref AlertTopic" in backlog
    assert "SNSPublishMessagePolicy" in backlog
    org = text[text.index("  OrgApiFunction:"):text.index("  OrgApiFunction:") + 40000]
    org = org[:org.index("      Events:")]
    assert "${IngestBucketName}/extraction_pending/*" in org      # Get/Put grant
    assert org.count("- extraction_pending/*") == 1          # ListBucket prefix
