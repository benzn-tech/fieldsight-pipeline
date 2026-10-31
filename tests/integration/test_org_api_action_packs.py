"""Integration: org-api action pack routes and internal tasks against real Postgres (Task 6).

The real dispatch, the real repositories and every query org-api adds around them: site
resolution on create, the claim task's creator lookup (action_packs.get_for_worker), the
S13 source re-check (get_topic_visible / live items / live documents), S15 `applicable`
(_action_item_edit_acl, may_create_item_on_site), task numbers on targets, the sweep on
list, and the export. S3 is a fake. The create and cancel routes COMMIT before their S3
write (unit-tested); here `db.commit` is replaced by a no-op so the `db` fixture's rollback
still isolates every test.

Skipped without TEST_DATABASE_URL -- a skip is NOT a pass.
"""
import json
import uuid

import pytest

from repositories import (action_items, action_packs as ap, companies, memberships,
                          project_docs, redactions, sites, topics, users)

pytestmark = pytest.mark.integration

org = pytest.importorskip("lambda_org_api")

DAY = "2026-10-30"


class FakeS3:
    def __init__(self):
        self.puts = []
        self.objs = {}

    def put_object(self, Bucket, Key, Body, ContentType=None):
        self.puts.append((Key, Body))

    def get_object(self, Bucket, Key):
        import io
        from botocore.exceptions import ClientError
        if (Bucket, Key) not in self.objs:
            raise ClientError({"Error": {"Code": "NoSuchKey", "Message": "x"}}, "GetObject")
        return {"Body": io.BytesIO(self.objs[(Bucket, Key)])}


@pytest.fixture
def env(db, monkeypatch):
    monkeypatch.setenv("ACTION_PACKS", "on")
    monkeypatch.delenv("COMPANY_STATUS_ENFORCE", raising=False)
    monkeypatch.setattr(org, "GRADED_ROLES", False)
    monkeypatch.setattr(db, "commit", lambda: None)
    fake = FakeS3()
    monkeypatch.setattr(org, "s3", lambda: fake)
    monkeypatch.setattr(org, "S3_BUCKET", "data-bucket")
    monkeypatch.setattr(org, "LAKE_BUCKET", "lake-bucket")
    return fake


def _tag():
    return uuid.uuid4().hex[:8]


def _user(db, co, role="worker", first="W"):
    t = _tag()
    u = users.upsert_user(db, f"sub-apo-{t}", f"apo-{t}@example.com",
                          company_id=co["id"], global_role=role)
    db.execute("UPDATE users SET first_name=%s, last_name=%s WHERE id=%s",
               (first, t, u["id"]))
    return users.get_user_by_sub(db, u["cognito_sub"])


def _world(db):
    co = companies.create_company(db, f"Co-apo-{_tag()}")
    site = sites.create_site(db, co["id"], f"Site-apo-{_tag()}")
    other_site = sites.create_site(db, co["id"], f"Site2-apo-{_tag()}")
    creator = _user(db, co, "worker", "Creator")
    memberships.ensure_membership(db, creator["id"], site["id"], "worker")
    return co, site, other_site, creator


def _call(db, u, method, route, body=None, query=None):
    ev = {"httpMethod": method, "path": "/api/org" + route, "queryStringParameters": query,
          "body": None if body is None else json.dumps(body), "headers": {},
          "requestContext": {"authorizer": {"claims": {"sub": u["cognito_sub"]}}}}
    res = org.dispatch(db, ev, method, route)
    return res["statusCode"], json.loads(res["body"])


def _topic(db, site, text="Fix the fire door", key=None, user_id=None):
    key = key or f"extractions/F_{_tag()}/{DAY}/sid{_tag()}.json"
    t = topics.upsert_topic(db, site["id"], DAY, "Fire doors", source_s3_key=key,
                            action_items=[{"text": text}], user_id=user_id)
    row = db.execute("SELECT id, stable_id FROM action_items WHERE topic_id=%s",
                     (t["id"],)).fetchone()
    return t, row[0], row[1]


def _ready_pack(db, site, creator, sources, actions, conclusion=None):
    pack = ap.create_queued(db, site_id=site["id"], created_by=creator["id"],
                            instruction="tidy the fire door items")
    assert ap.claim(db, pack["id"])
    assert ap.store_result(db, pack["id"], {
        "title": "Fire doors", "reading": "Close the fire door items",
        "conclusion": conclusion or [], "as_of": "2026-11-01T00:00:00+00:00",
        "sources": sources, "actions": actions}) == "ok"
    return str(pack["id"])


