"""Unit: every prefix ingest LISTS at runtime is inside its ListBucket condition.

Prod, 2026-09-06. `_list_report_pictures` lists
`users/{folder}/pictures/{date}/` to bind photos onto report topics, and that
prefix was not in the grant. The call is not inside a `try`, so AccessDenied
aborted the whole S3-triggered ingest: one delivery, two retries, and
`reports/2026-09-03/Ben_UCPK2` was never written to Aurora at all -- the day
simply did not exist. ItemWriter has carried `users/*` for the same call since
Task 3; ingest grew the call and not the grant.

The check drives the code for the prefix rather than repeating the string,
because the string is the thing that drifts. A grep-style test would have
matched a `users/*` that a rename had already made meaningless.
"""
import fnmatch
import os
import re

import pytest

ingest = pytest.importorskip("lambda_ingest")

TEMPLATE = os.path.join(os.path.dirname(__file__), "..", "..", "src", "template.yaml")


def _list_prefixes(resource):
    """The s3:prefix patterns on `resource`'s s3:ListBucket statement.

    Parsed from the resource's own block: the same key appears under other
    functions, and matching the wrong one would assert nothing about this role.
    """
    lines = open(TEMPLATE, encoding="utf-8").read().splitlines()
    start = next(i for i, l in enumerate(lines) if l.startswith(f"  {resource}:"))
    end = next((i for i in range(start + 1, len(lines)) if re.match(r"^  \S", lines[i])),
               len(lines))
    block = lines[start:end]
    i = next(i for i, l in enumerate(block) if "Action: s3:ListBucket" in l)
    out = []
    for l in block[i:]:
        s = l.strip()
        if s.startswith("- ") and not s.startswith("- !Sub") and "*" in s:
            out.append(s[2:].strip())
        elif s.startswith("- Effect:") and out:
            break
    return out


def _prefix_the_photo_binding_asks_for():
    """What the deployed code actually passes to list_objects_v2."""
    seen = {}

    class _S3:
        def get_paginator(self, name):
            class _P:
                def paginate(_self, Bucket=None, Prefix=None):
                    seen["prefix"] = Prefix
                    return iter(())
            return _P()

    original = ingest.s3
    ingest.s3 = lambda: _S3()
    try:
        ingest._list_report_pictures("Ben_UCPK2", "2026-09-03")
    finally:
        ingest.s3 = original
    return seen["prefix"]


def test_ingest_may_list_the_pictures_prefix_it_actually_uses():
    prefix = _prefix_the_photo_binding_asks_for()
    assert prefix == "users/Ben_UCPK2/pictures/2026-09-03/"      # the call is real
    patterns = _list_prefixes("IngestFunction")
    assert any(fnmatch.fnmatch(prefix, p) for p in patterns), (
        f"ingest lists {prefix!r} but its ListBucket condition allows {patterns}"
    )


def test_the_item_writer_grant_this_was_copied_from_is_still_there():
    """The two functions list the same prefix through the same helper. If one
    grant is removed the other is the surviving evidence of what is needed."""
    assert any(fnmatch.fnmatch("users/x/pictures/2026-09-03/", p)
               for p in _list_prefixes("ItemWriterFunction"))
