"""Final-review fix round for model fallback and recovery (spec 2026-10-07, rulings 1-10).

1  a model that ANSWERED unusably is a marker too (kind "parse", bounded re-drives);
2  no speech is a success-empty record, a missing key is a failure that wipes nothing,
   and a promised_only marker expires;
3  the primary survives one blip and keeps the function's long timeout;
4/5 slow backoff after 24 h, a per-tick cap, jitter and a time guard;
7/10 marker and extraction re-writes are conditional.
"""
import io
import json
import logging
import os
from datetime import datetime, timedelta, timezone

import pytest
from botocore.exceptions import ClientError

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
NOW = datetime(2026, 10, 8, 3, 0, tzinfo=timezone.utc)


class _NoJitter:
    @staticmethod
    def uniform(a, b):
        return 0


class _MaxJitter:
    @staticmethod
    def uniform(a, b):
        return b


def _client_error(code):
    return ClientError({"Error": {"Code": code}}, "Op")


class FakeS3:
    """ETag-aware: every put bumps the object's version; IfMatch must match it."""

    def __init__(self, objects=None):
        self.objects, self.version = {}, {}
        for k, v in (objects or {}).items():
            self.objects[k] = v if isinstance(v, bytes) else v.encode()
            self.version[k] = 1
        self.puts, self.deletes, self.if_match_seen = [], [], []
        self.before_put = None            # hook: runs just before a put lands

    def get_object(self, Bucket, Key):
        if Key not in self.objects:
            raise _client_error("NoSuchKey")
        return {"Body": io.BytesIO(self.objects[Key]), "ETag": f'"v{self.version[Key]}"'}

    def put_object(self, Bucket, Key, Body, **kw):
        if self.before_put:
            hook, self.before_put = self.before_put, None
            hook(self)
        if "IfMatch" in kw:
            self.if_match_seen.append(Key)
            if Key not in self.objects or kw["IfMatch"] != f'"v{self.version[Key]}"':
                raise _client_error("PreconditionFailed")
        self.puts.append(Key)
        self.objects[Key] = Body if isinstance(Body, bytes) else Body.encode()
        self.version[Key] = self.version.get(Key, 0) + 1
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

    def marker(self, base=BASE):
        raw = self.objects.get(ep.marker_key(base))
        return json.loads(raw) if raw else None


def _transcribe(text):
    words = text.split()
    return json.dumps({"results": {"transcripts": [{"transcript": text}], "items": [
        {"type": "pronunciation", "start_time": f"{i}.0", "end_time": f"{i + 1}.0",
         "alternatives": [{"content": w, "confidence": "0.9"}]} for i, w in enumerate(words)]}})


def _event(key=REQ_KEY):
    return {"Records": [{"s3": {"object": {"key": key}}}]}


@pytest.fixture
def lam(monkeypatch):
    s3 = FakeS3({REQ_KEY: json.dumps({"userFolder": FOLDER, "date": DATE, "sessionBase": BASE}),
                 SEG: _transcribe("pour the slab tomorrow morning")})
    monkeypatch.setattr(les, "s3", lambda: s3)
    monkeypatch.setattr(les, "S3_BUCKET", BUCKET)
    monkeypatch.setattr(les, "FINAL_EMPTY_RETRY_DELAY_S", 0)
    les._sites_cache = None
    return s3


def _mk(base=BASE, **over):
    m = {"userFolder": FOLDER, "date": DATE, "sessionBase": base, "request_key": REQ_KEY,
         "attempts": 0, "first_failed_at": ep.iso(NOW - timedelta(hours=1)),
         "next_attempt_at": ep.iso(NOW - timedelta(minutes=1)), "expedite": False,
         "failure_kind": "model"}
    m.update(over)
    return m


@pytest.fixture
def bk(monkeypatch):
    monkeypatch.setattr(bl, "S3_BUCKET", BUCKET)
    return FakeS3({REQ_KEY: json.dumps({"userFolder": FOLDER, "date": DATE, "sessionBase": BASE})})


def _put(s3, m):
    s3.objects[ep.marker_key(m["sessionBase"])] = json.dumps(m).encode()
    s3.version[ep.marker_key(m["sessionBase"])] = 1


# ---- 1: a model that answered unusably ---------------------------------------