# ---------------------------------------------------------------- create / site resolution

def test_create_defaults_to_the_only_site_and_writes_the_request(db, env):
    co, site, _, _ = _world(db)
    solo = _user(db, co, "worker")
    memberships.ensure_membership(db, solo["id"], site["id"], "worker")
    status, body = _call(db, solo, "POST", "/action-packs", {"instruction": " close #69 "})
    assert status == 202 and body["state"] == "queued"
    row = db.execute("SELECT site_id, company_id, created_by, instruction, state "
                     "FROM action_packs WHERE id=%s", (body["id"],)).fetchone()
    assert row == (site["id"], co["id"], solo["id"], "close #69", "queued")
    [(key, raw)] = env.puts
    assert key == f"action_pack_requests/{body['id']}.json"
    assert json.loads(raw) == {"pack_id": body["id"], "caller_sub": solo["cognito_sub"],
                               "site_id": str(site["id"])}


def test_create_site_rules_for_a_multi_site_admin(db, env):
    co, site, other_site, _ = _world(db)
    admin = _user(db, co, "admin")
    assert _call(db, admin, "POST", "/action-packs", {"instruction": "x"})[0] == 400
    status, body = _call(db, admin, "POST", "/action-packs",
                         {"instruction": "x", "site_id": str(other_site["id"])})
    assert status == 202
    assert db.execute("SELECT site_id FROM action_packs WHERE id=%s",
                      (body["id"],)).fetchone()[0] == other_site["id"]


def test_create_site_out_of_scope_or_unknown_is_404(db, env):
    co, site, other_site, creator = _world(db)
    foreign_co = companies.create_company(db, f"Co-apo-f-{_tag()}")
    foreign = sites.create_site(db, foreign_co["id"], f"F-{_tag()}")
    for sid in (str(other_site["id"]), str(foreign["id"]), str(uuid.uuid4())):
        assert _call(db, creator, "POST", "/action-packs",
                     {"instruction": "x", "site_id": sid}) == (404, {"error": "site not found"})
    assert db.execute("SELECT count(*) FROM action_packs WHERE created_by=%s",
                      (creator["id"],)).fetchone()[0] == 0
    assert env.puts == []


def test_create_second_active_pack_is_409(db, env):
    _, site, _, creator = _world(db)
    assert _call(db, creator, "POST", "/action-packs", {"instruction": "a"})[0] == 202
    assert _call(db, creator, "POST", "/action-packs", {"instruction": "b"})[0] == 409
    assert len(env.puts) == 1


def test_create_inactive_company_refused_when_enforced(db, env, monkeypatch):
    co, site, _, creator = _world(db)
    ext_co = companies.create_company(db, f"Co-apo-x-{_tag()}")
    ext = _user(db, ext_co, "worker")
    memberships.add_external_membership(db, ext["id"], site["id"], "worker")
    db.execute("UPDATE companies SET status='suspended' WHERE id=%s", (co["id"],))
    # Shadow (enforcement off): logged and allowed.
    assert _call(db, ext, "POST", "/action-packs",
                 {"instruction": "x", "site_id": str(site["id"])})[0] == 202
    db.execute("UPDATE action_packs SET state='cancelled' WHERE created_by=%s", (ext["id"],))
    monkeypatch.setenv("COMPANY_STATUS_ENFORCE", "on")
    # Enforced: the site drops out of the caller's reach first, so it reads as unknown.
    assert _call(db, ext, "POST", "/action-packs",
                 {"instruction": "x", "site_id": str(site["id"])}) == (
        404, {"error": "site not found"})
    assert len(env.puts) == 1


# ---------------------------------------------------------------- read ACL matrix

def _roles(db, co, site):
    """{name: user} for the reader matrix on `site` of company `co`."""
    out = {}
    out["admin"] = _user(db, co, "admin")
    out["gm"] = _user(db, co, "gm")
    for role in ("pm", "site_manager", "worker"):
        u = _user(db, co, role if role != "worker" else "worker")
        memberships.ensure_membership(db, u["id"], site["id"], role)
        out[f"site_{role}"] = u
    out["pm_elsewhere"] = _user(db, co, "pm")
    other = companies.create_company(db, f"Co-apo-o-{_tag()}")
    out["other_admin"] = _user(db, other, "admin")
    plat = companies.create_company(db, f"Co-apo-p-{_tag()}")
    out["platform_admin"] = _user(db, plat, "platform_admin")
    return out


