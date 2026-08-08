# tests/unit/test_template_item_writer_group_iam.py
"""item-writer must be able to enqueue the updated-email requests.

This grant was `implicitDeny` on the deployed test role when Phase C's code was
written, and the failure it produces is silent by construction: the merged
topics land, every member's own topics are deleted, and then nobody is told —
because a missing S3 grant surfaces as an exception in a post-commit step that
deliberately swallows it rather than undoing a durable merge.

Fourth recurrence of the same shape in this repo (CLAUDE.md BUG-43 lesson 3, the
org-api route trap, and PR #288's ListBucket gap were the first three), so it is
pinned in CI rather than left to be found again with simulate-principal-policy.

Text-level assertions, same approach as test_template_org_api_media_iam.py: the
template is full of CFN intrinsics a plain YAML loader cannot resolve, and the
point here is the literal grant text.
"""
import re
from pathlib import Path

TEMPLATE = Path(__file__).resolve().parents[2] / "src" / "template.yaml"


def _item_writer_block():
    """The ItemWriterFunction resource body only, so a grant belonging to some
    OTHER function can never satisfy these."""
    text = TEMPLATE.read_text(encoding="utf-8")
    start = text.index("\n  ItemWriterFunction:\n")
    nxt = re.search(r"\n  [A-Za-z][A-Za-z0-9]*:\n", text[start + 1:])
    return text[start:start + 1 + nxt.start()] if nxt else text[start:]


def test_it_may_write_the_updated_email_requests():
    block = _item_writer_block()
    assert "arn:aws:s3:::${IngestBucketName}/session_finalize_requests/*" in block, \
        ("item-writer cannot enqueue the updated-email requests: the merge would "
         "publish and delete, and nobody would be told")


def test_it_may_read_the_merged_artifact_it_compares_against():
    # The suppression check reads the merged extraction to compare coverage.
    # Without GetObject it reads as unreadable, which errs towards re-merging —
    # not dangerous, but it would re-merge and re-email every group forever.
    block = _item_writer_block()
    assert "arn:aws:s3:::${IngestBucketName}/extractions/*" in block


def test_listbucket_accompanies_the_extractions_read():
    # S3 answers 403, not 404, for a missing key unless the caller holds
    # ListBucket for it — so "has this group published yet" would read as
    # DENIED where it means NO. Exactly the defect PR #288 fixed one prefix over.
    block = _item_writer_block()
    m = re.search(r"Action: s3:ListBucket\s*\n\s*Resource: !Sub arn:aws:s3:::"
                  r"\$\{IngestBucketName\}\s*\n\s*Condition:\s*\n\s*StringLike:\s*\n"
                  r"\s*s3:prefix:\s*\n((?:\s*- \S+\n)+)", block)
    assert m, "ItemWriterFunction has no prefix-conditioned ListBucket"
    prefixes = {ln.strip().lstrip("- ") for ln in m.group(1).splitlines() if ln.strip()}
    assert "extractions/*" in prefixes
