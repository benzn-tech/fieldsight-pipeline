"""lambda_org_seed is gone, and nothing finds a company by its name.

The seed was the one-shot Phase 3 move of identity from user_mapping.json into
Aurora. Long finished, still deployed on both stacks, and read as code: one
default invoke upserted every Cognito user into a single company. Removed, not
guarded (spec 2026-10-03, decision 6).
"""
import os

ROOT = os.path.join(os.path.dirname(__file__), "..", "..")
SRC = os.path.join(ROOT, "src")


def _read(*parts):
    return open(os.path.join(ROOT, *parts), encoding="utf-8").read()


def test_the_seed_module_is_gone():
    assert not os.path.exists(os.path.join(SRC, "lambda_org_seed.py"))


def test_no_stack_deploys_it():
    t = _read("src", "template.yaml")
    assert "OrgSeedFunction" not in t
    assert "OrgSeedLogGroup" not in t
    assert "lambda_org_seed" not in t


def test_nothing_finds_a_company_by_its_name():
    hits = []
    for dirpath, _, files in os.walk(SRC):
        for f in files:
            if f.endswith(".py"):
                text = open(os.path.join(dirpath, f), encoding="utf-8").read()
                if "get_company_by_name" in text:
                    hits.append(os.path.relpath(os.path.join(dirpath, f), SRC))
    assert hits == [], (
        "a company found by its name breaks the day it is renamed -- one rename "
        f"silently switched two features off. Found in: {hits}")