@pytest.mark.parametrize("answer", ["not json at all", '{"topics": [{"title": "cut off'])
def test_an_unusable_answer_marks_kind_parse_and_still_raises(lam, monkeypatch, answer):
    monkeypatch.setattr(llm_utils, "call_llm", lambda *a, **k: (answer, None))
    with pytest.raises(RuntimeError, match="Failed to parse"):
        les.lambda_handler(_event(), None)
    m = lam.marker()
    assert m["failure_kind"] == "parse" and m["request_key"] == REQ_KEY


def test_malformed_topics_mark_kind_parse_and_stay_a_value_error(lam, monkeypatch):
    monkeypatch.setattr(llm_utils, "call_llm",
                        lambda *a, **k: (json.dumps({"topics": "oops"}), None))
    with pytest.raises(ValueError, match="Malformed"):
        les.lambda_handler(_event(), None)
    assert lam.marker()["failure_kind"] == "parse"


def test_an_outage_marks_kind_model(lam, monkeypatch):
    monkeypatch.setattr(llm_utils, "call_llm", lambda *a, **k: (None, "HTTP 502"))
    with pytest.raises(RuntimeError):
        les.lambda_handler(_event(), None)
    assert lam.marker()["failure_kind"] == "model"


def test_a_live_pass_with_an_unusable_answer_leaves_no_marker(lam, monkeypatch):
    monkeypatch.setattr(llm_utils, "call_llm", lambda *a, **k: ("nope", None))
    with pytest.raises(RuntimeError):
        les.lambda_handler(_event(SEG), None)
    assert lam.marker() is None


def test_parse_markers_are_re_driven_six_times_then_given_up_but_still_counted(bk):
    _put(bk, _mk(failure_kind="parse"))
    t, redriven = NOW, 0
    for _ in range(12):
        r, stale = bl.redrive(bk, now=t, rng=_NoJitter)
        redriven += len(r)
        assert stale == 1                                  # the alarm stays on throughout
        m = bk.marker()
        t = ep.parse_iso(m["next_attempt_at"]) + timedelta(seconds=1)
    assert redriven == ep.PARSE_REDRIVE_MAX == 6
    assert m["attempts"] == 6 and m["gave_up_at"]
    assert bk.marker() is not None                         # kept: banner and alarm


def test_a_model_marker_is_never_given_up_on(bk):
    _put(bk, _mk(attempts=60))
    r, _ = bl.redrive(bk, now=NOW, rng=_NoJitter)
    assert r == [BASE] and "gave_up_at" not in bk.marker()


def test_expedite_allows_one_more_re_drive_of_a_given_up_parse_marker(bk):
    _put(bk, _mk(failure_kind="parse", attempts=6, gave_up_at=ep.iso(NOW)))
    assert bl.redrive(bk, now=NOW, rng=_NoJitter)[0] == []          # given up: nothing
    m = bk.marker()
    m.update(expedite=True, expedite_requested_at=ep.iso(NOW))
    _put(bk, m)
    assert bl.redrive(bk, now=NOW, rng=_NoJitter)[0] == [BASE]      # the one more
    later = NOW + timedelta(days=2)
    assert bl.redrive(bk, now=later, rng=_NoJitter)[0] == []        # and no more


def test_a_model_failure_after_parse_failures_clears_gave_up(bk):
    s3 = FakeS3()
    ep.record_failure(s3, BUCKET, user_folder=FOLDER, date=DATE, session_base=BASE,
                      request_key=REQ_KEY, error="x", now=NOW, kind="parse")
    m = s3.marker()
    m["gave_up_at"] = ep.iso(NOW)
    _put(s3, m)
    ep.record_failure(s3, BUCKET, user_folder=FOLDER, date=DATE, session_base=BASE,
                      request_key=REQ_KEY, error="502", now=NOW, kind="model")
    assert "gave_up_at" not in s3.marker() and s3.marker()["failure_kind"] == "model"


def test_org_api_lists_a_given_up_parse_marker_so_the_banner_stays():
    oa = pytest.importorskip("lambda_org_api", reason="requires psycopg (installed in CI)")
    s3 = FakeS3()
    _put(s3, _mk(failure_kind="parse", attempts=6, gave_up_at=ep.iso(NOW)))
    import unittest.mock as um
    with um.patch.object(oa, "s3", lambda: s3), um.patch.object(oa, "LAKE_BUCKET", BUCKET):
        resp = oa.sessions_pending(None, {"folder_name": FOLDER},
                                   {"queryStringParameters": {"date": DATE}})
    body = json.loads(resp["body"])
    assert [r["session_id"] for r in body["sessions"]] == [BASE]


