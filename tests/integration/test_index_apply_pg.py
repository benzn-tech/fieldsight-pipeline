"""Integration: the session-index apply step's SQL, run against a real Postgres.

The unit tests drive FakeConn, which never executes SQL. This file pins the three
statements the apply step rests on: the recording-tombstone check, the source-key archive
and the source-key delete (a Python list bound to text[]).

Skipped without TEST_DATABASE_URL -- a skip is NOT a pass.
"""
import uuid

import pytest

from repositories import chunks, companies, redactions, sites, users

pytestmark = pytest.mark.integration

lambda_ingest = pytest.importorskip("lambda_ingest", reason="requires psycopg (installed in CI)")

DATE = "2026-07-06"
EMB = [0.0] * 1024


def _chunk(conn, site, key, text, source_files):
    return chunks.insert_chunk(
        conn, site["id"], DATE, "window", text, EMB, source_s3_key=key,
        metadata={"source_files": source_files})["id"]


def _ids(conn, table="report_chunks", where="TRUE", params=()):
    return {r[0] for r in conn.execute(f"SELECT id FROM {table} WHERE {where}", params).fetchall()}


@pytest.fixture
def world(db):
    tag = uuid.uuid4().hex[:8]
    co = companies.create_company(db, f"IdxCo-{tag}")
    other = companies.create_company(db, f"IdxOther-{tag}")
    site = sites.create_site(db, co["id"], f"S-{tag}")
    osite = sites.create_site(db, other["id"], f"OS-{tag}")
    user = users.upsert_user(db, f"sub-{tag}", f"{tag}@x.nz", company_id=co["id"])
    folder = f"Idx{tag}"
    member = f"Benl1_2026-07-06_{tag}"
    k = f"extractions/{folder}/{DATE}/{member}.json"
    gk = f"extractions/{folder}/{DATE}/grp{tag}.json"
    return dict(db=db, co=co, other=other, site=site, osite=osite, user=user, folder=folder,
                member=member, k=k, gk=gk, tag=tag)


def test_tombstone_check_detects_a_tombstoned_key_only(world):
    db, w = world["db"], world
    prefix = f"extractions/{w['folder']}/{DATE}/{w['member']}"
    assert lambda_ingest._source_is_tombstoned(db, w["k"]) is False
    redactions.create_recording_tombstone(
        db, w["co"]["id"], prefix, "deleted", w["user"]["id"], "worker")
    assert lambda_ingest._source_is_tombstoned(db, w["k"]) is True
    assert lambda_ingest._source_is_tombstoned(db, w["gk"]) is False


def test_archive_by_sources_moves_only_this_companys_session_rows(world):
    db, w = world["db"], world
    k_row = _chunk(db, w["site"], w["k"], "session row", [w["member"]])
    report_row = _chunk(db, w["site"], f"reports/{DATE}/{w['folder']}/daily_report.json",
                        "report row", [w["member"]])
    group_row = _chunk(db, w["site"], w["gk"], "group row", [f"grp{w['tag']}", w["member"]])
    foreign_row = _chunk(db, w["osite"], w["k"], "other company row", [w["member"]])

    batch = uuid.uuid4()
    n = chunks.archive_chunks_for_sources(db, [w["k"]], batch, company_id=w["co"]["id"])

    assert n == 1
    assert _ids(db, where="id = ANY(%s)", params=([k_row, report_row, group_row, foreign_row],)) \
        == {report_row, group_row, foreign_row}
    assert _ids(db, "report_chunks_archive", "batch_id = %s", (batch,)) == {k_row}


def test_member_clear_leaves_report_and_group_rows(world):
    """The late-member race: a member request is cleared after the group took over."""
    db, w = world["db"], world
    k_row = _chunk(db, w["site"], w["k"], "member row", [w["member"]])
    report_row = _chunk(db, w["site"], f"reports/{DATE}/{w['folder']}/daily_report.json",
                        "report row", [w["member"]])
    group_row = _chunk(db, w["site"], w["gk"], "group row", [f"grp{w['tag']}", w["member"]])

    result = {"extraction_key": w["k"], "owner_company_id": str(w["co"]["id"]),
              "session_base": w["member"], "topics": [{"id": str(uuid.uuid4())}],
              "chunks": [], "replaces": []}
    outcome, _ = lambda_ingest._apply_index_result(db, result)

    assert outcome == "cleared"
    assert _ids(db, where="id = ANY(%s)", params=([k_row, report_row, group_row],)) \
        == {report_row, group_row}


def test_old_session_archive_would_have_taken_them(world):
    """Documents why the source-key archive exists: the session-base archive reaches the
    report row and the group row."""
    db, w = world["db"], world
    k_row = _chunk(db, w["site"], w["k"], "member row", [w["member"]])
    report_row = _chunk(db, w["site"], "reports/x/daily_report.json", "r", [w["member"]])
    group_row = _chunk(db, w["site"], w["gk"], "g", [w["member"]])
    chunks.archive_chunks_for_session(db, w["member"], [], uuid.uuid4(),
                                      company_id=w["co"]["id"])
    assert _ids(db, where="id = ANY(%s)", params=([k_row, report_row, group_row],)) == set()


def test_delete_for_sources_binds_a_list_and_leaves_report_rows(world):
    db, w = world["db"], world
    k_row = _chunk(db, w["site"], w["k"], "k", [w["member"]])
    gk_row = _chunk(db, w["site"], w["gk"], "gk", [w["member"]])
    report_row = _chunk(db, w["site"], "reports/x/daily_report.json", "r", [w["member"]])
    foreign_row = _chunk(db, w["osite"], w["k"], "f", [w["member"]])

    n = chunks.delete_chunks_for_sources(db, [w["k"], w["gk"]], w["co"]["id"])

    assert n == 2
    assert _ids(db, where="id = ANY(%s)", params=([k_row, gk_row, report_row, foreign_row],)) \
        == {report_row, foreign_row}
