"""Integration: status suggestions land only on items carried BY IDENTITY (spec 2026-10-27).

Two real writer passes (live, then final) on one extraction key, the same harness as
test_carry_forward_pass0.py. The final pass carries two suggestions:
  * on an item whose item_id the live pass also wore -> stored against its stable_id;
  * on an item whose TEXT matches a live item exactly but whose item_id is fresh -> the writer's
    text pass sets carried_from on it too, and the suggestion must still be dropped (Part C.3:
    carried_from alone is not identity).
Neither touches action_items.status. Re-running the final pass stores nothing new.

Skipped without TEST_DATABASE_URL -- a skip is NOT a pass.
"""
import json
import uuid

import pytest

from db.connection import get_connection
from repositories import companies, memberships, sites, users
from tests.integration.test_carry_forward_pass0 import _FakeS3

pytestmark = pytest.mark.integration

DATE = "2026-10-27"

lambda_item_writer = pytest.importorskip(
    "lambda_item_writer", reason="requires psycopg (installed in CI)")


def _extraction(tier, extracted_at, items):
    return {"schema_version": 1, "tier": tier, "extracted_at": extracted_at,
            "topics": [{"topic_title": "Drawings", "category": "progress", "summary": "s",
                        "time_range": "10:00 – 10:05", "participants": [],
                        "action_items": items, "safety_flags": []}]}


def _suggest(status="done"):
    return {"status": status, "quote": "Sarah sent the drawings over so that one is done",
            "at": "10:04:00", "segment_key": "seg-1", "offset_sec": 240.0,
            "evidence": "verified"}


def test_suggestions_attach_by_identity_never_by_text(monkeypatch, migrated_db_url):
    tag = uuid.uuid4().hex[:8]
    seed = get_connection(migrated_db_url, autocommit=True)
    co = site = user = None
    carried_id, text_live_id = str(uuid.uuid4()), str(uuid.uuid4())
    try:
        co = companies.create_company(seed, f"Sugg-Co-{tag}")
        site = sites.create_site(seed, co["id"], f"Sugg-Site-{tag}")
        folder = f"Sugg-{tag}"
        user = users.upsert_field_only_user(seed, co["id"], folder, "Fol", "Der", "worker")
        memberships.add_membership(seed, user["id"], site["id"], "worker")
        key = f"extractions/{folder}/{DATE}/sid{tag}.json"
        fake_s3 = _FakeS3({key: json.dumps(_extraction("live", "2026-10-27T10:00:00Z", [
            {"action": "Sarah to send the drawings", "item_id": carried_id},
            {"action": "Book the crane for Friday", "item_id": text_live_id}]))})
        monkeypatch.setattr(lambda_item_writer, "_s3_client", fake_s3)
        monkeypatch.setattr(lambda_item_writer, "get_connection",
                            lambda *a, **k: get_connection(migrated_db_url))
        monkeypatch.setattr(lambda_item_writer.match_request, "emit", lambda *a, **k: None)

        assert lambda_item_writer.write_extraction_items(DATE, folder, key)["topics"] == 1

        final = json.dumps(_extraction("final", "2026-10-27T10:30:00Z", [
            {"action": "Sarah sends the drawings to the engineer", "item_id": carried_id,
             "status_suggestion": _suggest("done")},
            # Same words as the live item, FRESH id: carried by text, not identity.
            {"action": "Book the crane for Friday", "item_id": str(uuid.uuid4()),
             "status_suggestion": _suggest("closed")}]))
        fake_s3.objects[key] = final
        assert lambda_item_writer.write_extraction_items(DATE, folder, key)["topics"] == 1

        rows = seed.execute(
            "SELECT a.text, a.stable_id, a.status, a.carried_from IS NOT NULL "
            "FROM action_items a JOIN topics t ON t.id = a.topic_id "
            "WHERE t.source_s3_key = %s AND t.superseded_at IS NULL ORDER BY a.text",
            (key,)).fetchall()
        by_text = {r[0]: r for r in rows}
        crane = by_text["Book the crane for Friday"]
        drawings = by_text["Sarah sends the drawings to the engineer"]
        assert crane[3] is True, "precondition: the text pass DID set carried_from on it"
        assert crane[2] == "open" and drawings[2] == "open", "a suggestion never writes status"

        stored = seed.execute(
            "SELECT stable_id, suggested_status, state, run, quote, segment_key "
            "FROM action_status_suggestions WHERE site_id = %s", (site["id"],)).fetchall()
        assert len(stored) == 1, stored
        assert stored[0][0] == drawings[1]
        assert stored[0][1:4] == ("done", "pending", "final:2026-10-27T10:30:00Z")
        assert stored[0][5] == "seg-1"

        # Re-running the final pass is idempotent. Re-run with the artifact the EXTRACTOR would
        # have written: its filter (status_suggestions.filter_suggestions, after resolve) never
        # leaves a suggestion on a fresh-id child, so the crane suggestion above exists only in
        # this hand-built artifact. A redelivery of a hand-built artifact is the one case the
        # writer guard cannot catch (the first write made the crane's fresh id its own lineage);
        # see _store_status_suggestions.
        fixed = json.loads(final)
        fixed["topics"][0]["action_items"][1].pop("status_suggestion")
        fake_s3.objects[key] = json.dumps(fixed)
        assert lambda_item_writer.write_extraction_items(DATE, folder, key)["topics"] == 1
        n = seed.execute("SELECT count(*) FROM action_status_suggestions WHERE site_id = %s",
                         (site["id"],)).fetchone()[0]
        assert n == 1
    finally:
        if site is not None:
            seed.execute("DELETE FROM sites WHERE id=%s", (site["id"],))
        if user is not None:
            seed.execute("DELETE FROM users WHERE id=%s", (user["id"],))
        if co is not None:
            seed.execute("DELETE FROM companies WHERE id=%s", (co["id"],))
        seed.close()