# ---- 2: no speech, missing key, promised_only expiry --------------------------

def test_no_speech_writes_the_empty_record_and_clears_the_marker(lam, monkeypatch):
    del lam.objects[SEG]
    _put(lam, _mk())
    monkeypatch.setattr(llm_utils, "call_llm",
                        lambda *a, **k: pytest.fail("no speech: no model call"))
    out = les.lambda_handler(_event(), None)
    assert out == {"results": [None]}
    rec = json.loads(lam.objects[ep.empty_key(BASE)])
    assert rec["reason"] == "no_usable_speech" and rec["sessionBase"] == BASE
    assert lam.marker() is None


def test_a_live_pass_with_no_speech_writes_no_empty_record(lam, monkeypatch):
    lam.objects[SEG] = b"{not json"
    assert les.extract_session(BUCKET, FOLDER, DATE, BASE) is None
    assert ep.empty_key(BASE) not in lam.objects


def test_a_missing_key_on_a_final_raises_and_wipes_no_marker(lam, monkeypatch):
    _put(lam, _mk(attempts=3))
    monkeypatch.setattr(llm_utils, "api_key_configured", lambda: False)
    with pytest.raises(les.ConfigMissing):
        les.lambda_handler(_event(), None)
    assert lam.marker()["attempts"] == 3 and ep.marker_key(BASE) not in lam.deletes
    assert lam.marker()["failure_kind"] == "model"       # not rewritten as a failure either


def test_a_final_that_wrote_nothing_does_not_clear_the_marker(lam, monkeypatch):
    _put(lam, _mk())
    monkeypatch.setattr(les, "extract_session", lambda *a, **k: None)
    les.lambda_handler(_event(), None)
    assert lam.marker() is not None


def test_finalize_says_nothing_captured_for_a_recorded_empty_session():
    sent, promised = [], []
    out = fin.process_finalize_request(
        {"kind": "rolling", "sessionId": SID, "recipient": "b@e.nz", "date": DATE,
         "timeRange": "10:00-10:01", "siteName": "UC PK", "summary": "", "openTodos": []},
        send=lambda *a: sent.append(a), write_result=lambda *a: None,
        already_sent=lambda r: False, read_pending=lambda sid: None,
        read_empty=lambda sid: True, promise_notes=lambda sid: promised.append(sid),
        recheck_final=lambda a: pytest.fail("nothing promised, nothing to re-check"))
    assert "Nothing was captured for this recording." in sent[0][2]
    assert promised == [] and out.get("kind") is None


def test_finalize_without_an_empty_record_still_only_promises():
    sent, promised = [], []
    fin.process_finalize_request(
        {"kind": "rolling", "sessionId": SID, "recipient": "b@e.nz", "date": DATE,
         "timeRange": "10:00-10:01", "siteName": "UC PK", "summary": "", "openTodos": []},
        send=lambda *a: sent.append(a), write_result=lambda *a: None,
        already_sent=lambda r: False, read_pending=lambda sid: None,
        read_empty=lambda sid: False, promise_notes=lambda sid: promised.append(sid),
        recheck_final=lambda a: False)
    assert "Nothing was captured" not in sent[0][2] and promised == [SID]


def test_read_empty_reads_the_record_and_treats_absent_as_false(monkeypatch):
    s3 = FakeS3({ep.empty_key(BASE): "{}"})
    import boto3
    monkeypatch.setattr(boto3, "client", lambda *a, **k: s3)
    monkeypatch.setattr(fin, "S3_BUCKET", BUCKET)
    assert fin._read_empty(SID) is True
    assert fin._read_empty("other") is False


def test_a_promised_only_marker_expires_after_24h_loudly_and_counts_until_then(bk, caplog):
    ep.promise_notes(bk, BUCKET, BASE, now=NOW - timedelta(hours=23))
    assert bl.redrive(bk, now=NOW) == ([], 1)             # counted, kept
    assert bk.marker() is not None
    with caplog.at_level(logging.ERROR):
        assert bl.redrive(bk, now=NOW + timedelta(hours=2)) == ([], 0)   # deleted
    assert bk.marker() is None and ep.marker_key(BASE) in bk.deletes
    assert any("EXTRACTION_PROMISE_EXPIRED" in r.getMessage() for r in caplog.records)


