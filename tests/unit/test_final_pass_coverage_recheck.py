"""Unit: the final pass must notice the session grew while it was thinking.

Measured on prod session `sid61be49d5...` (Ben_UCPK2, 2026-08-07):

    tier: final | extracted_at: 03:36:22Z | sources: 95 | chunks c0000 -> c0129
    transcripts on disk: 151 (c0000 -> c0150), last written 15:35:48 NZ

The last ten minutes of a 78-minute session — including the discussion of steel
and stair reinforcement — are absent from the authoritative record. The artifact
has nine topics and reads exactly like a complete one.

The roadmap said the tail "was not yet transcribed" and that the
overtake-and-rerun mechanism "did not fire". Both are wrong, and the logs say
so: `03:33:55.634 ... overtook an early final pass -- requested a re-run`. The
tail was fully on disk **34 seconds before** the final wrote.

What actually breaks is structural:

  * `extract_session` lists the session ONCE, before a ~170 s thinking call. 21
    transcripts landed during that call.
  * The post-call coverage re-check is guarded by `if not final:` — only a LIVE
    pass ever re-examines what it published.
  * A live pass only fires on a new `transcripts/` write, and the final pass
    writes AFTER the last transcript by construction (the finalize sweep is
    downstream of session close). So when the narrow final lands there is no
    trigger left in the system. The recovery path exists and is unreachable.

BUG-43's shape in the mirror: that fix removed "discard the expensive result if
the premise changed"; this is "keep the result but never re-examine the premise".

Two properties here are load-bearing and easy to break later:

1. **Re-list AFTER the write.** Re-listing first reopens the window it closes.
2. **Compare S3 keys to S3 keys**, never to `source_transcripts` —
   `assemble_deduped_turns` drops unnormalizable segments (three in this very
   session), so comparing against what parsed would see them as "new" every
   round and burn the whole generation budget on identical re-runs.
"""
import io
import json
import os

import pytest

os.environ.setdefault("AWS_ACCESS_KEY_ID", "testing")
os.environ.setdefault("AWS_SECRET_ACCESS_KEY", "testing")
os.environ.setdefault("AWS_DEFAULT_REGION", "ap-southeast-2")
os.environ.setdefault("S3_BUCKET", "test-bucket")
os.environ.setdefault("ANTHROPIC_API_KEY", "sk-ant-test-dummy-key")

les = pytest.importorskip("lambda_extract_session")
import llm_utils  # noqa: E402

# Fixtures are duplicated rather than imported from test_lambda_extract_session:
# tests/unit is not a package, so a cross-module import resolves only by accident
# of sys.path and breaks the moment pytest is invoked from elsewhere.
BUCKET = "test-bucket"
_SID = "9f8c1e2a4b6d47f0a1b2c3d4e5f60718"
CHUNK_BASE = f"sid{_SID}"
CHUNK_SEG1 = f"transcripts/Benl1/2026-07-06/Benl1_2026-07-06_10-00-00_sid{_SID}_c0001.json"
CHUNK_SEG2 = f"transcripts/Benl1/2026-07-06/Benl1_2026-07-06_10-00-30_sid{_SID}_c0002.json"
CHUNK_OUT_KEY = f"extractions/Benl1/2026-07-06/{CHUNK_BASE}.json"


class _FakePaginator:
    def __init__(self, objects):
        self.objects = objects

    def paginate(self, Bucket, Prefix):
        yield {"Contents": [{"Key": k} for k in self.objects if k.startswith(Prefix)]}


class FakeNoSuchKey(Exception):
    pass


