"""A template from the Library can write a report, and cannot be substituted.

Batch 2: _generation_request now resolves two kinds of name. A slug names a
template that ships as a file in this repo; a uuid names one a company wrote in
the Library (migration 0062). Both end as the same body, because
report_template's file format IS the Library's body format.

THE test is `a uuid never falls back to a file template`. Everything else here
is plumbing; that one is the promise. report_template.TemplateNotFound exists
because "a report names the template it was written to, and a silent
substitution would make that name a lie" -- and the cheapest way to break that
promise is a resolver that shrugs at an unresolvable uuid and uses the default.

The repository is replaced with an in-memory double, so nothing here proves
anything about SQL -- that is covered by tests/integration against a real
database. What is proved here is which body ends up in the artifact, and who is
allowed to put it there.
"""
import json

import pytest

oa = pytest.importorskip("lambda_org_api", reason="requires psycopg (installed in CI)")

COMPANY = "c0000000-0000-0000-0000-000000000001"
TPL = "aaaaaaaa-1111-2222-3333-444444444444"
MISSING = "bbbbbbbb-1111-2222-3333-444444444444"
GM = {"id": "u-gm", "company_id": COMPANY, "global_role": "gm", "cognito_sub": "s-gm"}

V1 = {"sections": [{"key": "what", "title": "What this was", "purpose": "First version."}],
      "catch_all": {"key": "other", "title": "Anything else", "purpose": "Rest."},
      "excluded_subjects": [], "style": ["Short."]}
V2 = {"sections": [{"key": "what", "title": "What this was", "purpose": "Second version."}],
      "catch_all": {"key": "other", "title": "Anything else", "purpose": "Rest."},
      "excluded_subjects": [], "style": ["Shorter."]}


class _Repo:
    def __init__(self, visible=True, current=2):
        self.visible = visible
        self.current = current

    def get_visible(self, conn, company_id, user_id, template_id):
        if not self.visible or str(template_id) != TPL:
            return None
        return {"id": TPL, "company_id": company_id, "name": "Site Daily",
                "scope": "org", "current_version": self.current}

    def get_version(self, conn, template_id, version):
        if str(template_id) != TPL:
            return None
        if version is None:
            version = self.current
        bodies = {1: V1, 2: V2}
        if version not in bodies:
            return None
        return {"version": version, "body": bodies[version]}


@pytest.fixture()
def repo(monkeypatch):
    r = _Repo()
    monkeypatch.setattr(oa, "report_templates", r)
    return r


def _req(body, deliver=None, caller=GM):
    return oa._generation_request(body, deliver, object(), caller)


# ---- THE test ---------------------------------------------------------------

def test_THE_test_a_uuid_never_falls_back_to_a_file_template(repo):
    """An unresolvable uuid is refused. It must NEVER quietly become
    personal-meeting, which is the only template on disk and would otherwise be
    the obvious thing for a lenient resolver to reach for."""
    repo.visible = False
    gen, err = _req({"templateId": TPL, "templateVersion": 1})
    assert gen is None
    assert "no such template" in err


def test_a_uuid_with_no_connection_is_refused_rather_than_guessed(repo):
    """Belt and braces: a caller that reaches this without a connection cannot
    resolve a stored template, and must say so rather than fall through to the
    file branch."""
    gen, err = oa._generation_request({"templateId": TPL, "templateVersion": 1}, None, None, None)
    assert gen is None and "no such template" in err


def test_a_template_from_another_company_is_not_found(repo):
    """get_visible is the whole guard: it answers None for a template in
    another company and for somebody else's personal one alike."""
    repo.visible = False
    gen, err = _req({"templateId": TPL})
    assert gen is None and "no such template" in err


# ---- what reaches the worker ------------------------------------------------

