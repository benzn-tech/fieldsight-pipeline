"""Integration: POST /status-pick-cards/{id} choose, when the CAS loses AFTER the PATCH ran
(spec 2026-11-12 §5.1 / §6). The fake conn in the unit tests never executes SQL, so only a
real Postgres can show the rollback: the handler sees the card as pending (a stale read,
as when another tab decides it between the read and the CAS), patches the item, the real
resolve_pick_card CAS returns None, and the raise must undo the PATCH and its content_edits
audit row. The `db` fixture is one outer transaction, so the handler's
`conn.transaction()` is a savepoint here -- the shape it runs in under `with conn:`.

Skipped without TEST_DATABASE_URL -- a skip is NOT a pass.
"""
import json
import uuid

import pytest

from repositories import companies, sites, topics, users
from repositories import status_pick_cards as repo

pytestmark = pytest.mark.integration

lambda_org_api = pytest.importorskip(
    "lambda_org_api", reason="requires psycopg (installed in CI)")

DATE = "2026-11-12"


def _seed(db):
    tag = uuid.uuid4().hex[:8]
    co = companies.create_company(db, f"PCD-Co-{tag}")
    site = sites.create_site(db, co["id"], f"PCD-Site-{tag}")
    t = topics.upsert_topic(db, site["id"], DATE, "Deliveries",
                            source_s3_key=f"extractions/PCD-{tag}/{DATE}/sid{tag}.json",
                            action_items=[{"text": "Order timber"}, {"text": "Book crane"}])
    items = {r[1]: (str(r[0]), str(r[2])) for r in db.execute(
        "SELECT id, text, stable_id FROM action_items WHERE topic_id=%s", (t["id"],))}
    admin = users.upsert_user(db, f"sub-pcd-{tag}", f"pcd-{tag}@example.com",
                              company_id=co["id"], global_role="admin")
    caller = {"id": admin["id"], "company_id": co["id"], "global_role": "admin",
              "cognito_sub": f"sub-pcd-{tag}", "first_name": "P", "last_name": "C"}
    card = repo.insert_pick_card(db, co["id"], site["id"], "done", "that one is done",
                                 f"seg-{tag}", 12.0,
                                 [items["Order timber"][1], items["Book crane"][1]], "final:x")
    return items, caller, card


def _edits(db, item_id):
    return db.execute("SELECT count(*) FROM content_edits WHERE row_id=%s",
                      (item_id,)).fetchone()[0]


def test_choose_cas_loss_leaves_no_audit_row(db, monkeypatch):
    items, caller, card = _seed(db)
    aid, stable = items["Order timber"]
    stale = repo.get_pick_card(db, card["id"])           # read while pending
    assert stale["state"] == "pending"
    assert repo.resolve_pick_card(db, card["id"], "none", None, caller["id"]) is not None
    monkeypatch.setattr(lambda_org_api.status_pick_cards_repo, "get_pick_card",
                        lambda conn, i: dict(stale))

    resp = lambda_org_api.decide_status_pick_card(
        db, caller, str(card["id"]), {"decision": "choose", "stable_id": stable})

    assert resp["statusCode"] == 409, resp["body"]
    assert json.loads(resp["body"])["error"] == "card already decided"
    assert _edits(db, aid) == 0
    assert db.execute("SELECT status FROM action_items WHERE id=%s",
                      (aid,)).fetchone()[0] == "open"
    # get_pick_card is patched (same module object), so read the row directly.
    assert db.execute("SELECT state FROM status_pick_cards WHERE id=%s",
                      (card["id"],)).fetchone()[0] == "none"


def test_choose_happy_path_writes_audit_row_and_resolves(db):
    """Control for the test above: the same seed without the lost CAS does write."""
    items, caller, card = _seed(db)
    aid, stable = items["Book crane"]
    resp = lambda_org_api.decide_status_pick_card(
        db, caller, str(card["id"]), {"decision": "choose", "stable_id": stable})
    assert resp["statusCode"] == 200, resp["body"]
    assert json.loads(resp["body"])["action_item_id"] == aid
    assert _edits(db, aid) == 1
    assert db.execute("SELECT status FROM action_items WHERE id=%s",
                      (aid,)).fetchone()[0] == "done"
    row = repo.get_pick_card(db, card["id"])
    assert row["state"] == "chosen" and str(row["chosen_stable_id"]) == stable
