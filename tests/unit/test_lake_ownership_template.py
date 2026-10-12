# tests/unit/test_lake_ownership_template.py
"""Lake ownership, Task 11: the template wiring for everything Tasks 1-10 built.

Text-level, like test_template_group_merge_flag.py: the template is full of CFN
intrinsics (!Sub/!Ref/!FindInMap) that a plain YAML loader cannot resolve, and what is
pinned here is the literal wiring. This file pins the wiring and only the wiring.

A flag has three pieces (the unwired-toggle trap): Parameter -> function env -> code reads
os.environ. Two of three has shipped as a no-op before, so each flag below checks all of
the pieces that exist for it."""
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
TEMPLATE = ROOT / "src" / "template.yaml"
SRC = ROOT / "src"


def _text():
    return TEMPLATE.read_text(encoding="utf-8")


def _resources():
    """name -> text of each top-level resource (2-space indent) in the Resources section."""
    text = _text()
    body = text.split("\nResources:\n", 1)[1]
    out, cur, buf = {}, None, []
    for line in body.split("\n"):
        m = re.match(r"^  (\w+):\s*$", line)
        if m:
            if cur:
                out[cur] = "\n".join(buf)
            cur, buf = m.group(1), []
        elif cur:
            buf.append(line)
    if cur:
        out[cur] = "\n".join(buf)
    return out


def _parameter(name):
    text = _text()
    params = text.split("\nParameters:\n", 1)[1].split("\nConditions:\n", 1)[0]
    m = re.search(r"\n  %s:\n((?:    .*\n|\s*\n|  #.*\n)*)" % name, params)
    return m.group(1) if m else None


def _statements(block):
    """Split a resource's text into IAM statements (each starts at a `- Effect:` line).
    Comment-only lines are dropped so a comment that mentions an action is not read as one."""
    stmts, cur, indent = [], None, 0
    for line in block.split("\n"):
        m = re.match(r"^(\s*)-\s+Effect:", line)
        if m:
            cur, indent = [line], len(m.group(1))
            stmts.append(cur)
        elif cur is not None:
            if line.strip().startswith("#"):
                continue
            if line.strip() and len(line) - len(line.lstrip()) < indent:
                cur = None  # left the statement list (next policy, Events, ...)
                continue
            cur.append(line)
    return ["\n".join(s) for s in stmts]


def _env_block(block):
    m = re.search(r"\n      Environment:\n        Variables:\n((?:          .*\n|\s*\n)*)", block + "\n")
    return m.group(1) if m else ""


def _has_action(stmt, action):
    return re.search(r"(^|[\s\[,-])%s(\s|$|,|\])" % re.escape(action), stmt, re.M) is not None


# --------------------------------------------------------------------------------------
# LakeOpsFunction
# --------------------------------------------------------------------------------------
def test_lake_ops_function_has_a_long_timeout_and_its_own_pool():
    block = _resources()["LakeOpsFunction"]
    assert re.search(r"^      Timeout: 900$", block, re.M)
    # A reserved pool would either cap the export or starve the mobile upload path; none.
    assert "ReservedConcurrentExecutions" not in block
    # No API events: it is invoked, never fronted by the gateway.
    assert "Type: Api" not in block
    assert "Handler: lambda_org_api.lambda_handler" in block
    assert re.search(r"^    Condition: HasOrgApi$", block, re.M)


def test_nothing_in_the_vpc_calls_the_lambda_api():
    """R11a: the VPC has no NAT and no Lambda interface endpoint, so org-api and LakeOps must
    not invoke anything. The export is requested through an S3 object instead."""
    res = _resources()
    for name in ("OrgApiFunction", "LakeOpsFunction"):
        assert "lambda:InvokeFunction" not in res[name], name
        assert "LAKE_OPS_FUNCTION" not in res[name], name
    assert "LAKE_OPS_FUNCTION" not in _text().replace("\n#", "\n")  # no live env anywhere


