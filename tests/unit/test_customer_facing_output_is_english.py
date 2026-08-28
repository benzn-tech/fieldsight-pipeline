"""Unit: every customer-facing prompt carries the output-language rule.

Site conversation here is routinely Chinese, or Chinese and English mixed in one sentence.
Left alone the model answers in whatever language it heard, so a customer opens their day
and finds half of it in a language their organisation does not read. The transcript is the
record and stays as spoken; what is GENERATED for display is a product surface.

These assert on the ASSEMBLED prompt, not on the source file. A rule that is imported and
never interpolated is a shape this repo has shipped more than once, and reading the source
cannot tell the two apart -- `build_extraction_prompt` returns a TUPLE, and a check that
forgets that reports "rule missing" for a prompt that has it.

The two carve-outs are asserted with the rule because dropping either one is worse than
never adding the rule at all:

* a translated `evidence` quote matches nothing in `evidence_match.check_quote`, so every
  citation would read as a fabrication;
* a transliterated proper noun stops two records of the same person or site from being the
  same thing.
"""
import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(
    os.path.abspath(__file__)))), "src"))

RULE = "LANGUAGE OF OUTPUT"
TURNS = [{"speaker": "S1", "text": "hello",
          "abs_start_str": "09:00:00", "abs_end_str": "09:00:05"}]
ARTIFACT = {"members": [{"date": "2026-08-28", "user_folder": "Ben"}], "topics": []}


def _prompts():
    ex = pytest.importorskip("lambda_extract_session")
    rg = pytest.importorskip("lambda_report_generator")
    mm = pytest.importorskip("lambda_meeting_minutes")
    sb = pytest.importorskip("session_brief")
    return {
        # build_extraction_prompt returns (prompt, stats) -- taking the tuple whole is how
        # the first version of this check reported a false negative.
        "extraction": ex.build_extraction_prompt("Ben", "2026-08-28", "sidX", TURNS, 1)[0],
        "group_merge": ex.build_group_prompt(ARTIFACT, []),
        "daily_report": rg.build_daily_prompt([], "Ben", "Site", "2026-08-28"),
        "weekly_report": rg.build_weekly_prompt([], "Site", "2026-08-20", "2026-08-28"),
        "monthly_report": rg.build_monthly_prompt([], [], "Site", "2026-08-01", "2026-08-28"),
        "meeting_minutes": mm.build_meeting_prompt([], {}),
        # Added after the brief shipped without the rule and wrote a customer's
        # session summary in Chinese. The rule existed, the module was simply not
        # on this list -- which is the only reason nothing caught it.
        "session_brief": sb.build_brief_prompt(TURNS),
    }


def test_every_customer_facing_prompt_carries_the_rule():
    missing = [n for n, p in _prompts().items() if RULE not in p]
    assert not missing, (
        f"these produce text a customer reads and do not ask for English: {missing}")


def test_each_prompt_is_a_string_not_a_tuple():
    """Guards the check itself. `in` on a tuple silently answers about ELEMENTS, so a
    prompt builder that changes its return shape would make the test above pass or fail
    for reasons that have nothing to do with the rule."""
    for name, p in _prompts().items():
        assert isinstance(p, str), f"{name} is {type(p).__name__}, not a prompt string"


def test_the_quote_carve_out_travels_with_the_rule():
    """A translated quote matches nothing in the mechanical check, so it would turn every
    citation into an apparent fabrication."""
    for name, p in _prompts().items():
        if RULE in p:
            assert "Never translate a quote" in p, f"{name} has the rule without the quote carve-out"


def test_the_proper_noun_carve_out_travels_with_the_rule():
    for name, p in _prompts().items():
        if RULE in p:
            assert "Do not translate or romanise a name" in p, \
                f"{name} has the rule without the proper-noun carve-out"


def test_no_prompt_still_asks_to_preserve_the_original_language():
    """`lambda_meeting_minutes` used to say 'Preserve the original language of decisions'.
    Two contradictory instructions in one prompt do not average out -- the model picks one,
    and which one it picks is not something anybody can predict or test for."""
    for name, p in _prompts().items():
        assert "Preserve the original language" not in p, \
            f"{name} contradicts the output-language rule"
