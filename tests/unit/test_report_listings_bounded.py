"""Task 8a: with LAKE_INDEX_READS=true the org-api report listings read lake_objects and make
no whole-prefix S3 listing. Flag off = the old path (regression pin). Table missing (repository
sentinel None) = old path + WARNING; an EMPTY index result is an answer and does not fall back."""
import datetime
import logging

import pytest

org = pytest.importorskip("lambda_org_api", reason="requires the org api lambda's dependencies")

TS = datetime.datetime(2026, 9, 2, 4, 7, 9, tzinfo=datetime.timezone.utc)
A = "aaaaaaaa-0000-0000-0000-000000000001"
ADMIN = {"global_role": "admin", "company_id": A, "folder_name": "Ada_L"}
PLATFORM = {"global_role": "platform_admin", "company_id": A, "folder_name": None}


class _NoListS3:
    """Any prefix listing is a failure of the contract."""
    def get_paginator(self, _op):
        raise AssertionError("whole-prefix S3 listing with LAKE_INDEX_READS=true")


class _S3WithOneReport:
    def __init__(self):
        self.prefixes = []

    def get_paginator(self, _op):
        outer = self

        class P:
            def paginate(self, Bucket=None, Prefix=""):
                outer.prefixes.append(Prefix)
                yield {"Contents": [
                    {"Key": "reports/2026-09-02/Old_F/daily_report.json", "LastModified": TS, "Size": 7},
                    {"Key": "reports/2026-09-02/Old_F/daily_report.docx", "LastModified": TS, "Size": 9}]}
        return P()


def _row(key, size=10, updated=TS):
    return {"s3_key": key, "size_bytes": size, "updated_at": updated}


@pytest.fixture
def idx(monkeypatch):
    """Fake repository recording its calls."""
    calls = []
    state = {"reports": [], "docx": [], "folders": []}

    def list_reports(conn, **kw):
        calls.append(("list_reports", kw))
        if kw.get("keys") is not None:
            return state["docx"]
        return state["reports"]

    def folders(conn, day, company_ids=None, **kw):
        calls.append(("folders", {"day": day, "company_ids": company_ids, **kw}))
        return state["folders"]
    monkeypatch.setattr(org.lake_objects, "list_reports", list_reports)
    monkeypatch.setattr(org.lake_objects, "report_folders_for_day", folders)
    monkeypatch.setattr(org, "s3", lambda: _NoListS3())
    monkeypatch.setenv("LAKE_INDEX_READS", "true")
    return calls, state


def test_report_history_from_the_index_touches_no_s3_listing(idx):
    calls, st = idx
    st["reports"] = [_row("reports/2026-09-02/Ada_L/daily_report.json", 25490),
                     _row("reports/2026-08-16/Ada_L/weekly_report.json", 5583)]
    st["docx"] = [_row("reports/2026-09-02/Ada_L/daily_report.docx", 40243)]
    res = org._read_org_report_history({"Ada_L"}, 20, conn=object(), caller=ADMIN)
    rows = {r["key"]: r for r in res["reports"]}
    daily = rows["reports/2026-09-02/Ada_L/daily_report.json"]
    assert daily == {"key": "reports/2026-09-02/Ada_L/daily_report.json", "type": "daily",
                     "date": "2026-09-02", "generated_at": TS.isoformat(), "size": 25490,
                     "docx_key": "reports/2026-09-02/Ada_L/daily_report.docx", "docx_size": 40243,
                     "docx_generated_at": TS.isoformat()}
    assert rows["reports/2026-08-16/Ada_L/weekly_report.json"]["type"] == "weekly"
    assert "docx_key" not in rows["reports/2026-08-16/Ada_L/weekly_report.json"]
    # scope handed to the repository: folder scope AND company (R8b)
    kw = calls[0][1]
    assert kw["folders"] == ["Ada_L"] and kw["company_ids"] == [A] and kw["own_folder"] == "Ada_L"


