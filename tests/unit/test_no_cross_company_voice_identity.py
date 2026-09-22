"""Unit: what may and may not cross a company boundary when a voice is recognised.

## The rule this file used to enforce, and why it changed

Until 2026-09-22 the rule here was absolute:

> **a table that says "these two company profiles are the same person" IS the cross-company
> disclosure, whether or not a vector ever moves.** The system should be unable to answer
> the question -- not merely decline to.

That is migration 0049's argument, and it is still the right argument about *attribution
data*. It was overturned as a product decision on 2026-09-22: the owner requires that a
worker who moves from company A to company B is recognised at B and **renamed
automatically**, which is the core value of the feature. Recording who decided, when, and
on what grounds, because a guard weakened silently is this repository's most expensive
recurring failure -- and because the next reader needs to be able to tell a decision from a
regression.

## The rule now

The old rule said *no linkage may exist*. The new rule says *linkage exists, and discloses
exactly one thing*:

> a cross-tenant recognition may return the person's NAME and how well the voice matched.
> It may never return anything that says where that name came from -- which company, which
> recording, which site, which employer, which session, or even how many companies hold a
> profile for them.

So the shape of the guard moves from "the schema cannot express the question" to "the served
response cannot carry the answer". That is a weaker guarantee and it is worth being honest
about: it is enforced by code rather than by the impossibility of a join, and code can be
edited. Hence three independent guards rather than one, and hence this file asserts the
SERVED SHAPE, which is the thing an attacker or an accident actually reaches.

`voice_identities` is deliberately tenant-free -- it is not a customer's row, it is a
person's -- so the company-scoping assertions below continue to apply to `speaker_*` and
deliberately do not apply to it. What replaces them for the global tier is the allow-list.
"""
import os
import re

import pytest

vp = pytest.importorskip("repositories.voiceprints")

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
MIGRATIONS = os.path.join(ROOT, "src", "migrations")

#: Everything a cross-tenant recognition may say. A POSITIVE list, because the failure mode
#: is somebody adding a field, not somebody removing one -- so a new column must fail this
#: test rather than quietly travel.
ALLOWED_CROSS_TENANT_FIELDS = {
    "identity_id",            # opaque; identifies the person to nobody who does not already know
    "display_name",           # the disclosure this feature exists to make
    "score",                  # how well the voice matched, so a reader can disbelieve it
    "matched_sample_count",   # how much evidence stood behind that score
}

#: Named individually rather than as "anything not in the allow-list", because each one is a
#: specific thing somebody will be tempted to add for a specific good reason, and the reason
#: is never good enough. Any of these tells company B something about company A.
FORBIDDEN_CROSS_TENANT_FIELDS = {
    "origin_company_id", "company_id", "company_name", "employer_name", "employer_source",
    "origin_s3_key", "s3_key", "session_base", "turn_ref", "site_id", "user_id",
    "created_at", "consent_at", "company_count", "companies",
}


#: **The rule, stated once so it has somewhere to be cited from.**
#:
#: `pytest.importorskip` is for a dependency a test environment may LEGITIMATELY lack --
#: `psycopg`, `onnxruntime`, `docx`, `yaml`. It is never right for a module that lives in
#: this repository: the only ways such an import can fail are a wrong path or a moved file,
#: and both of those should be red.
#:
#: 226 files in `tests/` currently call `importorskip`, and nobody has audited which side of
#: that line each one falls on. Two known consequences, both found on 2026-09-23: four files
#: skip in CI for want of `PyYAML` (84 tests that had never run anywhere, one of them a guard
#: written after the 2026-08-08 unwired-switch incident that went red the first time it was
#: ever executed), and the two guards below skipped for want of a module that did not exist
#: yet -- while this file reported green.
_IMPORTORSKIP_RULE = (
    "importorskip is for third-party dependencies an environment may legitimately lack; "
    "a module from this repository is imported directly so a missing one fails")


def _voice_identities():
    """The global-tier repository, imported so that its ABSENCE fails rather than skips.

    `pytest.importorskip` is the house style in this suite and it is wrong here, for a
    reason that is specific rather than stylistic. Those two guards — the served-field
    allow-list, and "a company basis cannot enrol into the global registry" — are the entire
    enforcement of the boundary that replaced migration 0049's impossibility argument. Under
    `importorskip`, deleting `voice_identities.py`, renaming it, or breaking its import makes
    both of them **skip**, and a skipped guard is reported as a passing suite.

    That is not hypothetical: as written on 2026-09-22 these two tests used `importorskip`
    and the module did not exist yet, so the allow-list guard reported green **without ever
    running once**. A guard that has never run and a guard that does not exist look identical
    in every report.

    Failing here is also the correct state while the global tier is unbuilt: the boundary is
    not enforced yet, and the suite should say so out loud rather than stay quiet about it.
    """
    try:
        import repositories.voice_identities as vi  # noqa: PLC0415
    except ImportError as exc:  # pragma: no cover - the message IS the point
        raise AssertionError(
            "repositories.voice_identities is not importable, so the cross-tenant "
            "allow-list is enforced by nothing. If the global identity tier has not been "
            "built yet, that is the honest state of this boundary and this test is "
            f"supposed to be red. Do not convert it back to importorskip. ({exc})")
    return vi