def test_a_failed_expiry_delete_keeps_counting(bk):
    ep.promise_notes(bk, BUCKET, BASE, now=NOW - timedelta(hours=30))

    def boom(Bucket, Key):
        raise RuntimeError("denied")
    bk.delete_object = boom
    assert bl.redrive(bk, now=NOW) == ([], 1)


# ---- 4/5: cost and the herd -----------------------------------------------------

def test_after_24h_of_failures_the_model_backoff_is_six_hours(bk):
    _put(bk, _mk(first_failed_at=ep.iso(NOW - timedelta(hours=25)), attempts=30))
    bl.redrive(bk, now=NOW, rng=_NoJitter)
    assert ep.parse_iso(bk.marker()["next_attempt_at"]) == NOW + timedelta(hours=6)


def test_before_24h_the_ladder_is_unchanged(bk):
    _put(bk, _mk(first_failed_at=ep.iso(NOW - timedelta(hours=23)), attempts=30))
    bl.redrive(bk, now=NOW, rng=_NoJitter)
    assert ep.parse_iso(bk.marker()["next_attempt_at"]) == NOW + timedelta(minutes=60)


def test_every_new_next_attempt_gets_up_to_120_seconds_of_jitter(bk):
    _put(bk, _mk(attempts=0))
    bl.redrive(bk, now=NOW, rng=_MaxJitter)
    assert ep.parse_iso(bk.marker()["next_attempt_at"]) == NOW + timedelta(minutes=15, seconds=120)
    _put(bk, _mk(attempts=0))
    bl.redrive(bk, now=NOW, rng=_NoJitter)
    assert ep.parse_iso(bk.marker()["next_attempt_at"]) == NOW + timedelta(minutes=15)


def _many(bk, n, **over):
    for i in range(n):
        base = f"sid{i:03d}"
        bk.objects[f"{les.FINAL_REQUESTS_PREFIX}{i:03d}.json"] = b"{}"
        bk.version[f"{les.FINAL_REQUESTS_PREFIX}{i:03d}.json"] = 1
        _put(bk, _mk(base=base, request_key=f"{les.FINAL_REQUESTS_PREFIX}{i:03d}.json",
                     first_failed_at=ep.iso(NOW - timedelta(hours=2, minutes=100 - i)),
                     **over))


def test_one_tick_re_drives_at_most_twenty_oldest_first(bk):
    _many(bk, 30)
    r, stale = bl.redrive(bk, now=NOW, rng=_NoJitter)
    assert len(r) == ep.REDRIVE_PER_TICK == 20
    assert r == [f"sid{i:03d}" for i in range(20)]          # oldest failure first
    assert stale == 30                                      # all still counted
    r2, _ = bl.redrive(bk, now=NOW, rng=_NoJitter)          # the next tick takes the rest
    assert r2 == [f"sid{i:03d}" for i in range(20, 30)]


def test_expedited_markers_go_first_even_past_the_cap(bk):
    _many(bk, 25)
    m = bk.marker("sid024")
    m["expedite"] = True
    _put(bk, m)
    r, _ = bl.redrive(bk, now=NOW, rng=_NoJitter)
    assert r[0] == "sid024" and len(r) == 20


def test_the_loop_stops_when_the_lambda_is_nearly_out_of_time(bk):
    _many(bk, 10)
    left = iter([60_000, 60_000, 60_000, 19_000, 5_000])

    r, _ = bl.redrive(bk, now=NOW, rng=_NoJitter, time_left_ms=lambda: next(left))
    assert len(r) == 3
    assert bk.marker("sid003")["attempts"] == 0              # untouched, due next tick


def test_the_handler_hands_the_context_clock_to_the_loop(bk, monkeypatch):
    _many(bk, 5)
    monkeypatch.setattr(bl, "_s3", lambda: bk)

    class Ctx:
        @staticmethod
        def get_remaining_time_in_millis():
            return 1_000
    out = bl.lambda_handler({"task": "redrive"}, Ctx())
    assert out["redriven"] == []


# ---- 7/10: conditional writes ----------------------------------------------------

def test_the_re_drive_writes_the_marker_with_if_match(bk):
    _put(bk, _mk())
    bl.redrive(bk, now=NOW, rng=_NoJitter)
    assert ep.marker_key(BASE) in bk.if_match_seen


