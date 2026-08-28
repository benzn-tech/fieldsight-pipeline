"""Unit: every S3 prefix org-api builds a key for is a prefix its role may touch.

`GET /sessions/{id}/brief` shipped last night and answered **500 for every session**. Not for
the interesting reason — the handler is right, and it has a `pending` branch for a session
with no brief yet. That branch was unreachable: `OrgApiFunctionRole` had no grant on
`session_brief/*`, and without one **S3 answers AccessDenied instead of NoSuchKey**, so the
`except ClientError` fell through to the re-raise.

That is the shape twice already in this repository's notes — a new route reaching a new prefix
whose IAM nobody extended — and both previous times the symptom was *not* an error page. Once
it was an empty list (`except ClientError: pass` → 200 with nothing in it) and once a silently
skipped throttle. This time it was a 500, which is the luckiest of the three outcomes and the
only reason it was found on the same day.

So the guard is not "session_brief is granted". It is the class: **every literal prefix org-api
composes an S3 key from must appear in its own policy.** The handler and the template are two
files that never mention each other, and the endpoint is written weeks before anyone deletes a
recording or opens a brief that does not exist.
"""
import os
import re

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
SRC = os.path.join(ROOT, "src", "lambda_org_api.py")
TEMPLATE = os.path.join(ROOT, "src", "template.yaml")

# Prefixes that are not S3 keys at all, or are reached through a helper that names its own
# bucket. Listed rather than pattern-matched so adding one is a decision, not a slip.
NOT_A_LAKE_PREFIX = {
    "https", "http", "s3", "arn", "application", "text", "image", "audio", "video",
    "multipart", "Bearer", "attachment",
}

#: Prefixes org-api composes as **database values**, never as objects it fetches.
#: `topics.source_s3_key` records where a topic came from, and `_source_prefixes_for` builds
#: the string a tombstone matches on — neither is an S3 call, and org-api deletes no objects
#: (deletion is a tombstone; the lake keeps every byte). Granting these would hand the API a
#: read on the raw extraction corpus for no route that wants one.
#: Checked, not assumed: `test_these_really_are_only_database_values` below.
DATABASE_VALUES_NOT_S3_KEYS = {"extractions"}


def _org_api_policy():
    """The OrgApiFunction Policies block, as text.

    Read as text rather than parsed: the template is full of `!Sub`, `!If` and `!Ref` tags a
    plain YAML loader rejects, and the question here — "does this prefix appear in this
    function's grants" — is answerable without a parser.
    """
    text = open(TEMPLATE, encoding="utf-8").read()
    start = text.index("\n  OrgApiFunction:\n")
    nxt = re.search(r"\n  [A-Z]\w*:\n    Type: AWS::", text[start + 20:])
    return text[start: start + 20 + (nxt.start() if nxt else len(text))]


def _key_prefixes():
    """Every `f"prefix/..."` that looks like a lake key, from the handler source."""
    src = open(SRC, encoding="utf-8").read()
    found = set()
    # `key = f"session_brief/{folder}/..."` and the Key= form used inline.
    for m in re.finditer(r'f"([a-z][a-z0-9_]*)/\{', src):
        found.add(m.group(1))
    return {p for p in found
            if p not in NOT_A_LAKE_PREFIX and p not in DATABASE_VALUES_NOT_S3_KEYS}


def test_the_scan_finds_the_prefixes_it_is_meant_to():
    """Fails OPEN otherwise: an empty prefix set would make the real test below vacuously
    green while reporting success, which is the same failure it exists to catch."""
    prefixes = _key_prefixes()
    assert {"session_rolling", "session_brief", "voiceprint_requests"} <= prefixes, sorted(
        prefixes)


def test_every_prefix_org_api_builds_a_key_for_is_granted():
    policy = _org_api_policy()
    missing = sorted(p for p in _key_prefixes() if f"/{p}/*" not in policy)
    assert not missing, (
        f"org-api composes S3 keys under {missing} and its role has no statement naming "
        f"them. A missing GetObject does not read as 'forbidden' at the call site — S3 "
        f"returns AccessDenied where the handler expects NoSuchKey, so the 'nothing here "
        f"yet' branch becomes an unhandled error or, worse, a swallowed empty result.")


def test_the_policy_block_is_the_right_one():
    """The extractor above walks the template by hand. If it ever returned the wrong
    function's block — or the whole file — the test above would pass on somebody else's
    grants."""
    policy = _org_api_policy()
    assert "OrgApiFunction" in policy
    assert "voiceprint_requests/*" in policy, "this is not org-api's policy block"
    assert "SessionFinalizeFunction" not in policy, "the block ran past its own function"


def test_these_really_are_only_database_values():
    """The exemption above is the whole way this guard can be wrong, so it is verified
    rather than trusted. Every `f"extractions/..."` in org-api must be an argument to a
    repository call, not to `s3()` — the moment one becomes an object read, the exemption
    turns into the missing grant it was written to explain away."""
    src = open(SRC, encoding="utf-8").read()
    for prefix in DATABASE_VALUES_NOT_S3_KEYS:
        for m in re.finditer(r'f"%s/\{' % prefix, src):
            line_start = src.rfind("\n", 0, m.start()) + 1
            line_end = src.index("\n", m.end())
            line = src[line_start:line_end]
            assert "s3(" not in line and "Key=" not in line, (
                f"{prefix}/ is exempted from the grant check because it is a database "
                f"value, and here it is being used as an S3 key: {line.strip()}")
