"""Every place that treats a missing S3 object as a NORMAL state must be able to see one.

S3 answers `AccessDenied` — not `NoSuchKey` — for a key that does not exist, unless the
caller holds `ListBucket` on that prefix. So code shaped like

    try:
        obj = s3().get_object(...)
    except ClientError as e:
        if code in ("NoSuchKey", "404"):
            return <the normal empty state>
        raise

is not merely unprotected without the grant: its normal-state branch is **unreachable**, and
what the caller gets instead is whatever the re-raise turns into. In org-api that was a 500 on
three endpoints; in ingest it is a report that fails instead of skipping.

This repository has now shipped that shape six times — `programmes/`, `reports/`,
`redactions/`, `session_brief/`, `session_rolling/`, `session_report_results/` — and each was
found by reading a 500, one at a time. The seventh (`embeddings/`) was found instead by
sweeping every prod role for GetObject-allowed / ListBucket-denied, before anything failed.

This test is the sweep, kept. The table below is the list of (function, prefix) pairs whose
code has a missing-key branch; adding a `get_object` with a `NoSuchKey` arm means adding a row
here on the same day, and the row fails until the grant exists.

Asserts against `template.yaml`, which is what actually reaches IAM. A unit test cannot
evaluate a policy — after deploy the check is `simulate-principal-policy` against the live
role, and the endpoint call that used to fail.
"""
import pathlib
import re

import pytest

TEMPLATE = pathlib.Path(__file__).resolve().parents[2] / "src" / "template.yaml"

# (function, prefix, what the missing-key branch means)
MISSING_KEY_IS_NORMAL = [
    ("OrgApiFunction", "session_brief/*",
     "GET /sessions/{id}/brief -> {'status': 'pending'} until the brief is written"),
    ("OrgApiFunction", "session_rolling/*",
     "GET /sessions/{id}/rolling -> pending until the summariser first runs"),
    ("OrgApiFunction", "session_report_results/*",
     "GET /sessions/{id}/report/status -> pending until the worker finishes"),
    ("OrgApiFunction", "programmes/*",
     "read_programme on a site that has never had a programme uploaded"),
    ("OrgApiFunction", "redactions/*",
     "the deletion mirror, missing because nothing has been deleted yet"),
    ("IngestFunction", "embeddings/*",
     "ingest_report skips a report whose vector sidecar was never written"),
    ("IngestFunction", "redactions/*",
     "_load_turns reads the mirror; missing must mean 'nothing deleted'"),
    # Every remaining reader of the deletion mirror. Six lambdas call
    # `deletion_mirror.deleted_sessions`, and a day with NO deletions has no mirror -- which
    # is the common case, not the edge one, so the missing-key branch here is the branch
    # that runs almost every time.
    #
    # These readers are LENIENT by design: an unreadable mirror answers "nothing was
    # deleted" behind one WARNING rather than failing the report. That makes a missing grant
    # WORSE here than in the strict readers above, not better -- it does not 500, it ships a
    # guard that never guards, and the only trace is a log line nobody is reading. #648
    # found exactly that on SessionReportFunction: implicitDeny on both stages, mailing a
    # session the customer had removed, with the check in place and inert.
    #
    # Swept against the live prod roles on 2026-08-31 rather than read off this file.
    ("AskAgentFunction", "redactions/*",
     "_deleted_sessions gates the stored report and the RAG chunks"),
    ("ReportGeneratorFunction", "redactions/*",
     "the nightly rebuild must not re-ingest a removed session"),
    ("SessionFinalizeFunction", "redactions/*",
     "_session_was_deleted gates the confirmation email"),
    ("SessionReportFunction", "redactions/*",
     "_session_was_deleted gates rendering and mailing the on-demand report"),
]


def _blocks():
    """Each top-level `SomethingFunction:` resource as raw text.

    Text, not parsed YAML: the template carries `!Sub`/`!Ref` tags that a plain YAML loader
    refuses, and the sibling grant test (test_agent_turn_filter_wiring) settled on the same
    approach for the same reason.
    """
    text = TEMPLATE.read_text(encoding="utf-8")
    starts = [(m.group(1), m.start())
              for m in re.finditer(r"^  ([A-Za-z0-9]+Function):", text, re.M)]
    out = {}
    for i, (name, pos) in enumerate(starts):
        end = starts[i + 1][1] if i + 1 < len(starts) else len(text)
        out[name] = text[pos:end]
    return out


def _reads_whole_bucket(block):
    """A SAM-managed bucket-wide read, which grants ListBucket with no prefix condition.

    Two of the mirror readers carry `S3ReadPolicy` instead of scoped inline statements, so
    they have no `s3:prefix` list to inspect and yet answer `allowed` to
    simulate-principal-policy -- confirmed against the live prod roles on 2026-08-31 before
    this branch was added, because a test that waves a function through on a pattern nobody
    checked is how a grant goes missing quietly.

    Adding a scoped grant to these would be redundant, and worse than redundant: it would
    teach the next reader that the scoped form is required when it is not. The sibling test
    test_agent_turn_filter_wiring takes the same position for the same functions.
    """
    return "S3ReadPolicy:" in block or "S3CrudPolicy:" in block


def _list_prefixes(block):
    """Every prefix under every `s3:prefix:` condition in this block.

    Plural on purpose — a function may carry more than one ListBucket statement (org-api has
    one per bucket), and reading only the first reports a present grant as missing.
    Comment lines are skipped rather than ending the sequence, which is how the sibling test
    once failed against a correct template.
    """
    prefixes = []
    lines = block.split("\n")
    for i, line in enumerate(lines):
        if line.strip() != "s3:prefix:":
            continue
        for nxt in lines[i + 1:]:
            stripped = nxt.strip()
            if stripped.startswith("#"):
                continue
            if stripped.startswith("- "):
                prefixes.append(stripped[2:].strip())
                continue
            break
    return prefixes


@pytest.mark.parametrize("fn,prefix,why", MISSING_KEY_IS_NORMAL,
                         ids=[f"{f}:{p}" for f, p, _ in MISSING_KEY_IS_NORMAL])
def test_a_missing_key_can_be_told_apart_from_a_denied_one(fn, prefix, why):
    block = _blocks().get(fn)
    assert block, f"{fn} is not in template.yaml"
    if _reads_whole_bucket(block):
        return
    granted = _list_prefixes(block)
    assert prefix in granted, (
        f"{fn} has GetObject but no ListBucket on {prefix} — so {why} raises AccessDenied "
        f"instead of NoSuchKey and the branch that handles it never runs. "
        f"Granted prefixes: {granted}")
