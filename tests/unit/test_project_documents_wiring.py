"""Project documents are wired end to end, or not at all (spec Rollout, R10, R13).

A function with no Events whose wire-s3-events.sh entry is missing deploys green and never
fires; a trigger without a read grant fires into AccessDenied; a flag set on one function
and not another makes the Ask tool and its backend disagree."""
import io
import json
import os
import re

import pytest

REPO = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
TEMPLATE = os.path.join(REPO, "src", "template.yaml")
WIRE = os.path.join(REPO, "scripts", "wire-s3-events.sh")
LIFECYCLE = os.path.join(REPO, "scripts", "wire-bucket-lifecycle.sh")
WORKFLOWS = {"test": os.path.join(REPO, ".github", "workflows", "deploy.yml"),
             "prod": os.path.join(REPO, ".github", "workflows", "deploy-prod.yml")}

_yaml = pytest.importorskip("yaml")


class _Loader(_yaml.SafeLoader):
    pass


for _tag in ("!Sub", "!Ref", "!If", "!Not", "!Equals", "!GetAtt", "!FindInMap",
             "!Join", "!Condition", "!Select", "!Split", "!ImportValue", "!And", "!Or"):
    _Loader.add_constructor(_tag, lambda loader, node: getattr(node, "value", None))


def _tpl():
    return _yaml.load(io.open(TEMPLATE, encoding="utf-8").read(), Loader=_Loader)


def _fn(name):
    return _tpl()["Resources"][name]["Properties"]


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
    out, marker = set(), "${%s}/" % bucket
    for st in _statements(fn):
        for action in _as_list(st.get("Action")):
            for res in _as_list(st.get("Resource")):
                if isinstance(res, str) and marker in res:
                    out.add((action, res.split(marker, 1)[1]))
    return out


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


def _wire_prefixes():
    return re.findall(r'"Name":"prefix","Value":"([^"]+)"', open(WIRE, encoding="utf-8").read())


def test_the_worker_exists_outside_the_vpc_with_both_reader_layers():
    fn = _fn("ProjectDocIndexFunction")
    assert fn["Handler"] == "lambda_project_doc_index.lambda_handler"
    assert fn["Runtime"] == "python3.12", "the docx layer is cp312 (BUG-24)"
    assert fn["Timeout"] == 900
    assert "VpcConfig" not in fn, "it calls DashScope; in the VPC it would hang (BUG-36)"
    assert "Events" not in fn, "S3 events on the lake are wired by script (BUG-33)"
    layers = json.dumps(fn["Layers"], default=str)
    assert "DocxLayerArn" in layers and "PdfLayerArn" in layers
    assert "PsycopgLayer" not in layers
    assert fn["EventInvokeConfig"]["MaximumRetryAttempts"] == 0
    env = fn["Environment"]["Variables"]
    assert env["DASHSCOPE_EMBED_MODEL"] == "text-embedding-v4"
    assert env["DASHSCOPE_EMBED_DIM"] == "1024"


def test_the_worker_may_read_its_inputs_and_write_only_its_output():
    g = _grants(_fn("ProjectDocIndexFunction"))
    assert ("s3:GetObject", "project_doc_sources/*") in g
    assert ("s3:GetObject", "project_doc_requests/*") in g
    assert ("s3:PutObject", "project_doc_vectors/*") in g
    assert ("s3:PutObject", "lake_manifest/*") in g
    assert ("s3:PutObject", "project_doc_sources/*") not in g
    assert ("s3:PutObject", "project_doc_requests/*") not in g


def test_org_api_may_do_exactly_what_the_routes_do():
    fn = _fn("OrgApiFunction")
    g = _grants(fn)
    assert ("s3:PutObject", "project_doc_sources/*") in g      # a presigned PUT is the role's
    assert ("s3:GetObject", "project_doc_sources/*") in g      # HEAD + the open presign
    assert ("s3:PutObject", "project_doc_requests/*") in g     # the request (and Retry)
    assert ("s3:PutObject", "project_doc_vectors/*") not in g  # the worker's alone
    for p in ("project_doc_sources/*", "project_doc_requests/*"):
        assert p in _list_prefixes(fn), f"{p}: a missing key would read as 403, not 404"


