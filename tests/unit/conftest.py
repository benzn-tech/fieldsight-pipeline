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
def _single_company_world_defaults(request):
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
        # Photo pins (M2 Task 6): every day rebind asks which photos a voice note
        # claimed or a person moved by hand. A connection double cannot answer, so:
        # none pinned. The real SQL is proven in tests/integration/test_photo_pins_sql.py.
        import photo_pins
        mp.setattr(photo_pins, "apply_voice_note_pins", lambda conn, folder, date: 0)
        mp.setattr(photo_pins, "pinned_photo_keys", lambda conn, folder, date: set())
        # Company offboarding gate (spec 2026-11-01 section 5.1): every caller resolution now
        # reads companies.status. A connection double cannot answer, so: active. Only the
        # SQL leaf is stubbed -- the flag, platform_admin exemption, archived branch and
        # WOULD_BLOCK logging in tenancy_status stay real. Gate tests override this with
        # their own monkeypatch. The real SQL is proven in
        # tests/integration/test_tenancy_status.py and test_company_gate_org_api.py.
        # test_tenancy_status.py tests company_status itself, so it stays real there.
        if request.module.__name__.rsplit(".", 1)[-1] != "test_tenancy_status":
            import tenancy_status
            mp.setattr(tenancy_status, "company_status",
                       lambda conn, company_id: tenancy_status.ACTIVE)
            # Live-company choke point (Task 4): nobody inactive, so inactive_site_ids is
            # None and nothing is filtered even when a test turns the flag on. Its SQL is
            # proven in tests/integration/test_company_choke_point.py.
            mp.setattr(tenancy_status, "inactive_company_ids", lambda conn: set())
        org =sys.modules.get("lambda_org_api")      # never import it just for this
        if org is not None:
            # The session whitelist (final review F1) asks the database which sessions lie
            # on which sites; a connection double cannot answer, so: nothing restricted.
            mp.setattr(org, "_session_hider", lambda conn, caller, folder, date: None)
            mp.setattr(org, "_may_see_siteless_of", lambda conn, caller, folder: True)
        yield