READ = {"creator": True, "admin": True, "gm": True, "site_pm": True, "site_site_manager": True,
        "site_worker": False, "pm_elsewhere": False, "other_admin": False,
        "platform_admin": False}
CANCEL = {"creator": 200, "admin": 200, "gm": 200, "site_pm": 403, "site_site_manager": 403,
          "site_worker": 404, "pm_elsewhere": 404, "other_admin": 404, "platform_admin": 404}


def test_read_cancel_archive_dismiss_acl_matrix(db, env):
    co, site, _, creator = _world(db)
    people = dict(_roles(db, co, site), creator=creator)
    w1 = {"ref": "W1", "kind": "web_source", "provenance": "web", "url": "https://e.x/a",
          "excerpt": "web text"}
    for name, readable in READ.items():
        pid = _ready_pack(db, site, creator, [w1], [
            {"seq": 1, "type": "note", "refs": ["W1"], "verifier": "supported",
             "rationale": "r"}])
        u = people[name]
        got = _call(db, u, "GET", f"/action-packs/{pid}")[0]
        assert got == (200 if readable else 404), name
        listed = {p["id"] for p in _call(db, u, "GET", "/action-packs")[1]["packs"]}
        assert (pid in listed) is readable, name
        assert _call(db, u, "GET", f"/action-packs/{pid}/export",
                     query={"format": "md"})[0] == (200 if readable else 404), name
        aid = str(db.execute("SELECT id FROM action_pack_actions WHERE pack_id=%s",
                             (pid,)).fetchone()[0])
        assert _call(db, u, "POST", f"/action-packs/{pid}/actions/{aid}/dismiss",
                     {})[0] == (200 if readable else 404), name
        assert _call(db, u, "POST", f"/action-packs/{pid}/archive",
                     {"archived": True})[0] == (200 if readable else 404), name
        # Cancel needs a live pack.
        db.execute("UPDATE action_packs SET state='cancelled' WHERE state IN "
                   "('queued','running') AND created_by=%s", (creator["id"],))
        live = ap.create_queued(db, site_id=site["id"], created_by=creator["id"],
                                instruction="x")
        assert _call(db, u, "POST", f"/action-packs/{live['id']}/cancel")[0] == CANCEL[name], name
        db.execute("UPDATE action_packs SET state='cancelled' WHERE id=%s", (live["id"],))


def test_dismiss_outcomes_against_the_real_table(db, env):
    _, site, _, creator = _world(db)
    w1 = {"ref": "W1", "kind": "web_source", "provenance": "web", "url": "https://e.x"}
    pid = _ready_pack(db, site, creator, [w1], [
        {"seq": 1, "type": "note", "refs": ["W1"], "verifier": "supported"}])
    aid = str(db.execute("SELECT id FROM action_pack_actions WHERE pack_id=%s",
                         (pid,)).fetchone()[0])
    assert _call(db, creator, "POST", f"/action-packs/{pid}/actions/{aid}/dismiss", {}) == (
        200, {"id": aid, "state": "dismissed"})
    row = db.execute("SELECT state, applied_by FROM action_pack_actions WHERE id=%s",
                     (aid,)).fetchone()
    assert row == ("dismissed", creator["id"])
    assert _call(db, creator, "POST", f"/action-packs/{pid}/actions/{aid}/dismiss", {}) == (
        409, {"error": "action is dismissed", "state": "dismissed"})
    assert _call(db, creator, "POST",
                 f"/action-packs/{pid}/actions/{uuid.uuid4()}/dismiss", {})[0] == 404
    _call(db, creator, "POST", f"/action-packs/{pid}/archive", {"archived": True})
    assert _call(db, creator, "POST", f"/action-packs/{pid}/actions/{aid}/dismiss",
                 {})[0] == 409


