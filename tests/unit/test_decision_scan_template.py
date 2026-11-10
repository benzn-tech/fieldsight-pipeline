"""Site decision loop P0 (shadow) is wired in the template, or it is not wired.

Ways this deploys green and does nothing, or does too much:
- the function created while the flag is off (the default), or created without the
  database (HasDb) it needs;
- a schedule without its `scan` input, so the handler raises on every tick;
- a missing ListBucket on session_report_results/: a missing result key answers 403,
  not 404, and the checklist_gap scan counts an error on every pending report forever;
- any SES or Lambda invoke grant: P0 sends nothing;
- org-api not told the flag (Task 6 reads SITE_DECISION_LOOP there);
- the workflows not passing the flag: SAM keeps a stack's previous parameter value, so
  only the workflow variable sets it.

Spec: docs/superpowers/specs/2026-11-05-site-decision-loop-design.md (rev 5), plan Task 5.
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
COND = "ShouldRunDecisionLoop"
FN = "DecisionScanFunction"
LAKE = "arn:aws:s3:::${IngestBucketName}"


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


def _props(doc, name):
    return doc["Resources"][name]["Properties"]


def _env(doc, name):
    return _props(doc, name)["Environment"]["Variables"]


def _as_list(v):
    return v if isinstance(v, list) else [v]


def _statements(doc, name):
    for pol in _props(doc, name)["Policies"]:
        if isinstance(pol, dict) and "Statement" in pol:
            yield from pol["Statement"]


def _sub(r):
    return r.get("Sub") if isinstance(r, dict) else r


def test_parameter_defaults_off(doc):
    p = doc["Parameters"]["SiteDecisionLoop"]
    assert p["Default"] == "off" and p["AllowedValues"] == ["off", "shadow"]


def test_function_exists_only_under_condition(doc):
    cond = doc["Conditions"][COND]["And"]
    assert {"Condition": "HasDb"} in cond
    assert {"Not": [{"Equals": [{"Ref": "SiteDecisionLoop"}, "off"]}]} in cond
    res = doc["Resources"][FN]
    assert res["Condition"] == COND
    p = res["Properties"]
    assert p["Handler"] == "lambda_decision_scan.lambda_handler"
    assert p["CodeUri"] == "src/"
    assert p["Timeout"] == 120 and p["MemorySize"] == 512
    assert p["Layers"] == [{"Ref": "PsycopgLayer"}]
    ref = _props(doc, "CheckGapsDbFunction")
    assert p["VpcConfig"] == ref["VpcConfig"]
    env, ref_env = _env(doc, FN), _env(doc, "CheckGapsDbFunction")
    for k in ("PGHOST", "PGDATABASE", "PGUSER", "PGPASSWORD", "COMPANY_STATUS_ENFORCE"):
        assert env[k] == ref_env[k], k
    assert env["SITE_DECISION_LOOP"] == {"Ref": "SiteDecisionLoop"}
    assert env["LAKE_BUCKET"] == {"Ref": "IngestBucketName"}
    assert {"VPCAccessPolicy": {}} in p["Policies"]


def test_two_schedules_with_inputs(doc):
    # Two, not three: the handler runs process() after each emit in the same invocation,
    # so no schedule wakes Aurora every few minutes (test_sweep_cadence_vs_autopause).
    # {"scan": "process"} stays valid for a manual invoke but has no schedule.
    events = _props(doc, FN)["Events"]
    got = {}
    for ev in events.values():
        assert ev["Type"] == "Schedule"
        pr = ev["Properties"]
        assert pr["State"] == {"If": ["ShouldEnableSchedules", "ENABLED", "DISABLED"]}
        got[json.loads(pr["Input"])["scan"]] = pr["Schedule"]
    assert got == {"checklist_gap": "rate(30 minutes)",
                   "todo_overdue": "cron(30 18 * * ? *)"}


def test_no_ses_or_invoke_permission(doc):
    for st in _statements(doc, FN):
        for a in _as_list(st["Action"]):
            assert not a.lower().startswith("ses:"), a
            assert a != "lambda:InvokeFunction", a
            assert a in ("s3:GetObject", "s3:ListBucket"), a
    # No SAM policy templates that grant SES or invoke either.
    for pol in _props(doc, FN)["Policies"]:
        if isinstance(pol, dict) and "Statement" not in pol:
            assert list(pol) == ["VPCAccessPolicy"], pol


def test_s3_scope_is_session_report_results_with_listbucket(doc):
    gets, lists = set(), set()
    for st in _statements(doc, FN):
        assert st["Effect"] == "Allow"
        for a in _as_list(st["Action"]):
            if a == "s3:ListBucket":
                assert [_sub(r) for r in _as_list(st["Resource"])] == [LAKE]
                lists |= set(_as_list(st["Condition"]["StringLike"]["s3:prefix"]))
            else:
                assert a == "s3:GetObject"
                gets |= {_sub(r) for r in _as_list(st["Resource"])}
    assert gets == {LAKE + "/session_report_results/*"}
    assert lists == {"session_report_results/*"}


def test_org_api_gets_the_flag(doc):
    assert _env(doc, "OrgApiFunction")["SITE_DECISION_LOOP"] == {"Ref": "SiteDecisionLoop"}


def test_nothing_outside_the_condition_refers_to_the_function(doc):
    for name, res in doc["Resources"].items():
        if res.get("Condition") == COND:
            continue
        assert FN not in json.dumps(res), name
    for out in (doc.get("Outputs") or {}).values():
        assert FN not in json.dumps(out)


@pytest.mark.parametrize("env", sorted(WORKFLOWS))
def test_workflows_pass_the_flag_with_off_default(env):
    text = io.open(WORKFLOWS[env], encoding="utf-8").read()
    assert "\"SiteDecisionLoop=${{ vars.%s_SITE_DECISION_LOOP || 'off' }}\"" % env in text
