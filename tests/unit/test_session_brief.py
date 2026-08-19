"""session_brief — the narrative record, and the structure derived from it.

Two mechanisms carry the weight and both are pure, so they are tested without an
LLM: alias validation (the model's guesses are useful, and sometimes wrong in a
way that silently merges two different things) and time-anchor re-derivation
(the model writes timestamps from memory and got one wrong by an hour on the
real session).

The third thing tested is compatibility. `brief_from_turns` has to be a drop-in
for `summarize_turns`, because the confirmation email reads exactly two keys and
must not change.
"""
import os

import pytest

os.environ.setdefault("AWS_ACCESS_KEY_ID", "testing")
os.environ.setdefault("AWS_SECRET_ACCESS_KEY", "testing")
os.environ.setdefault("AWS_DEFAULT_REGION", "ap-southeast-2")

sb = pytest.importorskip("session_brief")


def T(text, at="13:00:00", speaker="spk_0"):
    return {"abs_start_str": at, "speaker": speaker, "text": text}


# --- alias validation -------------------------------------------------------

def test_a_real_mishearing_is_kept():
    # What the entity layer exists for: a literal index answers zero for
    # "PB Tech" because the recogniser wrote "PV Tech".
    turns = [T("They bought it through PV Tech for a few hundred bucks.")]
    ents, rejected = sb.validate_aliases(
        [{"name": "PB Tech", "aliases": ["PV Tech"]}], turns)
    assert ents[0]["aliases"] == ["PV Tech"]
    assert rejected == []


def test_an_alias_that_is_another_entity_is_refused():
    # Offered for real: "Claude" as a spelling of "Plaud". Accepting it folds
    # every mention of the AI tool into a competitor device.
    turns = [T("Benny bought a Plaud."), T("A Claude licence is a hundred a month.")]
    ents, rejected = sb.validate_aliases(
        [{"name": "Plaud", "aliases": ["Claude"]},
         {"name": "Claude", "aliases": []}], turns)
    assert ents[0]["aliases"] == []
    assert rejected[0]["reason"] == "collides with another entity"


def test_an_alias_of_only_common_words_is_refused():
    # Offered for real: "record include" as a spelling of "Riccarton clinic".
    turns = [T("We record the meeting and include the notes, take %d." % i)
             for i in range(20)]
    ents, rejected = sb.validate_aliases(
        [{"name": "Riccarton clinic", "aliases": ["record include"]}], turns)
    assert ents[0]["aliases"] == []
    assert "common" in rejected[0]["reason"]


def test_common_is_measured_against_this_session_not_a_word_list():
    # "Ducting" is a common word on a demolition walk and a rare one in an
    # office. The rule has to follow the corpus, or it needs a maintained list
    # per trade and per language.
    site = [T("Move the ducting on level %d." % i) for i in range(20)]
    _, rejected_site = sb.validate_aliases(
        [{"name": "Some Vendor", "aliases": ["ducting"]}], site)
    _, rejected_office = sb.validate_aliases(
        [{"name": "Some Vendor", "aliases": ["ducting"]}],
        [T("We talked about pricing.")])
    assert rejected_site and not rejected_office


def test_an_entity_with_no_name_is_dropped():
    ents, _ = sb.validate_aliases([{"name": "  ", "aliases": ["x"]}],
                                  [T("hello there")])
    assert ents == []


def test_the_callers_entities_are_not_mutated():
    src = [{"name": "Plaud", "aliases": ["Claude"]}, {"name": "Claude", "aliases": []}]
    sb.validate_aliases(src, [T("Benny bought a Plaud device today.")])
    assert src[0]["aliases"] == ["Claude"]


# --- time anchors -----------------------------------------------------------

def test_an_anchor_is_moved_to_where_the_quote_actually_is():
    # Reproduced on the real session: photo-linking came back an hour early.
    turns = [T("Something unrelated entirely.", "13:33:11"),
             T("When I take the photo, I cannot see my photos in the app.", "14:33:11")]
    brief = {"sections": [{"bullets": [
        {"text": "Photo linking is broken.", "at": "13:33:11",
         "quote": "When I take the photo, I cannot see my photos in the app."}]}]}
    stats = sb.reanchor(brief, turns)
    assert brief["sections"][0]["bullets"][0]["at"] == "14:33:11"
    assert stats["reanchored"] == 1