def _all_sql():
    out = []
    for f in sorted(os.listdir(MIGRATIONS)):
        if f.endswith(".sql"):
            out.append((f, open(os.path.join(MIGRATIONS, f), encoding="utf-8").read()))
    return out


def test_every_speaker_table_is_company_scoped():
    """`company_id NOT NULL` on each of them, so an ATTRIBUTION row cannot exist outside a
    company even by mistake. A nullable column here would make "belongs to everybody"
    representable, and the whole point of the two-layer split is that attribution never is.

    Unchanged by the 2026-09-22 decision: the global tier is a separate table family
    (`voice_identit*`), and this assertion is exactly what keeps the two from blurring.
    """
    tables = {}
    for _f, sql in _all_sql():
        for m in re.finditer(r"CREATE TABLE (?:IF NOT EXISTS )?(speaker_\w+)\s*\((.*?)\n\);",
                             sql, re.S):
            tables[m.group(1)] = m.group(2)
    assert tables, "no speaker_* tables found; this test is reading the wrong place"
    for name, body in tables.items():
        assert re.search(r"company_id\s+uuid\s+NOT NULL", body), (
            f"{name} does not force a company; an attribution row outside a company is a "
            f"row nobody has a basis for")


def test_the_external_identity_is_unique_per_company_and_not_globally():
    """The sign-in id is the key that makes this work on a site, and it is also the obvious
    thing to make globally unique -- one row per person, tidy. That would be the leak: the
    same worker at two companies would collide into one ATTRIBUTION profile, and A's
    recordings would answer B's questions.

    Still true after the decision. Cross-company recognition happens through
    `voice_identities`, which carries a name and vectors and nothing else; it does not
    happen by merging two companies' `speaker_voiceprints` rows.
    """
    idx = [sql for _f, sql in _all_sql() if "speaker_voiceprints_external_ident" in sql]
    assert idx, "the external identity index is gone; nothing keeps sign-in ids apart"
    m = re.search(r"CREATE UNIQUE INDEX[^;]*speaker_voiceprints_external_ident(.*?);",
                  idx[0], re.S)
    assert "company_id" in m.group(1), (
        "the external identity is unique globally rather than per company, so one person "
        "signing in at two companies becomes one profile")


def test_no_attribution_table_pairs_two_companies_profiles():
    """The absence that still has to hold.

    What was reversed is "a person may be recognised across companies". What was NOT
    reversed is "company A's recordings, sites, employers and sessions may be reached from
    company B". A table pairing two `speaker_voiceprints` rows does exactly that: it hands
    whoever reads it a join from B's profile to A's attribution data.

    The sanctioned link is a single nullable `identity_id` on `speaker_voiceprints` pointing
    at the tenant-free `voice_identities`. That direction is one-way by construction -- an
    identity does not enumerate its profiles -- which is why it is allowed and a pairing
    table is not.
    """
    offenders = []
    for f, sql in _all_sql():
        for m in re.finditer(r"CREATE TABLE (?:IF NOT EXISTS )?(\w+)\s*\((.*?)\n\);", sql, re.S):
            name, body = m.group(1), m.group(2)
            if len(re.findall(r"REFERENCES speaker_voiceprints", body)) >= 2:
                offenders.append(f"{f}:{name} references speaker_voiceprints twice -- a pair "
                                 f"of company profiles is a join into the other tenant")
            if re.search(r"\bidentity_graph\b|\bprofile_pair\w*\b", body, re.I):
                offenders.append(f"{f}:{name} carries a profile-to-profile mapping")
    assert not offenders, "; ".join(offenders)


def test_a_cross_tenant_match_serves_exactly_the_allow_list():
    """The guard that replaces the old impossibility argument.

    `identity_match_row` is the ONE function that builds what company B is told about a
    person recognised from the global registry. This asserts its key set is exactly the
    allow-list -- not a subset, not a superset.

    Equality rather than "no forbidden key present" is deliberate. A subset check passes
    when somebody adds a field nobody thought to forbid, which is precisely how a field
    nobody thought about is the one that leaks.
    """
    vi = _voice_identities()
    row = {
        "id": "11111111-1111-1111-1111-111111111111",
        "display_name": "Ben",
        "consent_state": "granted",
        "consent_at": "2026-09-22T00:00:00Z",
        "origin_company_id": "22222222-2222-2222-2222-222222222222",
        "origin_s3_key": "users/Ben_UCPK2/audio/2026-08-13/x.wav",
        "employer_name": "ABC Ltd",
        "sample_count": 4,
        "score": 0.58,
    }
    served = vi.identity_match_row(row)
    assert set(served) == ALLOWED_CROSS_TENANT_FIELDS, (
        f"the cross-tenant shape changed: "
        f"added {set(served) - ALLOWED_CROSS_TENANT_FIELDS or '{}'}, "
        f"missing {ALLOWED_CROSS_TENANT_FIELDS - set(served) or '{}'}. Every field here "
        f"crosses a tenant boundary; adding one is a disclosure decision, not a refactor")


