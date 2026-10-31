"""Integration: POST /action-packs/{id}/actions/{aid}/apply against real Postgres (Task 7,
spec §5 + §14 S10/S11/S12/S15).

The real dispatch, repositories, advisory locks and content_edits. Most tests run in the
`db` fixture's rolled-back transaction (its commit is a no-op). The two concurrency tests
need committed rows and two connections, so they seed with an autocommit connection and
clean up explicitly.

The "re-extraction" in the concurrency tests is a faithful stand-in for lambda_item_writer's
write, not the writer itself: under the SAME `pg_advisory_xact_lock(hashtext(key))` it runs
the same repository calls the writer runs for a re-extraction of one key --
supersede_topics_for_source, upsert_topic, list_for_carry_forward and carry_identity.

Skipped without TEST_DATABASE_URL -- a skip is NOT a pass.
"""
import json
import threading
import time
import uuid

import pytest

from db.connection import get_connection
from repositories import (action_items, action_packs as ap, companies, content_edits,
                          memberships, sites, topics, users)

pytestmark = pytest.mark.integration

org = pytest.importorskip("lambda_org_api")

DAY = "2026-10-30"
EARLY = "2020-01-01T00:00:00+00:00"      # an as_of before every edit a test makes


@pytest.fixture
def env(db, monkeypatch):
    monkeypatch.setenv("ACTION_PACKS", "on")
    monkeypatch.delenv("COMPANY_STATUS_ENFORCE", raising=False)
    monkeypatch.setattr(org, "GRADED_ROLES", False)
    monkeypatch.setattr(db, "commit", lambda: None)
    return db


def _tag():
    return uuid.uuid4().hex[:8]


def _user(conn, co, role="worker", first="W"):
    t = _tag()
    u = users.upsert_user(conn, f"sub-apa-{t}", f"apa-{t}@example.com",
                          company_id=co["id"], global_role=role)
    conn.execute("UPDATE users SET first_name=%s, last_name=%s WHERE id=%s",
                 (first, t, u["id"]))
    return users.get_user_by_sub(conn, u["cognito_sub"])


def _name(u):
    return f"{u['first_name']} {u['last_name']}"


def _world(conn):
    co = companies.create_company(conn, f"Co-apa-{_tag()}")
    site = sites.create_site(conn, co["id"], f"Site-apa-{_tag()}")
    creator = _user(conn, co, "worker", "Creator")
    memberships.ensure_membership(conn, creator["id"], site["id"], "worker")
    admin = _user(conn, co, "admin", "Admin")
    return co, site, creator, admin


def _call(conn, u, method, route, body=None):
    ev = {"httpMethod": method, "path": "/api/org" + route, "queryStringParameters": None,
          "body": None if body is None else json.dumps(body), "headers": {},
          "requestContext": {"authorizer": {"claims": {"sub": u["cognito_sub"]}}}}
    res = org.dispatch(conn, ev, method, route)
    return res["statusCode"], json.loads(res["body"])


def _apply(conn, u, pid, aid, body=None):
    return _call(conn, u, "POST", f"/action-packs/{pid}/actions/{aid}/apply",
                 {} if body is None else body)


def _topic(conn, site, text="Fix the fire door", key=None, user_id=None):
    key = key or f"extractions/F_{_tag()}/{DAY}/sid{_tag()}.json"
    t = topics.upsert_topic(conn, site["id"], DAY, "Fire doors", source_s3_key=key,
                            action_items=[{"text": text}], user_id=user_id)
    row = conn.execute("SELECT id, stable_id FROM action_items WHERE topic_id=%s",
                       (t["id"],)).fetchone()
    return t, row[0], row[1]


SNAP_OPEN = {"status": "open", "deadline": None, "responsible": None, "priority": None,
             "text": "Fix the fire door", "updated_at": None}


