"""The extraction prompt must tell the model what QUALIFIES as an action item,
not only how to format one.

Measured on session sid15770a… (2026-08-13, a 71-minute strategy discussion):
6 action items came back and only 2 named an act anyone could finish. The other
4 were directions —

    Target market strategy -- focus high-hourly professionals
    Procurement strategy -- productize as standard IT
    Product strategy -- evaluate fixed sensors vs wearables
    AI pattern analysis -- feed notes to AI

— and the model was not over-reaching. The prompt's own first positive example
is "Go-to-market strategy -- consult Xiao Han & Benny", so it had been taught
that shape. What distinguishes the good one is its VERB: you can finish
consulting two named people; you can never finish "focusing".

`may be empty arrays` was already in the output rules and did not prevent any
of this, which is the point of these tests: permission is not a criterion.
"""
import os

import pytest

os.environ.setdefault("AWS_ACCESS_KEY_ID", "testing")
os.environ.setdefault("AWS_SECRET_ACCESS_KEY", "testing")
os.environ.setdefault("AWS_DEFAULT_REGION", "ap-southeast-2")
os.environ.setdefault("ANTHROPIC_API_KEY", "sk-ant-test-dummy-key")

les = pytest.importorskip("lambda_extract_session")


@pytest.fixture(scope="module")
def prompt():
    turns = [{"abs_start_str": "09:15:00", "speaker": "spk_0",
              "text": "We should focus on the high-hourly professionals."}]
    return les.build_extraction_prompt("Ben_Test", "2026-08-13", "sid_test", turns, 1)[0]


def test_admission_is_stated_before_the_formatting_rules(prompt):
    """The model reads in order. Deciding WHETHER there is an action has to come
    before deciding how to word one, or the formatting rules are what it obeys."""
    admission = prompt.index("FINISH AND TICK OFF")
    formatting = prompt.index("SURVIVE TRUNCATION")
    assert admission < formatting


def test_the_bar_tests_the_verb_not_the_subject(prompt):
    # "Go-to-market strategy -- consult ..." is fine and "Target market strategy
    # -- focus ..." is not; the two differ only in the verb, so the rule has to
    # say so explicitly or it reads as a ban on strategy topics.
    assert "Test the VERB, not the subject" in prompt


def test_direction_verbs_are_named(prompt):
    # Naming them is what makes the rule checkable by the model. These are the
    # four that actually appeared.
    for verb in ('"focus on X"', '"consider Y"', '"explore Z"', '"prioritise W"'):
        assert verb in prompt


def test_an_empty_action_list_is_explicitly_permitted_here(prompt):
    # The generic "may be empty arrays" line lives far away in the output rules
    # and demonstrably did not bind. The permission has to sit next to the bar.
    assert "produces NO action_items" in prompt
    assert "genuinely allowed to be empty" in prompt


def test_the_cost_of_inventing_is_stated(prompt):
    # Without this the model trades precision for coverage, which is the wrong
    # trade for a list someone is meant to work from.
    assert "bury the real ones" in prompt


def test_the_measured_failures_appear_as_bad_examples(prompt):
    for bad in ("Target market strategy -- focus high-hourly professionals",
                "Product strategy -- evaluate fixed sensors vs wearables"):
        assert bad in prompt
        # each must be marked Bad, not sitting loose where it reads as a model
        i = prompt.index(bad)
        assert prompt.rindex("Bad:", 0, i) > prompt.rindex("Good:", 0, i)


def test_the_surviving_good_example_still_shows_a_tickable_verb(prompt):
    # Kept deliberately: it is the contrast case. Removing it would leave the
    # rule with nothing to point at.
    assert "Go-to-market strategy -- consult Xiao Han & Benny" in prompt


def test_formatting_guidance_is_not_lost(prompt):
    # The admission bar is prepended, not a rewrite — the truncation rules that
    # the UI depends on must still be there.
    assert "FIRST 2-4 WORDS" in prompt
    assert "Put responsible/deadline in THEIR fields" in prompt
