"""session_brief emits open points, and drops the ones that fail admission.

The WIRING, not the rules — the rules are pinned in test_open_points.py. What is
asserted here is that the prompt asks for the field, that admission actually runs
on the answer, that the counts are recorded, and that nothing in this block can
cost the caller their brief.

That last one is the point of the whole file. The brief is what the confirmation
email and the website stand on. An enrichment that can take it down is a worse
trade than no enrichment, and `_store_brief` already takes exactly this posture
for the same reason.
"""
import json
from datetime import datetime, timedelta

import session_brief as sb

_T0 = datetime(2026, 8, 27, 14, 9, 0)


def _turn(offset, text):
    start = _T0 + timedelta(seconds=offset)
    return {"abs_start": start, "abs_end": start + timedelta(seconds=8),
            "abs_start_str": start.strftime("%H:%M:%S"),
            "speaker": "spk_0", "text": text}


TURNS = [_turn(0, "I think this pile is 150 in 3604 but I will have to check."),
         _turn(20, "The doors go in on Tuesday.")]

GOOD = {"quote": "I think this pile is 150 in 3604 but I will have to check.",
        "at": "14:09:00", "claim": "Pile size in NZS 3604",
        "kind": "standard", "subject": "3604"}
INVENTED = {"quote": "I think the roof is 200 but I will have to check.",
            "at": "14:09:00", "claim": "Roof depth",
            "kind": "standard", "subject": "roof"}


def _reply(open_points):
    return json.dumps({"headline": "Piles", "sections": [], "entities": [],
                       "tasks": [], "open_points": open_points})


def _llm(open_points):
    return lambda prompt, **kw: (_reply(open_points), None)


# --------------------------------------------------------------------------

def test_the_prompt_asks_for_open_points():
    prompt = sb.build_brief_prompt(TURNS)
    assert "open_points" in prompt
    assert "not sure of it" in prompt


def test_the_prompt_says_a_question_to_someone_else_is_a_task():
    """The one confusion worth pre-empting in the prompt: a request has a verb
    and a subject and looks exactly like an open point until you ask whose
    uncertainty it is."""
    assert "can you check the stock" in sb.build_brief_prompt(TURNS).lower()


def test_an_admitted_point_reaches_the_brief():
    brief = sb.brief_from_turns(TURNS, call_llm=_llm([GOOD]))
    assert len(brief["open_points"]) == 1
    assert brief["open_points"][0]["subject"] == "3604"
    assert brief["stats"]["open_points_admitted"] == 1
    assert brief["stats"]["open_points_rejected"] == {}


def test_an_invented_quote_does_not_reach_the_brief():
    brief = sb.brief_from_turns(TURNS, call_llm=_llm([INVENTED]))
    assert brief["open_points"] == []
    assert brief["stats"]["open_points_rejected"] == {"quote_unverified": 1}


def test_a_meeting_with_none_still_carries_the_field_and_the_counts():
    """`.get("open_points")` returning None must never be how a reader learns a
    meeting had none — that reads as "this build has no open points"."""
    brief = sb.brief_from_turns(TURNS, call_llm=_llm([]))
    assert brief["open_points"] == []
    assert brief["stats"]["open_points_admitted"] == 0
    assert brief["stats"]["open_points_rejected"] == {}


def test_a_model_that_omits_the_key_entirely_is_survivable():
    reply = json.dumps({"headline": "x", "sections": [], "entities": [], "tasks": []})
    brief = sb.brief_from_turns(TURNS, call_llm=lambda p, **kw: (reply, None))
    assert brief["open_points"] == []
    assert brief["stats"]["open_points_admitted"] == 0


def test_admission_blowing_up_costs_the_points_not_the_brief(monkeypatch):
    """The enrichment may not be able to take down the artifact it decorates."""
    import open_points

    def boom(*a, **k):
        raise RuntimeError("verifier exploded")

    monkeypatch.setattr(open_points, "admit", boom)
    brief = sb.brief_from_turns(TURNS, call_llm=_llm([GOOD]))

    assert brief is not None
    assert brief["headline"] == "Piles"
    assert brief["open_points"] == []
    assert brief["stats"]["open_points_rejected"] == {"admission_error": 1}


def test_the_pre_existing_stats_survive_the_new_keys():
    """`stats` already carries the re-anchoring counts and the alias rejections,
    and the email and the compare script read it. Adding keys must not displace
    the ones that were there."""
    brief = sb.brief_from_turns(TURNS, call_llm=_llm([GOOD]))
    for key in ("reanchored", "unmatched", "aliases_rejected"):
        assert key in brief["stats"], f"the open-points block displaced {key!r}"


def test_resolution_is_a_second_call_and_its_failure_is_not_fatal():
    """Two model calls, and only the first may take the brief down."""
    calls = []

    def call_llm(prompt, **kw):
        calls.append(prompt)
        if len(calls) == 1:
            return (_reply([GOOD]), None)
        return (None, "resolver timed out")

    brief = sb.brief_from_turns(TURNS, call_llm=call_llm)

    assert len(calls) == 2, "the resolution call did not happen"
    assert brief["open_points"][0]["resolution"] is None
    assert brief["stats"]["open_points_resolved"] == 0


def test_a_resolved_point_carries_its_cases_and_its_location():
    def call_llm(prompt, **kw):
        if "open_points" in prompt:                 # the brief prompt
            return (_reply([GOOD]), None)
        return ('{"resolutions": [{"index": 0, "cases": ["by load", "by height"], '
                '"where": "NZS 3604, foundations"}]}', None)

    brief = sb.brief_from_turns(TURNS, call_llm=call_llm)

    assert brief["open_points"][0]["resolution"]["cases"] == ["by load", "by height"]
    assert brief["stats"]["open_points_resolved"] == 1


def test_no_admitted_points_means_no_second_call():
    """A meeting that left nothing hanging must not pay for a model call to
    resolve nothing."""
    calls = []

    def call_llm(prompt, **kw):
        calls.append(prompt)
        return (_reply([]), None)

    sb.brief_from_turns(TURNS, call_llm=call_llm)
    assert len(calls) == 1


def test_a_resolver_that_raises_costs_the_resolutions_not_the_brief(monkeypatch):
    import open_points

    monkeypatch.setattr(open_points, "attach_resolutions",
                        lambda *a, **k: (_ for _ in ()).throw(RuntimeError("boom")))
    brief = sb.brief_from_turns(TURNS, call_llm=_llm([GOOD]))

    assert brief is not None
    assert len(brief["open_points"]) == 1
    assert brief["stats"]["open_points_resolved"] == 0
