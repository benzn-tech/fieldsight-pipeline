"""The grant that makes the §2.2 brief poll more than a permanent fallback.

`SessionFinalizeFunction` already had `s3:PutObject` on `session_brief/*` (it
stores the brief it re-summarises). Without `s3:GetObject` there too, the
final email's poll for the CONCURRENTLY-requested brief (handoff-sync plan
§2.2) is `AccessDenied` on every attempt, which a lenient reader turns into
"not ready yet" -- so every email would silently fall back to the request's
own rows, forever, with every unit test green (the same shape #648 shipped
for the deletion mirror).

Source-level, same posture as test_finalize_can_read_the_deletion_mirror.py:
a unit test cannot evaluate a live IAM policy, but it can catch the grant
being absent, or removed by a later edit, before it ever reaches a deploy.
`test_a_missing_key_can_be_told_apart_from_a_denied_one` in
test_missing_key_must_read_as_404.py additionally pins the ListBucket half.
"""
import io

import pytest

yaml = pytest.importorskip("yaml")


class _Loader(yaml.SafeLoader):
    pass


for _tag in ("!Sub", "!Ref", "!If", "!Not", "!Equals", "!GetAtt", "!FindInMap",
             "!Join", "!Condition", "!Select", "!Split", "!ImportValue", "!And", "!Or"):
    _Loader.add_constructor(_tag, lambda loader, node: getattr(node, "value", None))


def _as_list(v):
    return v if isinstance(v, list) else [v]


def _finalize_policy_statements():
    doc = yaml.load(io.open("src/template.yaml", encoding="utf-8").read(), Loader=_Loader)
    fn = doc["Resources"]["SessionFinalizeFunction"]["Properties"]
    out = []
    for policy in fn.get("Policies") or []:
        if isinstance(policy, dict):
            out.extend(policy.get("Statement") or [])
    return out


def test_finalize_may_read_the_session_brief_prefix():
    hits = [s for s in _finalize_policy_statements()
            if "s3:GetObject" in _as_list(s.get("Action"))
            and any("session_brief/" in str(r) for r in _as_list(s.get("Resource")))]
    assert hits, ("SessionFinalizeFunction cannot GetObject session_brief/* -- "
                  "the §2.2 brief poll is permanently AccessDenied, which reads "
                  "as 'not ready yet' and every final email falls back silently")


def test_finalize_still_keeps_its_put_grant_on_the_brief_prefix():
    """The write side (_store_brief) predates this and must not regress while
    adding the read side next to it."""
    hits = [s for s in _finalize_policy_statements()
            if "s3:PutObject" in _as_list(s.get("Action"))
            and any("session_brief/" in str(r) for r in _as_list(s.get("Resource")))]
    assert hits, "SessionFinalizeFunction lost its PutObject grant on session_brief/*"
