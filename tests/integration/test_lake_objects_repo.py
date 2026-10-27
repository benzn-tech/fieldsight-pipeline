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


# ---- fix round 1 ----

def _full(db, key):
    return db.execute("SELECT user_id, size_bytes, kind, folder, day, session_base, source, updated_at "
                      "FROM lake_objects WHERE s3_key=%s", (key,)).fetchone()


def test_missing_table_leaves_the_callers_transaction_usable(db):
    """The real savepoint: UndefinedTable inside an open transaction must not abort it."""
    db.execute("DROP TABLE lake_objects")  # rolled back with the test transaction
    assert lake_objects.upsert(db, "k", company_id=None, kind="media", source="upload") is False
    assert lake_objects.count(db) == 0
    assert lake_objects.owners_for_company(db, "11111111-1111-1111-1111-111111111111") == []
    assert db.execute("SELECT 1").fetchone()[0] == 1


def test_upsert_coalesces_user_and_size(db):
    a, _, _, _ = _world(db)
    uid = "33333333-3333-3333-3333-333333333333"
    lake_objects.upsert(db, "k", company_id=a["id"], user_id=uid, size_bytes=42, kind="media", source="upload")
    lake_objects.upsert(db, "k", company_id=None, kind="media", source="upload")
    row = _full(db, "k")
    assert str(row[0]) == uid and row[1] == 42
    lake_objects.upsert(db, "k", company_id=None, size_bytes=99, kind="media", source="upload")
    assert _full(db, "k")[1] == 99


def test_upsert_refreshes_descriptive_fields_without_erasing(db):
    a, _, _, _ = _world(db)
    lake_objects.upsert(db, "k", company_id=a["id"], kind="media", source="upload")
    lake_objects.upsert(db, "k", company_id=None, kind="transcript", folder="Ada_L", day="2026-10-01",
                        session_base="sb", source="backfill")
    row = _full(db, "k")
    assert row[2:7] == ("transcript", "Ada_L", __import__("datetime").date(2026, 10, 1), "sb", "backfill")
    lake_objects.upsert(db, "k", company_id=None, kind="transcript", source="backfill")
    assert _full(db, "k")[3] == "Ada_L"


def test_unchanged_upsert_does_not_bump_updated_at(db):
    a, _, _, _ = _world(db)
    kw = dict(company_id=a["id"], kind="media", folder="Ada_L", source="upload")
    lake_objects.upsert(db, "k", **kw)
    first = _full(db, "k")[7]
    db.execute("SELECT pg_sleep(0.05)")
    lake_objects.upsert(db, "k", **kw)
    assert _full(db, "k")[7] == first  # now() is the transaction start; guard keeps it untouched
    db.execute("UPDATE lake_objects SET updated_at = updated_at - interval '1 hour' WHERE s3_key='k'")
    old = _full(db, "k")[7]
    lake_objects.upsert(db, "k", **kw)
    assert _full(db, "k")[7] == old
    lake_objects.upsert(db, "k", **dict(kw, size_bytes=5))
    assert _full(db, "k")[7] > old


def test_upsert_many_conflict_path(db):
    a, b, _, _ = _world(db)
    lake_objects.upsert(db, "m1", company_id=a["id"], user_id=None, size_bytes=7, kind="media", source="upload")
    lake_objects.upsert(db, "m2", company_id=None, kind="report", source="backfill", reason="split_day")
    n = lake_objects.upsert_many(db, [
        {"key": "m1", "kind": "media", "source": "backfill", "folder": "Ada_L"},          # optional keys absent
        {"key": "m2", "company_id": b["id"], "kind": "report", "source": "backfill"},      # resolves + clears reason
        {"key": "m3", "kind": "media", "source": "manifest"},
    ])
    assert n == 3
    r1 = db.execute("SELECT company_id, size_bytes, folder FROM lake_objects WHERE s3_key='m1'").fetchone()
    assert r1 == (a["id"], 7, "Ada_L")
    r2 = _row(db, "m2")
    assert r2[0] == b["id"] and r2[2] is None
    assert lake_objects.count(db) == 3


def test_soft_deleted_rows_are_excluded_from_every_listing(db):
    a, _, _, _ = _world(db)
    for k in ("live", "dead"):
        lake_objects.upsert(db, "r/" + k, company_id=a["id"], kind="report", folder="F_" + k, day="2026-10-01",
                            source="upload")
        lake_objects.upsert(db, "u/" + k, company_id=None, kind="media", source="upload")
    db.execute("UPDATE lake_objects SET deleted_at=now() WHERE s3_key IN ('r/dead','u/dead')")
    assert [r["s3_key"] for r in lake_objects.owners_for_company(db, a["id"])] == ["r/live"]
    assert [r["s3_key"] for r in lake_objects.unresolved(db)] == ["u/live"]
    assert [r["s3_key"] for r in lake_objects.list_reports(db)] == ["r/live"]
    assert lake_objects.report_folders_for_day(db, "2026-10-01") == ["F_live"]


