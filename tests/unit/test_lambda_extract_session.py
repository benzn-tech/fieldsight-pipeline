"""
Tests for src/lambda_extract_session.py — Phase 4b, Task 2 (TDD).

Style mirrors tests/unit/test_lambda_ingest.py (FakeS3 object-store double)
and tests/unit/test_download_claims.py (dummy AWS env vars so an eager
boto3.client('s3') at import time never blows up on a missing credential
provider; no test here makes a real AWS or Claude call).
"""
import io
import json
import os

import pytest

os.environ.setdefault("AWS_ACCESS_KEY_ID", "testing")
os.environ.setdefault("AWS_SECRET_ACCESS_KEY", "testing")
os.environ.setdefault("AWS_DEFAULT_REGION", "ap-southeast-2")
# M-5 added an ANTHROPIC_API_KEY-configured guard at the top of
# extract_session(); a dummy key here (read once at llm_utils import
# time, same as ANTHROPIC_API_KEY itself) keeps every existing test -- which
# monkeypatches llm_utils.call_llm directly and never hits a real
# network call -- past that guard.
os.environ.setdefault("ANTHROPIC_API_KEY", "sk-ant-test-dummy-key")

les = pytest.importorskip("lambda_extract_session", reason="requires boto3 (installed in CI)")
import llm_utils  # noqa: E402  (import after importorskip, same module the handler calls)


BUCKET = "test-bucket"
CONFIG_KEY = "config/user_mapping.json"


class FakeNoSuchKey(Exception):
    """Shaped like botocore's ClientError for a missing object. The double used to
    raise a bare KeyError, which is NOT what S3 does — and since the code now has
    to tell 'absent' apart from 'could not read' (they license opposite actions),
    a double that signals absence the wrong way tests the wrong branch."""

    def __init__(self):
        super().__init__("NoSuchKey")
        self.response = {"Error": {"Code": "NoSuchKey"}}


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


class _FakePaginator:
    def __init__(self, objects):
        self.objects = objects

    def paginate(self, Bucket, Prefix):
        contents = [{"Key": k} for k in self.objects if k.startswith(Prefix)]
        yield {"Contents": contents}


def make_transcribe_json(text, start=0.0, end=None, speaker=None):
    """Build a minimal AWS Transcribe JSON with one pronunciation item per word,
    evenly spaced between start and end (default: 1s per word)."""
    words = text.split()
    n = len(words) or 1
    if end is None:
        end = start + n
    step = (end - start) / n
    items = []
    t = start
    for w in words:
        item = {
            "type": "pronunciation",
            "start_time": f"{t:.3f}",
            "end_time": f"{t + step:.3f}",
            "alternatives": [{"content": w, "confidence": "0.9"}],
        }
        if speaker:
            item["speaker_label"] = speaker
        items.append(item)
        t += step
    return {"results": {"transcripts": [{"transcript": text}], "items": items}}


def config_with_sites(sites):
    return json.dumps({"mapping": {}, "sites": sites})


@pytest.fixture(autouse=True)
def reset_site_cache():
    """load_sites() caches for the module's lifetime (warm container) --
    reset between tests so fixtures don't leak."""
    les._sites_cache = None
    yield
    les._sites_cache = None


SEG1_KEY = "transcripts/Benl1/2026-07-06/Benl1_2026-07-06_10-00-00_off0.0_to30.0_srcwav.json"
SEG2_KEY = "transcripts/Benl1/2026-07-06/Benl1_2026-07-06_10-00-00_off30.0_to60.0_srcwav.json"
OTHER_SESSION_KEY = "transcripts/Benl1/2026-07-06/Benl1_2026-07-06_11-00-00_off0.0_to30.0_srcwav.json"
SESSION_BASE = "Benl1_2026-07-06_10-00-00"
OUT_KEY = f"extractions/Benl1/2026-07-06/{SESSION_BASE}.json"


def _fake_call_llm_returning(payload):
    def _fake(prompt, max_tokens=4096, force_json=False, enable_thinking=None):
        return json.dumps(payload), None
    return _fake


# ---------------------------------------------------------------------------
# session_base_from_key
# ---------------------------------------------------------------------------