def _ready_pack(conn, site, creator, actions, as_of=EARLY, title="Fire doors"):
    """A `ready` pack whose every action cites R1 (a topic source on `site` the creator
    recorded, so even a worker creator still sees it -- S13)."""
    t, _, _ = _topic(conn, site, text="evidence", user_id=creator["id"])
    pack = ap.create_queued(conn, site_id=site["id"], created_by=creator["id"],
                            instruction="tidy the fire door items")
    assert ap.claim(conn, pack["id"])
    full = []
    for i, a in enumerate(actions, 1):
        full.append({"seq": i, "refs": ["R1"], "verifier": "supported", **a})
    assert ap.store_result(conn, pack["id"], {
        "title": title, "reading": "r", "conclusion": [], "as_of": as_of,
        "sources": [{"ref": "R1", "kind": "topic", "provenance": "records",
                     "topic_id": str(t["id"])}],
        "actions": full}) == "ok"
    ids = [str(r[0]) for r in conn.execute(
        "SELECT id FROM action_pack_actions WHERE pack_id=%s ORDER BY seq",
        (pack["id"],)).fetchall()]
    return str(pack["id"]), ids


def _close(stable, snapshot=None, status="done"):
    return {"type": "close_item", "target_stable_id": str(stable),
            "target_snapshot": snapshot or SNAP_OPEN, "proposed": {"status": status}}


def _action(conn, aid):
    return conn.execute("SELECT state, applied_by, applied_result, created_item_stable_id "
                        "FROM action_pack_actions WHERE id=%s", (aid,)).fetchone()


def _edits(conn, item_id):
    return conn.execute("SELECT field, before_text, after_text, actor_user_id "
                        "FROM content_edits WHERE row_id=%s ORDER BY created_at, field",
                        (item_id,)).fetchall()


def _status(conn, item_id):
    return conn.execute("SELECT status FROM action_items WHERE id=%s", (item_id,)).fetchone()[0]


# ---------------------------------------------------------------- repositories

def test_count_item_edits_after_walks_the_carried_chain(db):
    co, site, _, admin = _world(db)
    t_old, old_id, stable = _topic(db, site)
    content_edits.append_content_edit(db, co["id"], "action_items", old_id, "priority",
                                      None, "high", admin["id"], "admin")
    # Re-extraction: the old topic is superseded, the new row carries the stable_id.
    topics.supersede_topics_for_source(db, t_old["source_s3_key"], "test:1")
    t_new, new_id, _ = _topic(db, site, key=t_old["source_s3_key"])
    db.execute("UPDATE action_items SET stable_id=%s, carried_from=%s WHERE id=%s",
               (stable, old_id, new_id))
    assert content_edits.count_item_edits_after(db, stable, EARLY) == 1
    assert content_edits.count_item_edits_after(db, stable, "2999-01-01T00:00:00Z") == 0
    _, _, other = _topic(db, site)
    assert content_edits.count_item_edits_after(db, other, EARLY) == 0
    # A predecessor reached only through carried_from (its row is gone) still counts.
    db.execute("UPDATE action_items SET stable_id=gen_random_uuid() WHERE id=%s", (old_id,))
    assert content_edits.count_item_edits_after(db, stable, EARLY) == 1


def test_ensure_agent_topic_get_or_create_and_insert_item(db):
    co, site, _, admin = _world(db)
    key = f"agent://action_pack/{uuid.uuid4()}"
    t1 = topics.ensure_agent_topic(db, key, site["id"], "Pack title", DAY, admin["id"])
    t2 = topics.ensure_agent_topic(db, key, site["id"], "Other title", "2026-10-31", None)
    assert t1["id"] == t2["id"] and t1["source"] == "agent" and t1["source_s3_key"] == key
    assert (t1["title"], str(t1["report_date"]), t1["user_id"]) == (
        "Pack title", DAY, admin["id"])
    item = action_items.insert_item(db, t1["id"], site["id"], "Book the crane",
                                    responsible=None, deadline="2026-11-09", priority="high")
    row = db.execute("SELECT topic_id, text, deadline::text, deadline_text, priority, status, "
                     "stable_id FROM action_items WHERE id=%s", (item["id"],)).fetchone()
    assert row == (t1["id"], "Book the crane", "2026-11-09", "2026-11-09", "high", "open",
                   item["stable_id"])


