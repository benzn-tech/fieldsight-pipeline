"""A final extraction that fails on every model is recorded, re-driven until it
succeeds, and the recorder is told the truth meanwhile (spec 2026-10-07 D4/D5).

Incident (prod 2026-10-07): extract-session failed six times on muse 502/503, nothing
retried it, finalize waited 300 s and emailed "Nothing was captured for this recording."
"""
import io
import json
import os
from datetime import datetime, timedelta, timezone

import pytest

os.environ.setdefault("AWS_ACCESS_KEY_ID", "testing")
os.environ.setdefault("AWS_SECRET_ACCESS_KEY", "testing")
os.environ.setdefault("AWS_DEFAULT_REGION", "ap-southeast-2")
os.environ.setdefault("ANTHROPIC_API_KEY", "test-key")

import extraction_pending as ep
import lambda_extract_session as les
import lambda_extraction_backlog as bl
import lambda_session_finalize as fin
import llm_utils

BUCKET = "bkt"
SID = "2f3e" + "a" * 28
BASE = f"sid{SID}"
FOLDER, DATE = "Ben_Lin_test2", "2026-10-07"
REQ_KEY = f"{les.FINAL_REQUESTS_PREFIX}{SID}.json"
SEG = f"transcripts/{FOLDER}/{DATE}/{FOLDER}_{DATE}_10-00-00_off0.0_to30.0_srcwav_{BASE}_c0001.json"
NOW = datetime(2026, 10, 7, 3, 0, tzinfo=timezone.utc)
RANGE = "14:11–14:14"


class Missing(Exception):
    def __init__(self):
        super().__init__("NoSuchKey")
        self.response = {"Error": {"Code": "NoSuchKey"}}


class FakeS3:
    def __init__(self, objects=None):
        self.objects = {k: (v if isinstance(v, bytes) else v.encode())
                        for k, v in (objects or {}).items()}
        self.puts, self.deletes = [], []

    def get_object(self, Bucket, Key):
        if Key not in self.objects:
            raise Missing()
        return {"Body": io.BytesIO(self.objects[Key])}

    def put_object(self, Bucket, Key, Body, **kw):
        self.puts.append(Key)
        self.objects[Key] = Body if isinstance(Body, bytes) else Body.encode()
        return {}

    def delete_object(self, Bucket, Key):
        self.deletes.append(Key)
        self.objects.pop(Key, None)

    def list_objects_v2(self, Bucket, Prefix, **kw):
        return {"Contents": [{"Key": k} for k in sorted(self.objects) if k.startswith(Prefix)],
                "IsTruncated": False}

    def get_paginator(self, op):
        outer = self

        class _P:
            def paginate(self, Bucket, Prefix):
                yield {"Contents": [{"Key": k} for k in outer.objects if k.startswith(Prefix)]}
        return _P()

    def marker(self):
        raw = self.objects.get(ep.marker_key(BASE))
        return json.loads(raw) if raw else None


def _transcribe(text):
    words = text.split()
    return json.dumps({"results": {"transcripts": [{"transcript": text}], "items": [
        {"type": "pronunciation", "start_time": f"{i}.0", "end_time": f"{i + 1}.0",
         "alternatives": [{"content": w, "confidence": "0.9"}]} for i, w in enumerate(words)]}})


@pytest.fixture
def lam(monkeypatch):
    s3 = FakeS3({REQ_KEY: json.dumps({"userFolder": FOLDER, "date": DATE, "sessionBase": BASE}),
                 SEG: _transcribe("pour the slab tomorrow morning")})
    monkeypatch.setattr(les, "s3", lambda: s3)
    monkeypatch.setattr(les, "S3_BUCKET", BUCKET)
    les._sites_cache = None
    return s3


def _event(key=REQ_KEY):
    return {"Records": [{"s3": {"object": {"key": key}}}]}


def _model_down(*a, **k):
    return None, "HTTP 502: Provider returned error"


def _model_up(*a, **k):
    return json.dumps({"topics": [], "declared_site": None}), None


# ---- D4: the marker ---------------------------------------------------------