def test_session_base_parsing():
    # with _off suffix -> session_base strips it
    assert les.session_base_from_key(SEG1_KEY) == (
        "Benl1", "2026-07-06", "Benl1_2026-07-06_10-00-00"
    )
    # without _off (whole-segment file) -> session_base == filename minus .json
    whole_key = "transcripts/Benl1/2026-07-06/Benl1_2026-07-06_10-00-00.json"
    assert les.session_base_from_key(whole_key) == (
        "Benl1", "2026-07-06", "Benl1_2026-07-06_10-00-00"
    )
    # non-.json key -> skip
    assert les.session_base_from_key(
        "transcripts/Benl1/2026-07-06/Benl1_2026-07-06_10-00-00.txt"
    ) is None


def test_session_id_groups_all_chunks_into_one_base():
    # 2026-07 paradigm: every ~1-min chunk of one session carries the same
    # device-minted sid but a DIFFERENT _c{NNNN}; all must resolve to the SAME
    # session_base = "sid{id}" so they extract together, not one-per-minute.
    sid = "9f8c1e2a4b6d47f0a1b2c3d4e5f60718"
    c0 = f"transcripts/Ben_UCPK/2026-07-28/Benl1_2026-07-28_14-03-00_sid{sid}_c0000_off2.0_to58.0_srcwav.json"
    c7 = f"transcripts/Ben_UCPK/2026-07-28/Benl1_2026-07-28_14-10-00_sid{sid}_c0007_off1.0_to59.0_srcwav.json"
    assert les.session_base_from_key(c0) == ("Ben_UCPK", "2026-07-28", f"sid{sid}")
    assert les.session_base_from_key(c7) == ("Ben_UCPK", "2026-07-28", f"sid{sid}")
    # a different session_id -> different base (never grouped together)
    other = "transcripts/Ben_UCPK/2026-07-28/Benl1_2026-07-28_15-00-00_sidaaaabbbbccccddddeeeeffff00001111_c0000.json"
    assert les.session_base_from_key(other)[2] == "sidaaaabbbbccccddddeeeeffff00001111"


# ---------------------------------------------------------------------------
# Session gathering — only same-session segments, never a neighboring session
# ---------------------------------------------------------------------------

def test_gathers_only_same_session_segments(monkeypatch):
    fake_s3 = FakeS3({
        SEG1_KEY: json.dumps(make_transcribe_json("segment one text")),
        SEG2_KEY: json.dumps(make_transcribe_json("segment two text")),
        OTHER_SESSION_KEY: json.dumps(make_transcribe_json("unrelated session text")),
    })
    monkeypatch.setattr(les, "s3", lambda: fake_s3)

    keys = les.gather_session_segments(BUCKET, "Benl1", "2026-07-06", SESSION_BASE)

    assert keys == sorted([SEG1_KEY, SEG2_KEY])
    assert OTHER_SESSION_KEY not in keys


# ---------------------------------------------------------------------------
# Prompt construction — every segment's turns show up in the Claude prompt
# ---------------------------------------------------------------------------

def test_prompt_contains_all_segment_turns(monkeypatch):
    fake_s3 = FakeS3({
        SEG1_KEY: json.dumps(make_transcribe_json("UNIQUEWORDALPHA present here", start=0.0)),
        SEG2_KEY: json.dumps(make_transcribe_json("UNIQUEWORDBETA present here", start=30.0)),
    })
    monkeypatch.setattr(les, "s3", lambda: fake_s3)

    captured = {}

    def fake_call_llm(prompt, max_tokens=4096, force_json=False, enable_thinking=None):
        captured["prompt"] = prompt
        captured["max_tokens"] = max_tokens
        return json.dumps({"topics": [], "declared_site": None}), None

    monkeypatch.setattr(llm_utils, "call_llm", fake_call_llm)

    les.extract_session(BUCKET, "Benl1", "2026-07-06", SESSION_BASE)

    assert "UNIQUEWORDALPHA" in captured["prompt"]
    assert "UNIQUEWORDBETA" in captured["prompt"]
    assert "10:00:00" in captured["prompt"]  # abs_start_str of the first turn
    # BUG-16: max_tokens scales with segment count (2 segments here)
    assert captured["max_tokens"] == 4096 + 2 * 350


