"""Unit: naming a speaker in another company's recording is refused, not re-filed.

Two rules, each correct on its own, compose into a leak.

`_resolve_org_media_folder` does not pin a cross-company caller to a company — it calls that
branch *"the SOLE branch NOT pinned to caller.company_id"* — and for READING that is the role
working as designed. `speaker_corrections` takes the company from the caller and never from
the body, and that is also right: a body-supplied company would let one tenant queue work
against another's profiles.

Put them together and a platform operator naming a speaker in customer B's recording stores
**B's voiceprint under their own company**. This is measured rather than argued: TEST carries
such a row today, created by exactly that path.

The requirement it breaks is the one the product owner stated in the strongest terms — a
voiceprint built for company A must not become company B's, *"because this information was
established to help company A"*. `test_no_cross_company_voice_identity.py` asserts the schema
cannot LINK two companies' profiles; this file asserts the write path cannot CREATE a profile
in the wrong one. The schema half was already there and it did not help, because nothing here
was linking anything — it was filing a row under a company that had no claim on it.
"""
import json

import pytest

org = pytest.importorskip("lambda_org_api")

MINE = "6a23c57c-5fa3-4ef4-a93c-88e9543272fc"
THEIRS = "7a495d8a-c88a-43ea-bf5b-a6d1c89beb92"


def _caller(company=MINE, role="platform_admin"):
    return {"id": "u-1", "company_id": company, "global_role": role,
            "cognito_sub": "sub-1", "folder_name": "Ben_Lin"}


def _body(**kw):
    b = {"user": "Ben_UCPK2", "display_name": "Ben Lin",
         "source_filename": "ben_ucpk2_2026-08-12_16-52-24_sidbe41_c0004_off0.0_to114.0_srcwav.json",
         "start_sec": 10, "end_sec": 18}
    b.update(kw)
    return {"body": json.dumps(b)}


SESSION = "ben_ucpk2_2026-08-12_16-50-28_sidbe4190877c1d47f9b848a25cf4ca729a"


@pytest.fixture(autouse=True)
def _on(monkeypatch):
    monkeypatch.setattr(org, "SPEAKER_IDENTITY_MODE", "on")
    monkeypatch.setattr(org, "_resolve_org_media_folder",
                        lambda conn, caller, user, what="media": (user or "Ben_Lin", None))
    monkeypatch.setattr(org, "_may_correct_speakers", lambda conn, caller, folder: True)


def _owner(company):
    """A users row for the target folder, owned by `company`."""
    return {"id": "u-2", "company_id": company, "folder_name": "Ben_UCPK2",
            "global_role": "worker"}


def test_a_cross_company_recording_is_refused(monkeypatch):
    """The measured case, in both directions of the pair that produced it: an ALL-scope
    caller who legitimately resolved the folder, and a company that is not theirs."""
    monkeypatch.setattr(org.users, "get_by_folder_name_global",
                        lambda conn, folder: _owner(THEIRS))
    res = org.speaker_corrections(object(), _caller(), SESSION, _body())
    assert res["statusCode"] == 403
    assert "another company" in json.loads(res["body"])["error"]


def test_the_match_route_is_guarded_too(monkeypatch):
    """`speaker_match` resolves the same folder through the same ACL and writes an artifact
    scoped to the caller's company. Guarding only the enrolment route would leave the other
    door open, which is how this repository's last four leaks stayed open after their fix."""
    monkeypatch.setattr(org.users, "get_by_folder_name_global",
                        lambda conn, folder: _owner(THEIRS))
    res = org.speaker_match(object(), _caller(), SESSION, {"body": json.dumps({"user": "Ben_UCPK2"})})
    assert res["statusCode"] == 403


def test_an_in_company_recording_is_untouched(monkeypatch):
    """The guard must not become a second, stricter ACL. Same company: it does not refuse,
    and whatever the endpoint does next is the endpoint's business — asserted as "not 403"
    rather than as a specific success, so this test does not pin unrelated behaviour."""
    monkeypatch.setattr(org.users, "get_by_folder_name_global",
                        lambda conn, folder: _owner(MINE))
    assert org._same_company_as_folder(object(), _caller(), "Ben_UCPK2", "x") is None


def test_an_unknown_folder_passes_and_says_so(monkeypatch, caplog):
    """Device folders with no `users` row exist and corrections on them are legitimate, so
    the guard fails OPEN — deliberately, and out loud. A silent pass here would be
    indistinguishable from a checked one, which is the exact shape that let the leak run."""
    monkeypatch.setattr(org.users, "get_by_folder_name_global", lambda conn, folder: None)
    with caplog.at_level("WARNING"):
        assert org._same_company_as_folder(object(), _caller(), "SomeDevice", "x") is None
    assert any("cannot be checked" in r.getMessage() for r in caplog.records), (
        "the unchecked case left no trace, so 'same company' and 'no company to compare' "
        "look identical in the logs")


def test_the_lookup_is_the_global_one():
    """`get_by_folder_name` is company-scoped: asking it about another company's folder
    returns None, which this guard reads as 'unknown' and passes. Using it here would make
    the guard agree with every cross-company request it exists to refuse."""
    src = open("src/lambda_org_api.py", encoding="utf-8").read()
    fn = src[src.index("def _same_company_as_folder"):]
    fn = fn[:fn.index("\ndef ")]
    assert "get_by_folder_name_global" in fn
