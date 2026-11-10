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


# ---------------------------------------------------------------------------
# Cross-session status reconcile r0, Task 3: lambda_suggestion_writer accepts
# `status_suggestions` and `pick_cards`, re-reads every item it is handed, and
# takes company_id from the re-read row -- never from the event.
# ---------------------------------------------------------------------------

lambda_suggestion_writer = pytest.importorskip(
    "lambda_suggestion_writer", reason="requires psycopg (installed in CI)")


@pytest.fixture
def seeded(monkeypatch, migrated_db_url):
    """Two sites of one company, each with live action items, committed for real (the
    writer opens its own connection and commits). Yields a namespace; cleans up by
    cascade."""
    from repositories import topics
    tag = uuid.uuid4().hex[:8]
    seed = get_connection(migrated_db_url, autocommit=True)
    co = other_co = None
    try:
        co = companies.create_company(seed, f"SW-Co-{tag}")
        other_co = companies.create_company(seed, f"SW-Other-{tag}")
        site = sites.create_site(seed, co["id"], f"SW-Site-{tag}")
        site2 = sites.create_site(seed, co["id"], f"SW-Site2-{tag}")
        texts = ["Send drawings", "Book crane", "Order rebar", "Pour slab"]
        topics.upsert_topic(seed, site["id"], DATE, "T",
                            source_s3_key=f"extractions/sw/{tag}.json",
                            action_items=[{"text": t} for t in texts])
        topics.upsert_topic(seed, site2["id"], DATE, "T2",
                            source_s3_key=f"extractions/sw2/{tag}.json",
                            action_items=[{"text": "Other site item"}])
        stable = {r[0]: str(r[1]) for r in seed.execute(
            "SELECT text, stable_id FROM action_items WHERE site_id = ANY(%s)",
            ([site["id"], site2["id"]],)).fetchall()}
        monkeypatch.setattr(lambda_suggestion_writer, "get_connection",
                            lambda *a, **k: get_connection(migrated_db_url))

        class NS:
            pass
        ns = NS()
        ns.seed, ns.co, ns.other_co, ns.site, ns.site2, ns.stable, ns.tag = (
            seed, co, other_co, site, site2, stable, tag)
        yield ns
    finally:
        if co is not None:
            seed.execute("DELETE FROM sites WHERE company_id=%s", (co["id"],))
            seed.execute("DELETE FROM companies WHERE id=%s", (co["id"],))
        if other_co is not None:
            seed.execute("DELETE FROM companies WHERE id=%s", (other_co["id"],))
        seed.close()


def _st(ns, text, status="done", site=None, offset=1.0, **extra):
    e = {"stable_id": ns.stable[text], "site_id": str((site or ns.site)["id"]),
         "suggested_status": status, "quote": "so that one is done",
         "segment_key": f"seg-{ns.tag}", "offset_sec": offset, "run": "final:x"}
    e.update(extra)
    return e


def _stored(ns):
    return ns.seed.execute(
        "SELECT stable_id::text, suggested_status, company_id, site_id "
        "FROM action_status_suggestions WHERE company_id = ANY(%s) OR site_id = ANY(%s) "
        "ORDER BY stable_id",
        ([ns.co["id"], ns.other_co["id"]], [ns.site["id"], ns.site2["id"]])).fetchall()


def _skipped(**over):
    d = {"gone": 0, "moved_site": 0, "terminal": 0, "same": 0, "duplicate": 0, "failed": 0}
    d.update(over)
    return d


def test_reread_skips_gone(seeded):
    gone = _st(seeded, "Send drawings")
    gone["stable_id"] = str(uuid.uuid4())                 # no such item
    malformed = dict(gone, stable_id="not-a-uuid")
    r = lambda_suggestion_writer.lambda_handler({"status_suggestions": [gone, malformed]}, None)
    assert r == {"written": 0, "status_written": 0, "status_skipped": _skipped(gone=2)}
    assert _stored(seeded) == []


def test_reread_skips_moved_site(seeded):
    """The matcher thought the item was on site2; it is on site now."""
    r = lambda_suggestion_writer.lambda_handler(
        {"status_suggestions": [_st(seeded, "Send drawings", site=seeded.site2)]}, None)
    assert r["status_written"] == 0 and r["status_skipped"] == _skipped(moved_site=1)
    assert _stored(seeded) == []


