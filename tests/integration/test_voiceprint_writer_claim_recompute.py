"""Integration: when the voiceprint writer lands a session's names it re-derives the
session's items' claim_type (Track C Task 8), against a real Postgres.

Items are written through lambda_item_writer (a fake S3 holding the synthetic artifact,
the same harness as test_item_writer_event_links.py, copied rather than imported), then a
`match_names` event goes through lambda_voiceprint_writer with real record_turn_name. All
text is synthetic.

Skipped without TEST_DATABASE_URL -- a skip is NOT a pass.
"""
import importlib.util
import io
import json
import logging
import os
import uuid

import pytest

from db.connection import get_connection
from repositories import companies, memberships, sites, users

pytestmark = pytest.mark.integration

DATE = "2026-11-02"
STEM = "synthetic-chunk-a"

lambda_item_writer = pytest.importorskip(
    "lambda_item_writer", reason="requires psycopg (installed in CI)")
vw = pytest.importorskip("lambda_voiceprint_writer")
claim_type_store = pytest.importorskip("repositories.claim_type_store")


class _FakeS3:
    def __init__(self, objects):
        self.objects = objects

    def get_object(self, Bucket, Key):
        return {"Body": io.BytesIO(self.objects[Key].encode("utf-8"))}

    def get_paginator(self, op):
        assert op == "list_objects_v2"
        return _FakePaginator(self.objects)


class _FakePaginator:
    def __init__(self, objects):
        self.objects = objects

    def paginate(self, Bucket, Prefix):
        yield {"Contents": [{"Key": k} for k in self.objects if k.startswith(Prefix)]}


def _linked(start):
    return {"status": "linked", "source_stem": STEM, "start_sec": start,
            "end_sec": start + 4.0, "speaker_label": "spk_0", "quote_status": None,
            "score": 0.9, "candidate_path": "window", "rule_version": "lexical-test"}


def _artifact():
    return {
        "schema_version": 1, "tier": "final", "extracted_at": "2026-11-02T10:30:00Z",
        "topics": [{
            "topic_title": "Synthetic topic", "category": "progress", "summary": "s",
            "time_range": "10:00 – 10:05", "participants": [], "safety_flags": [],
            "action_items": [{"action": "Synthetic task one", "support": _linked(10.0)}],
            "findings": [{"observation": "Synthetic finding one", "support": _linked(20.0)}],
            "decisions": [], "questions": [],
        }],
    }


def _tenant(seed, tag, label, co=None):
    co = co or companies.create_company(seed, f"Claims-VW-{label}-{tag}")
    site = sites.create_site(seed, co["id"], f"Claims-VW-Site-{label}-{tag}")
    folder = f"ClaimsVW{label}-{tag}"
    user = users.upsert_field_only_user(seed, co["id"], folder, "Fol", "Der", "worker")
    other = users.upsert_field_only_user(seed, co["id"], f"{folder}-o", "Oth", "Er", "worker")
    memberships.add_membership(seed, user["id"], site["id"], "worker")
    return {"co": co, "site": site, "folder": folder, "user": user, "other": other}


@pytest.fixture
def world(monkeypatch, migrated_db_url):
    tag = uuid.uuid4().hex[:8]
    seed = get_connection(migrated_db_url, autocommit=True)
    tenants = []
    try:
        session_base = "sid" + uuid.uuid4().hex
        fake_s3 = _FakeS3({})
        monkeypatch.setattr(lambda_item_writer, "_s3_client", fake_s3)
        monkeypatch.setattr(lambda_item_writer, "get_connection",
                            lambda *a, **k: get_connection(migrated_db_url))
        monkeypatch.setattr(lambda_item_writer.match_request, "emit", lambda *a, **k: None)
        monkeypatch.setattr(vw, "get_connection", lambda *a, **k: get_connection(migrated_db_url))

        def tenant(label, same_company_as=None):
            t = _tenant(seed, tag, label,
                        co=same_company_as["co"] if same_company_as else None)
            t["owns_co"] = same_company_as is None
            tenants.append(t)
            t["key"] = f"extractions/{t['folder']}/{DATE}/{session_base}.json"
            fake_s3.objects[t["key"]] = json.dumps(_artifact())
            assert lambda_item_writer.write_extraction_items(
                DATE, t["folder"], t["key"]) == {"skipped": False, "topics": 1}
            t["vp"] = seed.execute(
                "INSERT INTO speaker_voiceprints (company_id, user_id, display_name, status) "
                "VALUES (%s, %s, 'Synthetic Person', 'confirmed') RETURNING id",
                (t["co"]["id"], t["user"]["id"])).fetchone()[0]
            t["other_vp"] = seed.execute(
                "INSERT INTO speaker_voiceprints (company_id, user_id, display_name, status) "
                "VALUES (%s, %s, 'Synthetic Other', 'confirmed') RETURNING id",
                (t["co"]["id"], t["other"]["id"])).fetchone()[0]
            return t

        yield {"seed": seed, "session_base": session_base, "tenant": tenant,
               "url": migrated_db_url}
    finally:
        for t in tenants:
            seed.execute("DELETE FROM sites WHERE id=%s", (t["site"]["id"],))
            seed.execute("DELETE FROM speaker_turn_names WHERE company_id=%s", (t["co"]["id"],))
            seed.execute("DELETE FROM speaker_voiceprints WHERE company_id=%s",
                         (t["co"]["id"],))
            for u in ("user", "other"):
                seed.execute("DELETE FROM users WHERE id=%s", (t[u]["id"],))
        for t in tenants:
            if t["owns_co"]:
                seed.execute("DELETE FROM companies WHERE id=%s", (t["co"]["id"],))
        seed.close()