def test_platform_admin_history_passes_no_company_restriction(idx):
    calls, st = idx
    org._read_org_report_history(None, 20, conn=object(), caller=PLATFORM)
    kw = calls[0][1]
    assert kw["folders"] is None and kw["company_ids"] is None and kw["own_folder"] is None


def test_empty_index_result_is_empty_and_does_not_fall_back(idx):
    assert org._read_org_report_history({"Ada_L"}, 20, conn=object(), caller=ADMIN) == {"reports": []}


def test_keep_guard_is_applied_to_index_rows_before_the_limit(idx):
    calls, st = idx
    st["reports"] = [_row("reports/2026-09-03/Ada_L/daily_report.json"),
                     _row("reports/2026-09-02/Ada_L/daily_report.json")]
    res = org._read_org_report_history(
        {"Ada_L"}, 1, keep=lambda k: "09-03" not in k, conn=object(), caller=ADMIN)
    assert [r["key"] for r in res["reports"]] == ["reports/2026-09-02/Ada_L/daily_report.json"]


def test_report_folders_from_the_index_touch_no_s3_listing(idx):
    calls, st = idx
    st["folders"] = ["Ada_L", "Bo_K"]
    assert org._list_report_folders("2026-09-02", object(), ADMIN) == ["Ada_L", "Bo_K"]
    assert calls[0][1]["company_ids"] == [A]
    assert calls[0][1]["key_suffix"] == "/daily_report.json"


def test_platform_admin_folders_are_unrestricted(idx):
    calls, st = idx
    org._list_report_folders("2026-09-02", object(), PLATFORM)
    assert calls[0][1]["company_ids"] is None


def test_empty_folder_result_does_not_fall_back(idx):
    assert org._list_report_folders("2026-09-02", object(), ADMIN) == []


def test_table_missing_runs_the_old_path_with_a_warning(monkeypatch, caplog):
    fake = _S3WithOneReport()
    monkeypatch.setattr(org, "s3", lambda: fake)
    monkeypatch.setattr(org.lake_objects, "list_reports", lambda conn, **kw: None)
    monkeypatch.setattr(org.lake_objects, "report_folders_for_day", lambda *a, **kw: None)
    monkeypatch.setenv("LAKE_INDEX_READS", "true")
    with caplog.at_level(logging.WARNING):
        hist = org._read_org_report_history(None, 20, conn=object(), caller=PLATFORM)
        folders = org._list_report_folders("2026-09-02", object(), PLATFORM)
    assert [r["key"] for r in hist["reports"]] == ["reports/2026-09-02/Old_F/daily_report.json"]
    assert folders == ["Old_F"]
    assert fake.prefixes == ["reports/", "reports/2026-09-02/"]
    assert sum("lake_objects is missing" in r.message for r in caplog.records) == 2


@pytest.mark.parametrize("flag", [None, "false", "True", ""])
def test_flag_off_uses_the_old_path_and_never_the_index(monkeypatch, flag):
    """Regression pin: the S3 path was the behaviour before this task."""
    fake = _S3WithOneReport()
    monkeypatch.setattr(org, "s3", lambda: fake)

    def boom(*a, **k):
        raise AssertionError("index read with LAKE_INDEX_READS off")
    monkeypatch.setattr(org.lake_objects, "list_reports", boom)
    monkeypatch.setattr(org.lake_objects, "report_folders_for_day", boom)
    if flag is None:
        monkeypatch.delenv("LAKE_INDEX_READS", raising=False)
    else:
        monkeypatch.setenv("LAKE_INDEX_READS", flag)
    assert org._list_report_folders("2026-09-02", object(), ADMIN) == ["Old_F"]
    assert len(org._read_org_report_history(None, 20, conn=object(), caller=PLATFORM)["reports"]) == 1


def test_call_sites_hand_conn_and_caller_to_the_listings():
    """The unwired-argument trap: the functions only use the index when given conn+caller."""
    import inspect
    assert "_list_report_folders(date, conn, caller)" in inspect.getsource(org.admin_disambiguation)
    assert "conn=conn, caller=caller" in inspect.getsource(org.get_org_report_history)