def test_ingest_may_read_the_vectors_and_tell_missing_from_denied():
    fn = _fn("IngestFunction")
    assert ("s3:GetObject", "project_doc_vectors/*") in _grants(fn)
    assert "project_doc_vectors/*" in _list_prefixes(fn)


def test_the_flag_exists_defaults_off_and_reaches_all_four_functions():
    p = _tpl()["Parameters"]["ProjectDocuments"]
    assert p["Default"] == "false" and p["AllowedValues"] == ["true", "false"]
    for name in ("OrgApiFunction", "RagSearchFunction", "AskAgentFunction",
                 "AskVoiceWorkerFunction"):
        assert _fn(name)["Environment"]["Variables"]["PROJECT_DOCUMENTS"] == "ProjectDocuments", name


def test_the_pdf_layer_parameter_defaults_empty():
    assert _tpl()["Parameters"]["PdfLayerArn"]["Default"] == ""


def test_both_workflows_pass_the_flag_off_by_default_and_the_layer():
    for stage, path in WORKFLOWS.items():
        text = open(path, encoding="utf-8").read()
        assert ('"ProjectDocuments=${{ vars.%s_PROJECT_DOCUMENTS || \'false\' }}"'
                % stage.upper()) in text, stage
        # Shell-guarded, never a bare override line: an unset variable would emit
        # `PdfLayerArn=` and SAM exits 2 (test_deploy_workflow_params).
        assert ('PDF_LAYER_PARAM="PdfLayerArn=${{ vars.%s_PDF_LAYER_ARN }}"'
                % stage.upper()) in text, stage
        assert 'if [ -n "${{ vars.%s_PDF_LAYER_ARN }}" ]; then' % stage.upper() in text, stage
        assert re.search(r"^\s+\$PDF_LAYER_PARAM \\$", text, re.M), stage
        assert not re.search(r'^\s+"PdfLayerArn=', text, re.M), stage


def test_both_triggers_are_in_the_wiring_script():
    text = open(WIRE, encoding="utf-8").read()
    assert '"Id":"fs-project-doc-index"' in text
    assert '{"Name":"prefix","Value":"project_doc_requests/"}' in text
    assert "${PREFIX}-project-doc-index" in text
    assert '"Id":"fs-ingest-project-doc"' in text
    assert '{"Name":"prefix","Value":"project_doc_vectors/"}' in text


def test_the_trigger_prefixes_collide_with_no_other_rule():
    prefixes = _wire_prefixes()
    for mine in ("project_doc_requests/", "project_doc_vectors/"):
        assert prefixes.count(mine) == 1
        for p in [p for p in prefixes if p != mine]:
            assert not (p.startswith(mine) or mine.startswith(p)), f"{mine} overlaps {p}"


def test_no_rule_fires_on_the_uploads_or_the_workers_output_loop():
    """BUG-13: the worker writes vectors (Ingest's trigger, never its own) and manifest
    lines; the uploads under project_doc_sources/ trigger nothing."""
    import lake_put
    prefixes = _wire_prefixes()
    for out in ("project_doc_sources/", lake_put.MANIFEST_PREFIX):
        assert not any(out.startswith(p) or p.startswith(out) for p in prefixes), out
    text = open(WIRE, encoding="utf-8").read()
    block = text[text.index('"Id":"fs-project-doc-index"') - 400:
                 text.index('"Id":"fs-project-doc-index"')]
    assert "PROJECT_DOC_INDEX_ARN" in block


def test_no_lifecycle_rule_reaches_project_documents():
    """Review Focus 5: documents are kept (spec: lifecycle none)."""
    script = open(LIFECYCLE, encoding="utf-8").read()
    for prefix in re.findall(r'"Prefix":\s*"([^"]*)"', script):
        for mine in ("project_doc_sources/", "project_doc_requests/", "project_doc_vectors/"):
            assert not (mine.startswith(prefix) or prefix.startswith(mine)), (prefix, mine)