def test_cancel_writes_state_and_marker(db, env):
    _, site, _, creator = _world(db)
    pack = ap.create_queued(db, site_id=site["id"], created_by=creator["id"], instruction="x")
    assert _call(db, creator, "POST", f"/action-packs/{pack['id']}/cancel") == (
        200, {"id": str(pack["id"]), "state": "cancelled"})
    assert db.execute("SELECT state FROM action_packs WHERE id=%s",
                      (pack["id"],)).fetchone()[0] == "cancelled"
    assert env.puts == [(f"action_pack_requests/{pack['id']}.cancel", b"{}")]
    status, body = _call(db, creator, "POST", f"/action-packs/{pack['id']}/cancel")
    assert status == 409 and body["state"] == "cancelled"


# ---------------------------------------------------------------- S13 + S15 on GET one

def test_s13_removed_sources_hide_their_claims_and_actions(db, env):
    co, site, other_site, creator = _world(db)
    pm = _user(db, co, "pm")
    memberships.ensure_membership(db, pm["id"], site["id"], "pm")
    live_t, live_item, live_stable = _topic(db, site, "Fix the fire door")
    gone_t, _, _ = _topic(db, site, "Order bricks")
    redactions.create_redaction(db, co["id"], gone_t["id"], "test", None, None,
                                scope="deleted")
    elsewhere_t, _, _ = _topic(db, other_site, "Elsewhere")           # pm cannot see
    doc_live, doc_gone = uuid.uuid4(), uuid.uuid4()
    for d in (doc_live, doc_gone):
        project_docs.insert_document(db, doc_id=d, site_id=site["id"], company_id=co["id"],
                                     title="Spec", filename="s.pdf", source_key="k/" + str(d),
                                     content_type="application/pdf", uploaded_by=pm["id"])
    project_docs.soft_delete(db, doc_gone)
    sources = [
        {"ref": "R1", "kind": "topic", "provenance": "records", "topic_id": str(live_t["id"]),
         "title": "Fire doors", "excerpt": "door is fixed"},
        {"ref": "R2", "kind": "topic", "provenance": "records", "topic_id": str(gone_t["id"]),
         "title": "SECRET-GONE-TITLE", "excerpt": "SECRET-GONE"},
        {"ref": "R3", "kind": "chunk", "provenance": "records", "chunk_id": str(uuid.uuid4()),
         "excerpt": "SECRET-NO-TOPIC"},
        {"ref": "R4", "kind": "topic", "provenance": "records",
         "topic_id": str(elsewhere_t["id"]), "excerpt": "SECRET-ELSEWHERE"},
        {"ref": "R5", "kind": "item", "provenance": "records", "stable_id": str(live_stable),
         "excerpt": "item text"},
        {"ref": "D1", "kind": "document", "provenance": "documents",
         "document_id": str(doc_live), "excerpt": "doc text"},
        {"ref": "D2", "kind": "document", "provenance": "documents",
         "document_id": str(doc_gone), "excerpt": "SECRET-DOC"},
        {"ref": "W1", "kind": "web_source", "provenance": "web", "url": "https://e.x",
         "excerpt": "web text"},
    ]
    actions = [
        {"seq": 1, "type": "note", "refs": ["R2", "R3"], "verifier": "supported",
         "rationale": "SECRET-ACTION"},                                 # all removed: hidden
        {"seq": 2, "type": "note", "refs": ["R2", "R1"], "verifier": "weak",
         "rationale": "mixed"},                                          # kept
        {"seq": 3, "type": "draft_email", "refs": ["D2"], "verifier": "supported",
         "rationale": "SECRET-DRAFT"},                                   # hidden
        {"seq": 4, "type": "note", "refs": ["D1", "W1", "R5"], "verifier": "supported"},
    ]
    conclusion = [{"text": "SECRET-CLAIM", "refs": ["R4"], "confidence": "supported"},
                  {"text": "Door fixed", "refs": ["R1", "R4"], "confidence": "supported"}]
    pid = _ready_pack(db, site, creator, sources, actions, conclusion)
    status, body = _call(db, pm, "GET", f"/action-packs/{pid}")
    assert status == 200
    by_ref = {s["ref"]: s for s in body["sources"]}
    assert {r for r, s in by_ref.items() if s["removed"]} == {"R2", "R3", "R4", "D2"}
    assert by_ref["R2"] == {"ref": "R2", "kind": "topic", "provenance": "records",
                            "removed": True}
    assert by_ref["R1"]["excerpt"] == "door is fixed" and by_ref["W1"]["excerpt"] == "web text"
    assert [a["seq"] for a in body["actions"]] == [2, 4]
    assert [c["text"] for c in body["conclusion"]] == ["Door fixed"]
    assert "SECRET" not in json.dumps(body)
    # The export honours the same removal.
    status, exp = _call(db, pm, "GET", f"/action-packs/{pid}/export", query={"format": "md"})
    assert status == 200 and "SECRET" not in exp["content"]
    assert "[R2] source removed" in exp["content"] and "door is fixed" in exp["content"]
    # The creator (a worker on the site, SELF reach) sees fewer: R4 is on another site.
    status, mine = _call(db, creator, "GET", f"/action-packs/{pid}")
    assert status == 200 and next(s for s in mine["sources"] if s["ref"] == "R4")["removed"]


