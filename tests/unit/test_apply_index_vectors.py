"""apply_index_vectors: the in-VPC step that turns an index_vectors/ object into
report_chunks rows -- or, when the extraction moved on, into nothing."""
import json

import pytest

mod = pytest.importorskip("lambda_ingest", reason="requires psycopg (installed in CI)")

KEY = "extractions/Ben_X/2026-07-06/Benl1_2026-07-06_10-00-00.json"
VKEY = "index_vectors/2026-07-06/Ben_X/Benl1_2026-07-06_10-00-00.json"
CID = "company-1"


class _Cur:
    def __init__(self, conn, sql):
        self.conn, self.sql = conn, sql
        self.rowcount = 3

    def fetchall(self):
        return [{"id": i} for i in self.conn.live]

    def fetchone(self):
        if "AS source_s3_key" in self.sql:
            return None if self.conn.tombstoned else (1,)
        return {"id": "chunk-1"}


class FakeConn:
    def __init__(self, live=(), tombstoned=False):
        self.live, self.tombstoned = list(live), tombstoned
        self.log = []

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def execute(self, sql, params=None):
        self.log.append((sql, params))
        return _Cur(self, sql)

    def cursor(self, row_factory=None):
        conn = self

        class C:
            def execute(self, sql, params=None):
                conn.log.append((sql, params))
                return _Cur(conn, sql)
        return C()

    def writes(self):
        return [(s, p) for s, p in self.log
                if s.lstrip().upper().startswith(("INSERT", "DELETE"))]

    def chunk_inserts(self):
        return [p for s, p in self.writes() if s.lstrip().startswith("INSERT INTO report_chunks (")]


class FakeS3:
    def __init__(self, obj):
        self.obj = obj

    def get_object(self, Bucket, Key):
        assert Key == VKEY
        return {"Body": type("B", (), {"read": lambda s: json.dumps(self.obj).encode()})()}


def result(topic_ids=("t1",), chunk_topics=("t1", "t2"), key=KEY, base="Benl1_2026-07-06_10-00-00",
           replaces=()):
    return {
        "extraction_key": key, "site_id": "site-1", "user_id": "u1", "owner_company_id": CID,
        "report_date": "2026-07-06", "user_folder": "Ben_X", "session_base": base,
        "tier": "final", "extracted_at": "x", "member_bases": [], "replaces": list(replaces),
        "topics": [{"id": t} for t in topic_ids],
        "chunks": [{"chunk_type": "topic", "chunk_text": f"c-{t}", "topic_id": t,
                    "metadata": {"index": "session"}, "embedding": [0.1] * 4}
                   for t in chunk_topics],
    }


@pytest.fixture
def run(monkeypatch):
    def _run(conn, res):
        monkeypatch.setattr(mod, "s3", lambda: FakeS3(res))
        monkeypatch.setattr(mod, "get_connection", lambda: conn)
        return mod.apply_index_vectors(VKEY)
    return _run


def test_router_sends_index_vectors_to_apply(monkeypatch):
    seen = []
    monkeypatch.setattr(mod, "apply_index_vectors", lambda k: seen.append(k) or {"index_applied": k})
    out = mod.lambda_handler({"Records": [{"s3": {"object": {"key": VKEY}}}]}, None)
    assert seen == [VKEY] and out["results"][0]["index_applied"] == VKEY


def test_lock_is_the_first_statement_and_matches_item_writer(run):
    conn = FakeConn(live=["t1"])
    run(conn, result())
    sql, params = conn.log[0]
    assert sql == "SELECT pg_advisory_xact_lock(hashtext(%s))" and params == (KEY,)


def test_overlap_applies_nulls_topic_not_live_and_scopes_deletes(run):
    conn = FakeConn(live=["t1"])
    member = "extractions/Ben_X/2026-07-06/m1.json"
    out = run(conn, result(replaces=[member]))
    assert out == {"index_applied": VKEY, "outcome": "applied", "chunks": 2}
    delete = next((s, p) for s, p in conn.writes() if s.startswith("DELETE"))
    assert "company_id" in delete[0]
    assert delete[1]["company_id"] == CID
    assert delete[1]["keys"] == [KEY, member]
    # (site, user, source_s3_key, topic_id, ...): t1 is live and kept, t2 is not and is NULLed
    assert [(p[2], p[3]) for p in conn.chunk_inserts()] == [(KEY, "t1"), (KEY, None)]


def test_stale_request_writes_nothing(run):
    conn = FakeConn(live=["t9"])
    out = run(conn, result(topic_ids=("t1",)))
    assert out["outcome"] == "stale" and out["chunks"] == 0
    assert conn.writes() == []


def test_cleared_when_no_topic_is_live_archives_by_session(run):
    conn = FakeConn(live=[])
    out = run(conn, result())
    assert out["outcome"] == "cleared"
    archive = [(s, p) for s, p in conn.writes() if "report_chunks_archive" in s]
    assert len(archive) == 1
    # What the archive selects: chunks of the request's company whose metadata.source_files
    # contains the session_base (topic_ids is empty, so there is no topic arm).
    p = archive[0][1]
    assert p["topic_ids"] == [] and p["company_id"] == CID
    assert p["session_base"] == "Benl1\\_2026-07-06\\_10-00-00"
    assert "source_files" in archive[0][0]
    assert conn.chunk_inserts() == []


def test_group_race_member_request_after_group_apply_is_cleared(run):
    # The group pass superseded the member's topics, so none is live for the member key.
    conn = FakeConn(live=[])
    out = run(conn, result(topic_ids=("m-t1",), chunk_topics=("m-t1",)))
    assert out["outcome"] == "cleared"
    assert conn.chunk_inserts() == []


def test_windows_only_not_tombstoned_replaces_chunks(run):
    conn = FakeConn(live=[], tombstoned=False)
    out = run(conn, result(topic_ids=(), chunk_topics=(None, None)))
    assert out["outcome"] == "applied" and out["chunks"] == 2
    assert any(s.startswith("DELETE FROM report_chunks WHERE source_s3_key") for s, _ in conn.writes())
    assert not any("report_chunks_archive" in s for s, _ in conn.writes())


def test_windows_only_tombstoned_archives_instead(run):
    conn = FakeConn(live=[], tombstoned=True)
    out = run(conn, result(topic_ids=(), chunk_topics=(None,)))
    assert out["outcome"] == "cleared"
    assert any("report_chunks_archive" in s for s, _ in conn.writes())
    assert conn.chunk_inserts() == []


def test_tombstone_check_is_derived_from_the_shared_predicate():
    from deleted_predicates import DELETED_SOURCE_PREDICATE
    assert DELETED_SOURCE_PREDICATE.format(alias="k") in mod._SOURCE_NOT_DELETED_SQL


def test_unexpected_error_emits_metric_and_reraises(run, capsys):
    class Boom(FakeConn):
        def execute(self, sql, params=None):
            raise RuntimeError("db down")
    with pytest.raises(RuntimeError):
        run(Boom(), result())
    assert "SearchIndexApplyFailed" in capsys.readouterr().out
