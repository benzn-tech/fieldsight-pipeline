"""Task 8a against a real Postgres: company filtering (R8a/R8b), docx pairing, ordering, limit,
and the call-site wiring end to end through _read_org_report_history."""
import pytest
from repositories import companies, lake_objects

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
    plat = {"global_role": "platform_admin", "company_id": a, "folder_name": None}
    assert sorted(_keys(org._read_org_report_history(None, 20, conn=db, caller=plat))) == \
        sorted([_k("2026-10-01", "Ada_L"), _k("2026-10-01", "Cy_M")])


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
