"""Unit: the session search index is wired end to end, or it is not wired.

Three hops carry each switch, and a missing one fails silently:

    repo variable -> workflow --parameter-overrides -> template Parameter -> function env

and every artifact hop needs both an IAM grant and an S3 notification rule. A missing
grant is swallowed as a logged warning at runtime (request writing never fails the
writer), and a missing notification just never fires, so none of this surfaces unless a
test pins it. Spec: docs/superpowers/specs/2026-10-13-search-index-from-extraction-design.md 4.1.
"""
import io
import os
import re

import pytest

REPO = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
TEMPLATE = os.path.join(REPO, "src", "template.yaml")
WIRE_SCRIPT = os.path.join(REPO, "scripts", "wire-s3-events.sh")
WORKFLOWS = {
    "TEST": os.path.join(REPO, ".github", "workflows", "deploy.yml"),
    "PROD": os.path.join(REPO, ".github", "workflows", "deploy-prod.yml"),
}

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


def _env(fn):
    return _resources()[fn]["Properties"]["Environment"]["Variables"]


def _as_list(v):
    return v if isinstance(v, list) else [v]


def _statements(policy):
    """Statements of an inline policy, flattening any list nested in the Statement list."""
    for st in _as_list(policy.get("Statement", [])):
        if isinstance(st, list):
            for inner in st:
                if isinstance(inner, dict):
                    yield inner
        elif isinstance(st, dict):
            yield st


def _grants(fn):
    """{(action, resource-suffix-after-the-bucket)} for every inline S3 statement."""
    out = set()
    for policy in _resources()[fn]["Properties"].get("Policies", []):
        if not isinstance(policy, dict):
            continue
        for st in _statements(policy):
            if st.get("Effect") != "Allow":
                continue
            for action in _as_list(st.get("Action")):
                for res in _as_list(st.get("Resource")):
                    if isinstance(res, str) and "${IngestBucketName}/" in res:
                        out.add((action, res.split("${IngestBucketName}/", 1)[1]))
    return out


def _list_prefixes(fn):
    prefixes = set()
    for policy in _resources()[fn]["Properties"].get("Policies", []):
        if not isinstance(policy, dict):
            continue
        for st in _statements(policy):
            if st.get("Action") == "s3:ListBucket":
                cond = st.get("Condition", {}).get("StringLike", {})
                prefixes.update(_as_list(cond.get("s3:prefix", [])))
    return prefixes


# ---- parameters and workflows ------------------------------------------------------

def test_the_two_parameters_exist_with_inert_defaults():
    params = _template()["Parameters"]
    w = params["SearchIndexWrite"]
    assert str(w["Default"]) == "false" and w["AllowedValues"] == ["true", "false"]
    r = params["SearchIndexRead"]
    assert r["Default"] == "report" and r["AllowedValues"] == ["report", "session"]


@pytest.mark.parametrize("stage", ["TEST", "PROD"])
def test_each_workflow_passes_both_parameters_from_its_own_repo_variable(stage):
    wf = io.open(WORKFLOWS[stage], encoding="utf-8").read()
    assert ("\"SearchIndexWrite=${{ vars.%s_SEARCH_INDEX_WRITE || 'false' }}\"" % stage) in wf
    assert ("\"SearchIndexRead=${{ vars.%s_SEARCH_INDEX_READ || 'report' }}\"" % stage) in wf


def test_neither_workflow_reads_the_other_stages_variable():
    for stage, other in (("TEST", "PROD"), ("PROD", "TEST")):
        wf = io.open(WORKFLOWS[stage], encoding="utf-8").read()
        assert f"{other}_SEARCH_INDEX" not in wf


# ---- function env -------------------------------------------------------------------

WRITERS = ("ItemWriterFunction", "OrgApiFunction", "IngestFunction", "NonWorkExpiryFunction")


@pytest.mark.parametrize("fn", WRITERS)
def test_every_function_that_emits_index_requests_gets_the_write_switch(fn):
    assert _env(fn).get("SEARCH_INDEX_WRITE") == "SearchIndexWrite"


def test_no_other_function_carries_the_write_switch():
    holders = sorted(
        n for n, r in _resources().items()
        if r.get("Type") == "AWS::Serverless::Function"
        and "SEARCH_INDEX_WRITE" in (r.get("Properties", {}).get("Environment") or {}).get("Variables", {}))
    assert holders == sorted(WRITERS)


def test_rag_search_gets_the_read_switch_and_nothing_else_does():
    assert _env("RagSearchFunction").get("SEARCH_INDEX_READ") == "SearchIndexRead"
    holders = [
        n for n, r in _resources().items()
        if r.get("Type") == "AWS::Serverless::Function"
        and "SEARCH_INDEX_READ" in (r.get("Properties", {}).get("Environment") or {}).get("Variables", {})]
    assert holders == ["RagSearchFunction"]


# ---- IAM (spec 4.1) -----------------------------------------------------------------

