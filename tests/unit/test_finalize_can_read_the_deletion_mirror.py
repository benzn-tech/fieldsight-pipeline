"""The grant that makes `_session_was_deleted` more than a WARNING line.

That check reads the deletion mirror LENIENTLY: a failure answers "nothing was
deleted" and logs. So a missing grant does not break finalize, it makes the whole
check inert -- one log line per invocation and a deleted recording mailed back to
the person who deleted it, with every unit test green.

`simulate-principal-policy` returned implicitDeny on BOTH stages before the grant
was added, which is exactly what that looks like from the outside: nothing.

Parsed structurally rather than grepped: `redactions/` appears in several roles'
policies, so a whole-file search would pass while THIS function had none.
"""
import io
import re

import pytest

yaml = pytest.importorskip("yaml")


class _Loader(yaml.SafeLoader):
    pass


for _tag in ("!Sub", "!Ref", "!If", "!Not", "!Equals", "!GetAtt", "!FindInMap",
             "!Join", "!Condition", "!Select", "!Split", "!ImportValue", "!And", "!Or"):
    _Loader.add_constructor(_tag, lambda loader, node: getattr(node, "value", None))


def _statements(resource):
    doc = yaml.load(io.open("src/template.yaml", encoding="utf-8").read(), Loader=_Loader)
    fn = doc["Resources"][resource]["Properties"]
    out = []
    for policy in fn.get("Policies") or []:
        if isinstance(policy, dict):
            out.extend(policy.get("Statement") or [])
    return out


def _finalize_policy_statements():
    return _statements("SessionFinalizeFunction")


def _as_list(v):
    return v if isinstance(v, list) else [v]


def test_finalize_may_read_the_deletion_mirror():
    hits = [s for s in _finalize_policy_statements()
            if "s3:GetObject" in _as_list(s.get("Action"))
            and any("redactions/" in str(r) for r in _as_list(s.get("Resource")))]
    assert hits, ("SessionFinalizeFunction cannot GetObject redactions/* -- "
                  "_session_was_deleted is inert and mails deleted recordings")


@pytest.mark.parametrize("resource", ["SessionFinalizeFunction", "SessionReportFunction"])
def test_every_lenient_reader_may_read_the_mirror(resource):
    """Both non-VPC workers that MAIL something read the mirror leniently, so a
    missing grant on either is invisible: it answers "nothing was deleted" behind
    one WARNING line. Parameterised rather than duplicated, because the second
    one was found only after the first shipped."""
    hits = [s for s in _statements(resource)
            if "s3:GetObject" in _as_list(s.get("Action"))
            and any("redactions/" in str(r) for r in _as_list(s.get("Resource")))]
    assert hits, f"{resource} cannot GetObject redactions/* -- its guard is inert"

    for s in _statements(resource):
        if "s3:ListBucket" not in _as_list(s.get("Action")):
            continue
        prefixes = ((s.get("Condition") or {}).get("StringLike") or {}).get("s3:prefix")
        if prefixes and any(str(p).startswith("redactions/") for p in _as_list(prefixes)):
            break
    else:
        pytest.fail(f"no ListBucket condition covers redactions/ for {resource}")


def test_finalize_may_list_the_mirror_prefix():
    """GetObject alone is not enough. Without ListBucket on the prefix, S3 answers
    AccessDenied rather than NoSuchKey for a key that was never written -- and a
    day with NO deletions has no mirror, which is the common case. The lenient
    reader would log an exception on every finalize, forever."""
    for s in _finalize_policy_statements():
        if "s3:ListBucket" not in _as_list(s.get("Action")):
            continue
        prefixes = ((s.get("Condition") or {}).get("StringLike") or {}).get("s3:prefix")
        if prefixes and any(str(p).startswith("redactions/") for p in _as_list(prefixes)):
            return
    pytest.fail("no ListBucket condition covers redactions/ for SessionFinalizeFunction")
