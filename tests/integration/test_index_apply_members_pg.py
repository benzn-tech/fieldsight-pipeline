"""Integration: a deleted group member's speech is never indexed (real Postgres).

The apply step asks `_source_is_tombstoned` for each member of a group request. The unit
tests' FakeConn cannot say whether that SQL, the member key it builds, and the filtering of
chunks by `metadata.source_files` agree; this file does.

Skipped without TEST_DATABASE_URL -- a skip is NOT a pass.
"""
import uuid

import pytest

from repositories import companies, redactions, sites, users

pytestmark = pytest.mark.integration

lambda_ingest = pytest.importorskip("lambda_ingest", reason="requires psycopg (installed in CI)")

DATE = "2026-07-06"
EMB = [0.0] * 1024


@pytest.fixture
def world(db):
    tag = uuid.uuid4().hex[:8]
    co = companies.create_company(db, f"MemCo-{tag}")
    site = sites.create_site(db, co["id"], f"S-{tag}")
    user = users.upsert_user(db, f"sub-{tag}", f"{tag}@x.nz", company_id=co["id"])
    return dict(db=db, co=co, site=site, user=user, tag=tag, folder=f"Mem{tag}")


def _chunk(kind, text, files, topic_id=None):
    return {"chunk_type": kind, "chunk_text": text, "topic_id": topic_id, "embedding": EMB,
            "metadata": {"index": "session", "source_files": files}}


def _request(w, gbase, members, chunks):
    return {
        "extraction_key": f"extractions/{w['folder']}/{DATE}/{gbase}.json",
        "site_id": str(w["site"]["id"]), "user_id": None,
        "owner_company_id": str(w["co"]["id"]), "report_date": DATE,
        "user_folder": w["folder"], "session_base": gbase, "tier": "group",
        "extracted_at": "t", "member_bases": [m["session_base"] for m in members],
        "replaces": [], "topics": [], "members": members, "chunks": chunks,
    }


def _stored(db, key):
    rows = db.execute(
        "SELECT chunk_type, chunk_text FROM report_chunks WHERE source_s3_key = %s", (key,)
    ).fetchall()
    return {(r[0], r[1]) for r in rows}


def test_a_tombstoned_members_windows_and_the_digest_are_not_indexed(world):
    db, w = world["db"], world
    dead, alive = f"Benl1_2026-07-06_dead{w['tag']}", f"Benl1_2026-07-06_live{w['tag']}"
    gbase = f"grp{w['tag']}"
    members = [{"user_folder": w["folder"], "date": DATE, "session_base": b} for b in (dead, alive)]
    redactions.create_recording_tombstone(
        db, w["co"]["id"], f"extractions/{w['folder']}/{DATE}/{dead}", "deleted",
        w["user"]["id"], "worker")
    req = _request(w, gbase, members, [
        _chunk("transcript_window", "DEAD speech", [f"{dead}_c0001.json"]),
        _chunk("transcript_window", "LIVE speech", [f"{alive}_c0001.json"]),
        _chunk("transcript_window", "MIXED speech", [f"{dead}_c0002.json", f"{alive}_c0002.json"]),
        _chunk("session_digest", "Digest listing", [gbase, dead, alive]),
        _chunk("transcript_window", "BLOCK nobody", [], None),
    ])

    outcome, n = lambda_ingest._apply_index_result(db, req)

    assert outcome == "applied"
    assert _stored(db, req["extraction_key"]) == {
        ("transcript_window", "LIVE speech"), ("transcript_window", "BLOCK nobody")}
    assert n == 2


def test_without_a_tombstone_everything_is_indexed(world):
    db, w = world["db"], world
    m = f"Benl1_2026-07-06_ok{w['tag']}"
    gbase = f"grp{w['tag']}"
    req = _request(w, gbase, [{"user_folder": w["folder"], "date": DATE, "session_base": m}], [
        _chunk("transcript_window", "speech", [f"{m}_c0001.json"]),
        _chunk("session_digest", "Digest", [gbase, m]),
        _chunk("topic", "a topic of a topic that is gone", [gbase], str(uuid.uuid4())),
    ])
    outcome, n = lambda_ingest._apply_index_result(db, req)
    assert outcome == "applied" and n == 2          # the non-live topic's chunk is dropped
    assert _stored(db, req["extraction_key"]) == {
        ("transcript_window", "speech"), ("session_digest", "Digest")}