def test_a_marker_cleared_between_listing_and_re_drive_is_not_resurrected(bk):
    _put(bk, _mk())
    real = bk.get_object
    state = {"n": 0}

    def get_then_clear(Bucket, Key):
        out = real(Bucket, Key)
        if Key == ep.marker_key(BASE):
            state["n"] += 1
            if state["n"] == 1:                         # the list pass has read it ...
                bk.objects.pop(Key)                     # ... then the final succeeds
        return out
    bk.get_object = get_then_clear
    r, _ = bl.redrive(bk, now=NOW, rng=_NoJitter)
    assert r == [] and bk.marker() is None
    assert REQ_KEY not in bk.puts                       # no second full final pass


def test_a_lost_race_is_retried_once_against_the_fresh_marker(bk):
    _put(bk, _mk())

    def other_writer(s3):
        m = s3.marker()
        m["last_error"] = "written by a failed pass"
        s3.objects[ep.marker_key(BASE)] = json.dumps(m).encode()
        s3.version[ep.marker_key(BASE)] += 1
    bk.before_put = other_writer
    r, _ = bl.redrive(bk, now=NOW, rng=_NoJitter)
    assert r == [BASE]
    m = bk.marker()
    assert m["last_error"] == "written by a failed pass" and m["attempts"] == 1


def test_update_falls_back_to_a_plain_put_when_the_sdk_has_no_if_match():
    from botocore.exceptions import ParamValidationError

    class OldSdk(FakeS3):
        def put_object(self, Bucket, Key, Body, **kw):
            if "IfMatch" in kw:
                raise ParamValidationError(report="Unknown parameter IfMatch")
            return super().put_object(Bucket, Key, Body, **kw)
    s3 = OldSdk()
    _put(s3, _mk())
    assert ep.update(s3, BUCKET, BASE, lambda m: m.update(attempts=9)) is not None
    assert s3.marker()["attempts"] == 9


def _fin_world(monkeypatch, objects):
    s3 = FakeS3(objects)
    import boto3
    monkeypatch.setattr(boto3, "client", lambda *a, **k: s3)
    monkeypatch.setattr(fin, "S3_BUCKET", BUCKET)
    return s3


def _ekey():
    return f"extractions/{FOLDER}/{DATE}/{BASE}.json"


def test_the_recheck_put_is_conditional(monkeypatch):
    s3 = _fin_world(monkeypatch, {_ekey(): json.dumps({"tier": "final", "topics": []})})
    assert fin._recheck_final_extraction({"sessionId": SID, "folder": FOLDER, "date": DATE})
    assert _ekey() in s3.if_match_seen
    assert json.loads(s3.objects[_ekey()])["recovered_after_error"] is True


def test_a_newer_final_written_in_between_is_flagged_not_overwritten(monkeypatch):
    s3 = _fin_world(monkeypatch, {_ekey(): json.dumps({"tier": "final", "v": "old"})})

    def newer_final(s):
        s.objects[_ekey()] = json.dumps({"tier": "final", "v": "NEWER"}).encode()
        s.version[_ekey()] += 1
    s3.before_put = newer_final
    fin._recheck_final_extraction({"sessionId": SID, "folder": FOLDER, "date": DATE})
    body = json.loads(s3.objects[_ekey()])
    assert body["v"] == "NEWER" and body["recovered_after_error"] is True


def test_the_recheck_falls_back_when_the_sdk_has_no_if_match(monkeypatch):
    from botocore.exceptions import ParamValidationError
    s3 = _fin_world(monkeypatch, {_ekey(): json.dumps({"tier": "final"})})
    real = s3.put_object

    def old(Bucket, Key, Body, **kw):
        if "IfMatch" in kw:
            raise ParamValidationError(report="Unknown parameter IfMatch")
        return real(Bucket, Key, Body, **kw)
    s3.put_object = old
    fin._recheck_final_extraction({"sessionId": SID, "folder": FOLDER, "date": DATE})
    assert json.loads(s3.objects[_ekey()])["recovered_after_error"] is True


# ---- template ---------------------------------------------------------------------

def test_the_template_grants_the_new_prefixes():
    from tests.unit.test_template_extraction_pending_wiring import _resource, _statement
    ex = _resource("ExtractSessionFunction")
    assert _statement(ex, "s3:PutObject", "/extraction_empty/*")
    fz = _resource("SessionFinalizeFunction")
    assert _statement(fz, "s3:GetObject", "/extraction_empty/*")
    assert "- extraction_empty/*" in fz                   # ListBucket: absent is 404, not 403
    assert _statement(_resource("ExtractionBacklogFunction"), "s3:DeleteObject",
                      "/extraction_pending/*")