def test_lock_action_reads_the_row_for_update(db):
    _, site, creator, _ = _world(db)
    _, _, stable = _topic(db, site)
    pid, [aid] = _ready_pack(db, site, creator, [_close(stable)])
    row = ap.lock_action(db, pid, aid)
    assert row["state"] == "proposed" and str(row["id"]) == aid
    assert ap.lock_action(db, pid, str(uuid.uuid4())) is None


# ---------------------------------------------------------------- close / update

def test_close_applies_through_the_patch_logic(db, env):
    co, site, creator, admin = _world(db)
    _, item_id, stable = _topic(db, site)
    pid, [aid] = _ready_pack(db, site, creator, [_close(stable)])
    status, body = _apply(db, admin, pid, aid)
    assert status == 200, body
    assert body["state"] == "applied"
    res = body["applied_result"]
    assert res["action_item_id"] == str(item_id) and len(res["content_edit_ids"]) == 1
    assert _status(db, item_id) == "done"
    assert _edits(db, item_id) == [("status", "open", "done", admin["id"])]
    state, by, stored, _ = _action(db, aid)
    assert (state, by, stored) == ("applied", admin["id"], res)


def test_double_apply_returns_the_stored_result_and_writes_nothing(db, env):
    _, site, creator, admin = _world(db)
    _, item_id, stable = _topic(db, site)
    pid, [aid] = _ready_pack(db, site, creator, [_close(stable)])
    first = _apply(db, admin, pid, aid)
    second = _apply(db, admin, pid, aid)
    assert first[0] == second[0] == 200
    assert second[1] == first[1]
    assert len(_edits(db, item_id)) == 1


def test_update_with_an_applier_edit_audits_as_the_applier(db, env):
    co, site, creator, admin = _world(db)
    member = _user(db, co, "worker", "Member")
    memberships.ensure_membership(db, member["id"], site["id"], "worker")
    _, item_id, stable = _topic(db, site)
    pid, [aid] = _ready_pack(db, site, creator, [{
        "type": "update_item", "target_stable_id": str(stable), "target_snapshot": SNAP_OPEN,
        "proposed": {"deadline": "2026-11-09", "priority": "low"}}])
    status, body = _apply(db, admin, pid, aid,
                          {"fields": {"priority": "high", "responsible": _name(member)}})
    assert status == 200, body
    row = db.execute("SELECT deadline::text, deadline_text, priority, responsible, updated_by "
                     "FROM action_items WHERE id=%s", (item_id,)).fetchone()
    assert row == ("2026-11-09", "2026-11-09", "high", _name(member), admin["cognito_sub"])
    assert {(f, a, who) for f, _b, a, who in _edits(db, item_id)} == {
        ("deadline", "2026-11-09", admin["id"]), ("priority", "high", admin["id"]),
        ("responsible", _name(member), admin["id"])}
    assert len(body["applied_result"]["content_edit_ids"]) == 3


def test_responsible_not_a_site_member_is_400_and_the_action_is_unchanged(db, env):
    _, site, creator, admin = _world(db)
    _, item_id, stable = _topic(db, site)
    pid, [aid] = _ready_pack(db, site, creator, [{
        "type": "update_item", "target_stable_id": str(stable), "target_snapshot": SNAP_OPEN,
        "proposed": {"responsible": "Nobody Here"}}])
    assert _apply(db, admin, pid, aid) == (
        400, {"error": "assignee must be a member of this site"})
    assert _action(db, aid)[0] == "proposed" and _edits(db, item_id) == []