def test_a_model_failure_on_the_final_pass_writes_the_marker_and_still_raises(lam, monkeypatch):
    monkeypatch.setattr(llm_utils, "call_llm", _model_down)
    with pytest.raises(RuntimeError, match="Claude call failed"):
        les.lambda_handler(_event(), None)      # still an Error: alarms and S3 retries unchanged
    m = lam.marker()
    assert m["userFolder"] == FOLDER and m["date"] == DATE and m["sessionBase"] == BASE
    assert m["request_key"] == REQ_KEY
    assert m["attempts"] == 0 and m["expedite"] is False
    assert "502" in m["last_error"]
    assert m["first_failed_at"] and m["next_attempt_at"] > m["first_failed_at"]


def test_a_second_failure_keeps_the_schedule_and_the_first_failure_time(lam, monkeypatch):
    monkeypatch.setattr(llm_utils, "call_llm", _model_down)
    first = {"userFolder": FOLDER, "date": DATE, "sessionBase": BASE, "request_key": REQ_KEY,
             "attempts": 3, "first_failed_at": "2026-10-07T01:00:00Z",
             "next_attempt_at": "2026-10-07T05:00:00Z", "expedite": False}
    lam.objects[ep.marker_key(BASE)] = json.dumps(first).encode()
    with pytest.raises(RuntimeError):
        les.lambda_handler(_event(), None)
    m = lam.marker()
    assert (m["attempts"], m["first_failed_at"], m["next_attempt_at"]) == (
        3, "2026-10-07T01:00:00Z", "2026-10-07T05:00:00Z")


def test_success_deletes_the_marker(lam, monkeypatch):
    lam.objects[ep.marker_key(BASE)] = json.dumps({"sessionBase": BASE}).encode()
    monkeypatch.setattr(llm_utils, "call_llm", _model_up)
    les.lambda_handler(_event(), None)
    assert lam.marker() is None and ep.marker_key(BASE) in lam.deletes


def test_a_live_pass_failure_leaves_no_marker(lam, monkeypatch):
    """Decision: a live failure is superseded by the final pass the close always
    requests, so it must not queue a re-drive of its own."""
    monkeypatch.setattr(llm_utils, "call_llm", _model_down)
    with pytest.raises(RuntimeError):
        les.lambda_handler(_event(SEG), None)
    assert lam.marker() is None


def test_a_parse_failure_is_not_marked(lam, monkeypatch):
    """A re-drive would repeat it unchanged (and pay for it hourly forever)."""
    monkeypatch.setattr(llm_utils, "call_llm", lambda *a, **k: ("not json at all", None))
    with pytest.raises(RuntimeError):
        les.lambda_handler(_event(), None)
    assert lam.marker() is None


def test_a_recovered_pass_after_the_error_email_is_flagged_for_the_follow_up(lam, monkeypatch):
    lam.objects[ep.marker_key(BASE)] = json.dumps(
        {"sessionBase": BASE, "error_email_sent_at": "2026-10-07T02:00:00Z"}).encode()
    monkeypatch.setattr(llm_utils, "call_llm", _model_up)
    les.lambda_handler(_event(), None)
    out = [k for k in lam.objects if k.startswith("extractions/")]
    assert json.loads(lam.objects[out[0]])["recovered_after_error"] is True


def test_a_recovered_pass_without_an_error_email_is_not_flagged(lam, monkeypatch):
    lam.objects[ep.marker_key(BASE)] = json.dumps({"sessionBase": BASE}).encode()
    monkeypatch.setattr(llm_utils, "call_llm", _model_up)
    les.lambda_handler(_event(), None)
    out = [k for k in lam.objects if k.startswith("extractions/")]
    assert "recovered_after_error" not in json.loads(lam.objects[out[0]])


# ---- D4: backoff and re-drive -----------------------------------------------

def test_the_backoff_is_5_15_30_60_then_hourly_forever():
    assert [ep.backoff_minutes(n) for n in range(0, 12)] == [5, 15, 30, 60] + [60] * 8


