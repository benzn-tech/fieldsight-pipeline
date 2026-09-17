"""Unit: the brief prompt asks for minutes, not for a telegraphic fragment.

The hand-off the user rejected reads `Rainwater tank front position -- discuss
with Paul Smith`: the recipient cannot tell what it was about. Their own minutes
carry the context in the line itself -- "Arborist report catching the cut and
fill for link bridge. IA and Civix to catch up." Task B3 of the 2026-09-17 plan
rewrites `build_brief_prompt`'s task object for that register: `{text, at,
assignee, due}`, with `why` and `basis` gone from the output and `basis`
surviving only as the rule that picks the sentence's verb.

**These tests cannot check a model's output.** They check that the wording is
still in the prompt, because the failure mode is someone tidying it away and
nothing going red -- the same claim, and the same limit,
`tests/unit/test_extraction_participants_prompt.py` states for the extraction
prompt. Whether the model OBEYS any of this is decided by reading a real run on
TEST, twice on the same session, and by nothing in this file.

Assertions normalise whitespace: the prompt is a wrapped f-string, so a sentence
quoted verbatim here spans source lines there.
"""
import os
import re

import pytest

os.environ.setdefault("AWS_ACCESS_KEY_ID", "testing")
os.environ.setdefault("AWS_SECRET_ACCESS_KEY", "testing")
os.environ.setdefault("AWS_DEFAULT_REGION", "ap-southeast-2")

sb = pytest.importorskip("session_brief")


def _turns():
    return [{"abs_start_str": "13:00:00", "speaker": "spk_0",
             "text": "We will send the ceiling details to Ignite this week."}]


def _flat(text):
    """One line, single-spaced -- so a quoted sentence survives the wrap."""
    return re.sub(r"\s+", " ", text)


def test_the_task_object_has_no_why_and_no_basis():
    """Mutation: restore `"why"` or `"basis"` in the tasks schema -> red.

    They are not renamed, they are retired (spec 3.3). `why` became a second
    line under the item that the confirmation email no longer renders, and
    `basis` was an output label -- `committed` / `inferred` -- that no reader
    of a set of minutes has any use for.
    """
    prompt = sb.build_brief_prompt(_turns())
    assert '"why":' not in prompt, (
        "`why` is retired: the task's OWN sentence carries the context now")
    assert '"basis":' not in prompt, (
        "`basis` survives only as the rule picking the verb, never as a field")
    assert "committed if someone took it on" not in prompt, (
        "a committed/inferred label must not be asked for in the output")
    for key in ('"text":', '"at":', '"assignee":', '"due":'):
        assert key in prompt, f"the task object still needs {key}"


def test_the_prompt_forbids_attributing_to_the_owner_when_the_speaker_is_unclear():
    """Mutation: delete the "when you cannot tell who spoke" sentence -> red.

    Asserted VERBATIM, and it is the negative half that is asserted, because
    that is the half a model gets wrong: given a name and a transcript of
    unlabelled voices it will hang the name on whatever it found. A wrong
    attribution is worse than no name (spec 3.2), and unlike `assignee` -- which
    `_real_name` can still strip a speaker label out of -- a name written into
    `text` has no code-side backstop at all.
    """
    prompt = _flat(sb.build_brief_prompt(_turns(), owner_name="Ben Lin"))
    assert ("When you cannot tell who spoke, write the sentence without a name "
            "and never attribute it to Ben Lin.") in prompt, (
        "the prohibition must name the owner and must survive verbatim")


def test_the_telegraphic_style_is_still_called_a_failure():
    """Mutation: delete rule 2 -> red.

    Rule 2 predates this plan and is the reason the brief and the extraction
    prompt contradicted each other; the spec resolved that in the brief's
    favour, so this task must not soften it while rewriting the task object two
    screens above it.
    """
    prompt = _flat(sb.build_brief_prompt(_turns()))
    assert ('2. **No telegraphic style.** "Procurement strategy -- productize '
            'as standard IT" is a failure: the reader cannot tell what to do '
            'with it.') in prompt
