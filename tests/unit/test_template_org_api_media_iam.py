# tests/unit/test_template_org_api_media_iam.py
"""Adversarial review BLOCKER 1: the P1 media routes (/audio-segments,
/video-segments, /media/presigned-url) list and read S3 prefixes that the
hand-written least-privilege OrgApiFunction role never granted, so they would
have shipped as a silent no-op — list_objects_v2 raises AccessDenied, the
handler swallowed it, and the frontend rendered the exact empty state the
branch exists to fix. Verified against the live prod role with
`aws iam simulate-principal-policy` (implicitDeny on audio_segments/,
web_video/ and users/ listings).

Text-level assertions, same approach as test_template_pgdatabase.py: the
template is full of CFN intrinsics (!Sub/!Ref/!ImportValue) that a plain YAML
loader cannot resolve, and the point here is the literal grant text."""
import re
from pathlib import Path

import pytest

TEMPLATE = Path(__file__).resolve().parents[2] / "src" / "template.yaml"


def _org_api_block():
    """The OrgApiFunction resource body only (2-space-indented resource key),
    so a grant belonging to some OTHER function can never satisfy these."""
    text = TEMPLATE.read_text(encoding="utf-8")
    start = text.index("\n  OrgApiFunction:\n")
    nxt = re.search(r"\n  [A-Za-z][A-Za-z0-9]*:\n", text[start + 1:])
    return text[start:start + 1 + nxt.start()] if nxt else text[start:]


@pytest.mark.parametrize("prefix", ["audio_segments", "web_video"])
def test_org_api_role_grants_getobject_on_media_prefixes(prefix):
    # Read-only, and read-only is enough: the routes list these prefixes and
    # mint presigned GETs, which are invalid unless the role itself may
    # GetObject the key.
    block = _org_api_block()
    assert f"arn:aws:s3:::${{DataBucketName}}/{prefix}/*" in block, \
        f"OrgApiFunction has no s3:GetObject grant on {prefix}/*"


def test_org_api_role_lists_every_prefix_the_media_routes_paginate():
    # ListBucket is prefix-CONDITIONED, so GetObject alone is not enough:
    # list_objects_v2 over an unlisted prefix is AccessDenied. users/* is in
    # here because /video-segments falls back to users/{folder}/video/ for
    # originals when no web_video/ preview exists.
    block = _org_api_block()
    m = re.search(r"Action: s3:ListBucket\s*\n\s*Resource: !Sub arn:aws:s3:::"
                  r"\$\{DataBucketName\}\s*\n\s*Condition:\s*\n\s*StringLike:\s*\n"
                  r"\s*s3:prefix:\s*\n((?:\s*- \S+\n)+)", block)
    assert m, "OrgApiFunction has no prefix-conditioned ListBucket on DataBucketName"
    prefixes = {line.strip().lstrip("- ") for line in m.group(1).splitlines() if line.strip()}
    for needed in ("programmes/*", "transcripts/*", "audio_segments/*",
                   "web_video/*", "users/*"):
        assert needed in prefixes, f"{needed} missing from the ListBucket s3:prefix condition"
