"""Company resolution reads the identity directory and nothing else.

`COMPANY_NAME` defaulted to "FieldSight", a company that exists in neither
database (the operator company is "FieldSight-platform"), so the name pin and
the name fallback had been returning None on both stacks for as long as anyone
could tell. A folder no directory row claims has no company: say so, name the
folder, and never guess a tenant. (Spec 2026-10-03, decision 4.)
"""
import os

import pytest

ing = pytest.importorskip("lambda_ingest", reason="requires psycopg (installed in CI)")

SRC = os.path.join(os.path.dirname(__file__), "..", "..", "src")


def test_a_folder_the_directory_knows_resolves_to_its_company(monkeypatch):
    monkeypatch.setattr(ing.users, "get_by_folder_name_global",
                        lambda conn, folder: {"company_id": "co-1"} if folder == "Ada_L" else None)
    monkeypatch.setattr(ing.companies, "get_company_by_id",
                        lambda conn, cid: {"id": cid, "name": "Acme"})
    assert ing.resolve_company(object(), "Ada_L") == {"id": "co-1", "name": "Acme"}


def test_a_folder_nobody_claims_has_no_company(monkeypatch):
    monkeypatch.setattr(ing.users, "get_by_folder_name_global", lambda conn, folder: None)
    asked = []
    monkeypatch.setattr(ing.companies, "get_company_by_id",
                        lambda conn, cid: asked.append(cid))
    assert ing.resolve_company(object(), "Ben_Lin") is None
    assert asked == [], "no company may be looked up for a folder nobody claims"


def test_a_directory_row_without_a_company_has_no_company(monkeypatch):
    monkeypatch.setattr(ing.users, "get_by_folder_name_global",
                        lambda conn, folder: {"company_id": None})
    assert ing.resolve_company(object(), "Ghost") is None


def test_the_unknown_folder_error_names_the_folder_and_not_the_seed():
    err = ing.unknown_folder_error("Ben_Lin")
    assert isinstance(err, RuntimeError)
    msg = str(err)
    assert "'Ben_Lin'" in msg
    assert "directory" in msg
    assert "seed" not in msg.lower(), (
        "the seed does not fix an unclaimed folder, and running it collapses every "
        "tenant into one company -- the error must never send anyone to it")


def test_the_pin_is_gone():
    assert not hasattr(ing, "COMPANY_NAME")
    assert not hasattr(ing, "MULTI_TENANT")


def test_the_pipeline_lambdas_and_the_stack_no_longer_carry_the_pin():
    # lambda_org_api's COMPANY_NAME is Task 2's; this checks only what Task 1 owns.
    hits = []
    for name in ("lambda_ingest.py", "lambda_item_writer.py"):
        text = open(os.path.join(SRC, name), encoding="utf-8").read()
        for token in ("COMPANY_NAME", "MULTI_TENANT_RESOLUTION"):
            if token in text:
                hits.append(f"{name}: {token}")
    template = open(os.path.join(SRC, "template.yaml"), encoding="utf-8").read()
    for token in ("MultiTenantResolution", "MULTI_TENANT_RESOLUTION"):
        if token in template:
            hits.append(f"template.yaml: {token}")
    assert hits == [], hits
