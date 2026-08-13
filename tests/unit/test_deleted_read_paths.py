"""Unit: no read path may surface a deleted topic.

Plan: docs/superpowers/plans/2026-08-14-user-deletes-a-recording.md phase 3 and 5.

The customer is told their recording is gone. If any read still returns it, they were lied
to — and the request that started this feature named that outcome exactly: 不能再被别人搜
出来了造成信任危机.

**This is an enumeration, not a list.** The previous "single choke point" in this codebase
(`company_excluded_topic_ids`) has two callers while `topics.py` holds a dozen reads, and
nobody noticed because the coverage was a list somebody maintained by hand. A hand-kept
list is wrong the day someone adds the thirteenth read. So this walks the source and fails
on anything it does not recognise.
"""
import ast
import os
import re

SRC = os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(
    os.path.abspath(__file__)))), "src")
TOPICS = os.path.join(SRC, "repositories", "topics.py")

# Reads that legitimately carry no redaction predicate, each with the reason. Adding to
# this list must be a decision, not a reflex — it is the only way a read escapes the rule.
EXEMPT = {
    "delete_topics_for_source": "a DELETE, not a read",
    "delete_topics_for_source_prefix": "a DELETE, not a read",
    "has_topics_for_source": "an existence probe the PIPELINE uses to decide whether "
                                 "to re-extract; hiding deleted rows here would make it "
                                 "re-extract them and resurrect the content",
    "has_topics_for_source_prefix": "the same existence probe in prefix form; filtering it would make the pipeline re-extract and resurrect the content",
    "list_expired_non_work": "the retention sweep, which must still SEE deleted topics — "
                             "hiding them here would leave their vectors alive forever, "
                             "the opposite of what a deletion is for",
}


def _functions_with_topic_reads():
    """{function name -> its source} for every function in topics.py that SELECTs topics."""
    text = open(TOPICS, encoding="utf-8").read()
    tree = ast.parse(text)
    lines = text.splitlines()
    out = {}
    for node in ast.walk(tree):
        if not isinstance(node, ast.FunctionDef):
            continue
        body = "\n".join(lines[node.lineno - 1:(node.end_lineno or node.lineno)])
        if re.search(r"FROM topics", body):
            out[node.name] = body
    return out


def test_every_topic_read_carries_the_deleted_predicate():
    """The property, not the instances."""
    offenders = []
    for name, body in _functions_with_topic_reads().items():
        if name in EXEMPT:
            continue
        # The PROPERTY, not the spelling: the exclusion may be inlined SQL or the shared
        # constant. Asserting the constant's NAME would pass on a query that imports it and
        # never uses it, and fail on a correct query that inlines the same condition.
        if "scope = 'deleted'" in body and "reverted_at IS NULL" in body:
            continue
        offenders.append(name)
    assert not offenders, (
        "these read topics without excluding deleted ones, so a customer who deleted a "
        "recording can still be shown it: " + ", ".join(sorted(offenders)) +
        " — add the predicate, or add the function to EXEMPT with a reason")


def test_the_exemptions_are_all_real_functions():
    """An exemption for a function that no longer exists is a hole with a comment on it."""
    names = set(_functions_with_topic_reads())
    stale = [n for n in EXEMPT if n not in names]
    assert not stale, f"EXEMPT names functions that do not read topics: {stale}"


def test_the_existence_probes_are_exempt_for_the_stated_reason():
    """Load-bearing, and the reason is counter-intuitive enough to pin.

    `has_topics_for_source` is how the pipeline decides whether a day has already been
    extracted. Filtering deleted rows out of it would make the pipeline conclude the day is
    un-extracted and RE-EXTRACT it — recreating exactly the content the customer deleted.
    The right answer for a probe is the opposite of the right answer for a view.
    """
    for probe in ("has_topics_for_source", "has_topics_for_source_prefix"):
        assert probe in EXEMPT
        assert "resurrect" in EXEMPT[probe] or "re-extract" in EXEMPT[probe]
