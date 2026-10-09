"""Final review F2 (residual): a person with a live external membership has whole-day
documents that span companies. The directory says so (`has_external`, never the sites) and
the report generator keeps those documents out of every SITE rollup."""
import json
import logging

import pytest

import directory

rg = pytest.importorskip("lambda_report_generator", reason="requires boto3")

DOC = {"version": 1, "published_at": "x", "sites": {"id-a": {"slug": "yard", "name": "Yard A"}},
       "people": {
           "Ann_Home": {"name": "Ann", "role": "worker", "primary_site": "id-a", "sites": ["id-a"],
                        "has_external": False},
           "Eve_Ext": {"name": "Eve", "role": "worker", "primary_site": "id-a", "sites": ["id-a"],
                       "has_external": True}}}


def test_build_publishes_the_flag_but_not_the_external_sites():
    users = [{"id": "u1", "folder_name": "Ann_Home", "first_name": "Ann", "last_name": "H",
              "global_role": "worker", "company_id": "c1"},
             {"id": "u2", "folder_name": "Eve_Ext", "first_name": "Eve", "last_name": "X",
              "global_role": "worker", "company_id": "c1"}]
    mem = [{"user_id": "u1", "site_id": "s1"}, {"user_id": "u2", "site_id": "s1"}]
    sites = [{"id": "s1", "company_id": "c1", "name": "Yard", "slug": "yard"}]
    doc = directory.build(users, mem, sites, "now", external_user_ids={"u2"})
    assert doc["people"]["Eve_Ext"]["has_external"] is True
    assert doc["people"]["Ann_Home"]["has_external"] is False
    assert doc["people"]["Eve_Ext"]["sites"] == ["s1"]          # home sites only


def test_the_site_rollup_never_ingests_a_person_with_an_external_membership(caplog):
    by_user = {"Ann_Home": [{"user_name": "Ann_Home"}], "Eve_Ext": [{"user_name": "Eve_Ext"}],
               "_summary": [{"x": 1}]}
    all_sites = {"Ann_Home": ["id-a"], "Eve_Ext": ["id-a"]}
    with caplog.at_level(logging.INFO):
        got = rg.group_reports_by_site(by_user, all_sites, {"Eve_Ext"}, "default")
    assert got == {"id-a": [{"user_name": "Ann_Home"}]}
    msgs = [r.getMessage() for r in caplog.records]
    assert sum("Eve_Ext" in m and "excluded from site rollups" in m for m in msgs) == 1
    # without the flag the same person is rolled up as before
    assert rg.group_reports_by_site(by_user, all_sites, set(), "default")["id-a"][1] ==         {"user_name": "Eve_Ext"}


def test_external_folders_reads_the_directory_flag(monkeypatch):
    monkeypatch.setattr(rg.directory, "load", lambda s3, bucket: DOC)
    assert rg.external_folders("b") == {"Eve_Ext"}
