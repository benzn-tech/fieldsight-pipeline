"""Integration: action_items.get_live_by_stable_id (action packs Task 2, spec §14 S12).

Real Postgres (skipped without TEST_DATABASE_URL -- a skip is NOT a pass). The `db`
fixture rolls back after each test.
"""
import uuid

import pytest

from repositories import action_items, companies, redactions, sites, topics

pytestmark = pytest.mark.integration

KEY = "extractions/T2_Folder/2026-11-01/sidT2.json"


def _site(db):
    co = companies.create_company(db, f"T2-Co-{uuid.uuid4().hex[:8]}")
    return co, sites.create_site(db, co["id"], f"T2-Site-{uuid.uuid4().hex[:8]}")


def _topic_with_item(db, site, text, key=KEY):
    t = topics.upsert_topic(db, site["id"], "2026-11-01", "T", source_s3_key=key,
                            action_items=[{"text": text}])
    row = db.execute("SELECT id, stable_id FROM action_items WHERE topic_id=%s",
                     (t["id"],)).fetchone()
    return t, row[0], row[1]


def test_returns_the_live_row_with_company_id(db):
    co, site = _site(db)
    _t, aid, stable = _topic_with_item(db, site, "Order timber")
    row = action_items.get_live_by_stable_id(db, stable)
    assert row["id"] == aid
    assert row["stable_id"] == stable
    assert row["company_id"] == co["id"]
    assert row["site_id"] == site["id"]
    assert row["text"] == "Order timber"
    assert row["source_s3_key"] == KEY


def test_a_superseded_pass_sharing_the_stable_id_is_never_returned(db):
    _co, site = _site(db)
    old_t, old_id, stable = _topic_with_item(db, site, "Order timber")
    # A newer row on a LATER pass of the same key carries the identity (carry_forward).
    new_t, new_id, _ = _topic_with_item(db, site, "Order the timber")
    db.execute("UPDATE topics SET superseded_at=now(), superseded_by_run='final:x' "
               "WHERE id=%s", (old_t["id"],))
    db.execute("UPDATE action_items SET stable_id=%s, carried_from=%s WHERE id=%s",
               (stable, old_id, new_id))
    # Make the superseded row the NEWEST by created_at, so ORDER BY alone would pick it:
    # only the live-topic predicate keeps it out.
    db.execute("UPDATE action_items SET created_at = now() + interval '1 hour' WHERE id=%s",
               (old_id,))
    row = action_items.get_live_by_stable_id(db, stable)
    assert row["id"] == new_id
    assert row["carried_from"] == old_id

    # And once the live row's topic is superseded too, nothing is returned.
    db.execute("UPDATE topics SET superseded_at=now(), superseded_by_run='final:y' "
               "WHERE id=%s", (new_t["id"],))
    assert action_items.get_live_by_stable_id(db, stable) is None


def test_a_deleted_topic_is_not_returned(db):
    co, site = _site(db)
    t, _aid, stable = _topic_with_item(db, site, "Order timber")
    redactions.create_redaction(db, co["id"], t["id"], "test", None, None, scope="deleted")
    assert action_items.get_live_by_stable_id(db, stable) is None


@pytest.mark.parametrize("bad", ["not-a-uuid", "", None, "69", "1234-zz"])
def test_malformed_id_returns_none_and_keeps_the_transaction_usable(db, bad):
    _co, site = _site(db)
    assert action_items.get_live_by_stable_id(db, bad) is None
    # The caller's transaction is intact: the site written above is still there.
    assert db.execute("SELECT 1 FROM sites WHERE id=%s", (site["id"],)).fetchone() == (1,)


def test_unknown_stable_id_returns_none(db):
    assert action_items.get_live_by_stable_id(db, uuid.uuid4()) is None