def test_extract_session_requests_force_json(monkeypatch):
    """extract_session must ask llm_utils.call_llm for structured (JSON) output --
    the extraction contract is parsed as JSON downstream, so a plain-text
    completion would break parsing. Minor-T2: close the force_json polarity gap."""
    fake_s3 = FakeS3({SEG1_KEY: json.dumps(make_transcribe_json("hello world"))})
    monkeypatch.setattr(les, "s3", lambda: fake_s3)

    captured = {}

    def _cap(prompt, max_tokens=4096, force_json=False, enable_thinking=None):
        captured["force_json"] = force_json
        return json.dumps({"topics": [], "declared_site": None}), None

    monkeypatch.setattr(llm_utils, "call_llm", _cap)

    les.extract_session(BUCKET, "Benl1", "2026-07-06", SESSION_BASE)

    assert captured["force_json"] is True


# ---------------------------------------------------------------------------
# Extraction contract — every key present, source_transcripts sorted
# ---------------------------------------------------------------------------

def test_writes_extraction_contract(monkeypatch):
    # SEG2_KEY sorts after SEG1_KEY alphabetically -- feed them to FakeS3 in
    # reverse dict-insertion order to prove source_transcripts is genuinely
    # sorted by the handler, not accidentally sorted by iteration order.
    fake_s3 = FakeS3({
        SEG2_KEY: json.dumps(make_transcribe_json("second segment", start=30.0)),
        SEG1_KEY: json.dumps(make_transcribe_json("first segment", start=0.0)),
    })
    monkeypatch.setattr(les, "s3", lambda: fake_s3)
    monkeypatch.setattr(
        llm_utils, "call_llm",
        _fake_call_llm_returning({"topics": [{"topic_title": "t"}], "declared_site": None}),
    )

    extraction = les.extract_session(BUCKET, "Benl1", "2026-07-06", SESSION_BASE)

    for key in ("schema_version", "user_folder", "date", "session_base",
                "source_transcripts", "extracted_at", "declared_site", "topics"):
        assert key in extraction

    assert extraction["schema_version"] == 1
    assert extraction["user_folder"] == "Benl1"
    assert extraction["date"] == "2026-07-06"
    assert extraction["session_base"] == SESSION_BASE
    assert extraction["source_transcripts"] == sorted(extraction["source_transcripts"])
    assert extraction["source_transcripts"] == [
        os.path.basename(SEG1_KEY), os.path.basename(SEG2_KEY)
    ]
    assert extraction["extracted_at"].endswith("Z")
    # topic gains a derived (empty, since no findings were returned) safety_flags
    # alongside whatever Claude returned -- the item-writer compatibility bridge.
    assert extraction["topics"] == [{"topic_title": "t", "safety_flags": []}]

    written = json.loads(fake_s3.objects[OUT_KEY])
    assert written == extraction


# ---------------------------------------------------------------------------
# Idempotent overwrite — re-running the same S3 event writes to the same key
# (also exercises lambda_handler's S3 Records dispatch + key URL-decoding)
# ---------------------------------------------------------------------------

def test_idempotent_overwrite_same_key(monkeypatch):
    fake_s3 = FakeS3({SEG1_KEY: json.dumps(make_transcribe_json("hello world"))})
    monkeypatch.setattr(les, "s3", lambda: fake_s3)
    monkeypatch.setattr(les, "S3_BUCKET", BUCKET)
    monkeypatch.setattr(
        llm_utils, "call_llm",
        _fake_call_llm_returning({"topics": [], "declared_site": None}),
    )

    # S3 event notifications encode spaces as '+' -- SEG1_KEY has none here,
    # but going through lambda_handler (not extract_session directly) still
    # exercises the unquote_plus + session_base_from_key dispatch path.
    event = {"Records": [{"s3": {"object": {"key": SEG1_KEY}}}]}
    les.lambda_handler(event, None)

    # A second pass only rewrites when the session actually GREW (a re-run over
    # the identical segment set is pure item-writer churn -- see _supersedes),
    # and min_interval_s=0 stands in for "the throttle window has passed".
    fake_s3.objects[SEG2_KEY] = json.dumps(
        make_transcribe_json("second segment", start=30.0))
    les.extract_session(BUCKET, "Benl1", "2026-07-06", SESSION_BASE, min_interval_s=0)

    assert len(fake_s3.put_calls) == 2
    assert all(c["Key"] == OUT_KEY for c in fake_s3.put_calls)
    extraction_keys = [k for k in fake_s3.objects if k.startswith("extractions/")]
    assert extraction_keys == [OUT_KEY]  # overwrite, never a second key


