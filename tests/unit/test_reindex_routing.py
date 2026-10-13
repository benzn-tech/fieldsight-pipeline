"""Edit routing by source family, and the reindex vectors regex.

An extraction-sourced topic (flag on) re-emits its session's index request; a
report-sourced topic, or any topic with the flag off, keeps the per-topic chain."""
import json

import pytest

reindex = pytest.importorskip("reindex", reason="requires psycopg (installed in CI)")

EXTRACTION = "extractions/Ada_L/2026-07-16/Ada_2026-07-16_09-00-00.json"
GROUP = "extractions/Ada_L/2026-07-16/grp0123456789ab.json"
REPORT = "reports/2026-07-16/Ada_L/daily_report.json"


class FakeS3:
    def __init__(self):
        self.puts = {}

    def put_object(self, Bucket, Key, Body, ContentType=None, **kw):
        self.puts[Key] = json.loads(Body)


def _row(source):
    return {"id": "t-1", "site_id": "s-1", "user_id": "u-9", "source_s3_key": source,
            "report_date": "2026-07-16", "site_name": "Alpha", "user_name": "Ada L",
            "time_range": "09:00 - 09:30", "title": "Slab pour", "category": "progress",
            "participants": [], "summary": "poured raft", "action_items": [],
            "safety_observations": [], "findings": [], "photos": []}


def _wire(monkeypatch, source, flag, company="co-1"):
    monkeypatch.setattr(reindex.topics, "get_topic_full", lambda conn, tid: _row(source))
    monkeypatch.setattr(reindex.aliases, "list_active", lambda conn, cid, site_ids=None: [])
    monkeypatch.setattr(reindex, "_company_id_for_site", lambda conn, sid: company)
    monkeypatch.setattr(reindex.redactions, "is_topic_redacted", lambda conn, tid: False)
    monkeypatch.setattr(reindex.redactions, "is_source_deleted", lambda conn, key: False)
    import lambda_org_api
    monkeypatch.setattr(lambda_org_api.redactions, "list_active_for_topics",
                        lambda conn, ids: {})
    if flag is None:
        monkeypatch.delenv("SEARCH_INDEX_WRITE", raising=False)
    else:
        monkeypatch.setenv("SEARCH_INDEX_WRITE", flag)
    seen = {}

    def fake_build(conn, key, **kw):
        seen["key"], seen["kw"] = key, kw
        return {"extraction_key": key, "report_date": kw["report_date"],
                "user_folder": kw["user_folder"], "session_base": kw["session_base"],
                "owner_company_id": kw["owner_company_id"],
                "members": [{"session_base": kw["session_base"]}], "topics": []}

    monkeypatch.setattr(reindex.index_request, "build", fake_build)
    return seen


def _kinds(s3):
    return ([k for k in s3.puts if k.startswith("index_requests/")],
            [k for k in s3.puts if k.startswith("reindex_requests/")])


def test_extraction_topic_with_flag_on_emits_one_index_request_and_no_reindex(monkeypatch):
    seen = _wire(monkeypatch, EXTRACTION, "true")
    s3 = FakeS3()
    key = reindex.enqueue_topic_reindex(s3, "bkt", object(), "t-1", "Ada_L", "2026-07-16")
    idx, rei = _kinds(s3)
    assert key == "index_requests/2026-07-16/Ada_L/Ada_2026-07-16_09-00-00.json"
    assert idx == [key] and rei == []
    assert seen["key"] == EXTRACTION
    assert seen["kw"]["owner_company_id"] == "co-1"
    assert seen["kw"]["tier"] is None and seen["kw"]["extracted_at"] is None


def test_report_topic_with_flag_on_still_emits_one_reindex_request(monkeypatch):
    _wire(monkeypatch, REPORT, "true")
    s3 = FakeS3()
    key = reindex.enqueue_topic_reindex(s3, "bkt", object(), "t-1", "Ada_L", "2026-07-16")
    idx, rei = _kinds(s3)
    assert key == "reindex_requests/2026-07-16/Ada_L/t-1.json"
    assert idx == [] and rei == [key]


@pytest.mark.parametrize("flag", [None, "false"])
def test_flag_off_keeps_the_per_topic_chain_for_extraction_topics(monkeypatch, flag):
    _wire(monkeypatch, EXTRACTION, flag)
    s3 = FakeS3()
    key = reindex.enqueue_topic_reindex(s3, "bkt", object(), "t-1", "Ada_L", "2026-07-16")
    idx, rei = _kinds(s3)
    assert idx == [] and rei == [key] == ["reindex_requests/2026-07-16/Ada_L/t-1.json"]


def test_group_key_is_emitted_without_members(monkeypatch):
    _wire(monkeypatch, GROUP, "true")
    s3 = FakeS3()
    key = reindex.enqueue_topic_reindex(s3, "bkt", object(), "t-1", "Ada_L", "2026-07-16")
    assert key == "index_requests/2026-07-16/Ada_L/grp0123456789ab.json"
    assert s3.puts[key]["members"] == []


def test_site_without_a_company_falls_back_to_the_per_topic_chain(monkeypatch):
    _wire(monkeypatch, EXTRACTION, "true", company=None)
    s3 = FakeS3()
    reindex.enqueue_topic_reindex(s3, "bkt", object(), "t-1", "Ada_L", "2026-07-16")
    idx, rei = _kinds(s3)
    assert idx == []  # no company: nothing to attribute the request to


def test_reindex_vectors_regex_matches_the_key_the_embedder_writes():
    ingest = pytest.importorskip("lambda_ingest", reason="requires psycopg (installed in CI)")
    key = reindex.vectors_key("2026-10-12", "Ben", "T1")
    assert ingest.REINDEX_VECTORS_RE.match(key)
    assert not ingest.REINDEX_VECTORS_RE.match("index_vectors/2026-10-12/Ben/T1.json")
    assert not ingest.INDEX_VECTORS_RE.match(key)


def test_vectors_keys_route_to_their_own_apply(monkeypatch):
    ingest = pytest.importorskip("lambda_ingest", reason="requires psycopg (installed in CI)")
    monkeypatch.setattr(ingest, "apply_reindex_vectors", lambda k: {"via": "reindex", "k": k})
    monkeypatch.setattr(ingest, "apply_index_vectors", lambda k: {"via": "index", "k": k})

    def route(key):
        ev = {"Records": [{"s3": {"object": {"key": key}}}]}
        return ingest.lambda_handler(ev, None)["results"][0]["via"]

    assert route("reindex_vectors/2026-10-12/Ben/T1.json") == "reindex"
    assert route("index_vectors/2026-10-12/Ben/S1.json") == "index"