class FakeS3:
    """Minimal S3 client double: object store keyed by S3 key, records puts."""

    def __init__(self, objects=None):
        self.objects = dict(objects or {})
        self.put_calls = []

    def get_object(self, Bucket, Key):
        if Key not in self.objects:
            raise FakeNoSuchKey()
        body = self.objects[Key]
        raw = body.encode("utf-8") if isinstance(body, str) else body
        return {"Body": io.BytesIO(raw)}

    def get_paginator(self, op):
        assert op == "list_objects_v2"
        return _FakePaginator(self.objects)

    def put_object(self, **kwargs):
        self.put_calls.append(kwargs)
        self.objects[kwargs["Key"]] = kwargs["Body"]
        return {}


def make_transcribe_json(text, start=0.0, end=None):
    words = text.split()
    n = len(words) or 1
    if end is None:
        end = start + n
    step = (end - start) / n
    items, t = [], start
    for w in words:
        items.append({
            "type": "pronunciation",
            "start_time": f"{t:.3f}",
            "end_time": f"{t + step:.3f}",
            "alternatives": [{"content": w, "confidence": "0.9"}],
        })
        t += step
    return {"results": {"transcripts": [{"transcript": text}], "items": items}}


@pytest.fixture(autouse=True)
def reset_site_cache():
    les._sites_cache = None
    yield
    les._sites_cache = None


def _rerun_requests(fake_s3):
    return [c for c in fake_s3.put_calls
            if c["Key"].startswith(les.FINAL_REQUESTS_PREFIX)]


def _llm_that_lands_a_transcript_midcall(fake_s3, key, text="the missing ten minutes"):
    """The whole bug in one fixture: a transcript arrives while the model is
    thinking. 21 of them did, over ~170 seconds."""
    def _call(prompt, max_tokens=None, force_json=False, enable_thinking=None):
        fake_s3.objects[key] = json.dumps(make_transcribe_json(text, start=30.0))
        return json.dumps({"topics": [], "declared_site": None}), None
    return _call


def _setup(monkeypatch, fake_s3):
    monkeypatch.setattr(les, "s3", lambda: fake_s3)
    monkeypatch.setattr(les, "S3_BUCKET", BUCKET)


# ------------------------------------------------------------------
# The fix
# ------------------------------------------------------------------

def test_a_final_pass_overtaken_mid_call_writes_and_asks_for_another(monkeypatch):
    fake_s3 = FakeS3({CHUNK_SEG1: json.dumps(make_transcribe_json("hello world"))})
    _setup(monkeypatch, fake_s3)
    monkeypatch.setattr(llm_utils, "call_llm",
                        _llm_that_lands_a_transcript_midcall(fake_s3, CHUNK_SEG2))

    result = les.extract_session(BUCKET, "Benl1", "2026-07-06", CHUNK_BASE, final=True)

    # The paid-for result is still published — BUG-43's rule, never discard it.
    assert result["tier"] == les.TIER_FINAL
    assert json.loads(fake_s3.objects[CHUNK_OUT_KEY])["tier"] == les.TIER_FINAL

    reruns = _rerun_requests(fake_s3)
    assert len(reruns) == 1, "the narrow final must ask for a fresher one"
    assert reruns[0]["Key"] == f"{les.FINAL_REQUESTS_PREFIX}{_SID}.json"
    assert json.loads(reruns[0]["Body"])["generation"] == 1


def test_a_final_pass_that_was_not_overtaken_asks_for_nothing(monkeypatch):
    """Termination: the common case must cost exactly one final pass."""
    fake_s3 = FakeS3({CHUNK_SEG1: json.dumps(make_transcribe_json("hello world"))})
    _setup(monkeypatch, fake_s3)
    monkeypatch.setattr(
        llm_utils, "call_llm",
        lambda *a, **k: (json.dumps({"topics": [], "declared_site": None}), None))

    les.extract_session(BUCKET, "Benl1", "2026-07-06", CHUNK_BASE, final=True)

    assert _rerun_requests(fake_s3) == []


