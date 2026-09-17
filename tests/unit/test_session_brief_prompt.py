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


def test_the_length_bound_does_not_contradict_the_register_example():
    """Mutation: restore `ONE sentence, not a paragraph.` -> red.

    Found in the B4 review, fixed here because it is the same wording defect in
    both prompts. The task object said "ONE sentence" while the register this
    whole change is aimed at -- the user's own minutes -- is two grammatical
    sentences ("Arborist report catching the cut and fill for link bridge. IA
    and Civix to catch up."). The upper bound is the half doing real work and
    is kept word for word; the example is the user's own and is not touched.
    """
    prompt = _flat(sb.build_brief_prompt(_turns()))
    assert "One or two short clauses, not a paragraph." in prompt
    assert "ONE sentence, not a paragraph." not in prompt, (
        "the bound must not contradict the example it is printed next to")
    # The same contradiction a second time, and this one is printed DIRECTLY
    # above the two-sentence example, which is the worst place for it.
    assert "One or two short clauses carrying their own context" in prompt
    assert "One sentence carrying its own context" not in prompt, (
        "rule 7's own prose contradicted the example on the next line")
    # And a third time, in the task schema's own description of `text`.
    assert ("One or two short clauses a reader who was in the room can act on"
            in prompt)
    assert "One sentence a reader who was in the room can act on" not in prompt, (
        "the field description states the bound first; it must agree too")


def test_a_non_owner_is_shown_committing_too():
    """Mutation: delete the Paul Smith example sentence from rule 7 -> red.

    B3's review (I-1): the only worked example of a correctly-named commitment
    named the recording's owner, and it appeared twice. A prompt that shows one
    shape only teaches that shape, and the shape it taught was "hang the name on
    the owner" -- the exact failure spec 3.2 calls worse than no name. The
    counter-example sits ALONGSIDE the owner example, not instead of it: both
    shapes have to be visible for the choice between them to be a choice.
    """
    prompt = _flat(sb.build_brief_prompt(_turns(), owner_name="Ben Lin"))
    assert ("Paul Smith confirmed the tank position is fixed before the slab "
            "pour") in prompt, (
        "a named NON-owner must also be shown as the subject of a commitment")


def test_the_owner_line_says_whose_device_it_is_not_whose_meeting():
    """Mutation: restore `This recording belongs to {owner_name}.` -> red.

    B3's review (I-1): "belongs to Ben Lin" reads as "this is mainly Ben's
    meeting" as easily as "this is Ben's device", and the first reading is an
    anchor toward attributing every unclear turn to him. The line has to state
    the fact it actually knows -- whose account the file came from -- and say
    outright that it implies nothing about who spoke.
    """
    prompt = _flat(sb.build_brief_prompt(_turns(), owner_name="Ben Lin"))
    assert ("This recording was made on Ben Lin's device, under their account. "
            "That is all it tells you: it does not mean the meeting was theirs, "
            "and it does not tell you who spoke on any line below.") in prompt
    assert "This recording belongs to" not in prompt, (
        "the ownership phrasing is what built the anchor; it must not come back")


def test_the_ban_on_speaker_labels_inside_the_task_sentence_is_stated_twice():
    """Mutation: delete the "Never a speaker label" clause under rule 7 -> red.

    B3's review (I-2): `_real_name` strips a label out of `assignee`; NOTHING
    guards `text`, so a raw spk_0 in `text` reaches the customer's minutes
    verbatim. The owner prohibition is repeated for the same reason -- a rule
    with no code-side backstop is stated where the model is writing, not only
    where the field is declared.
    """
    prompt = _flat(sb.build_brief_prompt(_turns(), owner_name="Ben Lin"))
    assert prompt.count("Never a speaker label (spk_0, Speaker 1) inside") == 2, (
        "the label ban belongs both in the task schema and next to rule 7's "
        "instruction on how the sentence reads")


def test_a_firm_commitment_must_not_be_hedged_out_of_caution():
    """Mutation: delete the "Do not hedge" sentences from rule 7 -> red.

    The counterweight. Everything else added in this round pushes AWAY from
    naming, and a model given only that pressure buys safety by turning real
    commitments into "to be confirmed" -- minutes that under-report what people
    agreed. This clause governs the VERB only and says so, so it cannot be read
    as licence to attribute a turn whose speaker is unclear.
    """
    prompt = _flat(sb.build_brief_prompt(_turns(), owner_name="Ben Lin"))
    assert ("Do not hedge a commitment somebody plainly made: \"to be confirmed\" "
            "is for work nobody took on, not a safer default.") in prompt
    assert ("This is about the verb, never the name -- when you cannot tell who "
            "spoke, the sentence still carries no name.") in prompt


# --- 2026-09-18: "the brief says where a task came from" --------------------

def test_the_schema_asks_for_a_section_copied_verbatim():
    """Mutation: delete the `"section":` line from the tasks schema -> red."""
    prompt = _flat(sb.build_brief_prompt(_turns()))
    assert '"section":' in prompt
    assert "copied verbatim" in prompt
    assert "null when this task does not belong to any section" in prompt


def test_the_instructions_state_the_section_rule_in_those_words():
    """Mutation: delete rule 8 (tasks: section) -> red. The spec requires the
    prompt to say, in prose, that the title must be copied verbatim from the
    sections the model just wrote -- not only in the schema's field
    description."""
    prompt = _flat(sb.build_brief_prompt(_turns()))
    assert ("Copy the exact title of the section above this task came from "
            "into `section`, verbatim") in prompt
    assert "not paraphrased and not shortened" in prompt


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
