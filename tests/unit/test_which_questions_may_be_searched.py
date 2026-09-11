"""What may be sent to a search engine, whole.

The entity breakdown this replaces failed on the ordinary case: asked "Which New
Zealand standard covers timber design?", subject extraction returned nothing on
three runs out of four, because the question contains no standard number -- the
number is the thing the asker does not know.

So the question goes out whole, and this decides whether it may. These tests are
the decision, in both directions: what it refuses, and -- just as load-bearing --
what it must NOT refuse, because a rule that refuses "New Zealand" refuses the
feature.
"""
import pytest

adm = pytest.importorskip("question_admission")


CHUNKS = [
    {"site_name": "SB1108 Ellesmere College", "report_date": "2026-09-02",
     "chunk_text": "Neil raised the scaffold tag on the north elevation. "
                   "Jesse asked about the toolbox copy. Stantec closed the "
                   "penetration detail."},
]


# ------------------------------------------------- what must go through

@pytest.mark.parametrize("q", [
    "Which New Zealand standard covers timber design?",
    "what is the NZ standard for scaffolding",
    "what does NZS 3604 cover",
    "does the Building Code require a licensed scaffolder above 5 metres",
    "what is WorkSafe's guidance on hot works permits",
])
def test_a_public_question_is_sent_whole(q):
    """Every one of these is two or three capitalised words. A rule that read
    capitals as names would refuse the entire feature -- these ARE the questions
    it exists for."""
    assert adm.screen(q, CHUNKS) is None, q


def test_a_question_with_no_records_at_all_is_still_judged():
    """Door A: retrieval returned nothing, so there is no corpus to compare
    against. The commercial rule still applies; the corpus rules simply have
    nothing to match."""
    assert adm.screen("what is the NZ standard for scaffolding", []) is None
    assert adm.screen("what did the variation cost", []) is not None


# ------------------------------------------------------- what must not

def test_a_colleague_named_in_the_records_stops_it():
    """`Neil` appears in this account's own excerpts. That is the signal -- not
    that it looks like a human name, which is unbounded, but that it is theirs."""
    r = adm.screen("did Neil sign off the scaffold tag", CHUNKS)
    assert r and "own records" in r


def test_a_site_name_stops_it():
    r = adm.screen("what happened at SB1108 Ellesmere College", CHUNKS)
    assert r and "site" in r


def test_a_supplier_this_account_deals_with_stops_it():
    """Stantec is a real public firm AND appears in these records. Being
    public is not the test; being theirs is."""
    r = adm.screen("is Stantec any good", CHUNKS)
    assert r and "own records" in r


@pytest.mark.parametrize("q", [
    "what did the variation cost",
    "how much was the claim",
    "what is in the contract for the retaining wall",
])
def test_commercial_terms_stop_it_whoever_is_named(q):
    r = adm.screen(q, CHUNKS)
    assert r and "commercial" in r, q


def test_a_paragraph_of_context_is_not_a_question():
    """A long question is a paragraph of this customer's situation, and that is
    exactly what must not leave."""
    assert adm.screen("x" * 400, CHUNKS) is not None


# --------------------------------------------------- the sharp edges

def test_a_leading_grammatical_capital_is_not_a_name():
    """"Which" and "What" start most questions and match the name shape. Reading
    them as names would refuse everything."""
    assert adm.screen("Which standard covers timber design?", CHUNKS) is None


def test_a_name_after_the_opening_word_is_still_caught():
    """Dropping the sentence-initial capital must drop only that word."""
    r = adm.screen("Which Neil signed it", CHUNKS)
    assert r and "own records" in r


def test_a_word_that_merely_contains_a_name_is_not_a_name():
    """`Ben` is in these records; `benchmark` is not `Ben`. Substring matching
    would refuse half the language."""
    chunks = [dict(CHUNKS[0], chunk_text="Ben covered the permit.")]
    assert adm.screen("what is the benchmark for bracing", chunks) is None


def test_a_refusal_says_which_rule_refused_it():
    """A guard whose refusals are invisible cannot be measured, and this
    repository has shipped several that were inert for months behind a silence
    that looked exactly like nothing to do."""
    for q in ("did Neil sign it", "what did the variation cost"):
        r = adm.screen(q, CHUNKS)
        assert isinstance(r, str) and len(r) > 10, q
