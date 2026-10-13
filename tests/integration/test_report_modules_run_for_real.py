"""Report modules against Postgres: versions kept, companies kept apart.

Owner, 2026-09-30: templates and modules are isolated by company -- a version we
write for company A changes nothing for company B. Every query here meets a
real database: the version table, the company-first resolution, the save-time
pin, and the expression unique index the company upsert infers.
"""
import pytest

import report_modules as rm

pytestmark = pytest.mark.integration


def _company(db, name):
    return db.execute("INSERT INTO companies (name) VALUES (%s) RETURNING id", (name,)).fetchone()[0]


def test_a_companys_own_version_is_theirs_alone(db):
    a, b = _company(db, "Alpha"), _company(db, "Beta")
    rm.sync_standard(db)
    tuned = "Safety for Alpha: always name the subcontractor and the zone."
    h = rm.add_company_version(db, a, "safety", "Safety", "list", tuned)

    safety_a = [m for m in rm.resolve_all(db, a) if m["key"] == "safety"][0]
    safety_b = [m for m in rm.resolve_all(db, b) if m["key"] == "safety"][0]
    assert (safety_a["source"], safety_a["purpose"], safety_a["hash"]) == ("company", tuned, h)
    assert safety_b["source"] == "standard" and safety_b["purpose"] == rm._standard("safety")["purpose"]

    assert rm.find_version(db, a, "safety", h) == tuned
    assert rm.find_version(db, b, "safety", h) is None, "B can never pin A's text"


def test_a_saved_template_gets_the_published_text_for_its_company(db):
    a, b = _company(db, "Alpha"), _company(db, "Beta")
    tuned = "Alpha's quality wording."
    h = rm.add_company_version(db, a, "quality", "Quality", "list", tuned)
    section = {"title": "Quality", "purpose": "edited by the client", "module": {"key": "quality", "hash": h}}
    body = {"sections": [section], "catch_all": {"title": "Anything else", "purpose": "Rest."}}

    out, err = rm.pin_modules(db, a, body)
    assert err is None and out["sections"][0]["purpose"] == tuned

    other = {"sections": [dict(section)], "catch_all": {"title": "Anything else", "purpose": "Rest."}}
    assert rm.pin_modules(db, b, other)[1], "the same pin is refused for another company"


def test_standard_versions_are_kept_after_the_text_moves_on(db):
    rm.sync_standard(db)
    old = rm._standard("summary")
    old_hash = rm.module_hash("summary", old["purpose"])
    rm.sync_standard(db)                                     # idempotent
    n = db.execute("SELECT count(*) FROM report_modules WHERE company_id IS NULL AND key='summary' "
                   "AND hash=%s", (old_hash,)).fetchone()[0]
    assert n == 1
    assert rm.find_version(db, _company(db, "Gamma"), "summary", old_hash) == old["purpose"]


def test_republishing_an_earlier_company_text_makes_it_current_again(db):
    a = _company(db, "Alpha")
    first = rm.add_company_version(db, a, "decisions", "Decisions", "list", "Version one.")
    rm.add_company_version(db, a, "decisions", "Decisions", "list", "Version two.")
    again = rm.add_company_version(db, a, "decisions", "Decisions", "list", "Version one.")
    assert again == first
    current = [m for m in rm.resolve_all(db, a) if m["key"] == "decisions"][0]
    assert current["purpose"] == "Version one."
