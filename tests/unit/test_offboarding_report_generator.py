"""Company offboarding, Task 9 (spec 2026-11-01 §5.5): the nightly generator makes no
report for an inactive company. Keyed by the report's SITE company (a person's daily
report: their primary site's company; site-less: their home company), and per-company
summaries are skipped for an inactive company. The generator has no database: it reads
`inactive_companies` from the published directory. Late uploads are still transcribed
and extracted -- that path (item-writer) is untouched and tested in
tests/integration/test_offboarding_late_upload_extracts.py.

The whole generate_daily_report path runs here; only I/O and the model are faked."""
import json
import logging
import os

import pytest

os.environ.setdefault("AWS_ACCESS_KEY_ID", "testing")
os.environ.setdefault("AWS_SECRET_ACCESS_KEY", "testing")
os.environ.setdefault("AWS_DEFAULT_REGION", "ap-southeast-2")
os.environ.setdefault("ANTHROPIC_API_KEY", "sk-ant-test-dummy-key")

rg = pytest.importorskip("lambda_report_generator", reason="requires boto3/urllib3 (installed in CI)")
import directory  # noqa: E402

CO_A = "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa"
CO_B = "bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb"
DATE = "2026-11-01"


def _doc(inactive=None):
    doc = {"version": 2,
           "sites": {"s-A": {"slug": "a", "company_id": CO_A},
                     "s-B": {"slug": "b", "company_id": CO_B}},
           "people": {"Ada": {"name": "Ada", "company_id": CO_A, "primary_site": "s-A"},
                      "Bo": {"name": "Bo", "company_id": CO_B, "primary_site": "s-B"}}}
    if inactive:
        doc["inactive_companies"] = inactive
    return doc


@pytest.fixture
def run(monkeypatch):
    """run(folders, doc, primary) -> (result, daily report keys, summary keys)."""
    def _run(folders, doc, primary=None):
        puts = []
        monkeypatch.delenv("SUMMARY_LAKE_WIDE", raising=False)
        data = {f: {"transcripts": [{"timestamp": None, "device": "d", "who": f}],
                    "photos": [], "total_duration": 60.0, "total_words": 10}
                for f in folders}
        monkeypatch.setattr(rg, "load_prompt_templates", lambda b: None)
        monkeypatch.setattr(rg, "list_s3_objects",
                            lambda b, p: [{"key": f"transcripts/{f}/{DATE}/x.json"} for f in folders])
        prim = primary if primary is not None else {
            f: p["primary_site"] for f, p in doc["people"].items() if p.get("primary_site")}
        monkeypatch.setattr(rg, "get_user_site_mapping",
                            lambda b: (rg._SpacedKeys(prim), {}, {}, {}))
        monkeypatch.setattr(rg, "read_meeting_manifest", lambda *a, **k: None)
        monkeypatch.setattr(rg, "process_user_data", lambda b, u, d, exclude_keys=None: data[u])
        monkeypatch.setattr(rg, "check_report_freshness", lambda *a, **k: (False, 0))
        monkeypatch.setattr(rg, "check_summary_freshness", lambda *a, **k: (False, 0))
        monkeypatch.setattr(rg, "build_weather_block_for_site", lambda *a, **k: None)
        monkeypatch.setattr(rg, "build_weather_findings", lambda *a, **k: None)
        monkeypatch.setattr(rg, "programme_for_site", lambda *a, **k: None)
        monkeypatch.setattr(rg, "generate_word_document", lambda *a, **k: None)
        monkeypatch.setattr(rg, "correlate_photos_with_transcripts", lambda t, p: t)
        monkeypatch.setattr(rg, "save_debug_record", lambda *a, **k: None)
        monkeypatch.setattr(rg, "external_folders", lambda b: set())
        monkeypatch.setattr(rg, "write_items_to_dynamodb", lambda *a, **k: None)
        monkeypatch.setattr(rg, "write_report_to_dynamodb", lambda *a, **k: None)
        monkeypatch.setattr(rg, "write_audit_entry", lambda *a, **k: None)
        monkeypatch.setattr(directory, "load", lambda c, b: doc)
        monkeypatch.setattr(directory, "reset_cache", lambda: None)
        monkeypatch.setattr(rg, "build_daily_prompt",
                            lambda correlated, who, *a, **k: "PROMPT")
        monkeypatch.setattr(rg, "call_claude_structured",
                            lambda p, max_tokens=4096: (json.dumps(
                                {"executive_summary": "s", "topics": []}), None))
        monkeypatch.setattr(rg, "lake_put", lambda bucket, key, body, **kw: puts.append(key))
        out = rg.generate_daily_report(DATE)
        daily = sorted(k.split("/")[2] for k in puts if k.endswith("/daily_report.json"))
        companies = sorted(k.split("/")[3] for k in puts
                           if "/companies/" in k and k.endswith("summary_report.json"))
        return out, daily, companies
    return _run


