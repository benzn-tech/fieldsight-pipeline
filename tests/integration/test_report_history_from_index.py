"""Task 8a against a real Postgres: company filtering (R8a/R8b), docx pairing, ordering, limit,
and the call-site wiring end to end through _read_org_report_history."""
import datetime
import io
import json
import uuid

import pytest
from repositories import companies, lake_objects, users

pytestmark = pytest.mark.integration
org = pytest.importorskip("lambda_org_api")


def _rep(db, key, company, *, folder, day, size=10, reason=None):
    assert lake_objects.upsert(db, key, company_id=company, kind="report", folder=folder, day=day,
                               size_bytes=size, source="upload", reason=reason)


def _world(db):
    a = companies.create_company(db, "HistA")
    b = companies.create_company(db, "HistB")
    return a["id"], b["id"]


def _k(day, folder, name="daily_report.json"):
    return f"reports/{day}/{folder}/{name}"


def _keys(res):
    return [r["key"] for r in res["reports"]]


@pytest.fixture
def flag(monkeypatch):
    monkeypatch.setenv("LAKE_INDEX_READS", "true")


def test_company_a_never_sees_company_b_rows(db, flag):
    a, b = _world(db)
    _rep(db, _k("2026-10-01", "Ada_L"), a, folder="Ada_L", day="2026-10-01")
    _rep(db, _k("2026-10-01", "Cy_M"), b, folder="Cy_M", day="2026-10-01")
    admin = {"global_role": "admin", "company_id": a, "folder_name": "Ada_L"}
    # even a folder scope that wrongly named Cy_M cannot reach company B's row
    res = org._read_org_report_history({"Ada_L", "Cy_M"}, 20, conn=db, caller=admin)
    assert _keys(res) == [_k("2026-10-01", "Ada_L")]
    # platform_admin history stays on the S3 listing (a5c56763), so it is not asserted here


def test_external_member_b_site_report_is_hidden_from_a_admin_but_listed_for_the_member(db, flag):
    a, b = _world(db)
    # Eve's home is company A; her report for a day on B's site belongs to B.
    _rep(db, _k("2026-10-02", "Eve_X"), b, folder="Eve_X", day="2026-10-02")
    # and a split day row of hers belongs to nobody
    _rep(db, _k("2026-10-03", "Eve_X"), None, folder="Eve_X", day="2026-10-03", reason="split_day")
    admin_a = {"global_role": "admin", "company_id": a, "folder_name": "Ada_L"}
    assert _keys(org._read_org_report_history({"Eve_X"}, 20, conn=db, caller=admin_a)) == []
    eve = {"global_role": "worker", "company_id": a, "folder_name": "Eve_X"}
    assert _keys(org._read_org_report_history({"Eve_X"}, 20, conn=db, caller=eve)) == \
        [_k("2026-10-03", "Eve_X"), _k("2026-10-02", "Eve_X")]
    # B's admin sees the B-site row, not the NULL one
    admin_b = {"global_role": "admin", "company_id": b, "folder_name": "Bo_K"}
    assert _keys(org._read_org_report_history({"Eve_X"}, 20, conn=db, caller=admin_b)) == \
        [_k("2026-10-02", "Eve_X")]


def test_null_rows_are_excluded_from_report_folders_for_a_company_admin(db, flag):
    a, b = _world(db)
    _rep(db, _k("2026-10-04", "Ada_L"), a, folder="Ada_L", day="2026-10-04")
    _rep(db, _k("2026-10-04", "Cy_M"), b, folder="Cy_M", day="2026-10-04")
    _rep(db, _k("2026-10-04", "Nul_Z"), None, folder="Nul_Z", day="2026-10-04", reason="split_day")
    # not a daily_report.json: ignored whatever its company
    _rep(db, _k("2026-10-04", "Wk_Q", "weekly_report.json"), a, folder="Wk_Q", day="2026-10-04")
    _rep(db, _k("2026-10-04", "Dbg_R", "daily_report_debug.json"), a, folder="Dbg_R", day="2026-10-04")
    admin = {"global_role": "admin", "company_id": a, "folder_name": "Ada_L"}
    assert org._list_report_folders("2026-10-04", db, admin) == ["Ada_L"]
    plat = {"global_role": "platform_admin", "company_id": a, "folder_name": None}
    assert sorted(org._list_report_folders("2026-10-04", db, plat)) == ["Ada_L", "Cy_M", "Nul_Z"]


def test_docx_pairing_ordering_and_limit(db, flag):
    a, _ = _world(db)
    for day in ("2026-09-01", "2026-09-03", "2026-09-02"):
        _rep(db, _k(day, "Ada_L"), a, folder="Ada_L", day=day, size=100)
    _rep(db, _k("2026-09-03", "Ada_L", "daily_report.docx"), a, folder="Ada_L", day="2026-09-03", size=555)
    _rep(db, _k("2026-09-03", "Ada_L", "daily_report_debug.json"), a, folder="Ada_L", day="2026-09-03")
    admin = {"global_role": "admin", "company_id": a, "folder_name": "Ada_L"}
    res = org._read_org_report_history({"Ada_L"}, 2, conn=db, caller=admin)
    assert _keys(res) == [_k("2026-09-03", "Ada_L"), _k("2026-09-02", "Ada_L")]
    first = res["reports"][0]
    assert first["docx_key"] == _k("2026-09-03", "Ada_L", "daily_report.docx")
    assert first["docx_size"] == 555 and first["size"] == 100 and first["date"] == "2026-09-03"
    assert "docx_key" not in res["reports"][1]


