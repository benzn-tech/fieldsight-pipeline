"""The gate that decides what may reach a search engine.

Spec: docs/superpowers/specs/2026-08-31-ask-external-corroboration-design.md §4

Most of this file is written as attacks. A privacy gate that is only tested with the
inputs it was designed for tests the author's imagination, and the input here is a model
label applied to a user's own sentence — the two least trustworthy sources in the
system. So the cases below are the ways somebody, or something, gets conversation text
out of the account, and each asserts the gate stops it.

The `no I/O` property is itself load-bearing and is asserted: the moment this module can
reach a network or a model, "deterministic given its input" stops being true and the
argument in §4 collapses.
"""
import pytest

gate = pytest.importorskip("corroboration_gate")


def _screen(*items, cap=3):
    return gate.screen(list(items), max_entities=cap)


def E(entity, kind="company", claim=None):
    return {"entity": entity, "kind": kind, "claim": claim}


# --------------------------------------------------------------------- what may leave

@pytest.mark.parametrize("entity,kind", [
    ("Naylor Love Construction", "company"),
    ("NZS 3604", "standard"),
    ("AS/NZS 1170", "standard"),
    ("James Hardie fibre cement", "product"),
    ("WorkSafe New Zealand", "regulator"),
    ("CEO", "public_role"),
    ("managing director", "public_role"),
])
def test_a_bare_public_name_may_leave(entity, kind):
    assert gate.screen_entity(entity, kind) is None, "a public name was refused"


def test_the_allowed_entry_carries_the_claim_through():
    """Step 4 cannot assign `no_checkable_claim` without the claim, so the gate must not
    strip it while filtering."""
    r = _screen(E("Naylor Love Construction", claim="finished the job in 2019"))
    assert r.allowed == [{"entity": "Naylor Love Construction", "kind": "company",
                          "claim": "finished the job in 2019"}]


# ------------------------------------------------------------------------ the attacks

def test_a_transcript_sentence_labelled_as_a_company_is_refused():
    """The simplest exfiltration: call the sentence an entity. Length alone stops it,
    which is why the cap is on the entity and not on some later query string."""
    sentence = ("we agreed with Naylor Love that the variation would be priced at "
                "forty thousand before the claim goes in")
    assert "longer than" in gate.screen_entity(sentence, "company")


def test_a_commercial_term_riding_inside_a_company_name_is_refused():
    for entity in ["Naylor Love claim", "Naylor Love $40k variation",
                   "Naylor Love dispute", "Fletcher defect allocation",
                   "报价 Naylor Love"]:
        assert gate.screen_entity(entity, "company"), f"{entity!r} was allowed out"


def test_a_standard_narrowed_to_the_clause_under_dispute_is_refused():
    """`NZS 3604` leaks that the meeting touched timber framing. `NZS 3604 clause 8.2.3`
    leaks which part of it the argument is about, which is the argument."""
    assert gate.screen_entity("NZS 3604", "standard") is None
    for entity in ["NZS 3604 clause 8.2.3", "NZS 3604 section 8", "NZS 3604 table 8.2",
                   "AS/NZS 1170 amendment 3", "NZS 3604 8.2.3", "NZS 3604 第 8 条"]:
        assert gate.screen_entity(entity, "standard"), f"{entity!r} was allowed out"


def test_a_persons_name_labelled_as_a_company_is_refused():
    """The label is haiku's opinion. The shape check does not ask it."""
    for entity in ["John Smith", "Mary O'Brien", "Jean-Paul Sartre"]:
        assert gate.screen_entity(entity, "company") == "shaped like a person's name"


def test_a_firm_named_after_a_person_still_gets_through():
    """The cost of the rule above, bounded. A corporate marker is what tells the two
    apart when both are capitalised words."""
    for entity in ["Ben Smith Contracting", "John Smith Ltd", "Mary OBrien Builders"]:
        assert gate.screen_entity(entity, "company") is None, f"{entity!r} refused"


def test_a_quantity_hidden_in_something_called_a_company_is_refused():
    """Digits are how a number rides along. A standard is the one kind whose identity is
    a number, so it is the one kind that may carry them."""
    assert gate.screen_entity("Naylor Love 4200 m2", "company")
    assert gate.screen_entity("NZS 3604", "standard") is None


def test_an_unknown_kind_is_refused_rather_than_waved_through():
    """Extraction will invent a label eventually. The default has to be refusal: a
    permissive default costs a contract, a strict one costs a card."""
    for kind in ["person", "site", "address", "document", "topic", "", None, 7]:
        assert gate.screen_entity("Naylor Love Construction", kind), f"kind={kind!r}"


