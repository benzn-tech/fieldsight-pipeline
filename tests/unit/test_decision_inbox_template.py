"""Decision card inbox (P1 web route) is wired in the template and workflows, or it is not.

Ways this deploys green and does the wrong thing:
- a parameter default other than 'off' (the routes would serve cards the moment the stack
  updates), or an AllowedValue such as 'internal' (on org-api ACTION_PACKS=internal already
  means platform_admin-only -- the inbox uses 'companies');
- the company allow-list accepting spaces or free text (the route splits on ',');
- the env reaching a function other than OrgApiFunction;
- the workflows not passing the flag: SAM keeps a stack's previous parameter value, so only
  the workflow variable sets it; and a bare "DecisionInboxCompanies=" breaks
  --parameter-overrides, so the companies value is appended only when the variable is set.

Spec: docs/superpowers/specs/2026-11-12-decision-card-inbox-design.md (rev 2) §2.
"""
import io
import json
import os

import pytest

_yaml = pytest.importorskip("yaml")

REPO = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
TEMPLATE = os.path.join(REPO, "src", "template.yaml")
WORKFLOWS = {"TEST": os.path.join(REPO, ".github", "workflows", "deploy.yml"),
             "PROD": os.path.join(REPO, ".github", "workflows", "deploy-prod.yml")}


class _Loader(_yaml.SafeLoader):
    pass


def _tagged(loader, suffix, node):
    if isinstance(node, _yaml.ScalarNode):
        return {suffix: loader.construct_scalar(node)}
    if isinstance(node, _yaml.MappingNode):
        return {suffix: loader.construct_mapping(node, deep=True)}
    return {suffix: loader.construct_sequence(node, deep=True)}


_Loader.add_multi_constructor("!", _tagged)


@pytest.fixture(scope="module")
def doc():
    with io.open(TEMPLATE, encoding="utf-8") as f:
        return _yaml.load(f.read(), Loader=_Loader)


def test_decision_inbox_params_pinned(doc):
    p = doc["Parameters"]["DecisionInbox"]
    assert p["Type"] == "String"
    assert p["Default"] == "off"
    assert p["AllowedValues"] == ["off", "companies"]
    c = doc["Parameters"]["DecisionInboxCompanies"]
    assert c["Type"] == "String"
    assert c["Default"] == ""
    assert c["AllowedPattern"] == "^$|^[0-9a-fA-F-]+(,[0-9a-fA-F-]+)*$"


def test_decision_inbox_env_only_on_org_api(doc):
    env = doc["Resources"]["OrgApiFunction"]["Properties"]["Environment"]["Variables"]
    assert env["DECISION_INBOX"] == {"Ref": "DecisionInbox"}
    assert env["DECISION_INBOX_COMPANIES"] == {"Ref": "DecisionInboxCompanies"}
    for name, res in doc["Resources"].items():
        if name == "OrgApiFunction":
            continue
        text = json.dumps(res)
        assert "DECISION_INBOX" not in text, name
        assert '"DecisionInbox"' not in text and '"DecisionInboxCompanies"' not in text, name


@pytest.mark.parametrize("env", sorted(WORKFLOWS))
def test_workflows_pass_decision_inbox(env):
    text = io.open(WORKFLOWS[env], encoding="utf-8").read()
    assert "\"DecisionInbox=${{ vars.%s_DECISION_INBOX || 'off' }}\"" % env in text
    # companies: the conditional _PARAM shape used for ACTION_PACKS_PARAM
    guard = ('DECISION_INBOX_COMPANIES_PARAM=""\n'
             '          if [ -n "${{ vars.%s_DECISION_INBOX_COMPANIES }}" ]; then\n'
             '            DECISION_INBOX_COMPANIES_PARAM='
             '"DecisionInboxCompanies=${{ vars.%s_DECISION_INBOX_COMPANIES }}"\n'
             '          fi') % (env, env)
    assert guard in text
    assert "$DECISION_INBOX_COMPANIES_PARAM \\" in text
    # never a bare, unconditional companies override
    assert text.count("DecisionInboxCompanies=") == 1