def _marker(attempts=0, due_in_min=-1, **over):
    m = {"userFolder": FOLDER, "date": DATE, "sessionBase": BASE, "request_key": REQ_KEY,
         "attempts": attempts, "first_failed_at": ep.iso(NOW - timedelta(minutes=10)),
         "next_attempt_at": ep.iso(NOW + timedelta(minutes=due_in_min)), "expedite": False}
    m.update(over)
    return m


@pytest.fixture
def bk(monkeypatch):
    monkeypatch.setattr(bl, "S3_BUCKET", BUCKET)
    return FakeS3({REQ_KEY: json.dumps({"userFolder": FOLDER, "date": DATE, "sessionBase": BASE})})


def test_a_due_marker_re_puts_the_original_request_and_reschedules(bk):
    bk.objects[ep.marker_key(BASE)] = json.dumps(_marker(attempts=0)).encode()
    original = bk.objects[REQ_KEY]
    redriven, _ = bl.redrive(bk, now=NOW)
    assert redriven == [BASE] and REQ_KEY in bk.puts       # the put IS the trigger
    assert bk.objects[REQ_KEY] == original
    m = bk.marker()
    assert m["attempts"] == 1
    assert m["next_attempt_at"] == ep.iso(NOW + timedelta(minutes=15))


def test_a_marker_that_is_not_due_is_left_alone(bk):
    bk.objects[ep.marker_key(BASE)] = json.dumps(_marker(due_in_min=+3)).encode()
    redriven, _ = bl.redrive(bk, now=NOW)
    assert redriven == [] and bk.puts == []


def test_it_never_gives_up_attempt_ten_still_schedules(bk):
    bk.objects[ep.marker_key(BASE)] = json.dumps(_marker(attempts=9)).encode()
    redriven, _ = bl.redrive(bk, now=NOW)
    assert redriven == [BASE]
    m = bk.marker()
    assert m["attempts"] == 10 and m["next_attempt_at"] == ep.iso(NOW + timedelta(minutes=60))