def test_the_cross_tenant_row_is_built_field_by_field_not_filtered():
    """HOW it is built, not only what comes out.

    A function that copies a row and deletes the bad keys passes the test above and leaks the
    moment a new column appears upstream -- the deny-list failure, one level down. Building
    the dict literally means an unknown column has no path out.

    Asserted on the source because the behavioural difference only shows up with a column
    that does not exist yet, which is exactly the one a test cannot construct.
    """
    src = open(os.path.join(ROOT, "src", "repositories", "voice_identities.py"),
               encoding="utf-8").read()
    body = src[src.index("def identity_match_row"):]
    body = body[:body.index("\ndef ", 1)] if "\ndef " in body[1:] else body
    for banned in ("dict(row)", "row.copy()", "**row", ".pop(", "del "):
        assert banned not in body, (
            f"identity_match_row uses {banned!r}: it is filtering a row rather than building "
            f"one. A column added upstream would travel by default, which is the failure the "
            f"allow-list exists to prevent")


def test_no_query_reads_attribution_profiles_without_a_company():
    """Every read of the ATTRIBUTION table names a company. One that does not would return
    another company's people, and the failure is silent -- more candidates, better-looking
    matches, and no error anywhere."""
    src = open(os.path.join(ROOT, "src", "repositories", "voiceprints.py"),
               encoding="utf-8").read()
    bad = []
    for m in re.finditer(r'"(SELECT[^"]*speaker_voiceprints[^"]*)"', src):
        stmt = m.group(1)
        start = src.rfind("cur.execute(", 0, m.start())
        call = src[start:src.index(").fetchone()", m.start())
                   if ").fetchone()" in src[m.start():m.start() + 1200]
                   else m.end()]
        if "company_id" not in call:
            bad.append(stmt[:70])
    assert not bad, f"reads without a company filter: {bad}"


def test_the_global_tier_never_reaches_the_company_library_listing():
    """`GET /api/org/voiceprints` is the company's own library and stays company-scoped.

    The tempting change is to enrich that listing with "also known at 2 other companies", or
    to let it resolve names through the global tier. Either turns a per-tenant listing into
    the cross-company answer the allow-list refuses to give -- by a different route, through
    a function nobody thinks of as the disclosure surface.
    """
    src = open(os.path.join(ROOT, "src", "repositories", "voiceprints.py"),
               encoding="utf-8").read()
    body = src[src.index("def list_profiles"):]
    assert "voice_identity_samples" not in body and "voice_identities" not in body, (
        "list_profiles reaches the global identity tables; the company library must answer "
        "only from this company's own rows")


def test_a_company_that_has_settled_no_basis_enrols_nobody():
    """The fallback is the strict rule, not a permissive one. A company with no configured
    basis must behave exactly as the system did before any of this existed.

    Untouched by the 2026-09-22 decision, and load-bearing for it: a company-level basis is
    what permits an ATTRIBUTION profile. It is explicitly NOT enough to put a vector in the
    global registry, which needs the subject's own person-level consent
    (`voice_identities.consent_state`).
    """
    import lambda_org_api as org
    src = open(os.path.join(ROOT, "src", "lambda_org_api.py"), encoding="utf-8").read()
    assert 'company_basis = caller.get("voiceprint_consent_basis")' in src, (
        "the basis is no longer taken from the caller's company; wherever it comes from "
        "now, it must still be a COMPANY fact and not something this request supplies")
    assert re.search(r"attest\s*=\s*\(ENROL_ON_CORRECTION and company_basis", src), (
        "the company's basis is not required for the attested path, so a company that "
        "settled nothing would still enrol")
    assert org.ENROL_ON_CORRECTION in (True, False)


def test_a_company_basis_alone_cannot_enrol_into_the_global_registry():
    """The one place migration 0049's original argument survives intact.

    A company's induction and subcontract can settle that IT may hold a voice. They cannot
    settle that every other customer of this platform may recognise that person -- nobody at
    that induction was told so. So the global tier requires `consent_state = 'granted'` on
    the person's own identity row, and a company basis is never accepted in its place.
    """
    vi = _voice_identities()
    src = open(os.path.join(ROOT, "src", "repositories", "voice_identities.py"),
               encoding="utf-8").read()
    assert "voiceprint_consent_basis" not in src, (
        "the global registry reads the COMPANY's consent basis. A company cannot consent on "
        "behalf of a person to being recognised by other companies")
    assert hasattr(vi, "identities_for_matching"), (
        "no gated read path for the global tier; matching would have to write its own query "
        "and the consent filter would live in the caller, where its failure is invisible")
