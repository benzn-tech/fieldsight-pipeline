# tests/unit/test_template_reconcile_iam.py
"""Cross-session status reconcile R0 (Task 4): the grants and env both ends need.

item-writer (in-VPC) writes reconcile_requests/; the non-VPC MatcherFunction consumes it
and verifies quotes against the session's own transcript, which means it reads
transcripts/, voice_ask/ (agent_turn_filter) and audio_segments/ (the batch time map read
by _rebase_batch_turns). A missing grant here is silent by construction: a denied
batch-map read is swallowed and quietly shifts every anchor, and a denied put is
swallowed post-commit.

Text-level assertions, same approach as test_template_item_writer_group_iam.py.
"""
import re
from pathlib import Path

TEMPLATE = Path(__file__).resolve().parents[2] / "src" / "template.yaml"


def _block(name):
    text = TEMPLATE.read_text(encoding="utf-8")
    start = text.index(f"\n  {name}:\n")
    nxt = re.search(r"\n  [A-Za-z][A-Za-z0-9]*:\n", text[start + 1:])
    return text[start:start + 1 + nxt.start()] if nxt else text[start:]


def _ingest_list_prefixes(block):
    m = re.search(r"Action: s3:ListBucket\s*\n\s*Resource: !Sub arn:aws:s3:::"
                  r"\$\{IngestBucketName\}\s*\n\s*Condition:\s*\n\s*StringLike:\s*\n"
                  r"\s*s3:prefix:\s*\n((?:\s*(?:- \S+|#.*)\n)+)", block)
    assert m, "no prefix-conditioned ListBucket on the ingest bucket"
    return {ln.strip().lstrip("- ") for ln in m.group(1).splitlines()
            if ln.strip() and not ln.strip().startswith("#")}


def test_item_writer_may_put_reconcile_requests():
    block = _block("ItemWriterFunction")
    assert re.search(r"Action: s3:PutObject\s*\n\s*Resource: !Sub arn:aws:s3:::"
                     r"\$\{IngestBucketName\}/reconcile_requests/\*", block)


def test_item_writer_env():
    block = _block("ItemWriterFunction")
    assert "RECONCILE_STATUS: !Ref ReconcileStatus" in block
    assert "RECONCILE_CANDIDATE_CAP: '40'" in block


def test_matcher_may_read_what_the_worker_reads():
    block = _block("MatcherFunction")
    for prefix in ("reconcile_requests", "transcripts", "voice_ask", "audio_segments"):
        assert f"- !Sub arn:aws:s3:::${{IngestBucketName}}/{prefix}/*" in block, prefix


def test_matcher_may_list_so_a_missing_key_is_404_not_403():
    prefixes = _ingest_list_prefixes(_block("MatcherFunction"))
    assert {"match_requests/*", "reconcile_requests/*", "transcripts/*",
            "voice_ask/*"} <= prefixes


def test_matcher_has_the_evidence_env_with_extract_sessions_values():
    matcher, extract = _block("MatcherFunction"), _block("ExtractSessionFunction")
    for name in ("STATUS_EVIDENCE_FLOOR_TOKENS", "EVIDENCE_WINDOW_SEC",
                 "EVIDENCE_FUZZY_THRESHOLD"):
        want = re.search(rf"\n\s*{name}: (.+)\n", extract).group(1).strip()
        got = re.search(rf"\n\s*{name}: (.+)\n", matcher)
        assert got, f"MatcherFunction lacks {name}"
        assert got.group(1).strip() == want, name


def test_reconcile_status_parameter_defaults_off():
    text = TEMPLATE.read_text(encoding="utf-8")
    params = re.search(r"^Parameters:\n(.*?)^[A-Za-z]", text, re.S | re.M).group(1)
    seg = re.search(r"^  ReconcileStatus:\s*$(.*?)(?=^  \w+:\s*$|\Z)", params, re.S | re.M)
    assert seg, "no ReconcileStatus parameter"
    assert "Default: 'false'" in seg.group(1)
    assert "AllowedValues: ['true', 'false']" in seg.group(1)


def test_matcher_mirrors_extract_sessions_turn_env():
    """The worker renders turns with assemble_session_turns, which reads the announcement and
    audio-tag knobs at import; a value set only on ExtractSessionFunction would let the
    reconcile model see a different transcript than extraction did."""
    matcher, extract = _block("MatcherFunction"), _block("ExtractSessionFunction")
    for name in ("DEVICE_ANNOUNCEMENT_PATTERNS", "FILTER_AUDIO_EVENT_TAGS"):
        want = re.search(rf"\n\s*{name}: (.+)\n", extract).group(1).strip()
        got = re.search(rf"\n\s*{name}: (.+)\n", matcher)
        assert got, f"MatcherFunction lacks {name}"
        assert got.group(1).strip() == want, name

def test_matcher_keeps_the_default_http_timeout():
    """The reconcile call is bounded by the Lambda's remaining time, not by LLM_HTTP_TIMEOUT:
    setting it on MatcherFunction would change the programme match/impact calls too."""
    assert not re.search(r"^\s*LLM_HTTP_TIMEOUT:", _block("MatcherFunction"), re.M)


def test_matcher_can_write_reconcile_results():
    """The matcher writes one reconcile_results/ marker per processed request through
    lake_put, which also appends a lake_manifest/ line in the same bucket."""
    block = _block("MatcherFunction")
    lists = re.findall(r"Action: s3:PutObject\s*\n\s*Resource:\s*\n((?:\s*(?:- \S+.*|#.*)\n)+)",
                       block)
    assert lists, "MatcherFunction has no PutObject statement"
    resources = {ln.strip().lstrip("- ") for group in lists for ln in group.splitlines()
                 if ln.strip() and not ln.strip().startswith("#")}
    assert "!Sub arn:aws:s3:::${IngestBucketName}/reconcile_results/*" in resources
    assert "!Sub arn:aws:s3:::${IngestBucketName}/lake_manifest/*" in resources
