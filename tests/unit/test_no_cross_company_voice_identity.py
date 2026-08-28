"""Unit: nothing may link one person's voiceprint across two companies.

The same human works for company A and later for company B. Their voice is the same voice.
The *data* is not the same data: it was captured under A's site induction and A's subcontract,
and B has no basis for any of it.

So the requirement is not "do not share the vectors". It is stronger and stranger:

> **a table that says "these two company profiles are the same person" IS the cross-company
> disclosure, whether or not a vector ever moves.**

Somebody with access to that table learns that a worker at B was previously at A, which is a
fact about a person that neither company was given the right to obtain. The system should be
unable to answer the question — not merely decline to.

That property is invisible in ordinary review, because the thing to look for is the ABSENCE of
something, and every step towards it looks locally reasonable: a `people` table to deduplicate
profiles, a `person_id` to make matching cheaper, a nightly job to merge obvious duplicates.
Each is a normal thing to build. This file is where that gets stopped.
"""
import os
import re

import pytest

vp = pytest.importorskip("repositories.voiceprints")

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
MIGRATIONS = os.path.join(ROOT, "src", "migrations")


def _all_sql():
    out = []
    for f in sorted(os.listdir(MIGRATIONS)):
        if f.endswith(".sql"):
            out.append((f, open(os.path.join(MIGRATIONS, f), encoding="utf-8").read()))
    return out


def test_every_voiceprint_table_is_company_scoped():
    """`company_id NOT NULL` on each of them, so a row cannot exist outside a company even
    by mistake. A nullable column here would make "belongs to everybody" representable."""
    tables = {}
    for _f, sql in _all_sql():
        for m in re.finditer(r"CREATE TABLE (?:IF NOT EXISTS )?(speaker_\w+)\s*\((.*?)\n\);",
                             sql, re.S):
            tables[m.group(1)] = m.group(2)
    assert tables, "no speaker_* tables found; this test is reading the wrong place"
    for name, body in tables.items():
        assert re.search(r"company_id\s+uuid\s+NOT NULL", body), (
            f"{name} does not force a company; a voiceprint outside a company is a "
            f"voiceprint nobody has a basis for")


def test_the_external_identity_is_unique_per_company_and_not_globally():
    """The sign-in id is the key that makes this work on a site, and it is also the obvious
    thing to make globally unique — one row per person, tidy. That would be the leak: the same
    worker at two companies would collide into one profile, and A's recordings would answer
    B's questions."""
    idx = [sql for _f, sql in _all_sql() if "speaker_voiceprints_external_ident" in sql]
    assert idx, "the external identity index is gone; nothing keeps sign-in ids apart"
    body = idx[0]
    m = re.search(r"CREATE UNIQUE INDEX[^;]*speaker_voiceprints_external_ident(.*?);", body, re.S)
    cols = m.group(1)
    assert "company_id" in cols, (
        "the external identity is unique globally rather than per company, so one person "
        "signing in at two companies becomes one profile")


def test_no_table_maps_a_person_across_companies():
    """The absence, asserted.

    Any table that pairs two voiceprints, or that carries a person key without a company
    beside it, can answer "is the Ben at B the Ben at A". Nothing in this schema may.
    """
    offenders = []
    for f, sql in _all_sql():
        for m in re.finditer(r"CREATE TABLE (?:IF NOT EXISTS )?(\w+)\s*\((.*?)\n\);", sql, re.S):
            name, body = m.group(1), m.group(2)
            refs = re.findall(r"REFERENCES speaker_voiceprints", body)
            if len(refs) >= 2:
                offenders.append(f"{f}:{name} references speaker_voiceprints twice — "
                                 f"a pair of profiles is a cross-company link")
            if re.search(r"\bglobal_person\w*|\bperson_uuid\b|\bidentity_graph\b", body, re.I):
                offenders.append(f"{f}:{name} carries a global person key")
    assert not offenders, "; ".join(offenders)


def test_no_query_reads_profiles_without_a_company():
    """Every read of the profile table names a company. One that does not would return
    another company's people, and the failure is silent — more candidates, better-looking
    matches, and no error anywhere."""
    src = open(os.path.join(ROOT, "src", "repositories", "voiceprints.py"),
               encoding="utf-8").read()
    bad = []
    for m in re.finditer(r'"(SELECT[^"]*speaker_voiceprints[^"]*)"', src):
        stmt = m.group(1)
        # The statement is often built from adjacent string literals; take the whole call.
        start = src.rfind("cur.execute(", 0, m.start())
        call = src[start:src.index(").fetchone()", m.start())
                   if ").fetchone()" in src[m.start():m.start() + 1200]
                   else m.end()]
        if "company_id" not in call:
            bad.append(stmt[:70])
    assert not bad, f"reads without a company filter: {bad}"


def test_a_company_that_has_settled_no_basis_enrols_nobody():
    """The fallback is the strict rule, not a permissive one. A company with no configured
    basis must behave exactly as the system did before any of this existed."""
    import lambda_org_api as org
    src = open(os.path.join(ROOT, "src", "lambda_org_api.py"), encoding="utf-8").read()
    assert "company_basis = companies.voiceprint_consent_basis" in src
    assert re.search(r"attest\s*=\s*\(ENROL_ON_CORRECTION and company_basis", src), (
        "the company's basis is not required for the attested path, so a company that "
        "settled nothing would still enrol")
    assert org.ENROL_ON_CORRECTION in (True, False)