def test_the_generation_bound_stops_the_chain_loudly(monkeypatch, caplog):
    """A transcriber rewriting keys in a loop would otherwise chain final passes
    forever. When the bound stops it the session IS short — that has to be a log
    line someone can find, not a silent stop."""
    caplog.set_level("WARNING")
    fake_s3 = FakeS3({CHUNK_SEG1: json.dumps(make_transcribe_json("hello world"))})
    _setup(monkeypatch, fake_s3)
    monkeypatch.setattr(llm_utils, "call_llm",
                        _llm_that_lands_a_transcript_midcall(fake_s3, CHUNK_SEG2))

    les.extract_session(BUCKET, "Benl1", "2026-07-06", CHUNK_BASE, final=True,
                        generation=les.FINAL_RERUN_MAX_GENERATIONS - 1)

    assert json.loads(fake_s3.objects[CHUNK_OUT_KEY])["tier"] == les.TIER_FINAL
    assert _rerun_requests(fake_s3) == []
    assert any("FINAL_RERUN_MAX_GENERATIONS" in r.getMessage() for r in caplog.records)


# ------------------------------------------------------------------
# The two properties that are easy to break later
# ------------------------------------------------------------------

def test_unnormalizable_segments_do_not_trigger_a_rerun(monkeypatch):
    """The keys-vs-keys rule. This session had three unnormalizable transcripts
    (c0004, c0005, c0064). Comparing the fresh listing against what actually
    PARSED would count them as new on every round, so every session with one
    corrupt segment would burn the whole generation budget re-running itself —
    a fix manufacturing the cost it was written to prevent."""
    fake_s3 = FakeS3({
        CHUNK_SEG1: json.dumps(make_transcribe_json("hello world")),
        CHUNK_SEG2: "{ this is not a transcript",
    })
    _setup(monkeypatch, fake_s3)
    monkeypatch.setattr(
        llm_utils, "call_llm",
        lambda *a, **k: (json.dumps({"topics": [], "declared_site": None}), None))

    result = les.extract_session(BUCKET, "Benl1", "2026-07-06", CHUNK_BASE, final=True)

    assert len(result["source_transcripts"]) == 1, "the corrupt one is dropped"
    assert _rerun_requests(fake_s3) == [], (
        "a dropped segment is not a grown session")


def test_the_relist_happens_after_the_write(monkeypatch):
    """Ordering is the fix, not an implementation detail: re-listing first leaves
    a window where a transcript is caught by neither this pass nor a live pass
    that already did its own write-time re-read."""
    fake_s3 = FakeS3({CHUNK_SEG1: json.dumps(make_transcribe_json("hello world"))})
    _setup(monkeypatch, fake_s3)

    order = []
    real_put = fake_s3.put_object
    real_gather = les.gather_session_segments

    def spy_put(**kwargs):
        order.append(("put", kwargs["Key"]))
        return real_put(**kwargs)

    def spy_gather(*a, **k):
        order.append(("list", None))
        return real_gather(*a, **k)

    fake_s3.put_object = spy_put
    monkeypatch.setattr(les, "gather_session_segments", spy_gather)
    monkeypatch.setattr(
        llm_utils, "call_llm",
        lambda *a, **k: (json.dumps({"topics": [], "declared_site": None}), None))

    les.extract_session(BUCKET, "Benl1", "2026-07-06", CHUNK_BASE, final=True)

    kinds = [k for k, _ in order]
    write_at = order.index(("put", CHUNK_OUT_KEY))
    assert kinds.count("list") == 2, "one listing at entry, one after the write"
    last_list = len(order) - 1 - kinds[::-1].index("list")
    assert last_list > write_at, "the re-list must come after the extraction is written"


# ------------------------------------------------------------------
# The request artifact
# ------------------------------------------------------------------

def test_a_request_without_a_generation_is_the_first_round(monkeypatch):
    """Every artifact the finalize sweep writes lacks the field, as does every
    artifact written before it existed. Both mean 0, not a crash."""
    fake_s3 = FakeS3({
        f"{les.FINAL_REQUESTS_PREFIX}x.json": json.dumps(
            {"userFolder": "Benl1", "date": "2026-07-06", "sessionBase": CHUNK_BASE}),
    })
    _setup(monkeypatch, fake_s3)
    parsed = les.parse_final_request(BUCKET, f"{les.FINAL_REQUESTS_PREFIX}x.json")
    assert parsed == ("Benl1", "2026-07-06", CHUNK_BASE, 0)