def test_keep_guard_pages_past_hidden_rows(db, flag, monkeypatch):
    monkeypatch.setattr(org, "_REPORT_INDEX_PAGE", 2)
    a, _ = _world(db)
    for i in range(1, 8):
        day = f"2026-08-0{i}"
        _rep(db, _k(day, "Ada_L"), a, folder="Ada_L", day=day)
    admin = {"global_role": "admin", "company_id": a, "folder_name": "Ada_L"}
    hidden = {_k(f"2026-08-0{i}", "Ada_L") for i in (7, 6, 5, 4)}  # limit 1 -> page 2: still pages
    res = org._read_org_report_history({"Ada_L"}, 1, keep=lambda k: k not in hidden,
                                       conn=db, caller=admin)
    assert _keys(res) == [_k("2026-08-03", "Ada_L")]


def test_missing_table_gives_the_sentinel(db):
    db.execute("DROP TABLE lake_objects")
    assert lake_objects.list_reports(db, on_missing=None) is None
    assert lake_objects.report_folders_for_day(db, "2026-10-01", on_missing=None) is None


# ---- F1 (R8f): history shows when the OBJECT was written, not when the index learned of it ----
LM = datetime.datetime(2026, 3, 4, 5, 6, 7, tzinfo=datetime.timezone.utc)


class _BackfillS3:
    def __init__(self, objs):
        self.objs = sorted(objs)

    def list_objects_v2(self, Bucket, Prefix="", MaxKeys=1000, ContinuationToken=None,
                        StartAfter=None, **kw):
        keys = [k for k in self.objs if k.startswith(Prefix) and (not StartAfter or k > StartAfter)]
        return {"Contents": [{"Key": k, "Size": 77, "LastModified": LM} for k in keys],
                "IsTruncated": False}


class _Conn:
    def __init__(self, c):
        self._c = c

    def __getattr__(self, n):
        return getattr(self._c, n)

    def commit(self):
        pass


def test_history_after_a_backfill_shows_the_s3_last_modified(db, flag):
    a, _ = _world(db)
    tag = uuid.uuid4().hex[:6]
    folder = f"Ada_{tag}"
    u = users.upsert_user(db, f"sub-{tag}", f"{tag}@a.nz", company_id=a, first_name="Ada",
                          last_name="L", global_role="worker")
    users.set_folder_name(db, f"sub-{tag}", folder)
    jk, dk = _k("2026-03-03", folder), _k("2026-03-03", folder, "daily_report.docx")
    res = org.backfill_lake_objects(_Conn(db), _BackfillS3([jk, dk]), apply=True, buckets=["bkt"])
    assert res["resolved"] == 2
    admin = {"global_role": "admin", "company_id": a, "folder_name": "Boss_X"}
    row = org._read_org_report_history({folder}, 20, conn=db, caller=admin)["reports"][0]
    assert row["generated_at"] == LM.isoformat()
    assert row["docx_generated_at"] == LM.isoformat()
    # the index time is later and is NOT what is shown
    assert db.execute("SELECT updated_at > %s FROM lake_objects WHERE s3_key=%s", (LM, jk)).fetchone()[0]


def test_a_newer_write_of_the_same_size_updates_the_stamp_and_null_never_erases_it(db):
    a, _ = _world(db)
    t1 = datetime.datetime(2026, 5, 1, tzinfo=datetime.timezone.utc)
    t2 = datetime.datetime(2026, 5, 2, tzinfo=datetime.timezone.utc)
    k = _k("2026-05-01", "Ada_L")

    def put(t):
        return lake_objects.upsert(db, k, company_id=a, kind="report", folder="Ada_L",
                                   day="2026-05-01", size_bytes=10, source="manifest",
                                   object_modified_at=t)

    def stamp():
        return db.execute("SELECT object_modified_at FROM lake_objects WHERE s3_key=%s", (k,)).fetchone()[0]
    put(t1)
    assert stamp() == t1
    put(t2)                                     # same size, same owner: only the stamp differs
    assert stamp() == t2
    put(t1)                                     # an older line arriving late never moves it back
    assert stamp() == t2
    put(None)                                   # a mint-style write (NULL) never erases it
    assert stamp() == t2
    # a mint row has no stamp and history falls back to updated_at
    k2 = _k("2026-05-02", "Ada_L")
    lake_objects.upsert(db, k2, company_id=a, kind="report", folder="Ada_L", day="2026-05-02",
                        source="upload")
    assert db.execute("SELECT object_modified_at FROM lake_objects WHERE s3_key=%s", (k2,)).fetchone()[0] is None
