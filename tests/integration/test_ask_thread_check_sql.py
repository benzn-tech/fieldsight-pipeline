"""Integration: the Ask-thread re-check (spec 2026-10-22 §6) against real
report_chunks, redactions, memberships and project_documents rows."""
import uuid

import pytest

import ask_thread_check as atc
from repositories import chunks, companies, memberships, project_docs, redactions, sites, users

pytestmark = pytest.mark.integration

SESSION = "extractions/Ben_L/2026-10-20/sid" + "a" * 32
DAY_KEY = "reports/2026-10-20/Ben_L/daily_report.json"


def _vec():
    v = [0.0] * 1024
    v[0] = 1.0
    return v


def _admin(db, co, sub):
    u = users.upsert_user(db, sub, sub + "@t.nz", company_id=co["id"])
    return {"id": u["id"], "company_id": co["id"], "global_role": "admin",
            "folder_name": None}


def _turn(tid, keys=(), docs=(), days=()):
    return {"turn_id": tid, "question": "q", "at": 1.0, "result": {"answer": "a"},
            "sources": {"record_keys": list(keys),
                        "documents": [{"document_id": str(d), "site_id": None} for d in docs],
                        "report_days": [{"folder": f, "report_date": d} for f, d in days]}}


def test_reingested_key_still_visible(db):
    co = companies.create_company(db, "Threads Co A")
    s = sites.create_site(db, co["id"], "S")
    caller = _admin(db, co, "sub-thr-a")
    first = chunks.insert_chunk(db, s["id"], "2026-10-20", "transcript", "poured B2", _vec(),
                                source_s3_key=SESSION)
    assert atc.withheld_turns(db, caller, [_turn("t1", [SESSION])]) == set()

    # The nightly re-ingest: the key's rows are deleted and re-inserted, ids change.
    chunks.delete_chunks_for_sources(db, [SESSION], co["id"])
    again = chunks.insert_chunk(db, s["id"], "2026-10-20", "transcript", "poured B2", _vec(),
                                source_s3_key=SESSION)
    assert again["id"] != first["id"]
    assert atc.withheld_turns(db, dict(caller), [_turn("t1", [SESSION])]) == set()

    # Gone for good: no live row carries the key.
    chunks.delete_chunks_for_sources(db, [SESSION], co["id"])
    assert atc.withheld_turns(db, dict(caller), [_turn("t1", [SESSION])]) == {"t1"}


def test_tombstoned_session_key_withheld(db):
    co = companies.create_company(db, "Threads Co B")
    s = sites.create_site(db, co["id"], "S")
    caller = _admin(db, co, "sub-thr-b")
    chunks.insert_chunk(db, s["id"], "2026-10-20", "transcript", "x", _vec(),
                        source_s3_key=SESSION)
    other = "extractions/Ben_L/2026-10-20/sid" + "b" * 32
    chunks.insert_chunk(db, s["id"], "2026-10-20", "transcript", "y", _vec(),
                        source_s3_key=other)
    redactions.create_recording_tombstone(db, co["id"], SESSION, "removed", None, "admin")
    out = atc.withheld_turns(db, caller, [_turn("t1", [SESSION]), _turn("t2", [other]),
                                          _turn("t3")])
    assert out == {"t1"}


def test_deleted_session_inside_day_report_withheld(db):
    co = companies.create_company(db, "Threads Co C")
    s = sites.create_site(db, co["id"], "S")
    caller = _admin(db, co, "sub-thr-c")
    chunks.insert_chunk(db, s["id"], "2026-10-20", "report", "the day", _vec(),
                        source_s3_key=DAY_KEY)
    turns = [_turn("t1", [DAY_KEY], days=[("Ben_L", "2026-10-20")])]
    assert atc.withheld_turns(db, caller, turns) == set()

    # One session of that day is deleted. The day report's own key is never
    # tombstoned, so only rule 4 (folder + date) can catch it.
    redactions.create_recording_tombstone(db, co["id"], SESSION, "removed", None, "admin")
    assert atc.withheld_turns(db, dict(caller), turns) == {"t1"}
    # Another day of the same folder is untouched.
    next_day = "reports/2026-10-21/Ben_L/daily_report.json"
    chunks.insert_chunk(db, s["id"], "2026-10-21", "report", "next day", _vec(),
                        source_s3_key=next_day)
    assert atc.withheld_turns(db, dict(caller), [
        _turn("t2", [next_day], days=[("Ben_L", "2026-10-21")])]) == set()


def test_site_removed_from_scope_withheld(db):
    co = companies.create_company(db, "Threads Co D")
    s = sites.create_site(db, co["id"], "S")
    w = users.upsert_user(db, "sub-thr-w", "w@thr.nz", company_id=co["id"])
    users.set_folder_name(db, "sub-thr-w", "Ben_L")
    memberships.add_membership(db, w["id"], s["id"], "worker")
    chunks.insert_chunk(db, s["id"], "2026-10-20", "transcript", "mine", _vec(),
                        source_s3_key=SESSION, user_id=w["id"])

    def caller():
        # A fresh dict per request: visible_scope memoises on the caller dict.
        return {"id": w["id"], "company_id": co["id"], "global_role": "worker",
                "folder_name": "Ben_L"}

    assert atc.withheld_turns(db, caller(), [_turn("t1", [SESSION])]) == set()
    memberships.archive_membership(db, w["id"], s["id"])
    assert atc.withheld_turns(db, caller(), [_turn("t1", [SESSION])]) == {"t1"}


def test_author_restricted_caller_needs_own_author(db):
    co = companies.create_company(db, "Threads Co E")
    s = sites.create_site(db, co["id"], "S")
    w = users.upsert_user(db, "sub-thr-w2", "w2@thr.nz", company_id=co["id"])
    other = users.upsert_user(db, "sub-thr-o2", "o2@thr.nz", company_id=co["id"])
    memberships.add_membership(db, w["id"], s["id"], "worker")
    theirs = "extractions/Amy/2026-10-20/sid" + "c" * 32
    chunks.insert_chunk(db, s["id"], "2026-10-20", "transcript", "theirs", _vec(),
                        source_s3_key=theirs, user_id=other["id"])
    caller = {"id": w["id"], "company_id": co["id"], "global_role": "worker",
              "folder_name": None}
    assert atc.withheld_turns(db, caller, [_turn("t1", [theirs])]) == {"t1"}


def test_document_deleted_withheld(db):
    co = companies.create_company(db, "Threads Co F")
    s = sites.create_site(db, co["id"], "S")
    caller = _admin(db, co, "sub-thr-f")
    keep, gone = uuid.uuid4(), uuid.uuid4()
    for d in (keep, gone):
        project_docs.insert_document(db, doc_id=d, site_id=s["id"], company_id=co["id"],
                                     title="Spec", filename="spec.pdf", source_key="k/" + str(d),
                                     content_type="application/pdf", uploaded_by=caller["id"])
    project_docs.soft_delete(db, gone)
    out = atc.withheld_turns(db, caller, [_turn("t1", docs=[keep]), _turn("t2", docs=[gone])])
    assert out == {"t2"}