# ---------------------------------------------------------------------------
# declared_site — explicit arrival declaration -> fuzzy match against config
# ---------------------------------------------------------------------------

def test_declared_site_fuzzy_match(monkeypatch):
    fake_s3 = FakeS3({
        SEG1_KEY: json.dumps(make_transcribe_json("I have arrived at the site")),
        CONFIG_KEY: config_with_sites({"sb1108": {"name": "Ellesmere College"}}),
    })
    monkeypatch.setattr(les, "s3", lambda: fake_s3)
    monkeypatch.setattr(
        llm_utils, "call_llm",
        _fake_call_llm_returning({
            "topics": [],
            "declared_site": {"stated": "Ellesmere Collage", "confidence": 0.82},
        }),
    )

    extraction = les.extract_session(BUCKET, "Benl1", "2026-07-06", SESSION_BASE)

    assert extraction["declared_site"] == {
        "stated": "Ellesmere Collage",
        "matched_site": "Ellesmere College",
        "confidence": 0.82,
    }


def test_declared_site_null_passthrough(monkeypatch):
    fake_s3 = FakeS3({
        SEG1_KEY: json.dumps(make_transcribe_json("just discussing the schedule")),
        CONFIG_KEY: config_with_sites({"sb1108": {"name": "Ellesmere College"}}),
    })
    monkeypatch.setattr(les, "s3", lambda: fake_s3)
    monkeypatch.setattr(
        llm_utils, "call_llm",
        _fake_call_llm_returning({"topics": [], "declared_site": None}),
    )

    extraction = les.extract_session(BUCKET, "Benl1", "2026-07-06", SESSION_BASE)

    assert extraction["declared_site"] is None

    # Missing key entirely must also passthrough as None (no KeyError).
    monkeypatch.setattr(
        llm_utils, "call_llm",
        _fake_call_llm_returning({"topics": []}),
    )
    # final=True so this second pass is neither throttled nor stood down as
    # redundant by the coverage check -- we want a genuinely fresh extraction
    # built from the payload above, not the one the first call already wrote.
    extraction2 = les.extract_session(BUCKET, "Benl1", "2026-07-06", SESSION_BASE,
                                      final=True)
    assert extraction2["declared_site"] is None


# ---------------------------------------------------------------------------
# Claude failure -> RuntimeError, no S3 write (S3 event retry semantics)
# ---------------------------------------------------------------------------

def test_claude_failure_raises(monkeypatch):
    fake_s3 = FakeS3({SEG1_KEY: json.dumps(make_transcribe_json("hello world"))})
    monkeypatch.setattr(les, "s3", lambda: fake_s3)

    # call_llm itself fails
    monkeypatch.setattr(
        llm_utils, "call_llm",
        lambda prompt, max_tokens=4096, force_json=False, enable_thinking=None: (None, "boom"),
    )
    with pytest.raises(RuntimeError):
        les.extract_session(BUCKET, "Benl1", "2026-07-06", SESSION_BASE)
    assert fake_s3.put_calls == []

    # call_llm succeeds but returns unparseable JSON
    monkeypatch.setattr(
        llm_utils, "call_llm",
        lambda prompt, max_tokens=4096, force_json=False, enable_thinking=None: ("not json at all {{{", None),
    )
    with pytest.raises(RuntimeError):
        les.extract_session(BUCKET, "Benl1", "2026-07-06", SESSION_BASE)
    assert fake_s3.put_calls == []


# ---------------------------------------------------------------------------
# Corrupt transcript segment is skipped, not fatal to the session
# ---------------------------------------------------------------------------

