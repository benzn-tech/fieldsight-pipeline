"""The digitise worker is wired end to end, or it is not wired (spec 2026-10-16, Rollout).

A function with NO Events whose wire-s3-events.sh entry is missing deploys green and never
fires; a trigger without a read grant fires into AccessDenied; a lifecycle rule missing
from the script's exclusion list makes the script refuse on its second run. Each of those
has happened in this repo once already.
"""
import io
import json
import os
import re

import pytest

REPO = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
TEMPLATE = os.path.join(REPO, "src", "template.yaml")
WIRE = os.path.join(REPO, "scripts", "wire-s3-events.sh")
LIFECYCLE = os.path.join(REPO, "scripts", "wire-bucket-lifecycle.sh")

_yaml = pytest.importorskip("yaml")


class _Loader(_yaml.SafeLoader):
    pass


for _tag in ("!Sub", "!Ref", "!If", "!Not", "!Equals", "!GetAtt", "!FindInMap",
             "!Join", "!Condition", "!Select", "!Split", "!ImportValue", "!And", "!Or"):
    _Loader.add_constructor(_tag, lambda loader, node: getattr(node, "value", None))


def _template():
    return _yaml.load(io.open(TEMPLATE, encoding="utf-8").read(), Loader=_Loader)


def _resources():
    return _template()["Resources"]


def _fn(name="TemplateDigitiseFunction"):
    return _resources()[name]["Properties"]


def _as_list(v):
    return v if isinstance(v, list) else [v]


def _statements(fn):
    for policy in fn.get("Policies", []):
        if not isinstance(policy, dict):
            continue
        for st in _as_list(policy.get("Statement", [])):
            for s in (st if isinstance(st, list) else [st]):
                if isinstance(s, dict) and s.get("Effect") == "Allow":
                    yield s


def _grants(fn, bucket="IngestBucketName"):
    out = set()
    marker = "${%s}/" % bucket
    for st in _statements(fn):
        for action in _as_list(st.get("Action")):
            for res in _as_list(st.get("Resource")):
                if isinstance(res, str) and marker in res:
                    out.add((action, res.split(marker, 1)[1]))
    return out


def test_THE_the_worker_exists_outside_the_vpc_on_the_docx_runtime():
    fn = _fn()
    assert fn["Handler"] == "lambda_template_digitise.lambda_handler"
    assert fn["Runtime"] == "python3.12", "the docx layer is cp312 (BUG-24)"
    assert fn["Timeout"] == 900
    assert "VpcConfig" not in fn, "it calls OpenRouter; in the VPC it would hang (BUG-36)"
    assert "Events" not in fn, "S3 events on the lake are wired by script (BUG-33)"
    assert "DocxLayerArn" in json.dumps(fn["Layers"], default=str)


def test_the_worker_reads_no_text_fallback_chain_and_has_its_own_empty_one():
    env = _fn()["Environment"]["Variables"]
    assert env["LLM_FALLBACK_MODELS"] == "", "Globals' text chain must not leak in"
    assert env["TEMPLATE_DIGITISE_FALLBACK_MODELS"] == ""
    # A parameter now (spec 2026-10-31 B2): the model moves by a repo variable, and the
    # default is still the one the 2026-10-16 gate measured.
    assert env["TEMPLATE_DIGITISE_MODEL"] == "TemplateDigitiseModel"  # !Ref, by name
    assert _template()["Parameters"]["TemplateDigitiseModel"]["Default"] == (
        "google/gemini-3.8-flash")
    assert env["LLM_CHAIN_BUDGET_SECONDS"] == "840"
    assert int(env["LLM_HTTP_TIMEOUT"]) < 900
    assert "QWEN_API_KEY" in env and "QWEN_BASE_URL" in env


def test_the_worker_may_read_its_inputs_and_write_only_its_result():
    g = _grants(_fn())
    assert ("s3:GetObject", "template_sources/*") in g
    assert ("s3:GetObject", "template_digitise_requests/*") in g
    assert ("s3:PutObject", "template_digitise_results/*") in g
    assert ("s3:PutObject", "lake_manifest/*") in g
    assert ("s3:PutObject", "template_sources/*") not in g
    assert ("s3:PutObject", "template_digitise_requests/*") not in g


def test_the_trigger_is_in_the_wiring_script():
    text = open(WIRE, encoding="utf-8").read()
    assert '"Id":"fs-template-digitise"' in text
    assert '{"Name":"prefix","Value":"template_digitise_requests/"}' in text
    assert "${PREFIX}-template-digitise" in text


