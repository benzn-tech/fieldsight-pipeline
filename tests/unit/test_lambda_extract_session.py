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

les = pytest.importorskip("lambda_extract_session", reason="requires boto3 (installed in CI)")
import claude_utils  # noqa: E402  (import after importorskip, same module the handler calls)


BUCKET = "test-bucket"
CONFIG_KEY = "config/user_mapping.json"


class FakeS3:
    """Minimal S3 client double: object store keyed by S3 key, records puts."""

    def __init__(self, objects=None):
        self.objects = dict(objects or {})
        self.put_calls = []

    def get_object(self, Bucket, Key):
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


def _fake_call_claude_returning(payload):
    def _fake(prompt, max_tokens=4096):
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

    def fake_call_claude(prompt, max_tokens=4096):
        captured["prompt"] = prompt
        captured["max_tokens"] = max_tokens
        return json.dumps({"topics": [], "declared_site": None}), None

    monkeypatch.setattr(claude_utils, "call_claude", fake_call_claude)

    les.extract_session(BUCKET, "Benl1", "2026-07-06", SESSION_BASE)

    assert "UNIQUEWORDALPHA" in captured["prompt"]
    assert "UNIQUEWORDBETA" in captured["prompt"]
    assert "10:00:00" in captured["prompt"]  # abs_start_str of the first turn
    # BUG-16: max_tokens scales with segment count (2 segments here)
    assert captured["max_tokens"] == 4096 + 2 * 350


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
        claude_utils, "call_claude",
        _fake_call_claude_returning({"topics": [{"topic_title": "t"}], "declared_site": None}),
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
    assert extraction["topics"] == [{"topic_title": "t"}]

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
        claude_utils, "call_claude",
        _fake_call_claude_returning({"topics": [], "declared_site": None}),
    )

    # S3 event notifications encode spaces as '+' -- SEG1_KEY has none here,
    # but going through lambda_handler (not extract_session directly) still
    # exercises the unquote_plus + session_base_from_key dispatch path.
    event = {"Records": [{"s3": {"object": {"key": SEG1_KEY}}}]}
    les.lambda_handler(event, None)
    les.lambda_handler(event, None)

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
        claude_utils, "call_claude",
        _fake_call_claude_returning({
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
        claude_utils, "call_claude",
        _fake_call_claude_returning({"topics": [], "declared_site": None}),
    )

    extraction = les.extract_session(BUCKET, "Benl1", "2026-07-06", SESSION_BASE)

    assert extraction["declared_site"] is None

    # Missing key entirely must also passthrough as None (no KeyError).
    monkeypatch.setattr(
        claude_utils, "call_claude",
        _fake_call_claude_returning({"topics": []}),
    )
    extraction2 = les.extract_session(BUCKET, "Benl1", "2026-07-06", SESSION_BASE)
    assert extraction2["declared_site"] is None


# ---------------------------------------------------------------------------
# Claude failure -> RuntimeError, no S3 write (S3 event retry semantics)
# ---------------------------------------------------------------------------

def test_claude_failure_raises(monkeypatch):
    fake_s3 = FakeS3({SEG1_KEY: json.dumps(make_transcribe_json("hello world"))})
    monkeypatch.setattr(les, "s3", lambda: fake_s3)

    # call_claude itself fails
    monkeypatch.setattr(claude_utils, "call_claude", lambda prompt, max_tokens=4096: (None, "boom"))
    with pytest.raises(RuntimeError):
        les.extract_session(BUCKET, "Benl1", "2026-07-06", SESSION_BASE)
    assert fake_s3.put_calls == []

    # call_claude succeeds but returns unparseable JSON
    monkeypatch.setattr(claude_utils, "call_claude",
                         lambda prompt, max_tokens=4096: ("not json at all {{{", None))
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
        claude_utils, "call_claude",
        _fake_call_claude_returning({"topics": [], "declared_site": None}),
    )

    extraction = les.extract_session(BUCKET, "Benl1", "2026-07-06", SESSION_BASE)

    assert extraction["source_transcripts"] == [os.path.basename(SEG1_KEY)]
