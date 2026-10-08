"""What may be sent to a search engine, whole.

Owner decision 2026-10-06 (spec 2026-10-06-ask-knowledge-then-verify-design.md
D4): admission keeps ONLY the length cap. The names-from-records, site-name and
commercial-term screens are gone.

The defect that decided it: "search online Which NZ standard governs the length
of flexi fire sprinkler piping" was refused because `Which` counted as a name
(the opener filter only dropped an opener at position 0) and `which` occurs in
every transcript. The reader was told nothing. These tests hold the rule in both
directions: the cap still bites, and nothing else does.
"""
import pytest

adm = pytest.importorskip("question_admission")


@pytest.mark.parametrize("q", [
    "Which New Zealand standard covers timber design?",
    "search online Which NZ standard governs the length of flexi fire sprinkler piping",
    "what is the NZ standard for scaffolding",
    "what does NZS 3604 cover",
    "does the Building Code require a licensed scaffolder above 5 metres",
    "what is WorkSafe's guidance on hot works permits",
])
def test_a_public_question_is_sent_whole(q):
    assert adm.screen(q) is None, q


@pytest.mark.parametrize("q", [
    "what did the variation cost",
    "how much was the claim",
    "what is in the contract for the retaining wall",
    "is $4,500 a fair price for NZ$ scaffold hire",
])
def test_a_commercial_term_is_no_longer_refused(q):
    """Owner decision: commercial details in a question may reach the search
    engine. Before 2026-10-06 each of these was refused."""
    assert adm.screen(q) is None, q


@pytest.mark.parametrize("q", [
    "did Neil sign off the scaffold tag",
    "what happened at SB1108 Ellesmere College",
    "is Stantec any good",
])
def test_a_name_or_site_is_no_longer_refused(q):
    """The screens read the account's own records; `screen` no longer takes
    them, so a name cannot refuse anything."""
    assert adm.screen(q) is None, q


def test_screen_takes_no_records():
    with pytest.raises(TypeError):
        adm.screen("Which standard covers timber design?", [{"chunk_text": "which"}])


def test_the_cap_is_the_last_screen_standing():
    assert adm.MAX_QUESTION_CHARS == 300
    assert adm.screen("x" * 300) is None
    assert adm.screen("x" * 301) == adm.TOO_LONG
    assert "300" in adm.TOO_LONG


def test_an_empty_question_is_refused():
    for q in ("", "   ", None):
        assert adm.screen(q) == adm.EMPTY


def test_a_refusal_says_which_rule_refused_it():
    """A guard whose refusals are invisible cannot be measured."""
    for q in ("", "x" * 400):
        r = adm.screen(q)
        assert isinstance(r, str) and len(r) > 10, q
