"""Unit: the extraction prompt defines what a participant IS.

The prompt used to say only "list participants by name". With no definition,
the model reasonably read it as "people this topic involves" and listed anyone
mentioned: a solo recording in which the wearer talks about Emily, Ben and
Daniel produced topics claiming all three took part in the conversation.

Measured on a real recording, old wording vs new:

    old: participants=['spk_0', 'Emily'] / ['spk_0', 'Ben', 'Daniel']
    new: participants=[]                 / []

This test cannot check a model's output. It checks that the constraint is still
in the prompt, because the failure mode is someone tidying the wording away and
nothing going red.
"""
import re

import pytest

extract = pytest.importorskip("lambda_extract_session")


def _prompt():
    src = extract.__file__.replace(".pyc", ".py")
    with open(src, encoding="utf-8") as fh:
        return fh.read()


def test_participants_are_defined_as_speakers_not_mentions():
    body = _prompt()
    assert "participants: ONLY people who actually SPOKE" in body, (
        "participants must be defined, or the model lists whoever is mentioned")


def test_the_prompt_says_a_mentioned_person_is_not_a_participant():
    body = _prompt()
    assert re.search(r"TALKED ABOUT is NOT a participant", body), (
        "the negative case is the one the model gets wrong; state it explicitly")


def test_the_prompt_points_mentioned_names_at_their_real_fields():
    """Without somewhere to put them, a model that is told 'not here' tends to
    drop the names entirely — losing who is responsible for what."""
    body = _prompt()
    assert "action_items.responsible" in body and "findings.entity_name" in body
