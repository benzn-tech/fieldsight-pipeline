"""Unit: the correction endpoint carries an employer, and says when it could not store one.

The endpoint's job is naming a speaker. The employer rides along, and everything here is
about making sure it can never damage that:

- **it is optional and stays optional.** No register, no employer, and the correction that
  builds the voiceprint must still work.
- **it cannot fail the correction.** A bad employer is a 400 before anything is written; a
  correction with no employer is untouched.
- **and when there is nowhere to put it, the caller is told.** That is the case worth the
  code: `upsert_profile` runs only inside the consent branch, so a company that has settled
  no basis, correcting without `consent_given`, creates no profile at all — and today that is
  the common case. Returning a bare `null` would make "we stored it" and "there was nowhere
  to put it" the same answer to somebody who had just typed one.
"""
import json

import pytest

org = pytest.importorskip("lambda_org_api")

CO = "6a23c57c-5fa3-4ef4-a93c-88e9543272fc"
SESSION = "ben_2026-08-12_16-50-28_sid" + "b" * 32


class _Conn:
    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def cursor(self, row_factory=None):
        return self

    def execute(self, sql, params=None):
        return self

    def fetchone(self):
        return None

    def fetchall(self):
        return []


def _caller():
    return {"id": "u-1", "company_id": CO, "global_role": "site_manager",
            "cognito_sub": "sub-1", "folder_name": "Ben"}


def _body(**kw):
    b = {"user": "Ben", "display_name": "Andy M",
         "source_filename": "ben_2026-08-12_16-52-24_sidbbbb_c0004_off0.0_to114.0_srcwav.json",
         "start_sec": 3.16, "end_sec": 112.66}
    b.update(kw)
    return {"body": json.dumps(b)}


@pytest.fixture(autouse=True)
def _wired(monkeypatch):
    monkeypatch.setattr(org, "SPEAKER_IDENTITY_MODE", "on")
    monkeypatch.setattr(org, "_resolve_org_media_folder",
                        lambda conn, caller, user, what="media": (user or "Ben", None))
    monkeypatch.setattr(org, "_may_correct_speakers", lambda conn, caller, folder: True)
    monkeypatch.setattr(org, "_same_company_as_folder",
                        lambda conn, caller, folder, what: None)
    monkeypatch.setattr(org, "_session_turns", lambda conn, f, d, s: [])
    class _S3:
        def put_object(self, **kw):
            return {}

    monkeypatch.setattr(org, "s3", lambda: _S3())


def _res(monkeypatch, **body):
    return org.speaker_corrections(_Conn(), _caller(), SESSION, _body(**body))


# ---- the field cannot damage the correction ------------------------------


def test_a_correction_without_an_employer_is_unchanged(monkeypatch):
    res = _res(monkeypatch)
    assert res["statusCode"] in (202, 409)
    if res["statusCode"] == 202:
        assert json.loads(res["body"])["employer"] is None


def test_a_name_without_a_source_is_refused_before_anything_is_written(monkeypatch):
    """400, not a silent drop and not a partial write. The pair is what makes the value
    auditable, and half of it records nothing anybody can act on."""
    res = _res(monkeypatch, employer_name="ABC Ltd")
    assert res["statusCode"] == 400
    assert "travel together" in json.loads(res["body"])["error"]


def test_a_source_without_a_name_is_refused(monkeypatch):
    res = _res(monkeypatch, employer_source="typed")
    assert res["statusCode"] == 400


def test_an_unknown_source_is_refused(monkeypatch):
    res = _res(monkeypatch, employer_name="ABC Ltd", employer_source="guessed")
    assert res["statusCode"] == 400
    assert "employer_source" in json.loads(res["body"])["error"]


def test_employer_ref_is_refused_rather_than_ignored(monkeypatch):
    """A field one side sends and the other silently drops is this repository's most-repeated
    failure. Accepting it now would 202 and lose the value until the Sign On Site adapter
    exists — and nobody would find out in between."""
    res = _res(monkeypatch, employer_name="ABC Ltd", employer_source="typed",
               employer_ref="SOS-1234")
    assert res["statusCode"] == 400
    assert "employer_ref" in json.loads(res["body"])["error"]


# ---- the answer when there is nowhere to store it -------------------------


def test_an_employer_with_no_profile_says_so_instead_of_returning_null(monkeypatch):
    """The case the helper exists for, and today the common one: no `consent_given`, and a
    company that has settled no basis, so no profile row is created."""
    monkeypatch.setattr(org, "ENROL_ON_CORRECTION", False)
    res = _res(monkeypatch, employer_name="ABC Ltd", employer_source="typed")
    assert res["statusCode"] in (202, 409)
    if res["statusCode"] == 202:
        emp = json.loads(res["body"])["employer"]
        assert emp is not None and emp["stored"] is False
        assert "nowhere" in emp["reason"] or "no voiceprint profile" in emp["reason"]


def test_the_helper_distinguishes_all_three_outcomes():
    """Driven directly, because the three answers are the design and the endpoint has other
    reasons to short-circuit."""
    assert org._employer_result(None, None, None) is None
    refused = org._employer_result("ABC Ltd", "typed", None)
    assert refused["stored"] is False and "reason" in refused
    stored = org._employer_result("ABC Ltd", "typed",
                                  {"employer_name": "ABC Ltd", "employer_source": "typed"})
    assert stored == {"stored": True, "name": "ABC Ltd", "source": "typed"}


def test_the_stored_answer_is_read_back_not_echoed():
    """`upsert_profile` returns the row as it was FOUND and updates the employer afterwards,
    so the object in hand carries the PREVIOUS employer. Echoing the request would report a
    write that may not have happened; reading the row reports what is there."""
    stored = org._employer_result("ABC Ltd", "typed",
                                  {"employer_name": "XYZ Ltd", "employer_source": "typed"})
    assert stored["name"] == "XYZ Ltd", (
        "the response echoed the request instead of the row; a failed or refused write would "
        "report success")


def test_the_source_list_is_the_repository_s_list():
    """One list, two files. The last time an enum lived in two places, one side sent `notice`
    and the other understood only `attestation`."""
    import repositories.voiceprints as vp

    src = open("src/lambda_org_api.py", encoding="utf-8").read()
    assert "voiceprints.EMPLOYER_SOURCES" in src, (
        "the endpoint restates the allowed sources instead of reading them; the two lists "
        "will drift and the 400 will read as a configuration problem")
    assert vp.EMPLOYER_SOURCES
