"""rag-search `mode: "items"` end to end through the handler, against a real PostgreSQL
(action packs spec §13 "items mode", §14 S4; Task 8).

Real caller resolution, real visible_scope (graded tiers, external memberships, the company
offboarding choke point), real SQL. The ACL is the Ask tools' one (`_ask_tools_where`) with
agent-created topics (`source='agent'`) admitted, live rows only.

Agent topics are inserted directly, the way apply does (`upsert_topic` cannot set `source`).
Skipped without TEST_DATABASE_URL. A SKIP IS NOT A PASS -- read the CI run.
"""
import uuid

import pytest

import lambda_rag_search
from repositories import action_items, companies, memberships, sites, topics, users

pytestmark = pytest.mark.integration

D = "2026-10-30"


def _tag():
    return uuid.uuid4().hex[:8]


def _user(db, co, name, role):
    t = _tag()
    u = users.upsert_user(db, f"sub-itm-{name}-{t}", f"itm-{name}-{t}@x.nz",
                          company_id=co["id"], first_name=name)
    db.execute("UPDATE users SET folder_name=%s, global_role=%s WHERE id=%s",
               (f"{name}_{t}", role, u["id"]))
    return users.get_user_by_sub(db, u["cognito_sub"])


def _item(db, site, user, text, **item):
    """One extraction topic by `user` on `site` carrying one action item; returns the item
    row (id, stable_id)."""
    folder = user["folder_name"] if user else "anon"
    t = topics.upsert_topic(db, site["id"], D, f"T {text}",
                            user_id=user["id"] if user else None,
                            source_s3_key=f"extractions/{folder}/{D}/{_tag()}.json",
                            action_items=[{"text": text, **item}])
    return db.execute("SELECT id, stable_id, topic_id FROM action_items WHERE topic_id=%s",
                      (t["id"],)).fetchone()


def _agent_item(db, site, user, text, status="open"):
    tid = db.execute(
        "INSERT INTO topics (site_id, user_id, source_s3_key, report_date, title, source) "
        "VALUES (%s, %s, %s, %s, %s, 'agent') RETURNING id",
        (site["id"], user["id"], f"agent://action_pack/{uuid.uuid4()}", D,
         "Pack topic")).fetchone()[0]
    return db.execute("INSERT INTO action_items (topic_id, site_id, text, status) "
                      "VALUES (%s, %s, %s, %s) RETURNING id, stable_id, topic_id",
                      (tid, site["id"], text, status)).fetchone()


@pytest.fixture
def w(db, monkeypatch):
    monkeypatch.setattr(lambda_rag_search, "get_cached_connection", lambda: db)
    monkeypatch.delenv("COMPANY_STATUS_ENFORCE", raising=False)
    a = companies.create_company(db, f"ItmA-{_tag()}")
    b = companies.create_company(db, f"ItmB-{_tag()}")
    s1 = sites.create_site(db, a["id"], f"Alpha-{_tag()}")
    s2 = sites.create_site(db, a["id"], f"Beta-{_tag()}")
    sb = sites.create_site(db, b["id"], f"Bravo-{_tag()}")
    u = {
        "gm": _user(db, a, "gm", "gm"),
        "pm": _user(db, a, "pm", "worker"),
        "sm": _user(db, a, "sm", "site_manager"),
        "w1": _user(db, a, "w1", "worker"),
        "w2": _user(db, a, "w2", "worker"),
        "ws2": _user(db, a, "ws2", "worker"),
        "bw": _user(db, b, "bw", "worker"),
        # Company B people on company A's project (project-owned tenancy).
        "ext_pm": _user(db, b, "extpm", "pm"),
        "ext_w": _user(db, b, "extw", "worker"),
    }
    memberships.ensure_membership(db, u["pm"]["id"], s1["id"], "pm")
    memberships.ensure_membership(db, u["sm"]["id"], s1["id"], "site_manager")
    memberships.ensure_membership(db, u["w1"]["id"], s1["id"], "worker")
    memberships.ensure_membership(db, u["w2"]["id"], s1["id"], "worker")
    memberships.ensure_membership(db, u["ws2"]["id"], s2["id"], "worker")
    memberships.ensure_membership(db, u["bw"]["id"], sb["id"], "worker")
    memberships.add_external_membership(db, u["ext_pm"]["id"], s1["id"], "pm")
    memberships.add_external_membership(db, u["ext_w"]["id"], s1["id"], "worker")

    _item(db, s1, u["w1"], "W1 task")
    _item(db, s1, u["w2"], "W2 task")
    _item(db, s1, u["pm"], "PM task")
    _item(db, s1, u["ext_w"], "Ext task")
    _item(db, s2, u["ws2"], "S2 task")
    _item(db, sb, u["bw"], "B secret")
    # The first applier of a pack is its synthetic topic's user (Task 7 ruling).
    _agent_item(db, s1, u["sm"], "Agent task")
    # A superseded pass: never shown, to anyone.
    old = _item(db, s1, u["w1"], "Old pass task")
    db.execute("UPDATE topics SET superseded_at = now() WHERE id=%s", (old[2],))
    # A deleted recording's topic: never shown.
    gone = _item(db, s1, u["w1"], "Deleted task")
    db.execute("INSERT INTO redactions (company_id, target_type, target_id, reason, scope) "
               "VALUES (%s, 'topic', %s, 'test', 'deleted')", (a["id"], gone[2]))
    # Finished items: outside the default status set.
    _item(db, s1, u["w1"], "Done task", status="done")
    return {"db": db, "a": a, "b": b, "s1": s1, "s2": s2, "sb": sb, **u}