def test_the_trigger_prefix_collides_with_no_other_rule():
    text = open(WIRE, encoding="utf-8").read()
    prefixes = re.findall(r'"Name":"prefix","Value":"([^"]+)"', text)
    mine = "template_digitise_requests/"
    for p in [p for p in prefixes if p != mine]:
        assert not (p.startswith(mine) or mine.startswith(p)), f"{mine} overlaps {p}"


def test_no_rule_fires_on_the_workers_own_output_or_the_uploads():
    text = open(WIRE, encoding="utf-8").read()
    prefixes = re.findall(r'"Name":"prefix","Value":"([^"]+)"', text)
    # lake_put writes a manifest line beside every result it puts, so the worker's output
    # includes MANIFEST_PREFIX too -- taken from lake_put itself, so a renamed prefix is
    # checked rather than a stale literal.
    import lake_put
    assert lake_put.MANIFEST_PREFIX == "lake_manifest/"
    for out in ("template_digitise_results/", "template_sources/", lake_put.MANIFEST_PREFIX):
        assert not any(out.startswith(p) or p.startswith(out) for p in prefixes), out


def test_the_three_prefixes_expire_after_ninety_days_and_the_script_still_reruns():
    script = open(LIFECYCLE, encoding="utf-8").read()
    body = re.search(r"--lifecycle-configuration '(\{.*?\})'", script, re.S).group(1)
    rules = {r["ID"]: r for r in json.loads(body)["Rules"]}
    for rid, prefix in (("template-sources-expiry", "template_sources/"),
                        ("template-digitise-requests-expiry", "template_digitise_requests/"),
                        ("template-digitise-results-expiry", "template_digitise_results/")):
        rule = rules.get(rid)
        assert rule and rule["Status"] == "Enabled", rid
        assert rule["Filter"]["Prefix"] == prefix
        assert rule["Expiration"]["Days"] == 90
        # the script refuses to run over rules it does not manage; its own must be listed
        assert "ID!=`%s`" % rid in script, f"{rid} missing from the exclusion query"


def test_a_failed_run_is_not_retried_into_a_second_paid_model_call():
    """Async S3 delivery retries twice by default; each retry re-runs the vision call.
    A result write that fails must not buy the same reading again (controller ruling)."""
    assert _fn()["EventInvokeConfig"]["MaximumRetryAttempts"] == 0


WORKFLOWS = {"test": os.path.join(REPO, ".github", "workflows", "deploy.yml"),
             "prod": os.path.join(REPO, ".github", "workflows", "deploy-prod.yml")}


def _list_prefixes(fn, bucket="IngestBucketName"):
    out = []
    for st in _statements(fn):
        if "s3:ListBucket" not in _as_list(st.get("Action")):
            continue
        if not any(isinstance(r, str) and r.endswith("${%s}" % bucket)
                   for r in _as_list(st.get("Resource"))):
            continue
        out += _as_list(((st.get("Condition") or {}).get("StringLike") or {}).get("s3:prefix"))
    return out


def test_the_switch_exists_defaults_off_and_reaches_org_api():
    tpl = _yaml.load(io.open(TEMPLATE, encoding="utf-8").read(), Loader=_Loader)
    p = tpl["Parameters"]["TemplateDigitise"]
    assert p["Default"] == "false" and p["AllowedValues"] == ["true", "false"]
    env = _fn("OrgApiFunction")["Environment"]["Variables"]
    assert env["TEMPLATE_DIGITISE"] == "TemplateDigitise"   # the !Ref, as the loader reads it


def test_both_workflows_pass_the_switch_and_default_it_off():
    for stage, path in WORKFLOWS.items():
        text = open(path, encoding="utf-8").read()
        line = '"TemplateDigitise=${{ vars.%s_TEMPLATE_DIGITISE || \'false\' }}"' % stage.upper()
        assert line in text, f"{stage} cannot set TemplateDigitise"


def test_org_api_may_do_exactly_what_the_routes_do():
    g = _grants(_fn("OrgApiFunction"))
    assert ("s3:PutObject", "template_digitise_requests/*") in g      # the request
    assert ("s3:PutObject", "template_sources/*") in g                # a presigned PUT is the role's
    assert ("s3:GetObject", "template_sources/*") in g                # HEAD + the preview presign
    assert ("s3:GetObject", "template_digitise_results/*") in g       # the poll
    assert ("s3:GetObject", "template_digitise_requests/*") in g      # pending vs 404
    assert ("s3:PutObject", "template_digitise_results/*") not in g   # the worker's alone


