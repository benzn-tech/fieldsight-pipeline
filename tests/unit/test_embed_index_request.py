# tests/unit/test_embed_index_request.py
import json
from datetime import datetime

import pytest

mod = pytest.importorskip("lambda_embed_report",
                          reason="requires psycopg (installed in CI)")
import index_request  # noqa: E402

les = pytest.importorskip("lambda_extract_session")

A_KEYS = [f"transcripts/Ben/2026-10-12/Ben_2026-10-12_11-49-00_sidaaaabbbbccccddddeeeeffff00001111_c000{i}.json" for i in (1, 2)]
B_KEYS = ["transcripts/Ben/2026-10-12/Ben_2026-10-12_15-10-00_sid99998888777766665555444433332222_c0001.json"]


def turn(hms, text, src):
    h, m, s = map(int, hms.split(":"))
    return {"speaker": "spk_0", "text": text, "abs_start": datetime(2026, 10, 12, h, m, s),
            "abs_start_str": hms, "abs_end_str": hms, "source_filename": src}


def make_req(**kw):
    req = {"extraction_key": "extractions/Ben/2026-10-12/sidaaaa.json", "extracted_at": "t1",
           "tier": "final", "site_id": "s", "user_id": "u", "owner_company_id": "c1",
           "report_date": "2026-10-12", "user_folder": "Ben", "session_base": "sidaaaa",
           "member_bases": [], "replaces": [],
           "members": [{"user_folder": "Ben", "date": "2026-10-12", "session_base": "sidaaaa"}],
           "topics": [{"id": "T1", "title": "Slab", "summary": "Pour tomorrow.",
                       "time_range": "11:50 - 11:55", "category": "progress", "participants": [],
                       "decisions": [], "questions": [], "action_items": [], "findings": [],
                       "work_class": "work"}]}
    req.update(kw)
    return req


class FakeS3:
    def __init__(self, objects):
        self.objects = dict(objects)
        self.puts = {}
        self.fail_get = {}

    def get_object(self, Bucket, Key):
        if Key in self.fail_get:
            raise self.fail_get[Key]
        if Key not in self.objects:
            err = Exception("nope")
            err.response = {"Error": {"Code": "NoSuchKey"}}
            raise err
        return {"Body": type("B", (), {"read": lambda s, v=self.objects[Key]: v})()}

    def put_object(self, Bucket, Key, Body, ContentType=None, **kw):
        if Key.startswith("lake_manifest/"):
            return
        self.puts[Key] = json.loads(Body)
        self.objects[Key] = Body.encode() if isinstance(Body, str) else Body


@pytest.fixture
def env(monkeypatch):
    calls = []
    monkeypatch.setattr(mod, "S3_BUCKET", "b")

    def embed(texts):
        calls.append(list(texts))
        return [[0.1] * 4 for _ in texts]

    monkeypatch.setattr(mod.dashscope_utils, "embed", embed)

    def install(req, objects=None):
        key = "index_requests/2026-10-12/Ben/sidaaaa.json"
        s3 = FakeS3({key: json.dumps(req).encode(), **(objects or {})})
        monkeypatch.setattr(mod, "s3", lambda: s3)
        monkeypatch.setattr(les, "s3", lambda: s3)
        return key, s3

    return install, calls


def run(key):
    return mod.lambda_handler({"Records": [{"s3": {"object": {"key": key}}}]}, None)


def vkey():
    return index_request.vectors_key("2026-10-12", "Ben", "sidaaaa")


def test_two_sessions_one_day_do_not_mix(env, monkeypatch):
    install, calls = env
    key, s3 = install(make_req(session_base="sidaaaabbbbccccddddeeeeffff00001111", members=[
        {"user_folder": "Ben", "date": "2026-10-12", "session_base": "sidaaaabbbbccccddddeeeeffff00001111"}]))

    class Pag:
        def paginate(self, Bucket, Prefix):
            yield {"Contents": [{"Key": k} for k in A_KEYS + B_KEYS]}

    s3.get_paginator = lambda op: Pag()
    seen = []

    def assemble(bucket, keys):
        seen.append(list(keys))
        return ([turn("11:51:00", "SESSION-A words", "a")] if keys == A_KEYS
                else [turn("15:11:00", "SESSION-B words", "b")]), [], {}

    monkeypatch.setattr(les, "assemble_session_turns", assemble)
    run(key)
    assert seen == [A_KEYS]
    out = s3.puts[index_request.vectors_key("2026-10-12", "Ben", "sidaaaabbbbccccddddeeeeffff00001111")]
    text = " ".join(c["chunk_text"] for c in out["chunks"])
    assert "SESSION-A" in text and "SESSION-B" not in text
    assert out["extraction_key"] == "extractions/Ben/2026-10-12/sidaaaa.json"
    assert all(len(c["embedding"]) == 4 for c in out["chunks"])


def test_second_run_with_identical_text_embeds_nothing(env, monkeypatch):
    install, calls = env
    key, s3 = install(make_req())
    monkeypatch.setattr(les, "gather_session_segments", lambda *a: A_KEYS)
    monkeypatch.setattr(les, "assemble_session_turns",
                        lambda b, k: ([turn("11:51:00", "hello slab", "a")], [], {}))
    run(key)
    assert len(calls) == 1
    first = s3.puts[vkey()]
    run(key)
    assert len(calls) == 1                      # zero embed calls the second time
    assert s3.puts[vkey()]["chunks"] == first["chunks"]