def _items(user, **args):
    return lambda_rag_search._search({"mode": "items", "sub": user["cognito_sub"],
                                      "args": args}, None)


def _texts(res):
    return sorted(r["text"] for r in res["rows"])


S1_ALL = ["Agent task", "Ext task", "PM task", "W1 task", "W2 task"]


@pytest.mark.parametrize("who, expected", [
    ("gm", sorted(S1_ALL + ["S2 task"])),
    ("pm", S1_ALL),
    # SELF+WORKERS: own (the agent topic's applier) + worker members of S1, the external
    # worker included; never the pm's.
    ("sm", ["Agent task", "Ext task", "W1 task", "W2 task"]),
    ("w1", ["W1 task"]),
    ("ws2", ["S2 task"]),
    ("bw", ["B secret"]),
    # External pm: the whole of S1 and nothing else of company A.
    ("ext_pm", S1_ALL),
    # External worker: own rows on S1 only.
    ("ext_w", ["Ext task"]),
])
def test_acl_matrix(w, who, expected):
    res = _items(w[who])
    assert _texts(res) == expected
    assert "error" not in res["meta"]


def test_unknown_caller_is_not_provisioned(w):
    res = lambda_rag_search._search({"mode": "items", "sub": "sub-nobody", "args": {}}, None)
    assert res["rows"] == [] and res["meta"]["error"] == "not_provisioned"


def test_inactive_company_site_drops_out_when_enforced(w, monkeypatch):
    w["db"].execute("UPDATE companies SET status='suspended' WHERE id=%s", (w["a"]["id"],))
    assert _texts(_items(w["ext_pm"])) == S1_ALL            # shadow: logged, allowed
    monkeypatch.setenv("COMPANY_STATUS_ENFORCE", "on")
    res = _items(w["ext_pm"])
    assert res["rows"] == [] and res["meta"]["empty_reason"] == "nothing_visible"


def test_item_fields_are_the_apply_snapshot_source(w):
    db = w["db"]
    row = _item(db, w["s1"], w["w1"], "Snapshot me", responsible="Ann", priority="high",
                deadline="2026-11-05")
    db.execute("UPDATE action_items SET updated_at='2026-10-31T01:02:03+00:00' WHERE id=%s",
               (row[0],))
    no = action_items.assign_task_no(db, row[1], w["s1"]["id"])
    [it] = [r for r in _items(w["w1"])["rows"] if r["text"] == "Snapshot me"]
    assert it["stable_id"] == str(row[1]) and it["task_no"] == no
    assert (it["status"], it["deadline"], it["responsible"], it["priority"]) == (
        "open", "2026-11-05", "Ann", "high")
    assert it["updated_at"] == "2026-10-31T01:02:03+00:00"     # isoformat, as apply compares
    assert it["site_id"] == str(w["s1"]["id"]) and it["site"] == w["s1"]["name"]
    assert it["topic_id"] == str(row[2]) and it["report_date"] == D
    assert it["source"] == "ai" and it["created_at"]


def test_agent_item_carries_source_agent(w):
    [it] = [r for r in _items(w["pm"])["rows"] if r["text"] == "Agent task"]
    assert it["source"] == "agent"


def test_site_filter_narrows_and_out_of_scope_site_is_no_match(w):
    res = _items(w["gm"], site_id=str(w["s2"]["id"]))
    assert _texts(res) == ["S2 task"]
    for sid in (str(w["sb"]["id"]), str(uuid.uuid4())):
        res = _items(w["gm"], site_id=sid)
        assert res["rows"] == [] and res["meta"]["empty_reason"] == "no_match"
    # Upper-case UUID: same site.
    assert _texts(_items(w["gm"], site_id=str(w["s2"]["id"]).upper())) == ["S2 task"]


def test_status_filter(w):
    assert _texts(_items(w["w1"], status=["done"])) == ["Done task"]
    assert _texts(_items(w["w1"], status=["open", "done"])) == ["Done task", "W1 task"]