def test_the_ladder_walks_15_30_60_60_60(bk):
    bk.objects[ep.marker_key(BASE)] = json.dumps(_marker(attempts=0)).encode()
    gaps, t = [], NOW
    for _ in range(5):
        bl.redrive(bk, now=t)
        due = ep.parse_iso(bk.marker()["next_attempt_at"])
        gaps.append(int((due - t).total_seconds() // 60))
        t = due
    assert gaps == [15, 30, 60, 60, 60]


def test_expedite_re_drives_at_once(bk):
    bk.objects[ep.marker_key(BASE)] = json.dumps(_marker(due_in_min=+50, expedite=True)).encode()
    redriven, _ = bl.redrive(bk, now=NOW)
    assert redriven == [BASE] and bk.marker()["expedite"] is False


def test_a_failed_put_leaves_the_marker_in_place(bk):
    bk.objects[ep.marker_key(BASE)] = json.dumps(_marker()).encode()
    real = bk.put_object

    def flaky(Bucket, Key, Body, **kw):
        if Key == REQ_KEY:
            raise RuntimeError("throttled")
        return real(Bucket, Key, Body, **kw)
    bk.put_object = flaky
    assert bl.redrive(bk, now=NOW)[0] == []
    assert bk.marker() is not None


def test_extraction_pending_counts_only_markers_older_than_30_minutes(bk):
    old = _marker(first_failed_at=ep.iso(NOW - timedelta(minutes=31)), due_in_min=+5)
    young = dict(_marker(first_failed_at=ep.iso(NOW - timedelta(minutes=29)), due_in_min=+5),
                 sessionBase="sidyoung")
    bk.objects[ep.marker_key(BASE)] = json.dumps(old).encode()
    bk.objects[ep.marker_key("sidyoung")] = json.dumps(young).encode()
    assert bl.redrive(bk, now=NOW)[1] == 1


def test_the_metric_is_an_emf_line_with_a_stage_dimension(capsys, monkeypatch):
    monkeypatch.setattr(bl, "METRIC_STAGE", "prod")
    bl.emit_pending_metric(2)
    line = json.loads(capsys.readouterr().out.strip().splitlines()[-1])
    spec = line["_aws"]["CloudWatchMetrics"][0]
    assert spec["Namespace"] == "FieldSight/Pipeline" and spec["Dimensions"] == [["Stage"]]
    assert spec["Metrics"][0]["Name"] == "ExtractionPending"
    assert line["ExtractionPending"] == 2 and line["Stage"] == "prod"


def test_the_fast_schedule_only_re_drives_and_never_scans(bk, monkeypatch):
    monkeypatch.setattr(bl, "_s3", lambda: bk)
    monkeypatch.setattr(bl, "scan", lambda c: pytest.fail("the 5-minute event must not scan"))
    out = bl.lambda_handler({"task": "redrive"}, None)
    assert out == {"redriven": [], "pending_stale": 0}


# ---- D5: the email tells the truth ------------------------------------------

def _rolling(**over):
    a = {"kind": "rolling", "sessionId": SID, "recipient": "ben@example.nz", "date": DATE,
         "timeRange": RANGE, "siteName": "UC PK", "summary": "", "openTodos": []}
    a.update(over)
    return a


def _run(artifact, **kw):
    sent, results, marked = [], [], []
    out = fin.process_finalize_request(
        artifact, send=lambda *a: sent.append(a),
        write_result=lambda sid, p: results.append((sid, p)),
        already_sent=kw.pop("already_sent", lambda rid: False),
        mark_error_email_sent=lambda sid: marked.append(sid), **kw)
    return out, sent, results, marked


def test_a_failed_extraction_gets_the_model_error_email_not_nothing_captured():
    out, sent, results, marked = _run(_rolling(), read_pending=lambda sid: {"sessionBase": BASE})
    _to, subject, text, html = sent[0]
    assert "Nothing was captured" not in text + html
    assert "notes are delayed" in subject and f"2026-10-07 {RANGE}" in subject
    assert text.startswith(f"Your recording from UC PK on 2026-10-07 {RANGE} reached us safely.")
    assert "temporary model error" in text
    assert "we'll email your notes as soon as processing completes" in text
    assert out["kind"] == "model_error"
    assert results[0][1]["status"] == "sent" and results[0][1]["kind"] == "model_error"
    assert marked == [SID]                                  # error_email_sent_at is recorded


def test_nothing_captured_only_when_extraction_succeeded_and_found_nothing():
    # a FINAL (extraction succeeded) with no items
    _o, sent, _r, marked = _run(_rolling(kind="final"),
                                read_pending=lambda sid: pytest.fail("no lookup"),
                                poll_brief=lambda *a, **k: None)
    assert "Nothing was captured for this recording." in sent[0][2] and marked == []
    # a rolling backstop with no marker and nothing to show must NOT say it: nobody has
    # read the audio, so "nothing was captured" would be a claim about audio unread
    promised = []
    _o, sent, results, marked = _run(_rolling(), read_pending=lambda sid: None,
                                     promise_notes=lambda sid: promised.append(sid),
                                     recheck_final=lambda a: False)
    _to, subject, text, html = sent[0]
    assert "Nothing was captured" not in text + html
    assert "notes are on the way" in subject and f"2026-10-07 {RANGE}" in subject
    assert text == ("Your recording reached us safely; your notes are still being processed "
                    "\u2014 we'll email them as soon as they're ready.\n")
    assert results[0][1]["kind"] == "notes_promised"
    assert promised == [SID] and marked == []


def test_a_rolling_backstop_with_real_rows_is_still_the_confirmation():
    rows = [{"text": "Pour the slab", "responsible": "Ben", "due": None}]
    _o, sent, _r, _m = _run(_rolling(openTodos=rows), read_pending=lambda sid: None,
                            promise_notes=lambda sid: pytest.fail("nothing was promised"))
    assert "Pour the slab" in sent[0][2] and "on the way" not in sent[0][1]


def test_a_failed_promise_send_stamps_nothing():
    promised = []

    def boom(*a):
        raise RuntimeError("ses down")
    out = fin.process_finalize_request(
        _rolling(), send=boom, write_result=lambda *a: None, already_sent=lambda r: False,
        read_pending=lambda sid: None, promise_notes=lambda sid: promised.append(sid))
    assert out["status"] == "error" and promised == []


# ---- the promise is a marker the final's success reads -----------------------

def test_promise_notes_creates_a_promised_only_marker_the_recovery_path_sees(monkeypatch):
    s3 = FakeS3()
    ep.promise_notes(s3, BUCKET, BASE, now=NOW)
    m = s3.marker()
    assert m["promised_only"] is True and m["error_email_sent_at"]
    assert "request_key" not in m
    # extract-session's recovery check reads exactly this stamp
    monkeypatch.setattr(les, "s3", lambda: s3)
    assert les._recovering_after_error_email(BUCKET, BASE) is True


def test_promise_notes_on_an_existing_marker_only_stamps_it():
    s3 = FakeS3()
    ep.record_failure(s3, BUCKET, user_folder=FOLDER, date=DATE, session_base=BASE,
                      request_key=REQ_KEY, error="x", now=NOW)
    ep.promise_notes(s3, BUCKET, BASE, now=NOW)
    m = s3.marker()
    assert m["error_email_sent_at"] and m["request_key"] == REQ_KEY and "promised_only" not in m


def test_the_re_driver_skips_a_promised_only_marker_but_counts_it_once_stale(bk=None):
    s3 = FakeS3()
    ep.promise_notes(s3, BUCKET, BASE, now=NOW - timedelta(minutes=45))
    assert bl.redrive(s3, now=NOW) == ([], 1)
    assert s3.puts == [ep.marker_key(BASE)]           # nothing re-put, nothing rescheduled


def test_a_real_failure_upgrades_a_promised_only_marker_and_keeps_the_promise():
    s3 = FakeS3()
    ep.promise_notes(s3, BUCKET, BASE, now=NOW)
    ep.record_failure(s3, BUCKET, user_folder=FOLDER, date=DATE, session_base=BASE,
                      request_key=REQ_KEY, error="502", now=NOW)
    m = s3.marker()
    assert "promised_only" not in m and m["request_key"] == REQ_KEY
    assert m["error_email_sent_at"] and m["next_attempt_at"]


# ---- race: the final landed before the email was stamped ---------------------

def _extraction_key():
    return f"extractions/{FOLDER}/{DATE}/{BASE}.json"


def _world(monkeypatch, objects):
    s3 = FakeS3(objects)
    import boto3
    monkeypatch.setattr(boto3, "client", lambda *a, **k: s3)
    monkeypatch.setattr(fin, "S3_BUCKET", BUCKET)
    return s3


def test_a_final_that_beat_the_email_gets_the_follow_up_re_fired(monkeypatch):
    s3 = _world(monkeypatch, {_extraction_key(): json.dumps({"tier": "final", "topics": []})})
    assert fin._recheck_final_extraction(_rolling(folder=FOLDER)) is True
    body = json.loads(s3.objects[_extraction_key()])
    assert body["recovered_after_error"] is True and body["tier"] == "final"
    assert _extraction_key() in s3.puts               # the put is what re-fires item-writer


def test_no_extraction_yet_means_nothing_to_re_fire(monkeypatch):
    s3 = _world(monkeypatch, {})
    assert fin._recheck_final_extraction(_rolling(folder=FOLDER)) is False
    assert s3.puts == []


def test_an_already_flagged_extraction_is_not_re_put(monkeypatch):
    s3 = _world(monkeypatch, {_extraction_key(): json.dumps(
        {"tier": "final", "recovered_after_error": True})})
    assert fin._recheck_final_extraction(_rolling(folder=FOLDER)) is True
    assert s3.puts == []


def test_a_live_extraction_is_not_a_final(monkeypatch):
    s3 = _world(monkeypatch, {_extraction_key(): json.dumps({"tier": "live"})})
    assert fin._recheck_final_extraction(_rolling(folder=FOLDER)) is False
    assert s3.puts == []


def test_the_race_tidies_a_promised_only_marker_the_final_already_cleared(monkeypatch):
    s3 = _world(monkeypatch, {_extraction_key(): json.dumps({"tier": "final"})})
    ep.promise_notes(s3, BUCKET, BASE, now=NOW)
    fin._recheck_final_extraction(_rolling(folder=FOLDER))
    assert s3.marker() is None


def test_the_race_keeps_a_real_marker(monkeypatch):
    s3 = _world(monkeypatch, {_extraction_key(): json.dumps({"tier": "final"})})
    ep.record_failure(s3, BUCKET, user_folder=FOLDER, date=DATE, session_base=BASE,
                      request_key=REQ_KEY, error="x", now=NOW)
    fin._recheck_final_extraction(_rolling(folder=FOLDER))
    assert s3.marker() is not None


def test_the_error_and_promise_paths_both_run_the_re_check():
    for pend in ({"sessionBase": BASE}, None):
        calls = []
        _run(_rolling(), read_pending=lambda sid, p=pend: p,
             promise_notes=lambda sid: None, recheck_final=lambda a: calls.append(a))
        assert len(calls) == 1
    calls = []                                         # a final / real-rows email: no re-check
    _run(_rolling(kind="final"), read_pending=lambda sid: None,
         poll_brief=lambda *a, **k: None, recheck_final=lambda a: calls.append(a))
    assert calls == []


def test_the_error_email_goes_out_once():
    out, sent, _r, marked = _run(_rolling(), already_sent=lambda rid: True,
                                 read_pending=lambda sid: {"sessionBase": BASE})
    assert out["status"] == "skipped" and sent == [] and marked == []


def test_a_failed_send_does_not_mark_the_marker():
    marked = []

    def boom(*a):
        raise RuntimeError("ses down")
    out = fin.process_finalize_request(
        _rolling(), send=boom, write_result=lambda *a: None, already_sent=lambda r: False,
        read_pending=lambda sid: {"sessionBase": BASE},
        mark_error_email_sent=lambda sid: marked.append(sid))
    assert out["status"] == "error" and marked == []


def test_the_follow_up_notes_email_has_its_own_key_and_goes_once():
    follow = _rolling(kind="final", followUp=True,
                      openTodos=[{"text": "Pour the slab", "responsible": "Ben", "due": None}])
    keys = []
    out, sent, results, _m = _run(follow, already_sent=lambda rid: keys.append(rid) or False,
                                  poll_brief=lambda *a, **k: follow["openTodos"])
    assert keys == [f"{SID}-notes"] and results[0][0] == f"{SID}-notes"   # {sid}.json untouched
    assert "Pour the slab" in sent[0][2] and "delayed" not in sent[0][1]
    # a duplicate event for the same follow-up: the result says sent, so nothing goes
    out2, sent2, _r, _m = _run(follow, already_sent=lambda rid: rid == f"{SID}-notes",
                               poll_brief=lambda *a, **k: follow["openTodos"])
    assert out2["status"] == "skipped" and sent2 == []


def test_item_writer_enqueues_the_follow_up_under_its_own_key_even_when_settled(monkeypatch):
    iw = pytest.importorskip("lambda_item_writer")
    import lambda_finalize_claim as fc
    monkeypatch.setattr(iw.meeting_session, "get",
                        lambda conn, sid: {"session_id": sid, "status": "sent", "user_id": "u"})
    monkeypatch.setattr(fc, "_resolve_context", lambda conn, row: {
        "recipient": "ben@example.nz", "folder": FOLDER, "date": DATE,
        "siteName": "UC PK", "timeRange": RANGE})
    extraction = {"tier": "final", "topics": []}
    assert iw._final_email_context("C", BASE, extraction, DATE) is None     # settled: no normal email
    ctx = iw._final_email_context("C", BASE, extraction, DATE, follow_up=True)
    assert ctx["followUp"] is True
    puts = []
    iw._enqueue_final_email(
        ctx, put=lambda k, b, only_if_absent=False: puts.append((k, only_if_absent)))
    assert puts == [(f"session_finalize_requests/{SID}-notes.json", True)]