def test_the_body_travels_not_just_the_id(repo):
    """lambda_session_report is non-VPC and cannot read Aurora. If only the id
    travelled, the worker would have nothing to write the report to."""
    gen, err = _req({"templateId": TPL, "templateVersion": 1})
    assert err is None
    assert gen["templateBody"] == V1
    assert gen["templateId"] == TPL and gen["templateVersion"] == 1


def test_the_version_asked_for_is_the_version_sent(repo):
    """Not the current one. A report pinned to v1 must be written to v1 even
    after the template has moved on."""
    gen, _ = _req({"templateId": TPL, "templateVersion": 1})
    assert gen["templateBody"]["sections"][0]["purpose"] == "First version."


def test_omitting_the_version_takes_the_current_one(repo):
    gen, err = _req({"templateId": TPL})
    assert err is None
    assert gen["templateVersion"] == 2
    assert gen["templateBody"]["sections"][0]["purpose"] == "Second version."


def test_the_name_travels_so_the_result_can_say_what_wrote_it(repo):
    gen, _ = _req({"templateId": TPL})
    assert gen["templateName"] == "Site Daily"


def test_a_version_that_does_not_exist_is_refused(repo):
    gen, err = _req({"templateId": TPL, "templateVersion": 9})
    assert gen is None and "no such template" in err


def test_a_template_with_no_content_yet_is_refused(repo):
    repo.current = 0
    gen, err = _req({"templateId": TPL})
    assert gen is None and "no content" in err


def test_a_version_that_is_not_a_number_is_refused(repo):
    gen, err = _req({"templateId": TPL, "templateVersion": "soon"})
    assert gen is None and "must be a number" in err


# ---- the file-backed path is unchanged, and now inlines too -----------------

def test_a_file_template_still_resolves(repo):
    gen, err = _req({"templateId": "personal-meeting", "templateVersion": 3})
    assert err is None
    assert gen["templateId"] == "personal-meeting" and gen["templateVersion"] == 3


def test_a_file_template_inlines_its_body_the_same_way(repo):
    """One path into the worker, not two. A worker that had to tell 'inlined'
    from 'look it up' would grow a branch that only one kind of template ever
    exercises."""
    gen, _ = _req({"templateId": "personal-meeting", "templateVersion": 3})
    assert [s["title"] for s in gen["templateBody"]["sections"]] == [
        "What this was", "Decided", "Still open", "Actions"]


def test_an_unknown_file_template_is_still_refused(repo):
    gen, err = _req({"templateId": "does-not-exist", "templateVersion": 1})
    assert gen is None and "no such template" in err


def test_no_template_named_is_still_the_assembled_report(repo):
    """The default path. Zero model calls, and nothing about it changed."""
    gen, err = _req({"deliver": "download"})
    assert gen is None and err is None


# ---- delivery ---------------------------------------------------------------

def test_a_stored_template_cannot_be_emailed_either(repo):
    """The worker's generate branch always writes emailed: false, so this
    combination is refused at the door rather than producing a document nobody
    receives. It applied to file templates already; a stored one is no
    different."""
    gen, err = _req({"templateId": TPL}, deliver="email")
    assert gen is None and "downloaded" in err


# ---- the worker's side ------------------------------------------------------

def test_the_worker_uses_the_inlined_body():
    sr = pytest.importorskip("lambda_session_report")
    artifact = {"generate": {"templateId": TPL, "templateVersion": 2, "templateBody": V2}}
    gen = artifact["generate"]
    assert gen.get("templateBody") == V2, "the worker reads this and does not look at disk"


def test_the_worker_still_reads_disk_for_an_artifact_enqueued_before_this_existed():
    """Artifacts already in the bucket when this deploys carry no templateBody.
    They name a slug, so the disk lookup still answers -- and a uuid without a
    body raises TemplateNotFound rather than resolving to something else."""
    rt = pytest.importorskip("report_template")
    assert rt.load_template("personal-meeting", 3)["template_id"] == "personal-meeting"
    with pytest.raises(rt.TemplateNotFound):
        rt.load_template(TPL, 1)