def test_human_edit_after_as_of_makes_it_stale(db, env):
    _, site, creator, admin = _world(db)
    _, item_id, stable = _topic(db, site)
    pid, [aid] = _ready_pack(db, site, creator, [_close(stable)])
    # A person edits a field the action does NOT touch, after the run read the item.
    assert _call(db, admin, "PATCH", f"/action-items/{item_id}", {"priority": "high"})[0] == 200
    status, body = _apply(db, admin, pid, aid)
    assert status == 409 and body["state"] == "stale"
    assert body["live"]["status"] == "open" and body["live"]["priority"] == "high"
    assert body["snapshot"] == SNAP_OPEN
    assert _status(db, item_id) == "open"
    state, _, stored, _ = _action(db, aid)
    assert state == "stale" and stored["reason"] == "changed"
    # Stale is sticky: a second apply answers the same, writing nothing.
    again = _apply(db, admin, pid, aid)
    assert again[0] == 409 and again[1]["state"] == "stale"
    assert len(_edits(db, item_id)) == 1


def test_an_edit_before_as_of_does_not_make_it_stale(db, env):
    co, site, creator, admin = _world(db)
    _, item_id, stable = _topic(db, site)
    content_edits.append_content_edit(db, co["id"], "action_items", item_id, "priority",
                                      None, None, admin["id"], "admin",
                                      created_at="2019-06-01T00:00:00Z")
    pid, [aid] = _ready_pack(db, site, creator, [_close(stable)])
    assert _apply(db, admin, pid, aid)[0] == 200


def test_touched_field_moved_since_the_snapshot_is_stale(db, env):
    _, site, creator, admin = _world(db)
    _, item_id, stable = _topic(db, site)
    db.execute("UPDATE action_items SET status='in_progress' WHERE id=%s", (item_id,))
    pid, [aid] = _ready_pack(db, site, creator, [_close(stable)])   # snapshot says open
    status, body = _apply(db, admin, pid, aid)
    assert status == 409 and body["state"] == "stale" and body["live"]["status"] == "in_progress"
    assert _status(db, item_id) == "in_progress"


def test_edit_on_a_carried_predecessor_is_stale(db, env):
    _, site, creator, admin = _world(db)
    t_old, old_id, stable = _topic(db, site)
    pid, [aid] = _ready_pack(db, site, creator, [_close(stable)])
    # The edit lands on the OLD row; then a re-extraction replaces it (nobody-touched carry
    # of identity only -- status etc. stay what the new pass wrote).
    assert _call(db, admin, "PATCH", f"/action-items/{old_id}", {"priority": "low"})[0] == 200
    topics.supersede_topics_for_source(db, t_old["source_s3_key"], "test:2")
    _, new_id, _ = _topic(db, site, key=t_old["source_s3_key"])
    db.execute("UPDATE action_items SET stable_id=%s, carried_from=%s WHERE id=%s",
               (stable, old_id, new_id))
    status, body = _apply(db, admin, pid, aid)
    assert status == 409 and body["state"] == "stale"
    assert _status(db, new_id) == "open" and _edits(db, new_id) == []


def test_target_gone_is_stale_with_no_live_row(db, env):
    _, site, creator, admin = _world(db)
    t, item_id, stable = _topic(db, site)
    pid, [aid] = _ready_pack(db, site, creator, [_close(stable)])
    topics.supersede_topics_for_source(db, t["source_s3_key"], "test:3")
    status, body = _apply(db, admin, pid, aid)
    assert status == 409 and body == {"error": "action is stale", "state": "stale",
                                      "live": None, "snapshot": SNAP_OPEN}
    assert _action(db, aid)[2] == {"reason": "target_gone"}


def test_already_in_the_proposed_state_is_a_noop(db, env):
    _, site, creator, admin = _world(db)
    _, item_id, stable = _topic(db, site)
    pid, [aid] = _ready_pack(db, site, creator, [_close(stable)])
    # Someone closed it by hand after the run (a content_edits row after as_of): still noop.
    assert _call(db, admin, "PATCH", f"/action-items/{item_id}", {"status": "done"})[0] == 200
    status, body = _apply(db, admin, pid, aid)
    assert status == 200 and body["state"] == "applied"
    assert body["applied_result"] == {"noop": True, "action_item_id": str(item_id)}
    assert len(_edits(db, item_id)) == 1                       # only the human's