def test_corrupt_transcript_skipped(monkeypatch):
    fake_s3 = FakeS3({
        SEG1_KEY: json.dumps(make_transcribe_json("this one is fine")),
        SEG2_KEY: "{not valid json at all",
    })
    monkeypatch.setattr(les, "s3", lambda: fake_s3)
    monkeypatch.setattr(
        llm_utils, "call_llm",
        _fake_call_llm_returning({"topics": [], "declared_site": None}),
    )

    extraction = les.extract_session(BUCKET, "Benl1", "2026-07-06", SESSION_BASE)

    assert extraction["source_transcripts"] == [os.path.basename(SEG1_KEY)]


# ---------------------------------------------------------------------------
# I-2 regression test: session grew (another segment landed) between the
# gather used to build the prompt and the recheck immediately before the
# S3 write -> raise, zero writes (S3 event retry picks up every segment).
# ---------------------------------------------------------------------------

def test_session_grown_during_extraction_still_writes(monkeypatch):
    """The old I-2 guard RAISED when the session grew mid-extraction, which was
    a livelock: segments land ~30s apart, the call took ~170s, so the recheck
    effectively always failed and threw away a completed extraction (94% of
    prod invocations on 2026-08-03 produced nothing). Growth must now still
    produce a write -- the next pass widens it."""
    class GrowingFakeS3(FakeS3):
        def get_paginator(self, op):
            self.list_calls = getattr(self, "list_calls", 0) + 1
            if self.list_calls == 2:
                self.objects[SEG2_KEY] = json.dumps(
                    make_transcribe_json("late arriving segment", start=30.0)
                )
            return super().get_paginator(op)

    fake_s3 = GrowingFakeS3({SEG1_KEY: json.dumps(make_transcribe_json("hello world"))})
    monkeypatch.setattr(les, "s3", lambda: fake_s3)
    monkeypatch.setattr(
        llm_utils, "call_llm",
        _fake_call_llm_returning({"topics": [], "declared_site": None}),
    )

    extraction = les.extract_session(BUCKET, "Benl1", "2026-07-06", SESSION_BASE)

    assert extraction is not None
    assert [c["Key"] for c in fake_s3.put_calls] == [OUT_KEY]


# ---------------------------------------------------------------------------
# Two-tier extraction: live (throttled, thinking OFF) vs final (unthrottled,
# thinking ON, authoritative). See lambda_extract_session's tier constants.
# ---------------------------------------------------------------------------

def _s3_with_one_segment():
    return FakeS3({SEG1_KEY: json.dumps(make_transcribe_json("hello world"))})


def test_live_pass_is_throttled_within_the_interval(monkeypatch):
    fake_s3 = _s3_with_one_segment()
    monkeypatch.setattr(les, "s3", lambda: fake_s3)
    calls = []

    def _counting(prompt, max_tokens=4096, force_json=False, enable_thinking=None):
        calls.append(enable_thinking)
        return json.dumps({"topics": [], "declared_site": None}), None

    monkeypatch.setattr(llm_utils, "call_llm", _counting)

    les.extract_session(BUCKET, "Benl1", "2026-07-06", SESSION_BASE)
    second = les.extract_session(BUCKET, "Benl1", "2026-07-06", SESSION_BASE)

    # Throttled BEFORE the expensive work: no second LLM call, no second write.
    assert second is None
    assert len(calls) == 1
    assert len(fake_s3.put_calls) == 1


def test_final_pass_ignores_the_throttle_and_uses_thinking(monkeypatch):
    fake_s3 = _s3_with_one_segment()
    monkeypatch.setattr(les, "s3", lambda: fake_s3)
    calls = []

    def _counting(prompt, max_tokens=4096, force_json=False, enable_thinking=None):
        calls.append(enable_thinking)
        return json.dumps({"topics": [], "declared_site": None}), None

    monkeypatch.setattr(llm_utils, "call_llm", _counting)

    live = les.extract_session(BUCKET, "Benl1", "2026-07-06", SESSION_BASE)
    final = les.extract_session(BUCKET, "Benl1", "2026-07-06", SESSION_BASE, final=True)

    assert calls == [False, True]                    # live fast, final thinking
    assert live["tier"] == les.TIER_LIVE
    assert final["tier"] == les.TIER_FINAL
    assert [c["Key"] for c in fake_s3.put_calls] == [OUT_KEY, OUT_KEY]


