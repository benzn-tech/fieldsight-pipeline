"""Unit: what we already know about a name — and the guess this endpoint refuses to make.

The first version of this plan had a `/employer-suggestions` route ranking candidates out of
`findings.entity_name`, so the UI could ask *"Please confirm Andy M is from ABC?"*. Prod shows
what that column actually holds:

    Jerry / PK Building | Client        Troy and Jay | -
    facade subbie       | facade        Zoe          | Rebar

People, groups, roles. **"Zoe | Rebar" says Zoe does rebar** — not that she works for a firm
called Rebar. Putting that behind a confirm prompt turns a trade into a company with one click,
and the click stamps it `employer_source: "suggested"`, which is a record.

So this endpoint answers one question — *has somebody here already told us?* — and returns
`null` when nobody has. `trade` rides along as grey helper text under its own key, never as the
answer, and these tests are mostly about keeping those two apart.
"""
import json

import pytest

org = pytest.importorskip("lambda_org_api")

CO = "6a23c57c-5fa3-4ef4-a93c-88e9543272fc"
OTHER = "99999999-9999-9999-9999-999999999999"
SITE = "11111111-2222-3333-4444-555555555555"


def _caller(company=CO):
    return {"id": "u-1", "company_id": company, "global_role": "site_manager",
            "cognito_sub": "sub-1", "folder_name": "Ben"}


def _event(**qs):
    return {"queryStringParameters": qs or None}


@pytest.fixture(autouse=True)
def _on(monkeypatch):
    monkeypatch.setattr(org, "SPEAKER_IDENTITY_MODE", "on")


def _wire(monkeypatch, known=None, trade=None, site_owner=CO):
    monkeypatch.setattr(org.voiceprints, "employer_for_name",
                        lambda conn, co, name: known)
    monkeypatch.setattr(org.findings, "trade_heard_for",
                        lambda conn, co, name, site=None: trade)
    monkeypatch.setattr(org.sites, "get_site",
                        lambda conn, sid: {"id": sid, "company_id": site_owner})



def _code(fn):
    """Function source with the docstring and comments removed.

    Twice now a source-scanning assertion has tripped on prose that NAMES the thing it
    forbids — these docstrings say "not `ILIKE`" and "not through `users`" precisely because
    those are the wrong answers. A scan that cannot tell an explanation from an instruction
    reports the explanation.
    """
    import ast
    import inspect
    import textwrap

    tree = ast.parse(textwrap.dedent(inspect.getsource(fn)))
    node = tree.body[0]
    if (node.body and isinstance(node.body[0], ast.Expr)
            and isinstance(node.body[0].value, ast.Constant)):
        node.body = node.body[1:]          # drop the docstring
    return ast.unparse(node)


def test_nobody_has_said_so_the_answer_is_null(monkeypatch):
    """The default, and the thing the whole design turns on. An empty field is a correct
    answer; a guess is not."""
    _wire(monkeypatch)
    res = org.speaker_known(object(), _caller(), _event(name="Andy M"))
    assert res["statusCode"] == 200
    assert json.loads(res["body"]) == {"known": None, "trade": None}


def test_a_recorded_employer_comes_back_with_its_provenance(monkeypatch):
    """`source` travels with the value everywhere. A UI that shows "ABC Ltd" without knowing
    whether a human typed it or a register supplied it cannot present it honestly."""
    _wire(monkeypatch, known={"id": "vp-1", "employer_name": "ABC Ltd",
                              "employer_source": "typed", "samples": 3})
    body = json.loads(org.speaker_known(object(), _caller(), _event(name="Andy M"))["body"])
    assert body["known"] == {"employer": "ABC Ltd", "source": "typed",
                             "profileId": "vp-1", "samples": 3}


def test_the_trade_is_never_offered_as_the_employer(monkeypatch):
    """The whole reason this endpoint is smaller than the first draft. A trade under `known`
    would be pre-filled by any reasonable UI, and the confirm click would record it."""
    _wire(monkeypatch, known=None, trade="Rebar")
    body = json.loads(org.speaker_known(object(), _caller(), _event(name="Zoe"))["body"])
    assert body["known"] is None, "a trade was returned as a known employer"
    assert body["trade"] == "Rebar"