def test_text_query_is_literal_and_case_insensitive(w):
    db = w["db"]
    _item(db, w["s1"], w["w1"], "Check 100% of bolts")
    assert _texts(_items(w["w1"], query="w1 TASK")) == ["W1 task"]
    # % and _ are literal characters, never wildcards.
    assert _texts(_items(w["w1"], query="100%")) == ["Check 100% of bolts"]
    assert _items(w["w1"], query="W_ task")["rows"] == []
    res = _items(w["w1"], query="nothing like this")
    assert res["rows"] == [] and res["meta"]["empty_reason"] == "no_match"


def test_task_no_filter(w):
    db = w["db"]
    sids = {r["text"]: r["stable_id"] for r in _items(w["gm"])["rows"]}
    n_w1 = action_items.assign_task_no(db, sids["W1 task"], w["s1"]["id"])
    n_w2 = action_items.assign_task_no(db, sids["W2 task"], w["s1"]["id"])
    n_s2 = action_items.assign_task_no(db, sids["S2 task"], w["s2"]["id"])
    assert _texts(_items(w["gm"], task_no=n_w2, site_id=str(w["s1"]["id"]))) == ["W2 task"]
    # Numbers are per site: without site_id every scoped site's #n comes back.
    assert n_w1 == n_s2 == 1
    assert _texts(_items(w["gm"], task_no=1)) == ["S2 task", "W1 task"]
    # Out of the caller's author scope: w1 cannot reach W2's number.
    assert _items(w["w1"], task_no=n_w2)["rows"] == []


def test_order_is_site_then_task_no_nulls_last_then_created(w):
    db = w["db"]
    sids = {r["text"]: r["stable_id"] for r in _items(w["gm"])["rows"]}
    action_items.assign_task_no(db, sids["W2 task"], w["s1"]["id"])     # #1
    action_items.assign_task_no(db, sids["PM task"], w["s1"]["id"])     # #2
    # One transaction: every created_at is the same now(); spread the unnumbered ones.
    for i, text in enumerate(["W1 task", "Ext task", "Agent task"]):
        db.execute("UPDATE action_items SET created_at = now() + %s * interval '1 minute' "
                   "WHERE stable_id=%s", (i, sids[text]))
    rows = _items(w["pm"])["rows"]
    assert [r["text"] for r in rows] == ["W2 task", "PM task", "W1 task", "Ext task",
                                         "Agent task"]
    assert [r["task_no"] for r in rows] == [1, 2, None, None, None]


def test_limit_and_truncation(w):
    res = _items(w["pm"], limit=2)
    assert len(res["rows"]) == 2 and res["meta"]["truncated"] is True
    res = _items(w["pm"], limit=5)
    assert len(res["rows"]) == 5 and res["meta"]["truncated"] is False


def test_one_row_per_stable_id_the_newest_live_one(w):
    """Two live rows can share a stable_id for one commit's width (concurrent passes);
    the items tool shows the one apply will act on (get_live_by_stable_id: newest)."""
    db = w["db"]
    first = _item(db, w["s1"], w["w1"], "Dup task")
    second = _item(db, w["s1"], w["w1"], "Dup task (newer)", status="blocked")
    db.execute("UPDATE action_items SET stable_id=%s, created_at=created_at + interval '1s' "
               "WHERE id=%s", (first[1], second[0]))
    rows = [r for r in _items(w["w1"])["rows"] if r["stable_id"] == str(first[1])]
    assert [r["text"] for r in rows] == ["Dup task (newer)"]
    live = action_items.get_live_by_stable_id(db, first[1])
    assert live["text"] == "Dup task (newer)"
    # The status filter applies to the newest row, never resurrects the older one.
    assert [r for r in _items(w["w1"], status=["open"])["rows"]
            if r["stable_id"] == str(first[1])] == []


def test_missing_task_number_table_is_tolerated(w):
    db = w["db"]
    db.execute("DROP TABLE action_task_numbers")           # rolled back with the fixture
    res = _items(w["pm"])
    assert _texts(res) == S1_ALL and all(r["task_no"] is None for r in res["rows"])
    # No ledger: no number can match.
    res = _items(w["pm"], task_no=1)
    assert res["rows"] == [] and res["meta"]["empty_reason"] == "no_match"


def test_bad_args_are_refused_before_any_sql(w):
    res = _items(w["pm"], site_id="not-a-uuid")
    assert res["rows"] == [] and res["meta"] == {"error": "bad_args", "field": "site_id"}


def test_existing_ask_tools_scope_still_excludes_agent_topics(w):
    """Ask's own day overview is unchanged: an agent topic is not a recording."""
    rows = topics.day_overview_rows(w["db"], [str(w["s1"]["id"])], D, D)
    assert "Pack topic" not in {r["title"] for r in rows}