def test_live_pass_never_downgrades_a_final_extraction(monkeypatch):
    fake_s3 = _s3_with_one_segment()
    monkeypatch.setattr(les, "s3", lambda: fake_s3)
    monkeypatch.setattr(
        llm_utils, "call_llm",
        _fake_call_llm_returning({"topics": [], "declared_site": None}),
    )

    les.extract_session(BUCKET, "Benl1", "2026-07-06", SESSION_BASE, final=True)
    puts_after_final = len(fake_s3.put_calls)

    # Throttle bypassed AND the session grew -- the only thing standing between
    # the live pass and a write is the tier check.
    fake_s3.objects[SEG2_KEY] = json.dumps(
        make_transcribe_json("later segment", start=30.0))
    result = les.extract_session(BUCKET, "Benl1", "2026-07-06", SESSION_BASE,
                                 min_interval_s=0)

    assert len(fake_s3.put_calls) == puts_after_final     # no write
    assert result["tier"] == les.TIER_FINAL              # the final one survives


def test_live_pass_stands_down_when_it_covers_no_new_segments(monkeypatch):
    """A slow pass finishing after a faster, wider one must not narrow what is
    already published (the race the old raise-guard was really protecting)."""
    fake_s3 = FakeS3({
        SEG1_KEY: json.dumps(make_transcribe_json("hello world")),
        SEG2_KEY: json.dumps(make_transcribe_json("second segment", start=30.0)),
    })
    monkeypatch.setattr(les, "s3", lambda: fake_s3)
    monkeypatch.setattr(
        llm_utils, "call_llm",
        _fake_call_llm_returning({"topics": [], "declared_site": None}),
    )
    les.extract_session(BUCKET, "Benl1", "2026-07-06", SESSION_BASE)
    wide = json.loads(fake_s3.objects[OUT_KEY])
    assert len(wide["source_transcripts"]) == 2

    # Now a narrower pass (only SEG1 visible) tries to write over it.
    del fake_s3.objects[SEG2_KEY]
    result = les.extract_session(BUCKET, "Benl1", "2026-07-06", SESSION_BASE,
                                 min_interval_s=0)

    assert len(fake_s3.put_calls) == 1                       # no second write
    assert result["source_transcripts"] == wide["source_transcripts"]


def test_unreadable_existing_extraction_does_not_license_an_overwrite(monkeypatch):
    """'Could not read it' and 'it is not there' license opposite actions. The
    first version returned None for both, so a denied or failed read looked like
    an empty slot — which would silently disable the throttle AND let a live pass
    clobber an authoritative final extraction. Standing down costs a delayed
    refresh; overwriting on a guess cannot be taken back."""
    class DeniedOnExtractions(FakeS3):
        def get_object(self, Bucket, Key):
            if Key.startswith("extractions/"):
                raise RuntimeError("AccessDenied")     # NOT a NoSuchKey
            return super().get_object(Bucket=Bucket, Key=Key)

    fake_s3 = DeniedOnExtractions({SEG1_KEY: json.dumps(make_transcribe_json("hello"))})
    monkeypatch.setattr(les, "s3", lambda: fake_s3)
    called = []
    monkeypatch.setattr(llm_utils, "call_llm",
                        lambda *a, **k: called.append(1) or (json.dumps(
                            {"topics": [], "declared_site": None}), None))

    result = les.extract_session(BUCKET, "Benl1", "2026-07-06", SESSION_BASE)

    assert result is None
    assert fake_s3.put_calls == []      # nothing overwritten on a guess
    assert called == []                 # and no LLM call wasted on a doomed pass