def test_illegal_transition_is_409_and_the_action_is_unchanged(db, env):
    _, site, creator, admin = _world(db)
    _, item_id, stable = _topic(db, site)
    db.execute("UPDATE action_items SET status='done' WHERE id=%s", (item_id,))
    snap = {**SNAP_OPEN, "status": "done"}
    pid, [aid] = _ready_pack(db, site, creator, [_close(stable, snap, status="closed")])
    status, body = _apply(db, admin, pid, aid)
    assert status == 409 and body["error"] == "status transition not allowed"
    assert body["from"] == "done" and body["to"] == "closed"
    assert _action(db, aid)[0] == "proposed" and _status(db, item_id) == "done"


def test_worker_without_acl_gets_403_and_the_action_stays_proposed(db, env):
    _, site, creator, _ = _world(db)
    _, item_id, stable = _topic(db, site)
    pid, [aid] = _ready_pack(db, site, creator, [_close(stable)])
    status, body = _apply(db, creator, pid, aid)        # creator = plain worker, not assignee
    assert status == 403
    assert _action(db, aid)[0] == "proposed" and _status(db, item_id) == "open"


def test_site_pm_may_apply(db, env):
    co, site, creator, _ = _world(db)
    pm = _user(db, co, "pm", "Pm")
    memberships.ensure_membership(db, pm["id"], site["id"], "pm")
    _, item_id, stable = _topic(db, site)
    pid, [aid] = _ready_pack(db, site, creator, [_close(stable)])
    assert _apply(db, pm, pid, aid)[0] == 200 and _status(db, item_id) == "done"


def test_unexpected_error_rolls_back_and_marks_failed(db, env, monkeypatch):
    _, site, creator, admin = _world(db)
    _, item_id, stable = _topic(db, site)
    pid, [aid] = _ready_pack(db, site, creator, [_close(stable)])

    def boom(*a, **k):
        raise RuntimeError("secret detail")
    monkeypatch.setattr(org.content_edits, "append_content_edit", boom)
    status, body = _apply(db, admin, pid, aid)
    assert status == 500 and body == {"error": "apply failed", "code": "internal"}
    assert "secret" not in json.dumps(body)
    assert _status(db, item_id) == "open"                       # the UPDATE rolled back
    state, by, stored, _ = _action(db, aid)
    assert (state, by, stored) == ("failed", admin["id"], {"error": "internal"})
    monkeypatch.undo()
    monkeypatch.setenv("ACTION_PACKS", "on")
    monkeypatch.setattr(org, "GRADED_ROLES", False)
    # failed is not terminal: a retry applies.
    assert _apply(db, admin, pid, aid)[0] == 200 and _status(db, item_id) == "done"


# ---------------------------------------------------------------- preconditions

def test_preconditions(db, env, monkeypatch):
    _, site, creator, admin = _world(db)
    _, item_id, stable = _topic(db, site)
    pid, [a_close, a_draft, a_unsup, a_dis] = _ready_pack(db, site, creator, [
        _close(stable),
        {"type": "draft_email", "proposed": {"body": "x"}},
        {**_close(stable), "verifier": "unsupported"},
        _close(stable)])
    assert _apply(db, admin, pid, a_draft)[0] == 409
    assert _apply(db, admin, pid, a_unsup)[0] == 409
    assert _apply(db, admin, pid, str(uuid.uuid4()))[0] == 404
    assert _apply(db, admin, pid, a_close, {"fields": {"text": "x"}})[0] == 400
    assert _apply(db, admin, pid, a_close, {"oops": 1})[0] == 400
    assert _call(db, admin, "POST", f"/action-packs/{pid}/actions/{a_dis}/dismiss", {})[0] == 200
    assert _apply(db, admin, pid, a_dis) == (409, {"error": "action is dismissed",
                                                   "state": "dismissed"})
    other_co = companies.create_company(db, f"Co-apa-o-{_tag()}")
    assert _apply(db, _user(db, other_co, "admin"), pid, a_close)[0] == 404
    ap.archive(db, pid, True)
    assert _apply(db, admin, pid, a_close) == (409, {"error": "pack is archived"})
    ap.archive(db, pid, False)
    monkeypatch.setenv("ACTION_PACKS", "off")
    assert _apply(db, admin, pid, a_close) == (404, {"error": "not found"})
    monkeypatch.setenv("ACTION_PACKS", "on")
    assert _status(db, item_id) == "open"
    assert [_action(db, a)[0] for a in (a_close, a_draft, a_unsup)] == ["proposed"] * 3


