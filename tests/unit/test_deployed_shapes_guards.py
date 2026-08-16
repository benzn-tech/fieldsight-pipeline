"""Unit: two defects that only the deployed stack could show, pinned so they cannot return.

Both were found by inspecting TEST, not by this suite, and both are invisible to it for the
same reason CLAUDE.md already names: `FakeConn` never prepares SQL and no test double has an
IAM policy.
"""
import os
import re

import pytest

SRC = os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(
    os.path.abspath(__file__)))), "src")


def test_the_profile_insert_casts_its_case_parameters():
    """`CASE WHEN %s IS NULL THEN NULL ELSE now() END` gives Postgres a parameter whose only
    use is `IS NULL`, so it cannot infer a type and the INSERT fails at PREPARE time — every
    time, for any value. Observed on TEST as

        psycopg.errors.IndeterminateDatatype: could not determine data type of parameter $7
        at repositories/voiceprints.py upsert_profile

    which means naming a speaker who has no profile yet 500s deterministically. FakeConn
    does not prepare, so the suite ran green over it."""
    sql = open(os.path.join(SRC, "repositories", "voiceprints.py"), encoding="utf-8").read()
    for m in re.finditer(r"CASE WHEN %s(::[a-z]+)? IS NULL", sql):
        assert m.group(1), (
            "an untyped parameter used only in `IS NULL` — Postgres cannot infer it and the "
            "statement fails at prepare time; add an explicit cast")


def _function_block(role_marker):
    """Everything from one function's definition to the next one's.

    A fixed-size window was the first version of this and it silently measured the wrong
    function: org-api's ListBucket sits ~170 lines below its marker, past the window, so the
    test failed on a template that was already correct. Bounding on the next resource
    definition is the thing that was meant."""
    text = open(os.path.join(SRC, "template.yaml"), encoding="utf-8").read()
    start = text.index("\n  " + role_marker)
    nxt = re.search(r"\n  [A-Za-z]\w*(Function|Role|Api):", text[start + 3:])
    return text[start:start + 3 + (nxt.start() if nxt else len(text))]


def _list_bucket_prefixes(role_marker):
    """The s3:prefix ENTRIES of every ListBucket statement in one function's block.

    Deliberately not "does the block mention redactions/ anywhere": the first version
    asserted that, and it passed with the ListBucket prefix deleted — because the block also
    carries a GetObject `Resource: .../redactions/*`, which contains the same substring.
    GetObject is exactly the grant that is NOT sufficient here, so the loose test asserted
    the presence of the thing the strict one exists to distinguish from. The revert-check
    caught it; nothing else would have."""
    block = _function_block(role_marker)
    if "s3:ListBucket" not in block:
        pytest.fail(f"{role_marker} has no ListBucket statement at all")
    prefixes = []
    for m in re.finditer(r"s3:prefix:\s*\n((?:\s*(?:#[^\n]*|-\s*\S+)\n)+)", block):
        prefixes += re.findall(r"-\s*(\S+)", m.group(1))
    return prefixes


@pytest.mark.parametrize("marker", ["OrgApiFunction:", "IngestFunction:"])
def test_the_deletion_mirror_prefix_is_listable(marker):
    """Without ListBucket on `redactions/`, a MISSING mirror answers 403 rather than 404.
    `deleted_sessions_strict` raises `MirrorUnreadable` on anything that is not NoSuchKey, so
    the delete endpoint skips the mirror write — and since the mirror is missing precisely
    because it has never been written, the first delete of a day can never create it. Ever.

    Empirically: `aws s3 ls s3://…/redactions/ --recursive` returned zero objects, and
    org-api logged the AccessDenied → MirrorUnreadable traceback on a real delete.

    The consequence is quiet and total: the DB tombstones commit, the endpoint returns OK,
    the web hides the content — and the nightly report, the emailed report and Ask keep
    serving the deleted recording, because those read the mirror and there is none."""
    prefixes = _list_bucket_prefixes(marker)
    assert prefixes, f"{marker}'s ListBucket has no s3:prefix condition to read"
    assert "redactions/*" in prefixes, (
        f"{marker} can GetObject on redactions/* but cannot LIST it — its ListBucket "
        f"prefixes are {prefixes}. A missing mirror is then a 403, not a 404, and the "
        "mirror can never be created.")