def test_reread_skips_terminal(seeded):
    seeded.seed.execute("UPDATE action_items SET status='done' WHERE stable_id=%s",
                        (seeded.stable["Send drawings"],))
    seeded.seed.execute("UPDATE action_items SET status='closed' WHERE stable_id=%s",
                        (seeded.stable["Book crane"],))
    r = lambda_suggestion_writer.lambda_handler({"status_suggestions": [
        _st(seeded, "Send drawings", status="blocked"),
        _st(seeded, "Book crane", status="done")]}, None)
    assert r["status_written"] == 0 and r["status_skipped"] == _skipped(terminal=2)
    assert _stored(seeded) == []


def test_reread_skips_same(seeded):
    seeded.seed.execute("UPDATE action_items SET status='blocked' WHERE stable_id=%s",
                        (seeded.stable["Send drawings"],))
    r = lambda_suggestion_writer.lambda_handler(
        {"status_suggestions": [_st(seeded, "Send drawings", status="blocked")]}, None)
    assert r["status_written"] == 0 and r["status_skipped"] == _skipped(same=1)
    assert _stored(seeded) == []


def test_company_from_row_not_event(seeded):
    """The event carries a bogus company_id (another tenant's); the stored row carries the
    item's own company. A second delivery is a duplicate, not a second row."""
    e = _st(seeded, "Send drawings", company_id=str(seeded.other_co["id"]))
    r = lambda_suggestion_writer.lambda_handler({"status_suggestions": [e]}, None)
    assert r["status_written"] == 1 and r["status_skipped"] == _skipped()
    assert _stored(seeded) == [(seeded.stable["Send drawings"], "done",
                                seeded.co["id"], seeded.site["id"])]
    r = lambda_suggestion_writer.lambda_handler({"status_suggestions": [e]}, None)
    assert r["status_written"] == 0 and r["status_skipped"] == _skipped(duplicate=1)
    assert len(_stored(seeded)) == 1


def _pick(ns, candidates, offset=5.0):
    return {"site_id": str(ns.site["id"]), "suggested_status": "done",
            "quote": "that one is done", "segment_key": f"seg-{ns.tag}",
            "offset_sec": offset, "candidates": candidates, "run": "final:x",
            "company_id": str(ns.other_co["id"])}


def _picks(ns):
    return ns.seed.execute(
        "SELECT candidates, company_id, site_id, offset_sec FROM status_pick_cards "
        "WHERE company_id = ANY(%s) OR site_id = ANY(%s) ORDER BY offset_sec",
        ([ns.co["id"], ns.other_co["id"]], [ns.site["id"], ns.site2["id"]])).fetchall()


def test_pick_card_requires_all_candidates_live_on_site(seeded):
    s = seeded.stable
    ghost = str(uuid.uuid4())
    # Card 1: three live on site + one gone + one on site2 -> stored with the three.
    # Card 2: one live on site, one gone, one on site2 -> fewer than 2 survive -> skipped.
    r = lambda_suggestion_writer.lambda_handler({"pick_cards": [
        _pick(seeded, [s["Send drawings"], ghost, s["Book crane"], s["Other site item"],
                       s["Order rebar"]], offset=5.0),
        _pick(seeded, [s["Pour slab"], ghost, s["Other site item"]], offset=6.0)]}, None)
    # Card 2: the ghost leaves 2 possible; the other-site drop takes it below 2 -> moved_site.
    assert r == {"written": 0, "picks_written": 1, "picks_skipped": _pskipped(moved_site=1)}
    rows = _picks(seeded)
    assert len(rows) == 1
    assert rows[0][0] == [s["Send drawings"], s["Book crane"], s["Order rebar"]]
    assert rows[0][1] == seeded.co["id"], "company from the re-read rows, not the event"
    assert rows[0][2] == seeded.site["id"]

    # Redelivery: duplicate, no second card.
    r = lambda_suggestion_writer.lambda_handler({"pick_cards": [
        _pick(seeded, [s["Send drawings"], s["Book crane"]], offset=5.0)]}, None)
    assert r["picks_written"] == 0 and r["picks_skipped"]["duplicate"] == 1
    assert len(_picks(seeded)) == 1