def test_an_unmatchable_anchor_keeps_its_time_and_is_counted():
    # Silent drift is the thing to avoid. A wrong anchor nobody counted is worse
    # than one that shows up in a number.
    turns = [T("Completely different subject matter here.", "09:00:00")]
    brief = {"sections": [{"bullets": [{"text": "x", "at": "10:00:00",
                                        "quote": "zzzzzzzzzzzzzzzz"}]}]}
    stats = sb.reanchor(brief, turns)
    assert brief["sections"][0]["bullets"][0]["at"] == "10:00:00"
    assert stats["unmatched"] == 1


def test_a_task_without_a_quote_is_anchored_by_its_rarest_words():
    turns = [T("Just chatting about the weather and the drive over.", "09:00:00"),
             T("The sparkies need to isolate that distribution board first.", "09:40:00")]
    brief = {"tasks": [{"text": "Isolate the distribution board",
                        "why": "sparkies", "at": "08:00:00"}]}
    sb.reanchor(brief, turns)
    assert brief["tasks"][0]["at"] == "09:40:00"


# --- drop-in compatibility --------------------------------------------------

_BRIEF_JSON = """{"headline": "Procurement blocks the device; package it as a phone.",
 "sections": [{"title": "Procurement", "bullets": [
   {"text": "A $100/month Claude licence needs a business case.", "at": "00:00:00",
    "quote": "I need a hundred dollar license Claude"}]}],
 "entities": [{"name": "PB Tech", "aliases": ["PV Tech"], "kind": "company",
               "note": "NZ retailer"}],
 "tasks": [{"text": "Price the device as a company phone", "why": "procurement",
            "at": "00:00:00", "assignee": "Sam", "due": "Friday",
            "basis": "committed"}]}"""


def _turns():
    return [T("They go, I need a hundred dollar license Claude, and they say "
              "write me a business case.", "13:41:05"),
            T("You can get it through PV Tech and price the device as a "
              "company phone.", "13:40:56")]


def _llm(reply):
    return lambda *a, **k: (reply, None)


def test_it_returns_the_two_keys_the_email_reads():
    out = sb.brief_from_turns(_turns(), call_llm=_llm(_BRIEF_JSON))
    assert out["summary"] == "Procurement blocks the device; package it as a phone."
    assert out["open_todos"] == [{"text": "Price the device as a company phone",
                                  "responsible": "Sam", "due": "Friday"}]


def test_it_also_returns_the_brief_itself():
    out = sb.brief_from_turns(_turns(), call_llm=_llm(_BRIEF_JSON))
    assert out["sections"]
    assert out["entities"][0]["aliases"] == ["PV Tech"]
    assert out["stats"]["reanchored"] >= 1


def test_a_task_nobody_was_given_leaves_the_owner_empty():
    j = _BRIEF_JSON.replace('"assignee": "Sam"', '"assignee": null')
    out = sb.brief_from_turns(_turns(), call_llm=_llm(j))
    assert out["open_todos"][0]["responsible"] is None


def test_no_tasks_is_a_valid_brief():
    j = _BRIEF_JSON[:_BRIEF_JSON.index('"tasks"')] + '"tasks": []}'
    out = sb.brief_from_turns(_turns(), call_llm=_llm(j))
    assert out["open_todos"] == []
    assert out["summary"]


@pytest.mark.parametrize("reply", [None, "", "not json at all", "{broken"])
def test_an_unusable_reply_returns_none_so_the_caller_falls_back(reply):
    assert sb.brief_from_turns(_turns(), call_llm=_llm(reply)) is None


def test_no_turns_makes_no_llm_call():
    called = []

    def spy(*a, **k):
        called.append(1)
        return ("{}", None)

    assert sb.brief_from_turns([], call_llm=spy) is None
    assert called == []


def test_a_fenced_reply_is_still_parsed():
    out = sb.brief_from_turns(_turns(),
                              call_llm=_llm("```json\n" + _BRIEF_JSON + "\n```"))
    assert out["summary"]


def test_the_transcript_is_elided_rather_than_truncated_when_oversized():
    big = [T("word " * 200, "13:00:00") for _ in range(400)]
    rendered = sb.render_turns(big, limit=5000)
    assert len(rendered) <= 5100
    assert "elided" in rendered
    assert rendered.startswith("[13:00:00]")