def test_pack_not_ready_is_409(db, env):
    _, site, creator, admin = _world(db)
    pack = ap.create_queued(db, site_id=site["id"], created_by=creator["id"], instruction="x")
    assert _apply(db, admin, str(pack["id"]), str(uuid.uuid4())) == (
        409, {"error": "pack is not ready", "state": "queued"})


def test_s13_hidden_action_is_404(db, env):
    _, site, creator, admin = _world(db)
    _, item_id, stable = _topic(db, site)
    pid, [aid] = _ready_pack(db, site, creator, [_close(stable)])
    # Its only source's topic is deleted -> the action cites only removed sources.
    src_topic = db.execute("SELECT topic_id FROM action_pack_sources WHERE pack_id=%s",
                           (pid,)).fetchone()[0]
    db.execute("DELETE FROM topics WHERE id=%s", (src_topic,))
    assert _apply(db, admin, pid, aid) == (404, {"error": "action not found"})
    assert _status(db, item_id) == "open"


def test_inactive_company_is_refused_when_enforced(db, env, monkeypatch):
    co, site, creator, admin = _world(db)
    ext_co = companies.create_company(db, f"Co-apa-x-{_tag()}")
    ext = _user(db, ext_co, "worker", "Ext")
    memberships.add_external_membership(db, ext["id"], site["id"], "pm")
    _, item_id, stable = _topic(db, site)
    pid, [aid] = _ready_pack(db, site, ext, [_close(stable)])
    db.execute("UPDATE companies SET status='suspended' WHERE id=%s", (co["id"],))
    monkeypatch.setenv("COMPANY_STATUS_ENFORCE", "on")
    status, _ = _apply(db, ext, pid, aid)
    assert status in (404, 409)                 # out of reach, or refused: never applied
    assert _status(db, item_id) == "open" and _action(db, aid)[0] == "proposed"


# ---------------------------------------------------------------- create_item

def _create(text="Book the crane", **extra):
    return {"type": "create_item", "proposed": {"text": text, **extra}}


def test_create_item_creates_topic_item_number_and_audit(db, env):
    co, site, creator, admin = _world(db)
    pid, [a1, a2] = _ready_pack(db, site, creator, [
        _create(site_id=None, priority="high"), _create("Call council")], title="Crane week")
    db.execute("UPDATE action_pack_actions SET proposed = proposed || "
               "jsonb_build_object('site_id', %s::text) WHERE id=%s", (str(site["id"]), a1))
    status, body = _apply(db, admin, pid, a1, {"fields": {"deadline": "2026-11-09"}})
    assert status == 200, body
    res = body["applied_result"]
    item = db.execute(
        "SELECT a.id, a.text, a.priority, a.deadline::text, a.status, a.stable_id, a.site_id, "
        "t.id, t.source, t.source_s3_key, t.title, t.user_id, t.report_date::text "
        "FROM action_items a JOIN topics t ON t.id = a.topic_id WHERE a.id=%s",
        (res["action_item_id"],)).fetchone()
    import nz_time
    assert item[1:5] == ("Book the crane", "high", "2026-11-09", "open")
    assert item[6] == site["id"]
    assert item[8:] == ("agent", f"agent://action_pack/{pid}", "Crane week", admin["id"],
                        nz_time.nz_today().isoformat())
    assert res["task_no"] == db.execute(
        "SELECT task_no FROM action_task_numbers WHERE stable_id=%s", (item[5],)).fetchone()[0]
    [edit] = _edits(db, item[0])
    assert edit[0] == "created" and edit[3] == admin["id"]
    assert json.loads(edit[2]) == {"pack_id": pid, "action_id": a1}
    assert res["content_edit_ids"] == [str(db.execute(
        "SELECT id FROM content_edits WHERE row_id=%s", (item[0],)).fetchone()[0])]
    state, by, _, created = _action(db, a1)
    assert (state, by, created) == ("applied", admin["id"], item[5])
    # The second create_item from the same pack reuses the topic.
    status, body2 = _apply(db, admin, pid, a2)
    assert status == 200
    t2 = db.execute("SELECT topic_id FROM action_items WHERE id=%s",
                    (body2["applied_result"]["action_item_id"],)).fetchone()[0]
    assert t2 == item[7]
    assert body2["applied_result"]["task_no"] == res["task_no"] + 1
    assert db.execute("SELECT count(*) FROM topics WHERE source_s3_key=%s",
                      (f"agent://action_pack/{pid}",)).fetchone()[0] == 1
    # Double apply: the stored result, nothing new.
    assert _apply(db, admin, pid, a1) == (200, body)
    assert db.execute("SELECT count(*) FROM action_items WHERE topic_id=%s",
                      (item[7],)).fetchone()[0] == 2