def test_a_trade_does_not_suppress_a_real_answer(monkeypatch):
    """Both keys are independent. The two came from different questions and neither one's
    presence says anything about the other's."""
    _wire(monkeypatch, known={"id": "vp-1", "employer_name": "ABC Ltd",
                              "employer_source": "sign_on_site", "samples": 0},
          trade="Rebar")
    body = json.loads(org.speaker_known(object(), _caller(), _event(name="Zoe"))["body"])
    assert body["known"]["employer"] == "ABC Ltd"
    assert body["trade"] == "Rebar"


def test_the_feature_switch_hides_it(monkeypatch):
    """`off` is the rollback, and a rollback that leaves an endpoint answering is not one.
    The wiring below is deliberate: everything would succeed, so a 404 can only come from the
    gate — with no wiring, "not found" and "nothing found" are the same 404."""
    _wire(monkeypatch, known={"id": "vp-1", "employer_name": "ABC Ltd",
                              "employer_source": "typed", "samples": 1})
    monkeypatch.setattr(org, "SPEAKER_IDENTITY_MODE", "off")
    assert org.speaker_known(object(), _caller(), _event(name="Andy M"))["statusCode"] == 404


def test_a_name_is_required(monkeypatch):
    _wire(monkeypatch)
    assert org.speaker_known(object(), _caller(), _event())["statusCode"] == 400
    assert org.speaker_known(object(), _caller(), _event(name="   "))["statusCode"] == 400


def test_another_company_s_site_is_not_reachable_by_guessing_a_uuid(monkeypatch):
    """404 for both "no such site" and "somebody else's site". Distinguishing them would make
    the endpoint an oracle for which uuids exist."""
    _wire(monkeypatch, site_owner=OTHER)
    res = org.speaker_known(object(), _caller(), _event(name="Andy M", site=SITE))
    assert res["statusCode"] == 404


def test_the_site_filter_reaches_the_trade_lookup(monkeypatch):
    """Otherwise the parameter is accepted, validated, and then quietly ignored — the field
    that looks like it narrows and does not."""
    seen = {}
    monkeypatch.setattr(org.voiceprints, "employer_for_name", lambda conn, co, name: None)
    monkeypatch.setattr(org.findings, "trade_heard_for",
                        lambda conn, co, name, site=None: seen.setdefault("site", site))
    monkeypatch.setattr(org.sites, "get_site",
                        lambda conn, sid: {"id": sid, "company_id": CO})
    org.speaker_known(object(), _caller(), _event(name="Zoe", site=SITE))
    assert seen["site"] == SITE


def test_the_route_is_wired():
    """A handler nothing dispatches to is the shape this session has removed seven times."""
    src = open("src/lambda_org_api.py", encoding="utf-8").read()
    assert '"/speakers/known" and method == "GET"' in src
    assert "return speaker_known(conn, caller, event)" in src


def test_the_lookup_is_company_pinned_and_exact():
    """Two failures a double cannot see, asserted on the SQL.

    An unpinned lookup answers with another tenant's row; `ILIKE '%name%'` lets "Andy M"
    inherit "Andy Mason"'s employer, which is one person's details reaching another's record.
    """
    import repositories.voiceprints as vp

    body = _code(vp.employer_for_name)
    assert "company_id = %s" in body
    assert "lower(display_name) = lower(%s)" in body, "the name match is not exact"
    assert "ILIKE" not in body
    assert "status <> 'withdrawn'" in body, (
        "a withdrawn profile still answers; withdrawal that keeps answering is not withdrawal")


def test_the_trade_query_reaches_the_tenant_in_one_hop():
    """`findings` has no user column. Reaching the tenant through `users` — which the first
    draft of the plan proposed, citing a rule about `topics` — is not possible here and would
    silently drop every NULL-author row if it were."""
    import repositories.findings as f

    body = _code(f.trade_heard_for)
    assert "JOIN sites s ON s.id = f.site_id" in body
    assert "s.company_id = %s" in body
    assert "users" not in body