def test_a_role_that_is_not_public_is_refused():
    assert gate.screen_entity("CEO", "public_role") is None
    assert gate.screen_entity("site foreman Dave", "public_role")


def test_malformed_extraction_yields_no_corroboration_and_no_exception():
    """A model returning nonsense must cost the reader a card, never the answer they are
    waiting for."""
    assert gate.screen(None).allowed == []
    assert gate.screen("Naylor Love").allowed == []
    assert gate.screen([None, 7, "x", {"kind": "company"}]).allowed == []
    assert gate.screen([{"entity": {"nested": "object"}, "kind": "company"}]).allowed == []


# ------------------------------------------------------------------------- the cap

def test_the_cap_is_three_and_truncation_is_reported():
    r = _screen(E("Naylor Love Ltd"), E("Fletcher Building"), E("Hawkins Ltd"),
                E("Dominion Constructors"))
    assert len(r.allowed) == 3
    assert r.truncated is True


def test_truncated_is_false_when_the_cap_did_not_bite():
    r = _screen(E("Naylor Love Ltd"), E("Fletcher Building"))
    assert r.truncated is False


def test_rejections_do_not_count_toward_truncation():
    """`truncated` means the reader is seeing fewer cards than existed. A refused entity
    was never going to be a card, and conflating the two would report a privacy refusal
    as a display limit."""
    r = _screen(E("Naylor Love Ltd"), E("John Smith"), E("Fletcher Building"))
    assert r.truncated is False
    assert len(r.allowed) == 2
    assert len(r.rejected) == 1


def test_three_spellings_of_one_company_do_not_spend_the_whole_budget():
    r = _screen(E("Naylor Love Ltd"), E("naylor love ltd"), E("Naylor  Love   Ltd"),
                E("Fletcher Building"))
    assert [a["entity"] for a in r.allowed] == ["Naylor Love Ltd", "Fletcher Building"]


# --------------------------------------------------------------- the refusals are loud

def test_every_refusal_comes_back_with_a_reason():
    """A gate whose rejections are invisible cannot be measured, and this repository has
    shipped guards that were inert for months behind exactly that silence."""
    r = _screen(E("John Smith"), E("Naylor Love claim"), E("x" * 200))
    assert len(r.rejected) == 3
    assert all(isinstance(x.reason, str) and x.reason for x in r.rejected)
    assert {x.entity for x in r.rejected} >= {"John Smith", "Naylor Love claim"}


# ------------------------------------------------------------- the property, not a case