def test_read_existing_extraction_separates_absent_from_unreadable(monkeypatch):
    fake_s3 = FakeS3({})
    monkeypatch.setattr(les, "s3", lambda: fake_s3)
    assert les.read_existing_extraction(BUCKET, OUT_KEY) is None          # absent

    fake_s3.objects[OUT_KEY] = "{ not json"
    assert les.read_existing_extraction(BUCKET, OUT_KEY) is les.UNKNOWN   # corrupt

    fake_s3.objects[OUT_KEY] = json.dumps(["not", "a", "dict"])
    assert les.read_existing_extraction(BUCKET, OUT_KEY) is les.UNKNOWN   # wrong shape

    fake_s3.objects[OUT_KEY] = json.dumps({"tier": "live"})
    assert les.read_existing_extraction(BUCKET, OUT_KEY) == {"tier": "live"}


def test_final_request_artifact_routes_to_a_final_pass(monkeypatch):
    fake_s3 = _s3_with_one_segment()
    req_key = f"{les.FINAL_REQUESTS_PREFIX}sid-abc.json"
    fake_s3.objects[req_key] = json.dumps({
        "userFolder": "Benl1", "date": "2026-07-06", "sessionBase": SESSION_BASE,
    })
    monkeypatch.setattr(les, "s3", lambda: fake_s3)
    monkeypatch.setattr(les, "S3_BUCKET", BUCKET)
    calls = []

    def _counting(prompt, max_tokens=4096, force_json=False, enable_thinking=None):
        calls.append(enable_thinking)
        return json.dumps({"topics": [], "declared_site": None}), None

    monkeypatch.setattr(llm_utils, "call_llm", _counting)

    les.lambda_handler({"Records": [{"s3": {"object": {"key": req_key}}}]}, None)

    assert calls == [True]                                   # thinking mode
    assert json.loads(fake_s3.objects[OUT_KEY])["tier"] == les.TIER_FINAL


def test_unreadable_final_request_is_skipped_not_raised(monkeypatch):
    """A dead artifact must not retry-storm: S3 events retry on exception, and
    every retry would fail identically (same reasoning as M-5/M-6)."""
    fake_s3 = _s3_with_one_segment()
    req_key = f"{les.FINAL_REQUESTS_PREFIX}broken.json"
    fake_s3.objects[req_key] = "{not json"
    monkeypatch.setattr(les, "s3", lambda: fake_s3)
    monkeypatch.setattr(les, "S3_BUCKET", BUCKET)
    monkeypatch.setattr(
        llm_utils, "call_llm",
        _fake_call_llm_returning({"topics": [], "declared_site": None}),
    )

    result = les.lambda_handler({"Records": [{"s3": {"object": {"key": req_key}}}]}, None)

    assert result == {"results": []}
    assert fake_s3.put_calls == []


# ---------------------------------------------------------------------------
# M-5 — missing ANTHROPIC_API_KEY: skip quietly (no raise, no retry-storm),
# and never even reach S3/Claude.
# ---------------------------------------------------------------------------

def test_missing_api_key_returns_none_without_raising(monkeypatch):
    monkeypatch.setattr(llm_utils, "api_key_configured", lambda: False)

    def fail_if_called(*a, **k):
        raise AssertionError("must not gather/call Claude when API key is missing")

    monkeypatch.setattr(les, "s3", fail_if_called)
    monkeypatch.setattr(llm_utils, "call_llm", fail_if_called)

    result = les.extract_session(BUCKET, "Benl1", "2026-07-06", SESSION_BASE)

    assert result is None


# ---------------------------------------------------------------------------
# M-6 — no usable speaker turns (e.g. every segment is corrupt/empty): skip
# quietly, no Claude call, no write.
# ---------------------------------------------------------------------------

def test_no_turns_returns_none_without_claude_call(monkeypatch):
    fake_s3 = FakeS3({SEG1_KEY: "{not valid json at all"})
    monkeypatch.setattr(les, "s3", lambda: fake_s3)

    def fail_if_called(*a, **k):
        raise AssertionError("must not call Claude when there are no usable turns")

    monkeypatch.setattr(llm_utils, "call_llm", fail_if_called)

    result = les.extract_session(BUCKET, "Benl1", "2026-07-06", SESSION_BASE)

    assert result is None
    assert fake_s3.put_calls == []


