"""The second half of a chain that starts in the browser.

The first half lives in fieldsight-ui, tests/what-you-type-reaches-the-prompt.
It takes one row of the Library's section editor -- a sentence somebody typed
into the "what goes in this section" box -- and drives it through the real
functions Save uses (`sectionsToSchema`) and the real function the API layer
uses (`toBackendBody`), then writes the resulting body to

    tests/fixtures/editor-to-prompt.body.json

which is the file this test reads. That file is the JOIN, and it is the whole
point: two tests that each build their own copy of the middle prove the two
ends and nothing about the road between them. Three separate failures reached
the owner today, every one with green assertions on both sides of the gap.

So nothing here constructs a template body. It loads what the browser would
have sent and renders it exactly as lambda_session_report does.

THE test is `the sentence a person typed is in the prompt`. It is the owner's
own acceptance criterion, in code: change the description of a section, and
the report changes with it.

WHAT IT STILL DOES NOT PROVE: that the model obeys the sentence. Nothing but a
real generation shows that, which is why the TEST run with a real recording is
the acceptance step this cannot replace.
"""
import io
import json
import os

import pytest

import report_template

FIXTURE = os.path.join(os.path.dirname(__file__), "..", "fixtures",
                       "editor-to-prompt.body.json")

# The sentence typed into the editor in the browser half. Written out here
# rather than read from the fixture: if the fixture stopped carrying it, a test
# that took its expectation FROM the fixture would happily pass on whatever was
# left.
TYPED = "Only hazards raised by name, and whether a control was agreed."

SCOPE = {"folder": "Ben_UCPK2", "date": "2026-09-24", "from": "09:00",
         "to": "11:30", "recordings": 3}


@pytest.fixture()
def body():
    with io.open(FIXTURE, encoding="utf-8") as fh:
        return json.load(fh)


def _prompt(body, actions=None, transcript="[09:05:00] Ben: roofing"):
    return report_template.render_prompt(body, SCOPE, actions or [], transcript)


# ---- THE test ---------------------------------------------------------------

def test_THE_test_the_sentence_a_person_typed_is_in_the_prompt(body):
    assert TYPED in _prompt(body), (
        "the description typed in the Library did not reach the prompt")


def test_it_sits_under_its_own_heading(body):
    """Not merely present somewhere: under the heading it belongs to. A
    sentence that arrived attached to the wrong section would read as the
    feature working while producing the wrong report."""
    prompt = _prompt(body)
    heading = prompt.index("### Safety")
    following = prompt.index("###", heading + 3)
    assert TYPED in prompt[heading:following]


def test_the_fixture_is_the_browser_s_own_output(body):
    """A guard on the join. If the browser half stopped writing the sentence,
    this file would still render happily and prove nothing -- so the fixture is
    checked for the thing the chain exists to carry."""
    purposes = [s.get("purpose") for s in body["sections"]]
    assert TYPED in purposes, (
        "the fixture no longer carries the typed sentence; re-run the browser "
        "half (fieldsight-ui: node --test tests/what-you-type-reaches-the-prompt.test.js)")


# ---- editing one section changes one section --------------------------------

def test_changing_the_sentence_changes_the_prompt(body):
    """The owner's acceptance criterion: edit a description, regenerate, and
    the output follows. This is that, one layer below the model."""
    before = _prompt(body)

    edited = json.loads(json.dumps(body))
    for section in edited["sections"]:
        if section["title"] == "Safety":
            section["purpose"] = "Near misses only, with who reported them."
    after = _prompt(edited)

    assert TYPED in before and TYPED not in after
    assert "Near misses only" in after and "Near misses only" not in before


def test_nothing_else_moves(body):
    """Editing one description must not disturb the rest of the prompt --
    otherwise the diff the owner is asked to read would be full of noise and
    the real change invisible in it."""
    edited = json.loads(json.dumps(body))
    for section in edited["sections"]:
        if section["title"] == "Safety":
            section["purpose"] = "Near misses only, with who reported them."

    before_lines = set(_prompt(body).splitlines())
    after_lines = set(_prompt(edited).splitlines())
    changed = (before_lines ^ after_lines)
    assert changed == {TYPED, "Near misses only, with who reported them."}, changed


# ---- the rest of what the body has to carry ---------------------------------

def test_every_heading_the_person_wrote_is_a_heading_in_the_prompt(body):
    prompt = _prompt(body)
    for section in body["sections"]:
        assert "### " + section["title"] in prompt


def test_the_headings_keep_the_order_they_were_dragged_into(body):
    """Reordering in the editor is one of the two things it could always do;
    it means nothing if the prompt does not keep the order."""
    prompt = _prompt(body)
    positions = [prompt.index("### " + s["title"]) for s in body["sections"]]
    assert positions == sorted(positions)


def test_the_catch_all_is_last(body):
    """render_prompt appends it after the numbered sections, and it is the one
    part of the template the editor cannot see -- so it is the one most likely
    to be lost on the way and least likely to be noticed."""
    prompt = _prompt(body)
    assert "### " + body["catch_all"]["title"] in prompt
    assert prompt.index("### " + body["catch_all"]["title"]) > \
        max(prompt.index("### " + s["title"]) for s in body["sections"])


def test_the_kinds_and_fields_do_not_leak_into_the_prompt(body):
    """They are the editor's, for its own preview. The prompt describes a
    purpose in prose, and a stray `"kind": "list"` in it is an instruction
    nobody wrote."""
    prompt = _prompt(body)
    assert '"kind"' not in prompt and "'kind'" not in prompt
    assert '"fields"' not in prompt


# ---- the actions block, which is data and not instruction -------------------

def test_the_actions_are_carried_as_data(body):
    prompt = _prompt(body, actions=[
        {"action": "Send roofing prices", "owner": "Alex", "deadline": None}])
    assert "Send roofing prices" in prompt
    assert "Alex" in prompt
    assert "no date" in prompt, "an absent deadline is said, not invented"