def test_an_unusable_generation_falls_back_to_zero_loudly(monkeypatch, caplog):
    caplog.set_level("WARNING")
    fake_s3 = FakeS3({
        f"{les.FINAL_REQUESTS_PREFIX}x.json": json.dumps(
            {"userFolder": "Benl1", "date": "2026-07-06", "sessionBase": CHUNK_BASE,
             "generation": "three"}),
    })
    _setup(monkeypatch, fake_s3)
    parsed = les.parse_final_request(BUCKET, f"{les.FINAL_REQUESTS_PREFIX}x.json")
    assert parsed[3] == 0
    assert any("generation" in r.getMessage() for r in caplog.records)


def test_the_generation_rides_through_the_handler_and_increments(monkeypatch):
    fake_s3 = FakeS3({
        CHUNK_SEG1: json.dumps(make_transcribe_json("hello world")),
        f"{les.FINAL_REQUESTS_PREFIX}{_SID}.json": json.dumps(
            {"userFolder": "Benl1", "date": "2026-07-06", "sessionBase": CHUNK_BASE,
             "generation": 1}),
    })
    _setup(monkeypatch, fake_s3)
    monkeypatch.setattr(llm_utils, "call_llm",
                        _llm_that_lands_a_transcript_midcall(fake_s3, CHUNK_SEG2))

    les.lambda_handler({"Records": [{"s3": {
        "bucket": {"name": BUCKET},
        "object": {"key": f"{les.FINAL_REQUESTS_PREFIX}{_SID}.json"}}}]}, None)

    reruns = _rerun_requests(fake_s3)
    assert len(reruns) == 1
    assert json.loads(reruns[0]["Body"])["generation"] == 2


def test_a_failing_rerun_request_does_not_fail_the_extraction(monkeypatch):
    """Telemetry for a quality re-run must never lose an extraction that already
    succeeded."""
    fake_s3 = FakeS3({CHUNK_SEG1: json.dumps(make_transcribe_json("hello world"))})

    real_put = fake_s3.put_object

    def put(**kwargs):
        if kwargs["Key"].startswith(les.FINAL_REQUESTS_PREFIX):
            raise RuntimeError("denied")
        return real_put(**kwargs)

    fake_s3.put_object = put
    _setup(monkeypatch, fake_s3)
    monkeypatch.setattr(llm_utils, "call_llm",
                        _llm_that_lands_a_transcript_midcall(fake_s3, CHUNK_SEG2))

    result = les.extract_session(BUCKET, "Benl1", "2026-07-06", CHUNK_BASE, final=True)

    assert result["tier"] == les.TIER_FINAL
    assert CHUNK_OUT_KEY in fake_s3.objects


def test_a_listing_failure_after_the_write_does_not_fail_the_extraction(monkeypatch):
    fake_s3 = FakeS3({CHUNK_SEG1: json.dumps(make_transcribe_json("hello world"))})
    _setup(monkeypatch, fake_s3)
    monkeypatch.setattr(
        llm_utils, "call_llm",
        lambda *a, **k: (json.dumps({"topics": [], "declared_site": None}), None))

    calls = {"n": 0}
    real_gather = les.gather_session_segments

    def flaky(*a, **k):
        calls["n"] += 1
        if calls["n"] > 1:
            raise RuntimeError("list failed")
        return real_gather(*a, **k)

    monkeypatch.setattr(les, "gather_session_segments", flaky)

    result = les.extract_session(BUCKET, "Benl1", "2026-07-06", CHUNK_BASE, final=True)
    assert result["tier"] == les.TIER_FINAL