def test_a_suggestion_matching_the_current_status_is_not_stored(monkeypatch, migrated_db_url):
    """A person already ticked it while the meeting ran; "that's done" adds nothing."""
    tag = uuid.uuid4().hex[:8]
    seed = get_connection(migrated_db_url, autocommit=True)
    co = site = user = None
    item_id = str(uuid.uuid4())
    try:
        co = companies.create_company(seed, f"Sugg2-Co-{tag}")
        site = sites.create_site(seed, co["id"], f"Sugg2-Site-{tag}")
        folder = f"Sugg2-{tag}"
        user = users.upsert_field_only_user(seed, co["id"], folder, "Fol", "Der", "worker")
        memberships.add_membership(seed, user["id"], site["id"], "worker")
        key = f"extractions/{folder}/{DATE}/sid{tag}.json"
        fake_s3 = _FakeS3({key: json.dumps(_extraction("live", "2026-10-27T10:00:00Z", [
            {"action": "Sarah to send the drawings", "item_id": item_id}]))})
        monkeypatch.setattr(lambda_item_writer, "_s3_client", fake_s3)
        monkeypatch.setattr(lambda_item_writer, "get_connection",
                            lambda *a, **k: get_connection(migrated_db_url))
        monkeypatch.setattr(lambda_item_writer.match_request, "emit", lambda *a, **k: None)
        lambda_item_writer.write_extraction_items(DATE, folder, key)
        seed.execute("UPDATE action_items SET status='done', updated_by='x' WHERE item_id=%s",
                     (item_id,))

        fake_s3.objects[key] = json.dumps(_extraction("final", "2026-10-27T10:30:00Z", [
            {"action": "Sarah to send the drawings", "item_id": item_id,
             "status_suggestion": _suggest("done")}]))
        lambda_item_writer.write_extraction_items(DATE, folder, key)

        assert seed.execute("SELECT count(*) FROM action_status_suggestions WHERE site_id=%s",
                            (site["id"],)).fetchone()[0] == 0
    finally:
        if site is not None:
            seed.execute("DELETE FROM sites WHERE id=%s", (site["id"],))
        if user is not None:
            seed.execute("DELETE FROM users WHERE id=%s", (user["id"],))
        if co is not None:
            seed.execute("DELETE FROM companies WHERE id=%s", (co["id"],))
        seed.close()


def test_repo_reads_pending_by_row_id_and_resolves_once(db):
    """The read joins row id -> stable_id -> pending suggestion; resolve is a CAS."""
    from repositories import status_suggestions as repo, topics
    co = companies.create_company(db, f"Sugg3-Co-{uuid.uuid4().hex[:6]}")
    s = sites.create_site(db, co["id"], "Sugg3-Site")
    u = users.upsert_user(db, f"sub-{uuid.uuid4().hex[:8]}", "s@x.nz", company_id=co["id"])
    t = topics.upsert_topic(db, s["id"], DATE, "T", user_id=u["id"],
                            action_items=[{"text": "Send drawings"}])
    aid, stable = db.execute("SELECT id, stable_id FROM action_items WHERE topic_id=%s",
                             (t["id"],)).fetchone()
    made = repo.insert_suggestion(db, co["id"], s["id"], stable, "done", "q", "k", 1.0, "final:x")
    assert repo.insert_suggestion(db, co["id"], s["id"], stable, "done", "q", "k", 1.0,
                                  "final:y") is None            # same item+status: never twice

    got = repo.pending_for_action_item_ids(db, [aid])
    assert [r["id"] for r in got[str(aid)]] == [made["id"]]
    assert repo.get_suggestion(db, "not-a-uuid") is None

    assert repo.resolve_suggestion(db, made["id"], "dismissed", u["id"])["state"] == "dismissed"
    assert repo.resolve_suggestion(db, made["id"], "accepted", u["id"]) is None
    assert repo.pending_for_action_item_ids(db, [aid]) == {}