_TEXT = {"action_items": "text", "findings": "observation"}


def _claim(w, t, table, text):
    return w["seed"].execute(
        f"SELECT c.claim_type, c.claim_basis FROM {table} c "
        f"JOIN topics t ON t.id = c.topic_id WHERE t.source_s3_key = %s "
        f"AND t.superseded_at IS NULL AND c.{_TEXT[table]} = %s",
        (t["key"], text)).fetchone()


def _match_event(w, t):
    return {"op": "match_names", "company_id": str(t["co"]["id"]),
            "session_base": w["session_base"],
            "results": [
                {"turn_ref": f"{STEM}@10.0", "status": "confirmed", "person_key": str(t["vp"]),
                 "display_name": "Synthetic Person", "score": 0.9, "margin": 0.2},
                {"turn_ref": f"{STEM}@20.0", "status": "confirmed",
                 "person_key": str(t["other_vp"]), "display_name": "Synthetic Other",
                 "score": 0.9, "margin": 0.2}]}


def test_match_names_triggers_one_recompute_per_session(world, monkeypatch):
    w = world
    a = w["tenant"]("A")
    assert _claim(w, a, "action_items", "Synthetic task one")[0] is None
    real = claim_type_store.recompute_session
    calls = []

    def counting(conn, **kw):
        out = real(conn, **kw)
        calls.append((kw, out))
        return out
    monkeypatch.setattr(vw.claim_type_store, "recompute_session", counting)

    out = vw.lambda_handler(_match_event(w, a), None)
    assert out["written"] == 2
    assert len(calls) == 1, calls
    kw, counts = calls[0]
    assert kw == {"company_id": str(a["co"]["id"]), "site_id": None,
                  "session_base": w["session_base"], "statement_timeout": "15s"}
    assert counts["sites"] == 1 and counts["rows"] == 2 and counts["changed"] == 2
    act = _claim(w, a, "action_items", "Synthetic task one")
    fnd = _claim(w, a, "findings", "Synthetic finding one")
    assert act[0] == "observed" and act[1]["named_user_id"] == str(a["user"]["id"])
    assert fnd[0] == "stated" and fnd[1]["named_user_id"] == str(a["other"]["id"])
    assert "Synthetic" not in json.dumps([act[1], fnd[1]])


def test_recompute_failure_never_fails_the_name_write(world, monkeypatch, caplog):
    w = world
    a = w["tenant"]("A")

    def failing(conn, **kw):
        conn.execute("SELECT 1/0")
    monkeypatch.setattr(vw.claim_type_store, "recompute_session", failing)

    with caplog.at_level(logging.INFO):
        out = vw.lambda_handler(_match_event(w, a), None)
    assert out["written"] == 2
    names = w["seed"].execute(
        "SELECT count(*) FROM speaker_turn_names WHERE company_id = %s AND session_base = %s "
        "AND superseded_at IS NULL", (a["co"]["id"], w["session_base"])).fetchone()[0]
    assert names == 2, "the names must be committed even though the recompute failed"
    assert _claim(w, a, "action_items", "Synthetic task one")[0] is None
    warn = [r.getMessage() for r in caplog.records if r.levelname == "WARNING"]
    assert any("claim recompute failed" in m and "DivisionByZero" in m for m in warn), warn
    assert not any("Synthetic" in m for m in warn)


def test_same_session_base_in_another_company_is_not_recomputed(world):
    """Company B holds a topic under the SAME session_base (a different folder's key with
    the same stem). A's names landing must not touch B's rows -- B's stale value survives."""
    w = world
    a = w["tenant"]("A")
    b = w["tenant"]("B")
    stale = {"rule_version": "stale-sentinel", "support_status": "linked"}
    w["seed"].execute(
        "UPDATE action_items SET claim_type = 'observed', claim_basis = %s::jsonb "
        "WHERE site_id = %s", (json.dumps(stale), b["site"]["id"]))
    vw.lambda_handler(_match_event(w, a), None)
    assert _claim(w, a, "action_items", "Synthetic task one")[0] == "observed"
    got = _claim(w, b, "action_items", "Synthetic task one")
    assert got == ("observed", stale), "another company's row was recomputed"
    # And the store itself, asked for A with no site, names only A's site.
    conn = get_connection(w["url"])
    try:
        with conn:
            counts = claim_type_store.recompute_session(
                conn, company_id=str(a["co"]["id"]), session_base=w["session_base"])
    finally:
        conn.close()
    assert counts["sites"] == 1 and counts["changed"] == 0


