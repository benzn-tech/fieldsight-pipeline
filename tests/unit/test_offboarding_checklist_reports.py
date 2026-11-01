"""Company offboarding, Task 9: checklist_reports.auto_generate makes no report for a
check recorded on an inactive company's SITE (its `company_id` parameter is the site's
company), even when the recorder's own company is active. Skipped with
'company_inactive', never raised; the extraction that called it is untouched (R6)."""
import json
import logging

import checklist_reports as cr
import tenancy_status

USER = {"id": "u-1", "cognito_sub": "sub-1", "company_id": "c-home",
        "folder_name": "Ext_F", "global_role": "worker"}
WINDOWS = [{"name": "pre-pour", "start_at": "11:02:03", "end_at": "11:03:21",
            "template_id": "t-1"}]


def _wire(monkeypatch, site_status):
    calls, traces = [], []
    monkeypatch.setattr(cr.users, "get_by_folder_name", lambda c, co, f: USER)
    monkeypatch.setattr(cr.users, "get_user_by_sub", lambda c, sub: dict(USER))
    monkeypatch.setattr(cr.report_templates, "get_any",
                        lambda c, tid: {"id": tid, "name": "Checklist", "current_version": 1})
    monkeypatch.setattr(cr.topics_repo, "list_day_topics_for_binding", lambda c, f, d: [])
    monkeypatch.setattr(cr, "already_made", lambda *a, **k: False)
    monkeypatch.setattr(cr, "_record", lambda *a, **k: None)
    monkeypatch.setattr(cr.pipeline_trace, "event",
                        lambda stage, what, detail=None, **kw: traces.append(what))
    # The recorder's home company is active; the SITE's company has `site_status`.
    monkeypatch.setattr(tenancy_status, "company_status",
                        lambda conn, cid: site_status if cid == "c-site" else "active")

    def generate(conn, caller, date, event):
        calls.append(caller["id"])
        return {"statusCode": 202, "body": json.dumps({"requestId": "r1"})}

    def run():
        return cr.auto_generate(None, "c-site", "Ext_F", "2026-11-01", "sidA", WINDOWS,
                                generate=generate)
    return run, calls, traces


def test_inactive_site_company_skips_the_report(monkeypatch):
    monkeypatch.setenv("COMPANY_STATUS_ENFORCE", "on")
    run, calls, traces = _wire(monkeypatch, "suspended")
    assert run() == [("pre-pour", "company_inactive")]
    assert calls == [] and traces == ["company_inactive"]


def test_active_site_company_generates_as_before(monkeypatch):
    monkeypatch.setenv("COMPANY_STATUS_ENFORCE", "on")
    run, calls, _ = _wire(monkeypatch, "active")
    assert run() == [("pre-pour", "r1")] and calls == ["u-1"]


def test_flag_off_generates_and_logs(monkeypatch, caplog):
    monkeypatch.setenv("COMPANY_STATUS_ENFORCE", "off")
    caplog.set_level(logging.WARNING)
    run, calls, _ = _wire(monkeypatch, "exited")
    assert run() == [("pre-pour", "r1")]
    lines = [r.getMessage() for r in caplog.records
             if "COMPANY_GATE_WOULD_SUPPRESS" in r.getMessage()]
    assert len(lines) == 1 and "where=checklist-auto-report" in lines[0]
    assert "company=c-site" in lines[0]


def test_a_status_read_failure_never_raises(monkeypatch):
    monkeypatch.setenv("COMPANY_STATUS_ENFORCE", "on")
    run, calls, _ = _wire(monkeypatch, "active")

    def boom(conn, **kw):
        raise RuntimeError("db hiccup")
    monkeypatch.setattr(tenancy_status, "suppress_reason", boom)
    out = run()
    assert out == [("pre-pour", "error")] and calls == []