def test_s15_applicable_and_target_task_numbers(db, env):
    co, site, _, creator = _world(db)
    pm = _user(db, co, "pm")
    memberships.ensure_membership(db, pm["id"], site["id"], "pm")
    admin = _user(db, co, "admin")
    t, item_id, stable = _topic(db, site, "Fix the fire door")
    _t2, _item2, stable2 = _topic(db, site, "Order bricks")
    task_nos = action_items.task_nos_for_item_ids(db, [item_id])
    w1 = {"ref": "W1", "kind": "web_source", "provenance": "web", "url": "https://e.x"}
    r1 = {"ref": "R1", "kind": "topic", "provenance": "records", "topic_id": str(t["id"])}
    snap = {"status": "open", "text": "Fix the fire door"}
    actions = [
        {"seq": 1, "type": "close_item", "target_stable_id": str(stable),
         "refs": ["R1", "W1"],
         "verifier": "supported", "proposed": {"status": "done"}, "target_snapshot": snap},
        {"seq": 2, "type": "update_item", "target_stable_id": str(stable2), "refs": ["W1"],
         "verifier": "weak", "proposed": {"priority": "high"}},
        {"seq": 3, "type": "close_item", "target_stable_id": str(uuid.uuid4()),
         "refs": ["W1"], "verifier": "supported", "proposed": {"status": "done"}},
        {"seq": 4, "type": "close_item", "target_stable_id": str(stable), "refs": ["W1"],
         "verifier": "unsupported", "proposed": {"status": "done"}},
        {"seq": 5, "type": "create_item", "refs": ["W1"], "verifier": "supported",
         "proposed": {"text": "Book inspection"}},
        {"seq": 6, "type": "draft_rfi", "refs": ["W1"], "verifier": "supported"},
    ]
    pid = _ready_pack(db, site, creator, [w1, r1], actions)

    def flags(u):
        status, body = _call(db, u, "GET", f"/action-packs/{pid}")
        assert status == 200
        return {a["seq"]: a for a in body["actions"]}

    for u in (pm, admin):
        got = flags(u)
        assert {s: a["applicable"] for s, a in got.items()} == {
            1: True, 2: True, 3: False, 4: False, 5: True, 6: False}
        assert got[1]["target_task_no"] == task_nos.get(str(item_id))
        assert got[1]["target_snapshot"] == snap
        assert got[3]["target_task_no"] is None and got[3]["target_snapshot"] is None
    # The creator is a plain worker and not the assignee: nothing is applicable.
    assert not any(a["applicable"] for a in flags(creator).values())
    # The creator becomes the assignee of the first item: close/update on it opens up,
    # create_item does not (no assignee tier for a new task).
    db.execute("UPDATE action_items SET responsible=%s WHERE id=%s",
               (f"{creator['first_name']} {creator['last_name']}", item_id))
    got = flags(creator)
    assert got[1]["applicable"] is True and got[5]["applicable"] is False
    # The creator's reach (SELF) does not include the unattributed topic: the run's copy of
    # the target, and its task number, are withheld even though the action itself is shown.
    assert got[1]["target_snapshot"] is None and got[1]["target_task_no"] is None
    # A dismissed action, and an archived pack, are not applicable.
    aid1 = got[1]["id"]
    _call(db, pm, "POST", f"/action-packs/{pid}/actions/{aid1}/dismiss", {})
    assert flags(pm)[1]["applicable"] is False
    _call(db, pm, "POST", f"/action-packs/{pid}/archive", {"archived": True})
    assert not any(a["applicable"] for a in flags(pm).values())


# ---------------------------------------------------------------- list + sweep