def test_lake_ops_database_access_is_copied_from_org_api():
    res = _resources()
    org, ops = _env_block(res["OrgApiFunction"]), _env_block(res["LakeOpsFunction"])
    for var in ("PGHOST", "PGDATABASE", "PGUSER", "PGPASSWORD", "S3_BUCKET", "LAKE_BUCKET"):
        o = re.search(r"\n          %s: (.*)" % var, "\n" + org)
        p = re.search(r"\n          %s: (.*)" % var, "\n" + ops)
        assert o and p, var
        assert o.group(1) == p.group(1), f"{var} differs between OrgApi and LakeOps"
    block = res["LakeOpsFunction"]
    assert "VPCAccessPolicy" in block and "- !Ref PsycopgLayer" in block
    assert "SubnetIds: !Ref DbSubnetIds" in block


def test_lake_ops_iam_is_exactly_what_the_three_tasks_need():
    stmts = _statements(_resources()["LakeOpsFunction"])
    by_action = {}
    for s in stmts:
        for a in re.findall(r"(?:s3|lambda):\w+", s):
            by_action.setdefault(a, []).append(s)
    for a in ("s3:GetObject", "s3:ListBucket", "s3:PutObject", "s3:AbortMultipartUpload",
              "s3:ListMultipartUploadParts", "s3:DeleteObject", "s3:GetObjectTagging",
              "s3:PutObjectTagging"):
        assert a in by_action, f"LakeOps lacks {a}"
    # Puts: the export output and the manifest line of the helpers it imports. Nothing else.
    for s in by_action["s3:PutObject"]:
        targets = re.findall(r"arn:aws:s3:::\$\{\w+\}/(\S+)", s)
        assert targets and all(t in ("exports/*", "lake_manifest/*") for t in targets), s
    # ListBucket has no prefix condition: backfill walks the whole bucket.
    for s in by_action["s3:ListBucket"]:
        assert "s3:prefix" not in s


def test_lake_ops_runs_the_drain_daily_before_the_directory_republish():
    res = _resources()
    ops = res["LakeOpsFunction"]
    m = re.search(r"DrainLakeManifest:\n(?:          .*\n|            .*\n)+", ops + "\n")
    assert m, "no DrainLakeManifest event on LakeOpsFunction"
    ev = m.group(0)
    assert "Type: Schedule" in ev
    assert "Input: '{\"task\": \"drain_lake_manifest\"}'" in ev
    assert "State: !If [ShouldEnableSchedules, ENABLED, DISABLED]" in ev

    def cron(text):
        c = re.search(r"Schedule: cron\((\d+) (\d+) \* \* \? \*\)", text)
        assert c, text
        return int(c.group(2)) * 60 + int(c.group(1))

    drain = cron(ev)
    rep = re.search(r"DirectoryRepublish:\n(?:          .*\n|            .*\n)+", res["OrgApiFunction"] + "\n")
    assert rep and "republish_directory" in rep.group(0)
    # Both are fixed UTC crons, so "earlier in UTC" is "earlier" in NZST and in NZDT alike.
    assert drain < cron(rep.group(0))
    # And it ends before the first of the morning republishes starts: start + the 900 s
    # timeout must be at or before the 15:30 UTC site-coords run.
    coords = re.search(r"SiteCoordsRepublish:\n(?:          .*\n|            .*\n)+",
                       res["OrgApiFunction"] + "\n")
    assert coords and drain + 15 <= cron(coords.group(0))


def test_export_tick_is_scheduled_every_ten_minutes_and_gated():
    ops = _resources()["LakeOpsFunction"]
    m = re.search(r"ExportTick:\n(?:          .*\n|            .*\n)+", ops + "\n")
    assert m, "no ExportTick event on LakeOpsFunction"
    ev = m.group(0)
    assert "Schedule: rate(10 minutes)" in ev
    assert "Input: '{\"task\": \"export_tick\"}'" in ev
    assert "State: !If [ShouldEnableSchedules, ENABLED, DISABLED]" in ev
    code = (SRC / "lambda_org_api.py").read_text(encoding="utf-8")
    assert 'EXPORT_TICK_TASK = "export_tick"' in code


# --------------------------------------------------------------------------------------
# The three flags: Parameter -> env -> code
# --------------------------------------------------------------------------------------
def test_lake_index_reads_is_wired_in_three_pieces():
    p = _parameter("LakeIndexReads")
    assert p, "no LakeIndexReads parameter"
    assert "Default: 'false'" in p
    assert "AllowedValues: ['true', 'false']" in p
    assert "backfill + a drain" in p
    env = _env_block(_resources()["OrgApiFunction"])
    assert "LAKE_INDEX_READS: !Ref LakeIndexReads" in env
    code = (SRC / "lambda_org_api.py").read_text(encoding="utf-8")
    assert re.search(r'os\.environ\.get\("LAKE_INDEX_READS"\)', code)


