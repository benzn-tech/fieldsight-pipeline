"""Defaults for the single-company world every unit test here was written in.

Project-owned tenancy P3/P4 added reads that ask the database things a
connection double cannot answer. Each default below is "no": no external
membership, no person of another company, no rows outside the caller's sites,
no restricted sessions, an empty company site listing. The real SQL for every one
is proven in tests/integration/test_project_owned_reads.py.

Uses its OWN MonkeyPatch (not the shared `monkeypatch` fixture) so it can never
interleave with a test that reloads a module under that fixture.
"""
import sys

import pytest


@pytest.fixture(autouse=True)
def _single_company_world_defaults():
    from repositories import memberships, recordings, sites, users
    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(memberships, "has_live_external", lambda conn, user_id: False)
        mp.setattr(memberships, "external_site_roles", lambda conn, user_id: {})
        mp.setattr(memberships, "external_site_companies",
                   lambda conn, user_id, within_site_ids=None: {})
        mp.setattr(recordings, "author_day_has_rows_outside_sites",
                   lambda conn, user_id, date, site_ids, **kw: False)
        mp.setattr(recordings, "author_range_not_within_sites",
                   lambda conn, user_id, date_from, date_to, site_ids, **kw: False)
        mp.setattr(recordings, "session_site_companies", lambda conn, folder, date, base: [])
        mp.setattr(users, "get_by_folder_name_global", lambda conn, folder: None)
        mp.setattr(sites, "list_company_sites",
                   lambda conn, company_id, include_archived=False: [])
        org = sys.modules.get("lambda_org_api")      # never import it just for this
        if org is not None:
            # The session whitelist (final review F1) asks the database which sessions lie
            # on which sites; a connection double cannot answer, so: nothing restricted.
            mp.setattr(org, "_session_hider", lambda conn, caller, folder, date: None)
        yield