def test_org_api_can_tell_a_missing_key_from_a_denied_one_on_all_three():
    prefixes = _list_prefixes(_fn("OrgApiFunction"))
    for p in ("template_sources/*", "template_digitise_requests/*", "template_digitise_results/*"):
        assert p in prefixes, f"{p}: a missing key would read as AccessDenied (500), not 404"


# ---- per-model settings (spec 2026-10-31 B2) --------------------------------------
WORKFLOWS = {"TEST": os.path.join(REPO, ".github", "workflows", "deploy.yml"),
             "PROD": os.path.join(REPO, ".github", "workflows", "deploy-prod.yml")}
KNOBS = {"TemplateDigitiseModel": "TEMPLATE_DIGITISE_MODEL",
         "TemplateDigitiseReasoning": "TEMPLATE_DIGITISE_REASONING",
         "TemplateDigitiseProvider": "TEMPLATE_DIGITISE_PROVIDER",
         "TemplateDigitisePdfModel": "TEMPLATE_DIGITISE_PDF_MODEL"}


def test_the_settings_reach_the_worker_and_default_to_todays_request():
    """Inert on deploy: an unset repo variable must reproduce the request shipped
    2026-10-16 -- gemini, no reasoning, no pin, PDFs to the same model."""
    params = _template()["Parameters"]
    env = _fn()["Environment"]["Variables"]
    for param, var in KNOBS.items():
        assert env[var] == param, f"{var} is not wired to {param}"
    assert params["TemplateDigitiseReasoning"]["Default"] == ""
    assert params["TemplateDigitiseProvider"]["Default"] == ""
    assert params["TemplateDigitisePdfModel"]["Default"] == ""
    assert params["TemplateDigitiseReasoning"]["AllowedPattern"] == "^(|off|low|medium|high)$"


BACKSLASH = chr(92)  # the line continuation after a sam deploy argument
_STRIP = "| tr -d '[:space:]'"
_LOWER = "| tr '[:upper:]' '[:lower:]'"
# (parameter, variable suffix, shell value, lowercased)
_SETTINGS = (("TemplateDigitiseModel", "TEMPLATE_DIGITISE_MODEL", "TD_MODEL", False),
             ("TemplateDigitiseReasoning", "TEMPLATE_DIGITISE_REASONING", "TD_REASONING", True),
             ("TemplateDigitiseProvider", "TEMPLATE_DIGITISE_PROVIDER", "TD_PROVIDER", False),
             ("TemplateDigitisePdfModel", "TEMPLATE_DIGITISE_PDF_MODEL", "TD_PDF_MODEL", False))


def _line_assigning(text, var):
    lines = [ln.strip() for ln in text.splitlines() if ln.strip().startswith(var + '="$(')]
    assert len(lines) == 1, f"{var}: expected one normalising assignment, got {lines}"
    return lines[0]


@pytest.mark.parametrize("env", sorted(WORKFLOWS))
def test_both_workflows_pass_the_settings_from_repo_variables(env):
    """Normalised in the shell before use: a hand-typed repo variable with a space or
    a capital must neither split into stray `sam deploy` arguments nor reach the
    template's AllowedPattern / the endpoint pin as typed."""
    text = open(WORKFLOWS[env], encoding="utf-8").read()
    for param, var, shell, lower in _SETTINGS:
        line = _line_assigning(text, shell)
        assert "vars.%s_%s" % (env, var) in line, (env, var)
        assert _STRIP in line, f"{env} {shell}: whitespace not stripped"
        assert (_LOWER in line) is lower, f"{env} {shell}: lowercase {'missing' if lower else 'unexpected'}"
        assert '%s_PARAM="%s=$%s"' % (var, param, shell) in text, (env, param)
    # The model can never be empty: a cleared or blank variable falls back to the
    # template default, so it is passed unconditionally, quoted.
    assert 'TD_MODEL="${TD_MODEL:-google/gemini-3.8-flash}"' in text
    assert '"$TEMPLATE_DIGITISE_MODEL_PARAM" ' + BACKSLASH in text
    # The other three are '' by default and a bare `Key=` makes SAM exit 2: guarded,
    # and expanded quoted only when set.
    for _, var, shell, _ in _SETTINGS[1:]:
        assert 'if [ -n "$%s" ]; then' % shell in text, (env, shell)
        assert '${%s_PARAM:+"$%s_PARAM"} ' % (var, var) + BACKSLASH in text, (env, var)