def test_company_summary_serve_is_wired_in_three_pieces():
    p = _parameter("CompanySummaryServe")
    assert p, "no CompanySummaryServe parameter"
    assert "Default: 'off'" in p
    assert "AllowedValues: ['on', 'off']" in p
    assert "owner product decision" in p.lower()
    env = _env_block(_resources()["OrgApiFunction"])
    assert "COMPANY_SUMMARY_SERVE: !Ref CompanySummaryServe" in env
    code = (SRC / "lambda_org_api.py").read_text(encoding="utf-8")
    assert re.search(r'os\.environ\.get\("COMPANY_SUMMARY_SERVE"\)', code)


def test_summary_lake_wide_reaches_the_generator():
    # A literal env value, no Parameter: it is retired one release later by editing it.
    env = _env_block(_resources()["ReportGeneratorFunction"])
    assert re.search(r"\n          SUMMARY_LAKE_WIDE: 'on'\n", "\n" + env)
    code = (SRC / "lambda_report_generator.py").read_text(encoding="utf-8")
    assert re.search(r"os\.environ\.get\('SUMMARY_LAKE_WIDE'", code)


def test_org_api_requests_exports_through_s3_and_reads_the_results():
    org = _resources()["OrgApiFunction"]
    stmts = _statements(org)
    assert any(_has_action(s, "s3:PutObject") and "${IngestBucketName}/export_requests/*" in s
               for s in stmts)
    assert any(_has_action(s, "s3:GetObject") and "/exports/*" in s for s in stmts)
    # The status poll reads a state.json that does not exist yet: without ListBucket
    # on exports/ that is a 403, not a 404.
    lister = [s for s in stmts if _has_action(s, "s3:ListBucket") and "IngestBucketName" in s
              and "reports/*" in s]
    assert lister and "- exports/*" in lister[0]
    # org-api reads the exports; it never writes them, and never reads the requests back.
    assert not any("/exports/*" in s and _has_action(s, "s3:PutObject") for s in stmts)


# --------------------------------------------------------------------------------------
# Deletes and tagging stay where they belong
# --------------------------------------------------------------------------------------
# Functions that held s3:DeleteObject before lake ownership. Extending this list is a
# decision, not a chore: a new entry is a new function that can delete lake objects.
PRE_EXISTING_DELETERS = {
    "DownloaderFunction", "ExtractSessionFunction", "ExtractionBacklogFunction",
    "FargateTaskRole", "KeyframeFunction", "OrchestratorFunction", "OrgApiFunction",
    "SessionFinalizeFunction",
}


def test_delete_is_confined_to_the_manifest_for_lake_ops_and_added_nowhere_else():
    res = _resources()
    for s in _statements(res["LakeOpsFunction"]):
        if _has_action(s, "s3:DeleteObject"):
            resources = re.findall(r"arn:aws:s3:::\$\{\w+\}[^\s]*", s)
            assert resources and all(
                r.endswith("/lake_manifest/*") or r.endswith("/export_requests/*")
                for r in resources), s
    deleters = {name for name, block in res.items()
                if any(_has_action(s, "s3:DeleteObject") for s in _statements(block))}
    assert deleters - {"LakeOpsFunction"} <= PRE_EXISTING_DELETERS, \
        f"new s3:DeleteObject grant: {sorted(deleters - {'LakeOpsFunction'} - PRE_EXISTING_DELETERS)}"


def test_put_object_tagging_is_granted_to_lake_ops_only():
    holders = {name for name, block in _resources().items()
               if any(_has_action(s, "s3:PutObjectTagging") for s in _statements(block))}
    assert holders == {"LakeOpsFunction"}, holders
    # And nowhere as a bare string either (SAM policy templates are not used for it).
    assert _text().count("s3:PutObjectTagging") == len(
        re.findall(r"s3:PutObjectTagging", _resources()["LakeOpsFunction"]))


def test_there_is_no_managed_policy_and_no_bucket_notification_or_lifecycle():
    text = _text()
    assert "AWS::IAM::ManagedPolicy" not in text
    assert "NotificationConfiguration" not in text
    assert "LifecycleConfiguration" not in text