def test_create_item_acl_and_validation(db, env):
    co, site, creator, admin = _world(db)
    other_site = sites.create_site(db, co["id"], f"Site2-apa-{_tag()}")
    pid, [a_ok, a_bad_site, a_bad_resp] = _ready_pack(db, site, creator, [
        _create(), _create(), _create(responsible="Not A Member")])
    db.execute("UPDATE action_pack_actions SET proposed = proposed || "
               "jsonb_build_object('site_id', %s::text) WHERE id=%s",
               (str(other_site["id"]), a_bad_site))
    assert _apply(db, creator, pid, a_ok)[0] == 403              # a worker may not create
    assert _apply(db, admin, pid, a_bad_site)[0] == 409
    assert _apply(db, admin, pid, a_bad_resp) == (
        400, {"error": "assignee must be a member of this site"})
    assert [_action(db, a)[0] for a in (a_ok, a_bad_site, a_bad_resp)] == ["proposed"] * 3
    assert db.execute("SELECT count(*) FROM topics WHERE source_s3_key=%s",
                      (f"agent://action_pack/{pid}",)).fetchone()[0] == 0


def test_agent_topic_untouched_by_re_extraction_of_another_source(db, env):
    _, site, creator, admin = _world(db)
    t, _, _ = _topic(db, site)
    pid, [aid] = _ready_pack(db, site, creator, [_create()])
    assert _apply(db, admin, pid, aid)[0] == 200
    key = f"agent://action_pack/{pid}"
    folder_prefix = t["source_s3_key"].rsplit("/", 1)[0] + "/"
    topics.supersede_topics_for_source(db, t["source_s3_key"], "test:4")
    topics.supersede_topics_for_source_prefix(db, folder_prefix, "test:5")
    topics.supersede_topics_for_source_prefix(db, "extractions/", "test:6")
    topics.supersede_topics_for_source_prefix(db, "reports/", "test:7")
    assert db.execute("SELECT superseded_at FROM topics WHERE source_s3_key=%s",
                      (key,)).fetchone() == (None,)


# ---------------------------------------------------------------- concurrency (committed rows)

def _reextract(conn, key, site_id, old_item):
    """lambda_item_writer's re-extraction of one key, under the writer's own lock."""
    conn.execute("SELECT pg_advisory_xact_lock(hashtext(%s))", (key,))
    retired = topics.supersede_topics_for_source(conn, key, "test:reextract")
    new_t = topics.upsert_topic(conn, site_id, DAY, "Fire doors", source_s3_key=key,
                                action_items=[{"text": "Fix the fire door (reworded)"}])
    old = [r for r in action_items.list_for_carry_forward(conn, [r["id"] for r in retired],
                                                          site_id) if r["id"] == old_item][0]
    new_id = conn.execute("SELECT id FROM action_items WHERE topic_id=%s",
                          (new_t["id"],)).fetchone()[0]
    action_items.carry_identity(conn, new_id, old)
    return new_id


