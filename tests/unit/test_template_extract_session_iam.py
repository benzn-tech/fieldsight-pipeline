# tests/unit/test_template_extract_session_iam.py
"""ExtractSessionFunction must be able to tell "nothing is published yet" from
"I was not allowed to look".

S3 returns 404 NoSuchKey for a missing object ONLY to a caller that holds
s3:ListBucket for that key; without it the answer is 403 AccessDenied, which is
indistinguishable from a real permission failure. `read_existing_extraction`
maps 404 -> None (absent, go ahead) and anything else -> UNKNOWN (leave what is
published alone), so a missing ListBucket grant on extractions/ turns the FIRST
pass of every new session into `skipping live pass, cannot read what is
published`. Live extraction stops entirely; only the close-driven final pass
still runs, and nothing raises.

Observed on fieldsight-test 2026-08-08 with the grant conditioned on
transcripts/* alone, and confirmed with
`aws iam simulate-principal-policy --action-names s3:ListBucket`:
transcripts/ -> allowed, extractions/ -> implicitDeny.

This is the third recurrence of the same shape (CLAUDE.md BUG-43 lesson 3): a
function gained a read of its own output, the grant did not follow, and the
failure was silent. Text-level assertions, same approach as
test_template_org_api_media_iam.py."""
import re
from pathlib import Path

TEMPLATE = Path(__file__).resolve().parents[2] / "src" / "template.yaml"


def _extract_session_block():
    """The ExtractSessionFunction resource body only, so a grant belonging to
    some other function can never satisfy these."""
    text = TEMPLATE.read_text(encoding="utf-8")
    start = text.index("\n  ExtractSessionFunction:\n")
    nxt = re.search(r"\n  [A-Za-z][A-Za-z0-9]*:\n", text[start + 1:])
    return text[start:start + 1 + nxt.start()] if nxt else text[start:]


def _listbucket_prefixes(block):
    m = re.search(r"Action: s3:ListBucket\s*\n\s*Resource: !Sub arn:aws:s3:::"
                  r"\$\{IngestBucketName\}\s*\n\s*Condition:\s*\n\s*StringLike:\s*\n"
                  r"\s*s3:prefix:\s*\n((?:\s*- \S+\n)+)", block)
    assert m, "ExtractSessionFunction has no prefix-conditioned ListBucket"
    return {line.strip().lstrip("- ") for line in m.group(1).splitlines() if line.strip()}


def test_may_list_transcripts_it_gathers():
    # gather_session_segments paginates this prefix — the original reason the
    # grant exists.
    assert "transcripts/*" in _listbucket_prefixes(_extract_session_block())


def test_may_list_extractions_so_a_missing_key_reads_as_absent():
    # Not because anything lists extractions/, but because without it S3 answers
    # 403 instead of 404 for a key that simply does not exist yet — and the
    # throttle read then refuses to run the pass at all.
    prefixes = _listbucket_prefixes(_extract_session_block())
    assert "extractions/*" in prefixes, (
        "extractions/* missing from the ListBucket s3:prefix condition: a "
        "not-yet-written extraction key returns AccessDenied, read_existing_"
        "extraction returns UNKNOWN, and every live pass is skipped")


def test_still_holds_the_getobject_grant_the_listbucket_complements():
    # ListBucket alone does not let it read the object; both are required, and a
    # cleanup that removed either would restore the silent failure.
    assert "arn:aws:s3:::${IngestBucketName}/extractions/*" in _extract_session_block()