GRANTS = {
    "ItemWriterFunction": {("s3:PutObject", "index_requests/*")},
    "OrgApiFunction": {("s3:PutObject", "index_requests/*")},
    "NonWorkExpiryFunction": {("s3:PutObject", "index_requests/*")},
    "EmbedReportFunction": {("s3:GetObject", "index_requests/*"),
                            ("s3:GetObject", "index_vectors/*"),
                            ("s3:GetObject", "extractions/*"),
                            ("s3:PutObject", "index_vectors/*")},
    "IngestFunction": {("s3:GetObject", "index_vectors/*"),
                       ("s3:PutObject", "index_requests/*")},
}


@pytest.mark.parametrize("fn", sorted(GRANTS))
def test_the_grants_the_spec_lists_are_present(fn):
    missing = GRANTS[fn] - _grants(fn)
    assert not missing, f"{fn} lacks {sorted(missing)}"


def test_no_function_gets_more_index_access_than_the_spec_lists():
    for fn, resource in _resources().items():
        if resource.get("Type") != "AWS::Serverless::Function":
            continue
        got = {g for g in _grants(fn) if g[1].startswith("index_")}
        extra = got - GRANTS.get(fn, set())
        assert not extra, f"{fn} has unlisted index grants {sorted(extra)}"


def test_embed_report_can_tell_a_missing_vectors_artifact_from_a_denied_one():
    # _previous_embeddings treats NoSuchKey as the normal first run and logs anything else;
    # without ListBucket on the prefix S3 answers 403 for a missing key.
    assert "index_vectors/*" in _list_prefixes("EmbedReportFunction")


# ---- S3 notifications ---------------------------------------------------------------

def _wire_rules():
    """(id, function ARN variable, prefix, suffix) for every rule in wire-s3-events.sh."""
    text = io.open(WIRE_SCRIPT, encoding="utf-8").read()
    rules = []
    for m in re.finditer(
            r'jq -c --arg arn "\$(\w+)" \'\. \+ \[(.*?)\]\' <<<"\$DESIRED"\)', text, re.S):
        arn_var, body = m.groups()
        for r in re.finditer(
                r'\{"Id":"([^"]+)".*?"prefix","Value":"([^"]*)"\},\{"Name":"suffix","Value":"([^"]*)"',
                body, re.S):
            rules.append((r.group(1), arn_var) + r.group(2, 3))
    return rules


def test_the_wire_script_routes_the_two_new_prefixes_to_the_right_functions():
    rules = {r[0]: r for r in _wire_rules()}
    assert rules["fs-embed-index"][1:] == ("EMBED_ARN", "index_requests/", ".json")
    assert rules["fs-ingest-index"][1:] == ("INGEST_ARN", "index_vectors/", ".json")


def test_the_parser_sees_the_existing_rules_so_disjointness_is_not_vacuous():
    ids = {r[0] for r in _wire_rules()}
    assert {"fs-embed-reindex", "fs-ingest-reindex", "fs-programme-match", "fs-vad-wav"} <= ids
    assert len(ids) >= 14


def test_no_rule_prefix_is_a_prefix_of_another():
    """S3 rejects two rules whose prefixes overlap when their suffixes overlap
    ("Configuration is ambiguously defined"), and compares literal prefixes from the first
    character: a bare "index" or empty prefix would swallow every new rule."""
    rules = _wire_rules()
    for rid, _, prefix, _ in rules:
        assert prefix, f"{rid} has an empty prefix"
    for a in rules:
        for b in rules:
            if a[0] != b[0] and b[2].startswith(a[2]):
                # same prefix or nested prefix: tolerable only when the suffixes differ
                assert not (a[3].endswith(b[3]) or b[3].endswith(a[3])), (a, b)


def test_the_new_prefixes_are_disjoint_from_every_other_rule():
    rules = _wire_rules()
    new = [r for r in rules if r[0] in ("fs-embed-index", "fs-ingest-index")]
    assert len(new) == 2
    for n in new:
        for o in rules:
            if o[0] == n[0]:
                continue
            assert not o[2].startswith(n[2]) and not n[2].startswith(o[2]), (n, o)
    # the request and vectors prefixes are also not a prefix of each other
    assert not new[0][2].startswith(new[1][2]) and not new[1][2].startswith(new[0][2])


def test_the_action_agent_rule_is_disjoint_from_every_other_rule_and_its_own_output():
    """Action packs (spec 2026-11-01 §14 D3): the request prefix shares "action_pack" with
    the agent's own lake output (action_packs/) -- S3 compares literal prefixes, so neither
    may be a prefix of the other, and no rule may match that output."""
    rules = _wire_rules()
    agent = [r for r in rules if r[0] == "fs-action-agent"]
    assert agent == [("fs-action-agent", "ACTION_AGENT_ARN", "action_pack_requests/", ".json")]
    for o in rules:
        if o[0] != "fs-action-agent":
            assert not o[2].startswith(agent[0][2]) and not agent[0][2].startswith(o[2]), o
    output = "action_packs/00000000-0000-0000-0000-000000000000/p/web/W1.txt"
    cancel = "action_pack_requests/00000000-0000-0000-0000-000000000000.cancel"
    for key in (output, cancel):
        assert not any(key.startswith(r[2]) and key.endswith(r[3]) for r in rules), key