def test_list_sweeps_stale_runs_and_carries_names_and_counts(db, env):
    _, site, _, creator = _world(db)
    stale = ap.create_queued(db, site_id=site["id"], created_by=creator["id"], instruction="x")
    ap.claim(db, stale["id"])
    db.execute("UPDATE action_packs SET heartbeat_at = now() - interval '21 minutes' "
               "WHERE id=%s", (stale["id"],))
    status, body = _call(db, creator, "GET", "/action-packs")
    assert status == 200
    [row] = body["packs"]
    assert row["id"] == str(stale["id"]) and row["state"] == "failed"
    assert row["failure"] == "internal" and row["site_name"] == site["name"]
    assert row["action_counts"] == {"proposed": 0, "applied": 0, "dismissed": 0, "stale": 0,
                                    "failed": 0}
    assert _call(db, creator, "GET", "/action-packs", query={"state": "running"})[1] == {
        "packs": []}
    assert _call(db, creator, "GET", "/action-packs", query={"archived": "true"})[1] == {
        "packs": []}


def test_list_survives_a_sweep_error(db, env, monkeypatch):
    _, site, _, creator = _world(db)
    ap.create_queued(db, site_id=site["id"], created_by=creator["id"], instruction="x")

    def broken(conn, older_than):
        conn.execute("SELECT * FROM no_such_table_apo")
    monkeypatch.setattr(org.action_packs, "sweep_stale", broken)
    status, body = _call(db, creator, "GET", "/action-packs")
    assert status == 200 and len(body["packs"]) == 1


# ---------------------------------------------------------------- internal tasks

def test_claim_task_returns_the_run_identity_once(db, env):
    _, site, _, creator = _world(db)
    pack = ap.create_queued(db, site_id=site["id"], created_by=creator["id"],
                            instruction="close #69")
    pid = str(pack["id"])
    got = org._action_pack_claim(db, pid, {})
    assert got["claimed"] is True
    assert got["pack"] == {
        "id": pid, "site_id": str(site["id"]), "company_id": str(site["company_id"]),
        "instruction": "close #69", "created_by": str(creator["id"]),
        "created_by_sub": creator["cognito_sub"], "created_at": got["pack"]["created_at"]}
    assert org._action_pack_claim(db, pid, {}) == {"claimed": False}       # second delivery
    assert org._action_pack_claim(db, str(uuid.uuid4()), {}) == {"claimed": False}


def test_claim_task_fails_an_inactive_company_when_enforced(db, env, monkeypatch):
    co, site, _, creator = _world(db)
    pack = ap.create_queued(db, site_id=site["id"], created_by=creator["id"], instruction="x")
    db.execute("UPDATE companies SET status='exited' WHERE id=%s", (co["id"],))
    monkeypatch.setenv("COMPANY_STATUS_ENFORCE", "on")
    assert org._action_pack_claim(db, str(pack["id"]), {}) == {
        "claimed": False, "reason": "company_inactive"}
    assert db.execute("SELECT state, failure FROM action_packs WHERE id=%s",
                      (pack["id"],)).fetchone() == ("failed", "company_inactive")


def test_heartbeat_store_and_fail_tasks(db, env):
    _, site, _, creator = _world(db)
    pack = ap.create_queued(db, site_id=site["id"], created_by=creator["id"], instruction="x")
    pid = str(pack["id"])
    assert org._action_pack_heartbeat(db, pid, {}) == {"state": "queued"}
    ap.claim(db, pid)
    assert org._action_pack_heartbeat(db, pid, {"reading": "R", "title": "T"}) == {
        "state": "running"}
    assert db.execute("SELECT reading, title FROM action_packs WHERE id=%s",
                      (pid,)).fetchone() == ("R", "T")
    assert org._action_pack_heartbeat(db, str(uuid.uuid4()), {}) == {"state": None}
    # A constraint violation (a web kind with records provenance) fails the pack.
    bad = {"sources": [{"ref": "W1", "kind": "web_source", "provenance": "records"}]}
    assert org._action_pack_store(db, pid, {"payload": bad}) == {
        "error": "invalid", "detail": "constraint"}
    assert db.execute("SELECT state, failure FROM action_packs WHERE id=%s",
                      (pid,)).fetchone() == ("failed", "internal")
    assert org._action_pack_store(db, pid, {"payload": {"title": "t"}}) == {"error": "refused"}
    assert org._action_pack_fail(db, pid, {"reason": "model"}) == {"ok": False}
    # A fresh run stores cleanly.
    p2 = ap.create_queued(db, site_id=site["id"], created_by=creator["id"], instruction="y")
    ap.claim(db, p2["id"])
    good = {"title": "t", "sources": [{"ref": "W1", "kind": "web_source", "provenance": "web"}],
            "actions": [{"seq": 1, "type": "note", "refs": ["W1"], "verifier": "weak"}]}
    assert org._action_pack_store(db, str(p2["id"]), {"payload": good}) == {"ok": True}
    assert org._action_pack_store(db, str(p2["id"]), {"payload": {"zz": 1}}) == {
        "error": "invalid", "detail": "unknown_keys"}
    p3 = ap.create_queued(db, site_id=site["id"], created_by=creator["id"], instruction="z")
    assert org._action_pack_fail(db, str(p3["id"]), {"reason": "budget"}) == {"ok": True}


