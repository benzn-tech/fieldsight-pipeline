"""Who owns an action: a crew, a trade, or the person talking.

TEST 2026-10-08, the owner's pre-pour walk: "formworkers doing it tomorrow",
"steel fixers need to sort that", "I'll ring Hirepool" -- the email listed all
three with no owner, because the prompt asked for a "Person name".
"""
import pytest

es = pytest.importorskip("lambda_extract_session")
iw = pytest.importorskip("lambda_item_writer")


def test_the_prompt_asks_for_a_crew_or_trade_when_that_is_who_is_named():
    prompt = es._instructions_block()
    assert '"responsible": "Who will do it: a person, a crew or a trade' in prompt
    assert "crew or trade when that is who is named" in prompt


def test_THE_the_word_the_prompt_asks_for_is_one_item_writer_resolves():
    """The prompt's word for "the speaker" and item-writer's list are a
    contract between two files: a word only one side knows reaches the
    to-do list as written."""
    prompt = es._instructions_block()
    assert 'they will do it themselves' in prompt and 'write\n   "Speaker".' in prompt
    items = [{"action": "Spare vibrator on site", "responsible": "Speaker"}]
    assert iw._resolve_self_responsible(items, "Ben Lin") == 1
    assert items[0]["responsible"] == "Ben Lin"
