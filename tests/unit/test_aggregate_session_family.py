"""Ask-agent rendering of session-family rows (_folder_from_source,
_aggregate_topics(family="session"), and rag-search's index_family echo)."""
import io
import json
import os

import pytest

os.environ.setdefault("AWS_ACCESS_KEY_ID", "testing")
os.environ.setdefault("AWS_SECRET_ACCESS_KEY", "testing")
os.environ.setdefault("AWS_DEFAULT_REGION", "ap-southeast-2")
os.environ.setdefault("ANTHROPIC_API_KEY", "sk-ant-test-dummy-key")
os.environ.setdefault("RAG_SEARCH_FUNCTION", "fieldsight-test-rag-search")

laa = pytest.importorskip("lambda_ask_agent", reason="requires boto3/urllib3")
import dashscope_utils  # noqa: E402

SRC = "extractions/Ben/2026-10-12/sidaaaa.json"
OTHER = "extractions/Ben/2026-10-12/sidbbbb.json"


def window(frm, to, dist=0.2, src=SRC, date="2026-10-12", text="[09:05:00] Ben: pour the slab"):
    return {"chunk_type": "transcript_window", "chunk_text": text, "topic_id": None,
            "source_s3_key": src, "report_date": date, "site_id": "s-1",
            "site_name": "Ellesmere", "site_slug": "ell", "distance": dist,
            "metadata": {"index": "session", "block": {"from": frm, "to": to}}}


def digest(dist=0.3, src=SRC, date="2026-10-12", span="09:00–17:30"):
    return {"chunk_type": "session_digest", "chunk_text": f"Session {span}\n- [09:00 - 10:00] Slab pour",
            "topic_id": None, "source_s3_key": src, "report_date": date, "site_id": "s-1",
            "site_name": "Ellesmere", "site_slug": "ell", "distance": dist,
            "metadata": {"index": "session"}}


def topic(title="Slab pour", dist=0.1):
    return {"chunk_type": "topic", "chunk_text": title, "topic_id": "t-1", "topic_title": title,
            "source_s3_key": SRC, "report_date": "2026-10-12", "site_id": "s-1",
            "site_name": "Ellesmere", "site_slug": "ell", "distance": dist,
            "metadata": {"index": "session", "time_range": "09:00 - 10:00"}}


def test_folder_from_extraction_source():
    assert laa._folder_from_source(SRC) == "Ben"
    # existing arms untouched
    assert laa._folder_from_source("reports/2026-10-12/Jarley/daily_report.json") == "Jarley"
    assert laa._folder_from_source("transcripts/Jarley/2026-10-12/x.json") == "Jarley"


def test_two_windows_same_block_make_one_row():
    rows = laa._aggregate_topics([window("09:00", "09:40", 0.3), window("09:00", "09:40", 0.2)],
                                 family="session")
    assert len(rows) == 1
    r = rows[0]
    assert r["title"] == "Recording 09:00–09:40"
    assert r["topic_id"] == ""
    assert r["chunk_type"] == "transcript_window"
    assert r["score"] == 0.2
    assert r["site_name"] == "Ellesmere"
    assert r["report_date"] == "2026-10-12"
    assert r["route"].startswith("/timeline?date=2026-10-12&user=Ben")
    assert "&site=ell" in r["route"]
    assert set(r) == {"report_date", "site_name", "topic_id", "title", "snippet",
                      "chunk_type", "route", "score", "lexical"}


def test_blocks_differing_by_start_or_source_stay_apart():
    rows = laa._aggregate_topics([window("09:00", "09:40"), window("10:00", "10:30"),
                                  window("09:00", "09:40", src=OTHER)], family="session")
    assert len(rows) == 3


def test_digest_row_title_and_key_set():
    rows = laa._aggregate_topics([digest()], family="session")
    assert len(rows) == 1
    assert rows[0]["title"] == "Session 09:00–17:30"
    assert rows[0]["chunk_type"] == "session_digest"
    assert rows[0]["topic_id"] == ""
    assert "Session" in rows[0]["snippet"]


def test_report_family_drops_unassigned_windows_as_before():
    chunks = [window("09:00", "09:40"), topic()]
    assert [r["title"] for r in laa._aggregate_topics(chunks)] == ["Slab pour"]
    assert laa._aggregate_topics(chunks) == laa._aggregate_topics(chunks, family="report")
    # digest chunk in the report family is not a topic either
    assert laa._aggregate_topics([digest()]) == []


def test_session_family_keeps_topic_rows_and_distance_cutoff():
    rows = laa._aggregate_topics([topic(), window("09:00", "09:40", dist=0.9)], family="session")
    assert [r["title"] for r in rows] == ["Slab pour"]  # the 0.9 block is cut off


def test_browse_sorts_blocks_and_digests_with_other_rows():
    chunks = [window("14:00", "14:30", date="2026-10-12"),
              window("08:00", "08:30", date="2026-10-12"),
              window("13:00", "13:10", date="2026-10-13"),
              digest(span="07:00–17:00", date="2026-10-12")]
    rows = laa._aggregate_topics(chunks, browse=True, family="session")
    assert [(r["report_date"], r["title"]) for r in rows] == [
        ("2026-10-13", "Recording 13:00–13:10"),
        ("2026-10-12", "Session 07:00–17:00"),
        ("2026-10-12", "Recording 08:00–08:30"),
        ("2026-10-12", "Recording 14:00–14:30"),
    ]


class FakeClient:
    def __init__(self, payload):
        self.payload = payload

    def invoke(self, FunctionName, InvocationType, Payload):
        return {"Payload": io.BytesIO(json.dumps(self.payload).encode("utf-8"))}


def _search(monkeypatch, payload):
    monkeypatch.setattr(dashscope_utils, "embed", lambda texts, dim=None: [[0.1] * 1024])
    monkeypatch.setattr(laa, "_get_lambda_client", lambda: FakeClient(payload))
    return laa._rag_search_list({"question": "slab", "caller_sub": "s"})


def test_rag_search_list_passes_index_family(monkeypatch):
    out = _search(monkeypatch, {"chunks": [window("09:00", "09:40")], "index_family": "session"})
    assert [r["title"] for r in out["results"]] == ["Recording 09:00–09:40"]


def test_rag_search_list_missing_index_family_means_report(monkeypatch):
    out = _search(monkeypatch, {"chunks": [window("09:00", "09:40")]})
    assert out["results"] == []
