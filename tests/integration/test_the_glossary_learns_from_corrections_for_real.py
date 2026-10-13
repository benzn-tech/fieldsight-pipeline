"""The glossary learns from a person's own correction -- run against Postgres.

Owner, 2026-10-02: "Tikaha" corrected to TEKAHA by hand should become a
glossary entry without anyone confirming a prompt; only the narrow
"misheard name" case is learned; an admin can see and undo every entry.

THE test is `correcting a misheard name by hand teaches the site's glossary`.
"""
import pytest

import lambda_org_api as org
from repositories import aliases, topics

pytestmark = pytest.mark.integration


def _seed(db):
    cid = db.execute("INSERT INTO companies (name) VALUES ('Gloss Co') RETURNING id").fetchone()[0]
    uid = db.execute(
        "INSERT INTO users (cognito_sub, company_id, email, global_role, first_name, last_name) "
        "VALUES ('sub-gl', %s, 'gl@x.com', 'admin', 'Ben', 'Lin') RETURNING id", (cid,)).fetchone()[0]
    sid = db.execute("INSERT INTO sites (company_id, name) VALUES (%s, 'Test Project') RETURNING id",
                     (cid,)).fetchone()[0]
    tid = topics.upsert_topic(db, sid, "2026-10-02", "Tikaha Room Inspections",
                              source_s3_key="extractions/F/2026-10-02/s1.json",
                              time_range="12:44 - 12:45")["id"]
    caller = {"id": uid, "company_id": cid, "global_role": "admin"}
    return caller, sid, tid


@pytest.fixture(autouse=True)
def _no_reindex(monkeypatch):
    monkeypatch.setattr(org, "_enqueue_content_reindex", lambda *a, **k: None)


def _edit(db, caller, tid, title):
    import json
    res = org.patch_content(db, caller, "topics", str(tid), {"title": title})
    return res["statusCode"], json.loads(res["body"])


def test_THE_correcting_a_misheard_name_by_hand_teaches_the_sites_glossary(db):
    caller, sid, tid = _seed(db)
    code, body = _edit(db, caller, tid, "TEKAHA Room Inspections")
    assert code == 200 and [(l["wrong_term"], l["right_term"]) for l in body["learned"]] == \
        [("Tikaha", "TEKAHA")]
    active = aliases.list_active(db, caller["company_id"], site_ids=[str(sid)])
    assert [(a["wrong_term"], a["right_term"], a["source"], str(a["site_id"])) for a in active] == \
        [("Tikaha", "TEKAHA", "learned", str(sid))]


def test_a_change_of_meaning_teaches_nothing(db):
    caller, sid, tid = _seed(db)
    code, body = _edit(db, caller, tid, "Level 2 slab pour sign-off")
    assert code == 200 and body["learned"] == []
    assert aliases.list_active(db, caller["company_id"]) == []


def test_the_same_correction_twice_is_one_entry_and_a_new_one_replaces_it(db):
    caller, sid, tid = _seed(db)
    _edit(db, caller, tid, "TEKAHA Room Inspections")
    db.execute("UPDATE topics SET title='Tikaha Room Inspections' WHERE id=%s", (tid,))
    code, body = _edit(db, caller, tid, "TEKAHA Room Inspections")
    assert body["learned"] == [], "already known"
    db.execute("UPDATE topics SET title='Tikaha Room' WHERE id=%s", (tid,))
    _edit(db, caller, tid, "Te Kaha Room")
    active = aliases.list_active(db, caller["company_id"])
    assert [(a["wrong_term"], a["right_term"]) for a in active] == [("Tikaha", "Te Kaha")]


def test_an_admin_sees_and_undoes_an_entry_and_another_company_cannot(db):
    import json
    caller, sid, tid = _seed(db)
    _edit(db, caller, tid, "TEKAHA Room Inspections")
    listing = json.loads(org.list_aliases_endpoint(db, caller)["body"])["aliases"]
    assert [(a["wrong_term"], a["site_name"], a["created_by_name"]) for a in listing] == \
        [("Tikaha", "Test Project", "Ben Lin")]
    other = dict(caller, company_id=db.execute(
        "INSERT INTO companies (name) VALUES ('Other') RETURNING id").fetchone()[0])
    assert org.retire_alias_endpoint(db, other, listing[0]["id"])["statusCode"] == 404
    assert org.retire_alias_endpoint(db, caller, listing[0]["id"])["statusCode"] == 200
    assert aliases.list_active(db, caller["company_id"]) == []
    worker = dict(caller, global_role="worker")
    assert org.list_aliases_endpoint(db, worker)["statusCode"] == 403


def test_putting_a_correction_back_undoes_it_and_learns_nothing(db):
    caller, sid, tid = _seed(db)
    _edit(db, caller, tid, "TEKAHA Room Inspections")
    code, body = _edit(db, caller, tid, "Tikaha Room Inspections")
    assert code == 200 and body["learned"] == []
    assert aliases.list_active(db, caller["company_id"]) == [], "no entry rewriting either way"