# --------------------------------------------------------------------------------------
# lake_put callers can write their manifest line
# --------------------------------------------------------------------------------------
# Template function -> the modules through which it writes (needs s3:PutObject on
# lake_manifest/*). Derived from
#   grep -ln "lake_put\|lake_upload_file" src/*.py
# (23 modules, Task 4) followed through imports to each function's Handler, then narrowed
# to the handlers that actually CALL a writing helper (pipeline_trace.flush,
# extraction_pending.write/update, deletion_mirror.write_mirror, reindex.enqueue_*,
# match_request.emit, write_meeting_manifest, batch_seal.seal_*) rather than merely import
# a module that contains one.
# EXTEND THIS MAP when a new Lambda handler writes through lake_put.
LAKE_PUT_CALLERS = {
    "DownloaderFunction": "lambda_downloader",
    "TranscribeFunction": "lambda_transcribe, batch_seal, transcript_utils",
    "VadFunction": "lambda_vad",
    "ReportGeneratorFunction": "lambda_report_generator, deletion_mirror, pipeline_trace, transcript_utils",
    "MeetingMinutesFunction": "lambda_meeting_minutes, pipeline_trace, transcript_utils",
    "SessionReportFunction": "lambda_session_report, lambda_meeting_minutes, deletion_mirror, pipeline_trace",
    "OrgApiFunction": "deletion_mirror, extraction_pending, pipeline_trace, reindex, transcript_utils",
    "ExtractSessionFunction": "lambda_extract_session, extraction_pending, pipeline_trace",
    "RollingSummaryFunction": "lambda_rolling_summary, lambda_extract_session, extraction_pending",
    "NonWorkExpiryFunction": "deletion_mirror, extraction_pending, pipeline_trace, reindex",
    "FinalizeSweepFunction": "lambda_finalize_claim, batch_seal, transcript_utils",
    "SessionFinalizeFunction": "lambda_session_finalize, deletion_mirror, extraction_pending, pipeline_trace",
    "IngestFunction": "deletion_mirror, extraction_pending, match_request, pipeline_trace, reindex",
    "EmbedReportFunction": "lambda_embed_report, deletion_mirror, extraction_pending, match_request, reindex",
    "ItemWriterFunction": "lambda_item_writer, batch_seal, keyframe_request, lambda_finalize_claim, reindex",
    "KeyframeFunction": "lambda_keyframe, transcript_utils",
    "VoiceAuditFunction": "lambda_voice_audit",
    "ExtractionBacklogFunction": "lambda_extraction_backlog, extraction_pending",
    # Runs lambda_org_api, so it imports the same helpers org-api does. None of the three
    # operator tasks puts a tenant object, but the rule is "imports a caller", not "has
    # been seen calling".
    "LakeOpsFunction": "lambda_org_api and everything it imports",
}


# Handlers that import a lake_put module but never call its writing helpers. They stay
# WITHOUT the grant (RecordingSegments is pinned to "no PutObject" by its own test); the
# completeness scan below accepts them only if listed here with the reason.
IMPORTS_BUT_NEVER_WRITES = {
    "AskAgentFunction": "imports pipeline_trace via llm_usage; never flushes a trace",
    "MatcherFunction": "imports pipeline_trace via llm_usage; never flushes a trace",
    "SessionActivityFunction": "imports helpers; calls none of the writers",
    "RecordingSegmentsFunction": "reads transcripts only; its own test forbids PutObject",
    "SpeakerEmbedFunction": "reads a raw window via batch_seal; never seals",
}


def _manifest_writer_statement(block):
    for s in _statements(block):
        if _has_action(s, "s3:PutObject") and "lake_manifest/*" in s:
            return s
    return None


def test_every_lake_put_caller_can_put_its_manifest_line_in_both_buckets():
    res = _resources()
    for name in LAKE_PUT_CALLERS:
        assert name in res, f"{name} is not a template resource"
        s = _manifest_writer_statement(res[name])
        assert s, f"{name} has no s3:PutObject on lake_manifest/*"
        assert "${DataBucketName}/lake_manifest/*" in s, name
        assert "${IngestBucketName}/lake_manifest/*" in s, name
        # PutObject only: tagging and delete are not part of writing a manifest line.
        assert not _has_action(s, "s3:PutObjectTagging") and not _has_action(s, "s3:DeleteObject"), name
        assert "Resource: '*'" not in s