def _backfill_module():
    path = os.path.join(os.path.dirname(__file__), "..", "..", "scripts",
                        "claim_type_backfill.py")
    spec = importlib.util.spec_from_file_location("claim_type_backfill", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_backfill_dry_run_writes_nothing_then_apply_writes(world, monkeypatch):
    """Names landed without the hook (as before Task 8): the backfill's dry run reports the
    change and leaves the rows; --apply writes it."""
    w = world
    a = w["tenant"]("A")
    monkeypatch.setattr(vw, "_recompute_claims", lambda *a, **k: None)
    vw.lambda_handler(_match_event(w, a), None)
    assert _claim(w, a, "action_items", "Synthetic task one")[0] is None
    backfill = _backfill_module()
    import datetime
    conn = get_connection(w["url"])
    try:
        dry = backfill.run(conn, site_id=a["site"]["id"],
                           since=datetime.date(2026, 11, 1), apply=False)
    finally:
        conn.close()
    assert dry["sessions"] == 1 and dry["changed"] == 2
    assert _claim(w, a, "action_items", "Synthetic task one")[0] is None
    assert _claim(w, a, "findings", "Synthetic finding one")[0] is None
    conn = get_connection(w["url"])
    try:
        later = backfill.run(conn, site_id=a["site"]["id"],
                             since=datetime.date(2026, 11, 3), apply=True)
        assert later["sessions"] == 0
        done = backfill.run(conn, site_id=a["site"]["id"], apply=True)
    finally:
        conn.close()
    assert done["changed"] == 2
    assert _claim(w, a, "action_items", "Synthetic task one")[0] == "observed"
    assert _claim(w, a, "findings", "Synthetic finding one")[0] == "stated"


def test_a_timeout_is_bounded_logged_by_type_and_the_names_stand(world, monkeypatch, caplog):
    """The real store under the hook's statement_timeout: the setting is in force on the
    recompute's statements, and a forced cancel (QueryCanceled) is a logged failure -- the
    names are committed and the reply is the same shape as a clean run's."""
    w = world
    a = w["tenant"]("A")
    seen = []

    def cancelled(conn, company_id, site_id, session_base):
        seen.append(conn.execute("SHOW statement_timeout").fetchone()[0])
        conn.execute("SELECT set_config('statement_timeout', '10ms', true)")
        conn.execute("SELECT pg_sleep(1)")
    monkeypatch.setattr(claim_type_store, "_recompute_site", cancelled)

    with caplog.at_level(logging.INFO):
        out = vw.lambda_handler(_match_event(w, a), None)
    assert seen == ["15s"], seen
    assert out == {"written": 2, "declined": 0, "inherited": 0}
    names = w["seed"].execute(
        "SELECT count(*) FROM speaker_turn_names WHERE company_id = %s AND session_base = %s "
        "AND superseded_at IS NULL", (a["co"]["id"], w["session_base"])).fetchone()[0]
    assert names == 2
    assert _claim(w, a, "action_items", "Synthetic task one")[0] is None
    warn = [r.getMessage() for r in caplog.records if r.levelname == "WARNING"]
    assert any("QueryCanceled" in m for m in warn), warn
    assert not any("Synthetic" in m for m in warn)


def test_one_sites_failure_does_not_roll_back_the_others(world, monkeypatch, caplog):
    w = world
    a = w["tenant"]("A")
    a2 = w["tenant"]("A2", same_company_as=a)
    real = claim_type_store._recompute_site

    def flaky(conn, company_id, site_id, session_base):
        if str(site_id) == str(a["site"]["id"]):
            real(conn, company_id, site_id, session_base)   # its writes must roll back
            conn.execute("SELECT 1/0")
        return real(conn, company_id, site_id, session_base)
    monkeypatch.setattr(claim_type_store, "_recompute_site", flaky)
    # Names for the turns of both sites (one session, one company).
    with caplog.at_level(logging.INFO):
        out = vw.lambda_handler(_match_event(w, a), None)
    assert out["written"] == 2
    assert _claim(w, a, "action_items", "Synthetic task one")[0] is None
    # A's recorder named on A2's turn: A2's recorder is someone else, so `stated`.
    assert _claim(w, a2, "action_items", "Synthetic task one")[0] == "stated"
    warn = [r.getMessage() for r in caplog.records if r.levelname == "WARNING"]
    assert any("1 site(s) failed" in m and "DivisionByZero" in m for m in warn), warn


def test_a_site_with_only_superseded_topics_is_not_visited(world):
    w = world
    a = w["tenant"]("A")
    a2 = w["tenant"]("A2", same_company_as=a)
    w["seed"].execute("UPDATE topics SET superseded_at = now() WHERE site_id = %s",
                      (a2["site"]["id"],))
    conn = get_connection(w["url"])
    try:
        with conn:
            counts = claim_type_store.recompute_session(
                conn, company_id=str(a["co"]["id"]), session_base=w["session_base"])
    finally:
        conn.close()
    assert counts["sites"] == 1 and counts["failed"] == 0