def test_list_reports_limit_combined_filters_and_null_day_last(db):
    a, b, _, _ = _world(db)
    rows = [("r1", a, "F1", "2026-10-01"), ("r2", a, "F2", "2026-10-03"), ("r3", a, "F1", "2026-10-02"),
            ("r4", b, "F1", "2026-10-04"), ("r5", a, "F1", None)]
    for k, c, f, d in rows:
        lake_objects.upsert(db, k, company_id=c["id"], kind="report", folder=f, day=d, source="upload")
    assert [r["s3_key"] for r in lake_objects.list_reports(db)] == ["r4", "r2", "r3", "r1", "r5"]
    assert [r["s3_key"] for r in lake_objects.list_reports(db, limit=2)] == ["r4", "r2"]
    assert [r["s3_key"] for r in lake_objects.list_reports(db, company_ids=[a["id"]], folders=["F1"])] \
        == ["r3", "r1", "r5"]
    assert lake_objects.list_reports(db, company_ids=[b["id"]], folders=["F2"]) == []


def test_reassign_site_clears_reason_and_moves_soft_deleted_rows(db):
    a, b, s1, _ = _world(db)
    lake_objects.upsert(db, "x1", company_id=a["id"], site_id=s1["id"], kind="media", source="upload")
    lake_objects.upsert(db, "x2", company_id=a["id"], site_id=s1["id"], kind="media", source="upload")
    db.execute("UPDATE lake_objects SET reason='split_day' WHERE s3_key='x1'")
    db.execute("UPDATE lake_objects SET deleted_at=now() WHERE s3_key='x2'")
    assert lake_objects.reassign_site(db, s1["id"], b["id"]) == 2
    assert _row(db, "x1")[0] == b["id"] and _row(db, "x1")[2] is None
    assert _row(db, "x2")[0] == b["id"] and _row(db, "x2")[3] is not None  # still soft-deleted, moved


# ---- fix round 2 ----

def test_same_company_write_clears_a_stale_reason(db):
    a, _, _, _ = _world(db)
    _put(db, "r", a["id"], kind="report")
    db.execute("UPDATE lake_objects SET reason='split_day' WHERE s3_key='r'")
    _put(db, "r", a["id"], kind="report")  # same company, reason NULL: must clear
    assert _row(db, "r")[2] is None
    db.execute("UPDATE lake_objects SET reason='split_day' WHERE s3_key='r'")
    _put(db, "r", None, kind="report")  # no company supplied: reason kept
    assert _row(db, "r")[2] == "split_day"


def test_upsert_many_returns_rows_actually_written(db):
    a, _, _, _ = _world(db)
    lake_objects.upsert(db, "same", company_id=a["id"], kind="media", source="upload")
    n = lake_objects.upsert_many(db, [
        {"key": "same", "company_id": a["id"], "kind": "media", "source": "upload"},   # unchanged
        {"key": "new", "company_id": a["id"], "kind": "media", "source": "upload"},
    ])
    assert n == 1


# ---- operator pin (2026-10-27 Ben_Lin -> Ben_Lin_test2 reassignment) ----

def test_operator_row_is_never_rewritten_by_a_later_upsert(db):
    """R7e covers company only; a re-backfill resolving the SAME company with the resolver's
    user would otherwise put that user back over an operator's decision."""
    a, _, s1, _ = _world(db)
    def user(email, folder):
        return {"id": db.execute(
            "INSERT INTO users (company_id, email, global_role, folder_name) "
            "VALUES (%s, %s, 'worker', %s) RETURNING id", (a["id"], email, folder)).fetchone()[0]}
    u_resolver = user("resolver@example.com", "Res_Olver")
    u_owner = user("owner@example.com", "Own_Er")
    _put(db, "pinned", a["id"], s1["id"], user_id=u_resolver["id"])
    db.execute("UPDATE lake_objects SET user_id=%s, reason='operator' WHERE s3_key='pinned'",
               (u_owner["id"],))
    # same company + different user (what a backfill re-run produces): skipped entirely
    assert lake_objects.upsert_many(db, [{"key": "pinned", "company_id": a["id"],
                                          "user_id": u_resolver["id"], "kind": "media",
                                          "source": "backfill"}]) == 0
    row = db.execute("SELECT user_id, reason FROM lake_objects WHERE s3_key='pinned'").fetchone()
    assert row[0] == u_owner["id"] and row[1] == "operator"
    # a NULL-company write (the resolver's 'unresolved') does not touch it either
    assert lake_objects.upsert(db, "pinned", company_id=None, kind="media", source="backfill",
                               reason="unresolved") is True
    assert _row(db, "pinned")[2] == "operator"
    # a site move still moves it, and keeps the pin
    b = companies.create_company(db, "LakeC")
    assert lake_objects.reassign_site(db, s1["id"], b["id"]) == 1
    row = _row(db, "pinned")
    assert row[0] == b["id"] and row[2] == "operator"


def test_non_operator_reassign_still_clears_reason(db):
    a, b, s1, _ = _world(db)
    lake_objects.upsert(db, "sd", company_id=None, site_id=s1["id"], kind="report",
                        source="backfill", reason="split_day")
    assert lake_objects.reassign_site(db, s1["id"], b["id"]) == 1
    assert _row(db, "sd")[2] is None