def test_the_caller_map_covers_every_handler_that_imports_a_lake_put_module():
    """Re-derive the map from source so a new importer fails here rather than in prod."""
    import ast

    modules = {p.stem for p in SRC.glob("*.py")}
    call = re.compile(r"\blake_put\(|\blake_upload_file\(")
    callers = {m for m in modules
               if m != "lake_put" and call.search((SRC / f"{m}.py").read_text(encoding="utf-8"))}
    assert callers, "no lake_put callers found: the scan is broken"

    def imports(m):
        tree = ast.parse((SRC / f"{m}.py").read_text(encoding="utf-8"))
        found = set()
        for n in ast.walk(tree):
            if isinstance(n, ast.Import):
                found |= {a.name.split(".")[0] for a in n.names}
            elif isinstance(n, ast.ImportFrom) and n.module and n.level == 0:
                found.add(n.module.split(".")[0])
        return found & modules

    res = _resources()
    for name, block in res.items():
        m = re.search(r"^      Handler: (\w+)\.\w+$", block, re.M)
        cu = re.search(r"^      CodeUri: (\S+)$", block, re.M)
        if not m or not cu or cu.group(1).rstrip("/") != "src" or m.group(1) not in modules:
            continue
        seen, todo = set(), [m.group(1)]
        while todo:
            x = todo.pop()
            if x in seen:
                continue
            seen.add(x)
            todo.extend(imports(x))
        if name in IMPORTS_BUT_NEVER_WRITES:
            assert _manifest_writer_statement(block) is None,                 f"{name} is listed as never writing, yet has a lake_manifest grant"
        if seen & callers:
            assert name in LAKE_PUT_CALLERS or name in IMPORTS_BUT_NEVER_WRITES, (
                f"{name} imports {sorted(seen & callers)}, which call lake_put, but it is "
                f"not in LAKE_PUT_CALLERS (and so has no lake_manifest/ grant)")


# --------------------------------------------------------------------------------------
# Listings the new code does must be listable
# --------------------------------------------------------------------------------------
def test_the_delimiter_listing_of_transcripts_is_allowed_where_it_runs():
    """The generator and the minutes lambda list `transcripts/` with Delimiter='/'. Their
    grants are SAM's S3ReadPolicy (ListBucket with no prefix condition); this pins that
    neither was narrowed to a conditioned grant that `transcripts/` might not match."""
    res = _resources()
    for name in ("ReportGeneratorFunction", "MeetingMinutesFunction"):
        block = res[name]
        assert "S3ReadPolicy" in block, name
        assert "BucketName: !Ref DataBucketName" in block, name
        assert "s3:prefix" not in block, f"{name} gained a ListBucket prefix condition"
    # org-api's own conditioned list for the data bucket allows the prefix by wildcard.
    org = res["OrgApiFunction"]
    data_list = [s for s in _statements(org) if _has_action(s, "s3:ListBucket")
                 and "arn:aws:s3:::${DataBucketName}\n" in s + "\n"]
    assert data_list and "- transcripts/*" in data_list[0]


def test_lake_manifest_and_exports_match_no_s3_trigger():
    script = (ROOT / "scripts" / "wire-s3-events.sh").read_text(encoding="utf-8")
    prefixes = re.findall(r'"Name":"prefix","Value":"([^"]*)"', script)
    assert prefixes, "no prefixes parsed from wire-s3-events.sh"
    for key in ("lake_manifest/2026/10/12/x.jsonl", "exports/c/e/manifest.json",
                "exports/c/e/part-0001.zip", "export_requests/c/e.json"):
        assert not any(key.startswith(p) for p in prefixes), key
    # Every filter in the script is prefixed, so no suffix-only rule can catch these either.
    assert len(re.findall(r'"Name":"suffix"', script)) <= len(prefixes)
    for key in ("lake_manifest/2026/10/12/x.jsonl", "exports/c/e/manifest.json",
                "export_requests/c/e.json"):
        for rule in _eventbridge_key_rules():
            kind, value = rule
            assert not (key.startswith(value) if kind == "prefix" else key.endswith(value)), (key, rule)


def _eventbridge_key_rules():
    return re.findall(r"^\s+- (prefix|suffix): (\S+)$", _text(), re.M)
