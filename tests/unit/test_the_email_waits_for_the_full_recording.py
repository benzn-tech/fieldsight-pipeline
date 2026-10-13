"""The confirmation email (and a spoken check's report) waits for the full
record. TEST 2026-10-08 (Ben_Lin_test2 11:36): transcripts landed while the
final pass was thinking; the email went out from the first 90 seconds of a
five-minute pre-pour check, 40 seconds before the full record.
"""
import io
import json

import pytest

es = pytest.importorskip("lambda_extract_session")
iw = pytest.importorskip("lambda_item_writer")

KEY1 = ("transcripts/Ben_Lin_test2/2026-10-08/ben_lin_test2_2026-10-08_11-36-50"
        "_sid5cc16402d89c4b19809b4d2cdc92aa3b_c0000_off0.0_to30.0_srcwav.json")
KEY2 = KEY1.replace("11-36-50", "11-38-20").replace("c0000", "c0003")


def transcript(words):
    return {"results": {"transcripts": [{"transcript": " ".join(w for w, _ in words)}],
                        "items": [{"type": "pronunciation", "start_time": str(t),
                                   "end_time": str(t + 0.2), "speaker_label": "spk_0",
                                   "alternatives": [{"content": w, "confidence": "1.0"}]}
                                  for w, t in words]}}


class S3:
    """Lists one transcript, then -- as if one landed while the model thought -- two."""

    def __init__(self):
        self.lists, self.puts = 0, []

    def get_object(self, Bucket, Key):
        if Key in (KEY1, KEY2):
            return {"Body": io.BytesIO(json.dumps(transcript([("Starting", 1.0), ("pre-pour", 1.5)])).encode())}
        raise KeyError(Key)

    def get_paginator(self, op):
        outer = self

        class P:
            def paginate(self, Bucket, Prefix):
                outer.lists += 1
                keys = [KEY1] if outer.lists == 1 else [KEY1, KEY2]
                yield {"Contents": [{"Key": k} for k in keys if k.startswith(Prefix)]}
        return P()

    def put_object(self, **kw):
        self.puts.append(kw)
        return {}


@pytest.fixture
def run(monkeypatch):
    import llm_utils
    s3 = S3()
    monkeypatch.setattr(es, "s3", lambda: s3)
    monkeypatch.setattr(es, "_sites_cache", None)
    monkeypatch.setattr(es.time, "sleep", lambda s: None)
    monkeypatch.setattr(llm_utils, "call_llm", lambda prompt, **kw: (json.dumps(
        {"topics": [], "declared_site": None}), None))
    return s3


def test_THE_a_final_that_missed_transcripts_says_it_is_short(run):
    out = es.extract_session("b", "Ben_Lin_test2", "2026-10-08",
                             "sid5cc16402d89c4b19809b4d2cdc92aa3b", final=True)
    assert out.get("incomplete") is True
    written = json.loads(next(p for p in run.puts if p["Key"].startswith("extractions/"))["Body"])
    assert written["incomplete"] is True
    assert any(p["Key"].startswith("extraction_requests/") for p in run.puts), "and a re-run follows"


def test_at_the_rerun_cap_the_record_is_not_held_back(monkeypatch):
    monkeypatch.setattr(es, "gather_session_segments", lambda *a: [KEY1, KEY2])
    assert es._known_short("b", "f", "d", "s", [KEY1], es.FINAL_RERUN_MAX_GENERATIONS - 1) is False
    assert es._known_short("b", "f", "d", "s", [KEY1], 0) is True
    assert es._known_short("b", "f", "d", "s", [KEY1, KEY2], 0) is False


def test_a_listing_failure_reads_as_complete(monkeypatch):
    def boom(*a):
        raise RuntimeError("S3 down")
    monkeypatch.setattr(es, "gather_session_segments", boom)
    assert es._known_short("b", "f", "d", "s", [KEY1], 0) is False


def test_item_writer_sends_no_email_and_makes_no_report_from_a_short_record():
    src = open(iw.__file__, encoding="utf-8").read()
    i = src.index('if extraction.get("tier") == "final" and extraction.get("incomplete"):')
    j = src.index('elif extraction.get("tier") == "final":', i)
    assert "_final_email_context(" not in src[i:j]
    assert 'and not extraction.get("incomplete")):' in src