def _cleanup(seed, co, site):
    seed.execute("DELETE FROM action_packs WHERE site_id=%s", (site["id"],))
    seed.execute("DELETE FROM action_task_numbers WHERE site_id=%s", (site["id"],))
    seed.execute("DELETE FROM topics WHERE site_id=%s", (site["id"],))
    seed.execute("DELETE FROM memberships WHERE site_id=%s", (site["id"],))
    seed.execute("DELETE FROM content_edits WHERE company_id=%s", (co["id"],))
    seed.execute("DELETE FROM users WHERE company_id=%s", (co["id"],))
    seed.execute("DELETE FROM sites WHERE id=%s", (site["id"],))
    seed.execute("DELETE FROM companies WHERE id=%s", (co["id"],))


def test_apply_waits_for_a_re_extraction_and_resolves_to_the_new_live_row(
        migrated_db_url, monkeypatch):
    """Concurrent apply vs re-extraction serialise on the source lock, and the row that was
    superseded while apply waited resolves to its successor (S12)."""
    monkeypatch.setenv("ACTION_PACKS", "on")
    monkeypatch.delenv("COMPANY_STATUS_ENFORCE", raising=False)
    monkeypatch.setattr(org, "GRADED_ROLES", False)
    seed = get_connection(migrated_db_url, autocommit=True)
    co = site = None
    try:
        co, site, creator, admin = _world(seed)
        t, old_id, stable = _topic(seed, site)
        pid, [aid] = _ready_pack(seed, site, creator, [_close(stable)])
        writer = get_connection(migrated_db_url)
        applier = get_connection(migrated_db_url)
        try:
            new_id = _reextract(writer, t["source_s3_key"], site["id"], old_id)  # holds lock
            out = {}

            def apply():
                out["res"] = _apply(applier, admin, pid, aid)
                applier.commit()

            th = threading.Thread(target=apply)
            th.start()
            time.sleep(0.7)
            assert th.is_alive(), "apply must wait on the source lock"
            writer.commit()
            th.join(15)
            assert not th.is_alive()
        finally:
            writer.close()
            applier.close()
        status, body = out["res"]
        assert status == 200, body
        assert body["applied_result"]["action_item_id"] == str(new_id)
        assert _status(seed, new_id) == "done" and _status(seed, old_id) == "open"
    finally:
        if site is not None:
            _cleanup(seed, co, site)
        seed.close()


def test_re_extraction_waits_for_an_apply_in_flight(migrated_db_url, monkeypatch):
    """The other order: apply holds the source lock; the writer waits, then carries the
    applied close onto its new row (human_touched)."""
    monkeypatch.setenv("ACTION_PACKS", "on")
    monkeypatch.delenv("COMPANY_STATUS_ENFORCE", raising=False)
    monkeypatch.setattr(org, "GRADED_ROLES", False)
    seed = get_connection(migrated_db_url, autocommit=True)
    co = site = None
    try:
        co, site, creator, admin = _world(seed)
        t, old_id, stable = _topic(seed, site)
        pid, [aid] = _ready_pack(seed, site, creator, [_close(stable)])
        writer = get_connection(migrated_db_url)
        applier = get_connection(migrated_db_url)
        try:
            assert _apply(applier, admin, pid, aid)[0] == 200        # lock held, uncommitted
            out = {}

            def reextract():
                out["new_id"] = _reextract(writer, t["source_s3_key"], site["id"], old_id)
                writer.commit()

            th = threading.Thread(target=reextract)
            th.start()
            time.sleep(0.7)
            assert th.is_alive(), "the writer must wait on the source lock"
            applier.commit()
            th.join(15)
            assert not th.is_alive()
        finally:
            writer.close()
            applier.close()
        assert _status(seed, out["new_id"]) == "done"           # the close survived
    finally:
        if site is not None:
            _cleanup(seed, co, site)
        seed.close()