# ---------------------------------------------------------------- fix round 1

def test_heartbeat_plan_fields_do_not_erase_each_other(db, env):
    _, site, _, creator = _world(db)
    pack = ap.create_queued(db, site_id=site["id"], created_by=creator["id"], instruction="x")
    pid = str(pack["id"])
    ap.claim(db, pid)
    assert org._action_pack_heartbeat(db, pid, {"reading": "R first"}) == {"state": "running"}
    assert org._action_pack_heartbeat(db, pid, {"title": "T later"}) == {"state": "running"}
    assert org._action_pack_heartbeat(db, pid, {}) == {"state": "running"}
    assert db.execute("SELECT reading, title FROM action_packs WHERE id=%s",
                      (pid,)).fetchone() == ("R first", "T later")


def test_store_malformed_shape_never_reaches_the_database(db, env):
    _, site, _, creator = _world(db)
    pack = ap.create_queued(db, site_id=site["id"], created_by=creator["id"], instruction="x")
    pid = str(pack["id"])
    ap.claim(db, pid)
    for bad in ({"sources": "R1"}, {"actions": [{"seq": "1"}]},
                {"sources": [{"ref": "R1", "kind": "topic", "provenance": "records",
                              "topic_id": "nope"}]}):
        assert org._action_pack_store(db, pid, {"payload": bad})["error"] == "invalid"
    assert db.execute("SELECT state FROM action_packs WHERE id=%s",
                      (pid,)).fetchone()[0] == "running"
    assert db.execute("SELECT count(*) FROM action_pack_sources WHERE pack_id=%s",
                      (pid,)).fetchone()[0] == 0


def _stale(db, site, creator):
    pack = ap.create_queued(db, site_id=site["id"], created_by=creator["id"], instruction="x")
    ap.claim(db, pack["id"])
    db.execute("UPDATE action_packs SET heartbeat_at = now() - interval '21 minutes' "
               "WHERE id=%s", (pack["id"],))
    return str(pack["id"])


def test_get_one_fails_a_dead_run(db, env):
    _, site, _, creator = _world(db)
    pid = _stale(db, site, creator)
    status, body = _call(db, creator, "GET", f"/action-packs/{pid}")
    assert status == 200 and (body["state"], body["failure"]) == ("failed", "internal")


def test_a_dead_run_does_not_block_create(db, env):
    _, site, _, creator = _world(db)
    pid = _stale(db, site, creator)
    status, body = _call(db, creator, "POST", "/action-packs", {"instruction": "again"})
    assert status == 202 and body["id"] != pid
    assert db.execute("SELECT state FROM action_packs WHERE id=%s",
                      (pid,)).fetchone()[0] == "failed"