def test_the_gate_cannot_reach_a_network_or_a_model():
    """`deterministic given its input` is the whole argument of §4. It stops being
    true the moment this module can call something, so the absence is asserted
    rather than left to review.

    This reads the import graph rather than grepping the text, and the change was
    forced: `_NOT_WORTH_LOOKING_UP` lists brand names, several of which are also
    the names of forbidden modules, so a substring scan started failing on a
    denylist entry. Parsing is also strictly stronger than grepping -- a grep for
    `import boto3` never sees `__import__("boto3")`, which is asserted separately
    below.
    """
    import ast
    import inspect

    tree = ast.parse(inspect.getsource(gate))
    imported = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported.update(a.name.split(".")[0] for a in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            imported.add(node.module.split(".")[0])
        elif isinstance(node, ast.Call):
            name = getattr(node.func, "id", None) or getattr(node.func, "attr", None)
            assert name not in ("__import__", "import_module", "eval", "exec"),                 f"the gate can import or evaluate at runtime via {name}"

    assert imported <= {"re", "unicodedata", "__future__"},         f"the gate imports more than it needs: {imported}"


def test_normalisation_does_not_open_a_hole():
    """Full-width and decomposed forms are the same string to a search engine and must be
    the same string to the gate."""
    assert gate.screen_entity("Naylor Love ｃｌａｉｍ", "company")
    assert gate.screen_entity("NZS 3604 clause 8.2.3", "standard")


# ------------------------------------------------- the cost of the person rule, pinned

@pytest.mark.parametrize("entity", ["Ian McDonald", "Ian MacDonald", "Jean-Paul Sartre",
                                    "Mary O'Brien"])
def test_the_person_rule_catches_the_shapes_it_was_widened_for(entity):
    """Three separate widenings, each because the previous version let one of these out:
    the hyphen in `Jean-Paul`, the apostrophe-capital in `O'Brien`, and the bare internal
    capital in `McDonald`. They are listed together so the next person to touch this
    regex can see what it is holding."""
    assert gate.screen_entity(entity, "company") == "shaped like a person's name"


@pytest.mark.parametrize("entity", ["Naylor Love", "Te Whatu Ora"])
def test_organisations_shaped_exactly_like_a_name_are_refused_and_that_is_the_cost(entity):
    """Both are real organisations and both are refused, because nothing in the STRING
    distinguishes them from a person. This is deliberate and it is the direction the
    error has to fall: a refused organisation costs one card the reader could have looked
    up themselves; a person's name sent to a search engine is the thing the customer was
    promised would not happen.

    The fix for a specific wrong refusal is a marker in `_CORPORATE`, never a loosening
    of the person shape — one of those makes the gate more accurate, the other makes it
    leakier.
    """
    assert gate.screen_entity(entity, "company") == "shaped like a person's name"


# ------------------------------------- what is real, harmless, and pointless to send

# These are the entries the person-shape rule does NOT catch -- an acronym, a
# multi-word brand, a generic noun. If the list were deleted they would all pass,
# so this is where it earns its place. Single-word brands like `Microsoft` are
# refused by both rules and would keep passing this test with the list gone,
# which is why they are not the cases used to defend it.
@pytest.mark.parametrize("entity,kind", [
    ("AWS", "company"), ("Newstalk ZB", "company"), ("SIM card", "product"),
    ("Uber Eats", "product"), ("Google Drive", "product"), ("Wi-Fi", "product"),
    ("New World", "company"), ("Amazon Web Services", "company"),
    ("GPS", "product"), ("PDF", "product"),
])
def test_a_string_nobody_needs_looked_up_is_refused(entity, kind):
    """Measured on 40 real prod sessions: 22 strings would have left the account
    and only three were worth looking up. These spend a slot out of three -- the
    cap bit on 3 of the 11 sessions that produced any entity -- so they crowded
    out the useful ones while leaving the account for nothing.

    Asking the extraction prompt to skip them was tried first and partly ignored:
    22 became 18 with `McDonald's`, `iPad` and `Outlook` still coming through and
    `AWS` newly appearing. A prose instruction to a model is not a filter.
    """
    assert gate.screen_entity(entity, kind) == "nothing to learn from looking it up"


@pytest.mark.parametrize("entity", [
    "Apple Construction Ltd",          # `apple` is listed; this is not apple
    "Google Building Services",        # nor is this google
    "Platform Construction Limited",
])
def test_the_list_matches_whole_strings_and_not_substrings(entity):
    """A firm whose name contains a listed word is not that brand. Substring
    matching here would refuse real contractors, which is the expensive
    direction: a missing card for a real firm is the thing the reader wanted."""
    assert gate.screen_entity(entity, "company") is None


def test_the_list_is_case_and_spacing_insensitive():
    """Uses a multi-word entry so the person rule cannot supply the refusal."""
    for spelling in ["NEWSTALK ZB", "newstalk zb", "  Newstalk   ZB  ",
                     "Ｎewstalk ZB"]:
        assert gate.screen_entity(spelling, "company") ==             "nothing to learn from looking it up", spelling

# ---------------------------- the hole at one word, found on real sessions

@pytest.mark.parametrize("entity", ["Heidi", "Dave", "Raven", "Tenix", "Sam"])
def test_a_single_capitalised_word_is_refused_too(entity):
    """The rule required two name words, so `Naylor Love` was refused and `Heidi`
    walked through. Found on 40 real prod sessions, where a bare given name
    reached the allowed list.

    That was backwards. One capitalised word is MORE ambiguous than two: nothing
    in `Heidi`, `Raven` and `Tenix` says which is a person, which is a job code
    and which is a firm. They are the same string.
    """
    assert gate.screen_entity(entity, "company") == "shaped like a person's name"


@pytest.mark.parametrize("entity", ["VXT", "DB", "UCPK", "MBIE", "NZTA"])
def test_an_acronym_is_not_this_shape_and_still_passes(entity):
    """No lowercase run, so it never was a name shape. This is what keeps the
    one-word tightening from refusing every short organisation."""
    assert gate.screen_entity(entity, "company") is None


@pytest.mark.parametrize("entity", ["Fletcher Building", "Platform Construction Limited",
                                    "University of Otago", "Naylor Love Construction"])
def test_a_corporate_marker_still_outranks_the_name_shape(entity):
    assert gate.screen_entity(entity, "company") is None
