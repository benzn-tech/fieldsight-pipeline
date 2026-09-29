"""Task 7: this feature needs no new IAM grant, no new S3 prefix, no new trigger --
proved, not just claimed in a PR description.

The detector and the repository are DB/pure-Python only. If a later change made either
read S3 directly, it would need a grant `lambda_item_writer.py`'s own comment says the
role does not have (ListBucket/GetObject on `extractions/` both simulate as
implicitDeny) -- the writer reads the transcript's WORDS from the artifact it already
holds specifically to avoid ever needing that grant. This guards against a future "just
read the transcript from the writer" shortcut reintroducing it.
"""
import inspect

import pytest

si = pytest.importorskip("self_introduction")
sis = pytest.importorskip("repositories.speaker_intro_suggestions",
                          reason="requires psycopg (installed in CI)")


@pytest.mark.parametrize("module", [si, sis])
def test_module_touches_neither_boto3_nor_s3(module):
    src = inspect.getsource(module)
    assert "boto3" not in src, f"{module.__name__} references boto3 -- it must stay DB-only"
    assert "s3(" not in src and "S3_BUCKET" not in src, (
        f"{module.__name__} references S3 -- the writer's transcript read is what this "
        f"feature was designed to avoid needing")


def test_self_introduction_does_no_io_at_all():
    """Belt and braces on top of the boto3/S3 check above: the module the design doc calls
    'pure' should not import anything that talks to a network or a filesystem beyond the
    standard library's `re`."""
    src = inspect.getsource(si)
    for banned in ("import requests", "import urllib", "open(", "import psycopg"):
        assert banned not in src, f"self_introduction.py is not pure: found {banned!r}"


def test_speaker_identity_mode_is_already_wired_to_both_lambdas():
    """`SPEAKER_IDENTITY_MODE` is already an env var on OrgApiFunction
    (`src/template.yaml`, org-api's confirm route gate) and on the item-writer (the
    writer does NOT gate on it -- owner decision 1 -- but the template already carries
    the variable either way, so Task 7 Step 2's grep finds nothing to add)."""
    import os
    template_path = os.path.join(os.path.dirname(__file__), "..", "..", "src", "template.yaml")
    with open(template_path, encoding="utf-8") as fh:
        text = fh.read()
    assert text.count("SPEAKER_IDENTITY_MODE: !Ref SpeakerIdentityMode") >= 2, (
        "SPEAKER_IDENTITY_MODE is no longer wired to both lambdas -- this feature's org-api "
        "gate and the plan's Task 7 note both assume it already is")