def test_snapshot_viewer_reads_the_lake_for_readers_only(db, env):
    co, site, _, creator = _world(db)
    outsider = _user(db, co, "worker")
    pack = ap.create_queued(db, site_id=site["id"], created_by=creator["id"], instruction="x")
    pid = str(pack["id"])
    key = f"action_packs/{co['id']}/{pid}/web/W1.txt"
    ap.claim(db, pid)
    assert ap.store_result(db, pid, {"sources": [
        {"ref": "W1", "kind": "web_source", "provenance": "web", "url": "https://e.x/a",
         "fetched_at": "2026-11-01T00:00:00+00:00", "snapshot_s3_key": key},
        {"ref": "R1", "kind": "topic", "provenance": "records",
         "topic_id": str(uuid.uuid4())}]}) == "ok"
    env.objs[("lake-bucket", key)] = "page text \u2014 stored".encode("utf-8")
    status, body = _call(db, creator, "GET", f"/action-packs/{pid}/sources/W1/snapshot")
    assert status == 200
    assert body == {"ref": "W1", "url": "https://e.x/a", "fetched_at": body["fetched_at"],
                    "text": "page text \u2014 stored", "truncated": False}
    assert body["fetched_at"].startswith("2026-11-01")
    assert _call(db, outsider, "GET", f"/action-packs/{pid}/sources/W1/snapshot")[0] == 404
    assert _call(db, creator, "GET", f"/action-packs/{pid}/sources/R1/snapshot")[0] == 404
    env.objs.clear()
    assert _call(db, creator, "GET", f"/action-packs/{pid}/sources/W1/snapshot") == (
        404, {"error": "snapshot not found"})


# ---------------------------------------------------------------- Task 9: web snapshots

def test_store_records_web_snapshots_in_the_lake_index(db, env):
    co, site, _, creator = _world(db)
    pack = ap.create_queued(db, site_id=site["id"], created_by=creator["id"], instruction="x")
    pid = str(pack["id"])
    ap.claim(db, pid)
    key1 = f"action_packs/{co['id']}/{pid}/web/W2.txt"
    key2 = f"action_packs/{co['id']}/{pid}/web/W3.txt"
    payload = {"title": "t", "sources": [
        {"ref": "W1", "kind": "web_answer", "provenance": "web", "excerpt": "answer"},
        {"ref": "W2", "kind": "web_source", "provenance": "web", "url": "https://a.example/p",
         "fetched_at": "2026-11-01T01:02:03+00:00", "snapshot_s3_key": key1},
        {"ref": "W3", "kind": "web_source", "provenance": "web", "url": "https://b.example/q",
         "fetched_at": "2026-11-01T01:02:04+00:00", "snapshot_s3_key": key2},
        {"ref": "W4", "kind": "web_source", "provenance": "web", "url": "https://c.example/r",
         "snapshot_s3_key": None}],
        "actions": [{"seq": 1, "type": "note", "refs": ["W1", "W2"], "verifier": "weak"}]}
    assert org._action_pack_store(db, pid, {"payload": payload}) == {"ok": True}
    rows = db.execute("SELECT s3_key, company_id, site_id, kind, source, deleted_at "
                      "FROM lake_objects WHERE s3_key LIKE %s ORDER BY s3_key",
                      (f"action_packs/%/{pid}/%",)).fetchall()
    assert rows == [(key1, co["id"], site["id"], "action_pack_web", "upload", None),
                    (key2, co["id"], site["id"], "action_pack_web", "upload", None)]
    assert db.execute("SELECT ref, snapshot_s3_key FROM action_pack_sources WHERE pack_id=%s "
                      "ORDER BY ref", (pid,)).fetchall() == [
        ("W1", None), ("W2", key1), ("W3", key2), ("W4", None)]


def test_store_rejects_a_foreign_snapshot_key_before_writing(db, env):
    co, site, _, creator = _world(db)
    other = companies.create_company(db, f"Co-apo-other-{_tag()}")
    pack = ap.create_queued(db, site_id=site["id"], created_by=creator["id"], instruction="x")
    pid = str(pack["id"])
    ap.claim(db, pid)
    for key in (f"action_packs/{other['id']}/{pid}/web/W1.txt",   # another company
                f"action_packs/{co['id']}/{pid}/web/W9.txt",      # another ref
                "reports/secret.json"):
        payload = {"sources": [{"ref": "W1", "kind": "web_source", "provenance": "web",
                                "url": "https://a.example/p", "snapshot_s3_key": key}]}
        assert org._action_pack_store(db, pid, {"payload": payload}) == {
            "error": "invalid", "detail": "snapshot_key"}
        assert db.execute("SELECT count(*) FROM lake_objects WHERE s3_key=%s",
                          (key,)).fetchone()[0] == 0
    assert db.execute("SELECT state FROM action_packs WHERE id=%s",
                      (pid,)).fetchone()[0] == "running"
    assert db.execute("SELECT count(*) FROM action_pack_sources WHERE pack_id=%s",
                      (pid,)).fetchone()[0] == 0
