"""Company offboarding, Task 9: the non-VPC senders (report generator, backlog notices)
cannot read Aurora, so org-api publishes the inactive companies in the directory
(config/directory.json, `inactive_companies`) and directory.suppress_reason decides
from it through tenancy_status.suppress_decision (same flag, same shadow log)."""
import logging

import pytest

import directory

DOC = {"version": 2, "people": {
    "Ann_A": {"company_id": "co-A", "primary_site": "s-A", "sites": ["s-A"]},
    "Bob_B": {"company_id": "co-B", "primary_site": None, "sites": []}},
    "sites": {"s-A": {"slug": "a", "company_id": "co-A"},
              "s-B": {"slug": "b", "company_id": "co-B"}},
    "inactive_companies": {"co-B": "suspended"}}


def test_build_lists_only_inactive_companies():
    doc = directory.build([], [], [], "now",
                          company_statuses={"c1": "active", "c2": "exited", "c3": "suspended"})
    assert doc["inactive_companies"] == {"c2": "exited", "c3": "suspended"}


@pytest.mark.parametrize("statuses", [None, {}, {"c1": "active"}])
def test_build_is_unchanged_when_nobody_is_inactive(statuses):
    # Byte-identical to the document before this change: no new key at all.
    doc = directory.build([], [], [], "now", company_statuses=statuses)
    assert "inactive_companies" not in doc
    assert doc == directory.build([], [], [], "now")


def test_company_status_reads_the_document():
    assert directory.company_status(DOC, "co-B") == "suspended"
    assert directory.company_status(DOC, "co-A") == "active"
    assert directory.company_status({}, "co-B") == "active"
    assert directory.company_status(DOC, None) == "active"


def test_suppress_keys_on_the_site_company(monkeypatch):
    monkeypatch.setenv("COMPANY_STATUS_ENFORCE", "on")
    # Site of B, recipient/owner in A: the site decides.
    assert directory.suppress_reason(DOC, site_id="s-B", company_id="co-A",
                                     where="t") == "company_inactive"
    # Site of A, owner in B: the site decides.
    assert directory.suppress_reason(DOC, site_id="s-A", company_id="co-B", where="t") is None


def test_site_less_or_unknown_site_falls_back_to_the_company(monkeypatch):
    monkeypatch.setenv("COMPANY_STATUS_ENFORCE", "on")
    assert directory.suppress_reason(DOC, company_id="co-B", where="t") == "company_inactive"
    assert directory.suppress_reason(DOC, site_id="s-gone", company_id="co-B",
                                     where="t") == "company_inactive"
    assert directory.suppress_reason(DOC, site_id="s-gone", where="t") is None
    assert directory.suppress_reason(DOC, company_id="co-A", where="t") is None


def test_flag_off_allows_and_logs(monkeypatch, caplog):
    monkeypatch.setenv("COMPANY_STATUS_ENFORCE", "off")
    caplog.set_level(logging.WARNING)
    assert directory.suppress_reason(DOC, site_id="s-B", where="daily-report") is None
    [line] = [r.getMessage() for r in caplog.records
              if "COMPANY_GATE_WOULD_SUPPRESS" in r.getMessage()]
    assert "where=daily-report" in line and "company=co-B" in line and "site=s-B" in line
