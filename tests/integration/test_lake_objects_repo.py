"""lake_objects against a real Postgres: a fake connection never parses SQL, so the COALESCE
upsert, the site reassignment and the keyset pagination are only proven here."""
import pytest
from repositories import companies, lake_objects, sites

pytestmark = pytest.mark.integration


def _world(db):
    a = companies.create_company(db, "LakeA")
    b = companies.create_company(db, "LakeB")
    s1 = sites.create_site(db, a["id"], "Tower A")
    s2 = sites.create_site(db, a["id"], "Tower B")
    return a, b, s1, s2


def _put(db, key, company, site=None, **kw):
    return lake_objects.upsert(db, key, company_id=company, site_id=site, kind=kw.pop("kind", "media"),
                               source="upload", **kw)


def _row(db, key):
    return db.execute("SELECT company_id, site_id, reason, deleted_at FROM lake_objects WHERE s3_key=%s",
                      (key,)).fetchone()


def test_upsert_is_idempotent_and_null_never_erases_owner(db):
    a, _, s1, _ = _world(db)
    assert _put(db, "k1", a["id"], s1["id"]) is True
    assert _put(db, "k1", None, None) is True
    assert db.execute("SELECT count(*) FROM lake_objects WHERE s3_key='k1'").fetchone()[0] == 1
    row = _row(db, "k1")
    assert row[0] == a["id"] and row[1] == s1["id"]


def test_split_day_reason_kept_until_a_company_arrives(db):
    a, _, _, _ = _world(db)
    assert lake_objects.upsert(db, "r1", company_id=None, kind="report", source="backfill",
                               reason="split_day") is True
    assert _row(db, "r1")[2] == "split_day"
    assert _put(db, "r1", None, kind="report") is True  # reason not erased by a bare re-write
    assert _row(db, "r1")[2] == "split_day"
    assert [r["reason"] for r in lake_objects.unresolved(db)] == ["split_day"]
    assert _put(db, "r1", a["id"], kind="report") is True
    row = _row(db, "r1")
    assert row[0] == a["id"] and row[2] is None


def test_upsert_revives_a_soft_deleted_row(db):
    a, _, _, _ = _world(db)
    _put(db, "k1", a["id"])
    db.execute("UPDATE lake_objects SET deleted_at=now() WHERE s3_key='k1'")
    assert lake_objects.count(db) == 0
    _put(db, "k1", a["id"])
    assert _row(db, "k1")[3] is None


def test_reassign_site_moves_exactly_that_sites_rows(db):
    a, b, s1, s2 = _world(db)
    _put(db, "m1", a["id"], s1["id"])
    _put(db, "m2", a["id"], s1["id"])
    _put(db, "other", a["id"], s2["id"])
    _put(db, "nosite", a["id"], None)
    assert lake_objects.reassign_site(db, s1["id"], b["id"]) == 2
    assert _row(db, "m1")[0] == b["id"] and _row(db, "m2")[0] == b["id"]
    assert _row(db, "other")[0] == a["id"]
    assert _row(db, "nosite")[0] == a["id"]
    assert lake_objects.reassign_site(db, s1["id"], b["id"]) == 0


def test_owners_for_company_isolated_and_keyset_paginated(db):
    a, b, _, _ = _world(db)
    for i in range(5):
        _put(db, "a%d" % i, a["id"])
    _put(db, "b0", b["id"])
    p1 = lake_objects.owners_for_company(db, a["id"], limit=3)
    assert [r["s3_key"] for r in p1] == ["a0", "a1", "a2"]
    p2 = lake_objects.owners_for_company(db, a["id"], after_key=p1[-1]["s3_key"], limit=3)
    assert [r["s3_key"] for r in p2] == ["a3", "a4"]
    assert all(r["company_id"] == a["id"] for r in p1 + p2)


def test_unresolved_and_count_agree(db):
    a, _, _, _ = _world(db)
    _put(db, "x1", a["id"])
    _put(db, "u1", None)
    _put(db, "u2", None)
    assert [r["s3_key"] for r in lake_objects.unresolved(db)] == ["u1", "u2"]
    assert [r["s3_key"] for r in lake_objects.unresolved(db, limit=1, after_key="u1")] == ["u2"]
    assert lake_objects.count(db) == 3
    assert lake_objects.count(db, a["id"]) == 1


def test_upsert_many_and_report_listing(db):
    a, b, _, _ = _world(db)
    n = lake_objects.upsert_many(db, [
        {"key": "r/a1", "company_id": a["id"], "kind": "report", "folder": "Ada_L", "day": "2026-10-01",
         "source": "backfill"},
        {"key": "r/a2", "company_id": a["id"], "kind": "report", "folder": "Bo_K", "day": "2026-10-02",
         "source": "backfill"},
        {"key": "r/b1", "company_id": b["id"], "kind": "report", "folder": "Cy_M", "day": "2026-10-01",
         "source": "backfill"},
        {"key": "t/a1", "company_id": a["id"], "kind": "transcript", "folder": "Ada_L", "day": "2026-10-01",
         "source": "backfill"},
    ])
    assert n == 4
    assert [r["s3_key"] for r in lake_objects.list_reports(db, company_ids=[a["id"]])] == ["r/a2", "r/a1"]
    assert [r["s3_key"] for r in lake_objects.list_reports(db, folders=["Cy_M"])] == ["r/b1"]
    assert lake_objects.report_folders_for_day(db, "2026-10-01") == ["Ada_L", "Cy_M"]
    assert lake_objects.report_folders_for_day(db, "2026-10-01", company_ids=[a["id"]]) == ["Ada_L"]
    assert lake_objects.upsert_many(db, []) == 0


def test_indexes_exist(db):
    names = {r[0] for r in db.execute("SELECT indexname FROM pg_indexes WHERE tablename='lake_objects'")}
    assert {"idx_lake_objects_company", "idx_lake_objects_site", "idx_lake_objects_company_kind"} <= names


def test_source_check_constraint(db):
    import psycopg.errors as pe
    with pytest.raises(pe.CheckViolation):
        with db.transaction():
            lake_objects.upsert(db, "bad", company_id=None, kind="media", source="nonsense")