# ---------------------------------------------------------------------------
# Unified-extraction Task 1 — rich schema (findings/decisions/questions/origin)
# + derived legacy safety_flags (item-writer compatibility bridge)
# ---------------------------------------------------------------------------

def test_schema_has_new_fields():
    for field in ("findings", "decisions", "questions", "origin"):
        assert field in les.EXTRACTION_SCHEMA


def test_derive_safety_flags_from_findings():
    findings = [
        {"observation": "x", "domain": "safety", "severity": "major", "recommended_action": "y"},
        {"observation": "q", "domain": "quality", "severity": "minor"},
    ]
    assert les._derive_safety_flags(findings) == [
        {"observation": "x", "risk_level": "high", "recommended_action": "y"},
    ]


def test_derive_safety_flags_empty():
    assert les._derive_safety_flags(None) == []
    assert les._derive_safety_flags([]) == []
    assert les._derive_safety_flags(
        [{"observation": "q", "domain": "quality", "severity": "minor"}]
    ) == []


def test_extraction_topic_gains_derived_safety_flags_and_findings(monkeypatch):
    fake_s3 = FakeS3({SEG1_KEY: json.dumps(make_transcribe_json("hello world"))})
    monkeypatch.setattr(les, "s3", lambda: fake_s3)
    monkeypatch.setattr(
        llm_utils, "call_llm",
        _fake_call_llm_returning({
            "topics": [{
                "topic_title": "Block C Pour",
                "category": "safety",
                "origin": "inspection",
                "action_items": [
                    {"action": "Fix scaffold", "responsible": "Sam",
                     "deadline": None, "priority": "high"},
                ],
                "findings": [
                    {"observation": "Missing guardrail", "domain": "safety",
                     "severity": "major", "entity": {"name": None, "trade": "scaffolder"},
                     "recommended_action": "Install guardrail"},
                    {"observation": "Slab finish uneven", "domain": "quality",
                     "severity": "minor", "entity": {"name": None, "trade": None},
                     "recommended_action": None},
                ],
                "decisions": [],
                "questions": [],
            }],
            "declared_site": None,
        }),
    )

    extraction = les.extract_session(BUCKET, "Benl1", "2026-07-06", SESSION_BASE)

    topic = extraction["topics"][0]
    # action_items unchanged (item-writer contract)
    assert topic["action_items"] == [
        {"action": "Fix scaffold", "responsible": "Sam", "deadline": None, "priority": "high"},
    ]
    # new findings preserved verbatim
    assert len(topic["findings"]) == 2
    # legacy safety_flags derived from the safety-domain finding only
    assert topic["safety_flags"] == [
        {"observation": "Missing guardrail", "risk_level": "high",
         "recommended_action": "Install guardrail"},
    ]

    written = json.loads(fake_s3.objects[OUT_KEY])
    assert written["topics"][0]["safety_flags"] == topic["safety_flags"]
    assert written["topics"][0]["action_items"] == topic["action_items"]


# ---------------------------------------------------------------------------
# Life-conversation-separation Task 7 — schema + prompt request work_class
# ---------------------------------------------------------------------------

def test_prompt_and_schema_request_work_class():
    assert '"work_class"' in les.EXTRACTION_SCHEMA
    assert '"work_confidence"' in les.EXTRACTION_SCHEMA
    assert '"is_mixed"' in les.EXTRACTION_SCHEMA
    prompt = les.build_extraction_prompt("U", "2026-07-21", "sess", [], 0)
    assert "work_class" in prompt and "non_work" in prompt


def test_prompt_asks_to_split_by_subject_and_write_scannable_titles():
    prompt = les.build_extraction_prompt("U", "2026-07-21", "sess", [], 0)
    # topic granularity: split by subject, don't lump, don't over-split
    assert "BY SUBJECT" in prompt
    assert "Do NOT lump" in prompt and "Do NOT over-split" in prompt
    # topic_title: short + subject-first
    assert "topic_title" in prompt and "glanceable" in prompt
    # action_items: subject-first, glanceable, survive truncation, no generic openers
    assert "AT A GLANCE" in prompt and "SURVIVE TRUNCATION" in prompt
    assert "SUBJECT/OUTCOME" in prompt and "generic word" in prompt
