"""Unit: the extraction prompt asks for an action a person could send as minutes.

The action text used to be capped at "a handful of words (aim <= ~8)", which
produced titles like "Damaged doors -- replace, floors 1-3, PK building" -- a
label, not a line of minutes. The 2026-09-17 spec (§3.1) replaces the word count
with ONE SENTENCE carrying the subject, what is to happen, and the context that
makes it make sense, while KEEPING the lead-with-the-subject requirement, because
`action_items` also feed Today cards, the Tasks list and Timeline rows, where the
UI shows only the first few words.

WHAT THESE TESTS ARE, AND ARE NOT:

    These assertions only stop a FUTURE EDIT DELETING THE WORDING. They are NOT
    evidence that the model obeys it. "The instruction is present in the prompt"
    is a fact about the prompt; what the model writes is a separate question that
    only a real run, read by a person, can answer (plan: "the evidence rule for
    prompt tasks", N >= 2 because this repo has measured its own run-to-run
    jitter at 93.5% of an apparent effect).

Each test names the mutation that must turn it red.
"""
import pytest

extract = pytest.importorskip("lambda_extract_session")


def _block():
    return extract._instructions_block()


def test_the_action_is_one_sentence_not_eight_words():
    """Mutation: restore "keep the whole thing to a handful of words (aim <= ~8)"
    -> red."""
    body = _block()
    assert "<= ~8" not in body, (
        "the word count is retired; an 8-word cap cannot carry the context")
    assert "handful of words" not in body, (
        "the word count is retired; an 8-word cap cannot carry the context")
    # Updated in the B3 fix round: the literal used to be "ONE SENTENCE", which
    # contradicted this prompt's own strongest Good example -- the user's
    # register sample is TWO grammatical sentences ("Arborist report catching
    # the cut and fill for link bridge. IA and Civix to catch up."). Models
    # weight worked examples over prose, so the text was arguing with itself
    # while probably getting the wanted behaviour anyway. The bound that does
    # the real work is the upper one, and it is kept verbatim.
    assert "ONE OR TWO SHORT CLAUSES" in body, (
        "the replacement requirement must be stated, not merely implied")
    assert "not a paragraph" in body, (
        "length is a bound in BOTH directions; without this the fix "
        "trades an unreadable label for an unreadable essay")


def test_the_telegraphic_example_is_gone():
    """Mutation: restore the Good example "Damaged doors -- replace, floors 1-3,
    PK building" -> red. An example in the old register outranks any amount of
    prose asking for the new one."""
    body = _block()
    assert "Damaged doors -- replace, floors 1-3, PK building" not in body
    assert "Go-to-market strategy -- consult Xiao Han & Benny" not in body
    # ... and is replaced by examples in the target register (the owner's own
    # minutes are the model).
    assert ("Arborist report catching the cut and fill for link bridge. "
            "IA and Civix to catch up.") in body
    assert "Modular drop ceiling 100mm to send details to Ignite." in body


def test_a_bad_example_in_the_old_style_is_present():
    """Mutation: delete the telegraphic Bad example -> red. The contrast is what
    makes the register legible; "write a sentence" alone reads as a length hint."""
    body = _block()
    assert "Drop ceiling -- 100mm, details to Ignite" in body, (
        "a Bad example in the old telegraphic style must make the contrast explicit")
    assert "telegraphic" in body


def test_the_subject_still_comes_first():
    """Mutation: delete the lead-with-the-subject / SURVIVE TRUNCATION paragraph
    -> red. Controller ruling 1 (2026-09-17): the word count dies, the truncation
    rationale survives -- these items are card titles as well as email lines."""
    body = _block()
    assert "SURVIVE TRUNCATION" in body
    assert "FIRST 2-4 WORDS" in body
    assert "SUBJECT/OUTCOME" in body
    assert "Lead with that key subject" in body


def test_responsible_and_deadline_are_still_never_guessed():
    """Mutation: drop "do NOT guess" or fold the owner/date back into the action
    text -> red. The owner explicitly accepts blanks; an invented assignee or due
    date is the failure this line exists to prevent."""
    body = _block()
    assert "Put responsible/deadline in THEIR fields, not in the action text" in body
    assert "do NOT guess" in body
    assert 'never a vague placeholder ("the outstanding task")' in body


def test_a_discussion_that_reached_no_act_still_produces_no_action_items():
    """Mutation: delete the verb test / "A discussion that reached no act produces
    NO action_items" -> red. This paragraph is what holds the item COUNT up, and
    B4's largest risk is that a longer-text instruction quietly suppresses items."""
    body = _block()
    assert "FIRST decide whether there is an action at all" in body
    assert "Test the VERB, not the subject" in body
    assert "A discussion that reached no\n   act produces NO action_items" in body


def test_decisions_are_still_a_separate_list():
    """Mutation: fold `decisions` into action_items, or drop the empty-array rule
    -> red. #861 landed two commits before this branch; this test exists so the
    action-register edit cannot quietly undo it."""
    body = _block()
    assert ("5. decisions: explicit decisions made during the session -- "
            "decision, rationale, and decided_by") in body
    assert ("participants, action_items, findings, decisions, questions may be "
            "empty arrays") in body