def test_changed_text_embeds_only_the_misses(env, monkeypatch):
    install, calls = env
    key, s3 = install(make_req())
    monkeypatch.setattr(les, "gather_session_segments", lambda *a: A_KEYS)
    monkeypatch.setattr(les, "assemble_session_turns",
                        lambda b, k: ([turn("11:51:00", "hello slab", "a")], [], {}))
    run(key)
    topic_text = next(c["chunk_text"] for c in s3.puts[vkey()]["chunks"] if c["chunk_type"] == "topic")
    monkeypatch.setattr(les, "assemble_session_turns",
                        lambda b, k: ([turn("11:51:00", "something else", "a")], [], {}))
    run(key)
    assert len(calls) == 2
    assert topic_text not in calls[1]
    assert any("something else" in t for t in calls[1])


def test_reuse_read_failure_other_than_absent_is_logged_and_embeds_all(env, monkeypatch, caplog):
    install, calls = env
    key, s3 = install(make_req())
    denied = Exception("denied")
    denied.response = {"Error": {"Code": "AccessDenied"}}
    s3.fail_get[vkey()] = denied
    monkeypatch.setattr(les, "gather_session_segments", lambda *a: A_KEYS)
    monkeypatch.setattr(les, "assemble_session_turns",
                        lambda b, k: ([turn("11:51:00", "hello", "a")], [], {}))
    with caplog.at_level("WARNING"):
        run(key)
    assert calls and vkey() in s3.puts
    assert any("AccessDenied" in r.getMessage() for r in caplog.records)


def test_group_merges_member_turns_in_clock_order(env, monkeypatch):
    install, calls = env
    members = [{"user_folder": "Ben", "date": "2026-10-12", "session_base": "sid1"},
               {"user_folder": "Sam", "date": "2026-10-12", "session_base": "sid2"}]
    key, s3 = install(make_req(session_base="grp1", members=members,
                               member_bases=["sid1", "sid2"], topics=[]))
    monkeypatch.setattr(les, "gather_session_segments",
                        lambda b, f, d, base: [f"transcripts/{f}/{d}/{base}.json"])
    by = {"transcripts/Ben/2026-10-12/sid1.json": [turn("11:00:00", "BEN-LATE", "x")],
          "transcripts/Sam/2026-10-12/sid2.json": [turn("10:00:00", "SAM-EARLY", "y")]}
    monkeypatch.setattr(les, "assemble_session_turns", lambda b, k: (by[k[0]], [], {}))
    run(key)
    out = s3.puts[index_request.vectors_key("2026-10-12", "Ben", "grp1")]
    windows = [c for c in out["chunks"] if c["chunk_type"] == "transcript_window"]
    text = "\n".join(c["chunk_text"] for c in windows)
    assert "BEN-LATE" in text and "SAM-EARLY" in text
    assert text.index("SAM-EARLY") < text.index("BEN-LATE")


def test_member_without_segments_does_not_crash(env, monkeypatch, caplog):
    install, calls = env
    members = [{"user_folder": "Ben", "date": "2026-10-12", "session_base": "gone"},
               {"user_folder": "Ben", "date": "2026-10-12", "session_base": "sid1"}]
    key, s3 = install(make_req(session_base="grp1", members=members, topics=[]))
    monkeypatch.setattr(les, "gather_session_segments",
                        lambda b, f, d, base: [] if base == "gone" else ["k"])
    monkeypatch.setattr(les, "assemble_session_turns",
                        lambda b, k: ([turn("10:00:00", "KEPT", "x")], [], {}))
    with caplog.at_level("WARNING"):
        run(key)
    out = s3.puts[index_request.vectors_key("2026-10-12", "Ben", "grp1")]
    assert any("KEPT" in c["chunk_text"] for c in out["chunks"])
    assert any("no transcript segments" in r.getMessage() for r in caplog.records)


def test_router_sends_index_requests_and_ignores_vectors(env, monkeypatch):
    install, calls = env
    key, s3 = install(make_req())
    hit = []
    monkeypatch.setattr(mod, "embed_index_request", lambda k: hit.append(k) or {"k": k})
    run(key)
    run("index_vectors/2026-10-12/Ben/sidaaaa.json")
    run("reindex_requests/2026-07-16/Ada_L/t-1.json.vectors.json")
    assert hit == [key]


def test_empty_members_fall_back_to_the_previous_artifacts_members(env, monkeypatch, caplog):
    install, calls = env
    prev_members = [{"user_folder": "Ben", "date": "2026-10-12", "session_base": "sid1"},
                    {"user_folder": "Sam", "date": "2026-10-12", "session_base": "sid2"}]
    prev = {"members": prev_members, "chunks": []}
    key, s3 = install(make_req(session_base="grp1", members=[], topics=[]),
                      {index_request.vectors_key("2026-10-12", "Ben", "grp1"): json.dumps(prev).encode()})
    loaded = []
    monkeypatch.setattr(les, "gather_session_segments",
                        lambda b, f, d, base: loaded.append(base) or [f"k-{base}"])
    monkeypatch.setattr(les, "assemble_session_turns",
                        lambda b, k: ([turn("10:00:00", "FROM-" + k[0], "x")], [], {}))
    with caplog.at_level("INFO"):
        run(key)
    assert loaded == ["sid1", "sid2"]
    out = s3.puts[index_request.vectors_key("2026-10-12", "Ben", "grp1")]
    assert out["members"] == prev_members            # the next edit inherits them too
    assert any("FROM-k-sid1" in c["chunk_text"] for c in out["chunks"])
    assert any("falling back" in r.getMessage() for r in caplog.records)