def test_failed_status_insert_does_not_abort_batch(seeded, monkeypatch):
    """Against REAL Postgres: the first status insert executes a genuinely failing statement
    on the writer's own connection (Postgres marks the transaction aborted). Without the
    per-insert SAVEPOINT the programme suggestion, the second status suggestion and the pick
    card riding in the same transaction would all be lost."""
    from repositories import status_suggestions as real_repo
    real_insert = real_repo.insert_suggestion
    calls = []

    def flaky_insert(conn, company_id, site_id, stable_id, *a, **k):
        calls.append(stable_id)
        if len(calls) == 1:
            conn.execute("SELECT * FROM no_such_table_status_reconcile")
        return real_insert(conn, company_id, site_id, stable_id, *a, **k)

    monkeypatch.setattr(lambda_suggestion_writer.status_suggestions, "insert_suggestion",
                        flaky_insert)
    s = seeded.stable
    programme = {
        "site_id": str(seeded.site["id"]), "task_id": "T-1", "topic_id": None,
        "topic_title": "Steel frame progress", "topic_summary": "s",
        "topic_user_id": None, "report_date": DATE,
        "source_s3_key": f"extractions/sw/{seeded.tag}.json", "task_name": "Steel frame",
        "task_status_before": "in_progress", "task_progress_before": 40,
        "suggested_status": "completed", "suggested_progress": 100,
        "confidence": 0.9, "match_evidence": {"cosine": 0.1},
    }
    r = lambda_suggestion_writer.lambda_handler({
        "suggestions": [programme],
        "status_suggestions": [_st(seeded, "Send drawings"), _st(seeded, "Book crane")],
        "pick_cards": [_pick(seeded, [s["Order rebar"], s["Pour slab"]])]}, None)

    assert r == {"written": 1, "status_written": 1, "status_skipped": _skipped(failed=1),
                 "picks_written": 1,
                 "picks_skipped": _pskipped()}
    assert seeded.seed.execute(
        "SELECT count(*) FROM programme_progress_suggestions WHERE site_id=%s",
        (seeded.site["id"],)).fetchone()[0] == 1, "the programme suggestion must commit"
    assert [row[0] for row in _stored(seeded)] == [s["Book crane"]]
    assert len(_picks(seeded)) == 1


def _pskipped(**over):
    d = {"gone": 0, "moved_site": 0, "terminal": 0, "same": 0, "too_many": 0,
         "duplicate": 0, "failed": 0}
    d.update(over)
    return d


def test_pick_card_drops_terminal_and_same_candidates(seeded):
    """Choosing a closed candidate from a stale card would PATCH a hand-closed item back open;
    a candidate already at the card's status is a no-op choice. Both are dropped."""
    s = seeded.stable
    seeded.seed.execute("UPDATE action_items SET status='closed' WHERE stable_id=%s",
                        (s["Send drawings"],))
    seeded.seed.execute("UPDATE action_items SET status='blocked' WHERE stable_id=%s",
                        (s["Pour slab"],))
    blocked_card = dict(_pick(seeded, [s["Pour slab"], s["Order rebar"]], offset=9.0),
                        suggested_status="blocked")
    r = lambda_suggestion_writer.lambda_handler({"pick_cards": [
        # Suggested blocked; Pour slab is already blocked -> below 2 -> same.
        blocked_card,
        # 2 candidates, one closed -> below 2 because of the closed one -> terminal.
        _pick(seeded, [s["Book crane"], s["Send drawings"]], offset=7.0),
        # 3 candidates, one closed -> written with the other 2.
        _pick(seeded, [s["Book crane"], s["Send drawings"], s["Order rebar"]], offset=8.0),
    ]}, None)
    assert r == {"written": 0, "picks_written": 1, "picks_skipped": _pskipped(terminal=1, same=1)}
    rows = _picks(seeded)
    assert len(rows) == 1
    assert rows[0][0] == [s["Book crane"], s["Order rebar"]]
    assert rows[0][3] == 8.0