def _shadow(caplog):
    return [r.getMessage() for r in caplog.records
            if "COMPANY_GATE_WOULD_SUPPRESS" in r.getMessage()]


def test_inactive_company_gets_no_daily_report_and_no_company_summary(run, monkeypatch):
    monkeypatch.setenv("COMPANY_STATUS_ENFORCE", "on")
    out, daily, companies = run(["Ada", "Bo"], _doc({CO_B: "suspended"}))
    assert daily == ["Ada"] and companies == [CO_A]
    assert out["users_suppressed"] == ["Bo"]


def test_all_active_is_unchanged(run, monkeypatch):
    monkeypatch.setenv("COMPANY_STATUS_ENFORCE", "on")
    out, daily, companies = run(["Ada", "Bo"], _doc())
    assert daily == ["Ada", "Bo"] and companies == [CO_A, CO_B]
    assert out["users_suppressed"] == []


def test_flag_off_generates_everything_and_logs(run, monkeypatch, caplog):
    monkeypatch.setenv("COMPANY_STATUS_ENFORCE", "off")
    caplog.set_level(logging.WARNING)
    out, daily, companies = run(["Ada", "Bo"], _doc({CO_B: "exited"}))
    assert daily == ["Ada", "Bo"] and companies == [CO_A, CO_B]
    lines = _shadow(caplog)
    assert any("where=daily-report" in m and f"company={CO_B}" in m for m in lines)
    assert any("where=company-summary" in m and f"company={CO_B}" in m for m in lines)


def test_the_daily_report_is_keyed_by_the_site_company(run, monkeypatch):
    """Ada (home A) reports on B's site; Bo (home B, inactive) on A's site."""
    monkeypatch.setenv("COMPANY_STATUS_ENFORCE", "on")
    out, daily, _ = run(["Ada", "Bo"], _doc({CO_B: "suspended"}),
                        primary={"Ada": "s-B", "Bo": "s-A"})
    assert daily == ["Bo"] and out["users_suppressed"] == ["Ada"]


def test_site_less_daily_report_uses_the_home_company(run, monkeypatch):
    monkeypatch.setenv("COMPANY_STATUS_ENFORCE", "on")
    out, daily, _ = run(["Ada", "Bo"], _doc({CO_B: "suspended"}), primary={})
    assert daily == ["Ada"] and out["users_suppressed"] == ["Bo"]


def test_periodic_reports_drop_an_inactive_companys_dailies(monkeypatch):
    monkeypatch.setenv("COMPANY_STATUS_ENFORCE", "on")
    doc = _doc({CO_B: "suspended"})
    ada, bo = {"user_name": "Ada"}, {"user_name": "Bo"}
    by_user = {"Ada": [ada], "Bo": [bo], "_summary": [{"x": 1}]}
    every = [ada, bo, by_user["_summary"][0]]
    by_user, every = rg._drop_suppressed_reports(
        by_user, every, doc, {"Ada": "s-A", "Bo": "s-B"}, where="weekly-report")
    assert set(by_user) == {"Ada", "_summary"}
    assert every == [ada, {"x": 1}]


def test_periodic_flag_off_keeps_everything(monkeypatch):
    monkeypatch.setenv("COMPANY_STATUS_ENFORCE", "off")
    by_user = {"Bo": [{"user_name": "Bo"}]}
    every = list(by_user["Bo"])
    out_by_user, out_every = rg._drop_suppressed_reports(
        by_user, every, _doc({CO_B: "suspended"}), {"Bo": "s-B"}, where="weekly-report")
    assert out_by_user == by_user and out_every == every
