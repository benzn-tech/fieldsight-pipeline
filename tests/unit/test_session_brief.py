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


def test_a_task_is_anchored_from_its_own_sentence():
    # `why` must not participate in anchoring: a task whose rare words live only
    # in `why` (never in `text`) must NOT be findable. If it were, that would mean
    # `_snap_to_terms` is still being fed `why` -- the exact narrowing this repo's
    # own `f"{task['text']} {task['why']}"` used to do at session_brief.py:292.
    turns = [T("The sparkies need to isolate that distribution board first.", "09:40:00")]
    brief = {"tasks": [{"text": "", "why": "isolate distribution board sparkies",
                        "at": "08:00:00"}]}
    stats = sb.reanchor(brief, turns)
    assert brief["tasks"][0]["at"] == "08:00:00"   # unmoved -- an empty text anchors nothing
    assert stats["unmatched"] == 1


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
                                  "at": "13:40:56",
                                  "responsible": "Sam", "due": "Friday",
                                  "section": None}]


def test_a_todo_carries_only_text_responsible_due_at_and_section():
    # `to_session_summary` stops reading `why`: the code side of retiring the field
    # (the prompt still asks for it -- that is a later task). A `why` key anywhere
    # in a to-do means the old behaviour survived. `section` joined the shape
    # 2026-09-18 (the-brief-says-where-a-task-came-from).
    out = sb.brief_from_turns(_turns(), call_llm=_llm(_BRIEF_JSON))
    assert out["open_todos"]
    for todo in out["open_todos"]:
        assert set(todo.keys()) == {"text", "responsible", "due", "at", "section"}


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


def test_thinking_is_requested_explicitly_not_inherited_from_the_env():
    # SessionFinalizeFunction runs with QWEN_ENABLE_THINKING=false, set when the
    # summariser here was the terse rolling one. Every measurement behind this
    # brief was taken with thinking ON; inheriting the env would ship a
    # configuration none of them describe.
    seen = {}

    def spy(prompt, **kw):
        seen.update(kw)
        return (_BRIEF_JSON, None)

    sb.brief_from_turns(_turns(), call_llm=spy)
    assert seen.get("enable_thinking") is True


def test_a_speaker_label_is_not_an_assignee():
    """Measured on a real session: every one of five tasks came back assigned to `spk_0` or
    `spk_1`. The model was not hallucinating — those strings are literally what the rendered
    transcript puts in front of it, and the prompt asked who the task was given to.

    A label is worse than an empty field because it does not read as one. It reaches the
    confirmation email's Assignee column looking exactly like a name the reader does not
    recognise, when what the meeting actually contained was nobody being named.
    """
    import session_brief as sb

    for label in ("spk_0", "spk_1", "Speaker 1", "SPEAKER_02", "  spk_10  ", "speaker-3"):
        out = sb.to_session_summary({"headline": "h",
                                     "tasks": [{"text": "t", "assignee": label}]})
        assert out["open_todos"][0]["responsible"] is None, label

    # A real name survives, including ones that merely contain a digit or the word speaker.
    for name in ("Clement", "Deon Jay", "James O'Neill", "Speaker Systems Ltd"):
        out = sb.to_session_summary({"headline": "h",
                                     "tasks": [{"text": "t", "assignee": name}]})
        assert out["open_todos"][0]["responsible"] == name, name


def test_the_prompt_tells_the_model_the_same_thing():
    """Both halves, because neither is sufficient. The prompt is what stops the label being
    generated; the parser is what makes it not matter when it is generated anyway."""
    import session_brief as sb
    prompt = sb.build_brief_prompt([{"abs_start_str": "11:00:00", "speaker": "spk_0",
                                     "text": "hello"}])
    assert "spk_0" in prompt and "NOT names" in prompt


# --- task sections (2026-09-18, "the brief says where a task came from") ----
# Validation lives in CODE: a `section` that is not one of THIS brief's own
# section titles, character for character, is rewritten to null. This is what
# makes the linkage checkable -- the model can still attach a task to the
# WRONG section (that is a human-judgement failure, not a code one, per the
# spec's §5 "Risks") but it cannot make up a section that does not exist.

def test_a_section_matching_a_real_title_exactly_is_kept():
    brief = {"sections": [{"title": "Procurement"}, {"title": "Site walk"}],
             "tasks": [{"text": "t", "section": "Procurement"}]}
    valid = sb.validate_task_sections(brief)
    assert valid == 1
    assert brief["tasks"][0]["section"] == "Procurement"


def test_a_hallucinated_section_title_becomes_null():
    brief = {"sections": [{"title": "Procurement"}],
             "tasks": [{"text": "t", "section": "A section that was never written"}]}
    valid = sb.validate_task_sections(brief)
    assert valid == 0
    assert brief["tasks"][0]["section"] is None


def test_a_paraphrased_or_truncated_title_is_not_an_exact_match():
    # "exactly" -- not a fuzzy or substring match. A model that shortens or
    # rewords the title must not get credit for it: the whole point is that
    # the title is COPIED, not recognised.
    brief = {"sections": [{"title": "Procurement blocks the device"}],
             "tasks": [{"text": "t", "section": "Procurement"}]}
    assert sb.validate_task_sections(brief) == 0
    assert brief["tasks"][0]["section"] is None


def test_a_task_with_no_section_stays_null_and_is_not_counted_invalid():
    # A genuine orphan -- the model correctly said this task belongs to no
    # section -- is not the same failure as a hallucinated title, and must not
    # be double-counted as one.
    brief = {"sections": [{"title": "Procurement"}],
             "tasks": [{"text": "t", "section": None}, {"text": "u"}]}
    assert sb.validate_task_sections(brief) == 0
    assert brief["tasks"][0]["section"] is None
    assert brief["tasks"][1]["section"] is None


def test_a_non_string_section_is_rejected_without_crashing():
    brief = {"sections": [{"title": "Procurement"}],
             "tasks": [{"text": "t", "section": {"title": "Procurement"}}]}
    assert sb.validate_task_sections(brief) == 0
    assert brief["tasks"][0]["section"] is None


def test_no_sections_at_all_means_every_section_is_invalid():
    brief = {"tasks": [{"text": "t", "section": "Procurement"}]}
    assert sb.validate_task_sections(brief) == 0
    assert brief["tasks"][0]["section"] is None


_SECTIONED_BRIEF_JSON = """{"headline": "h",
 "sections": [{"title": "Procurement", "bullets": [
   {"text": "A $100/month Claude licence needs a business case.", "at": "00:00:00",
    "quote": "I need a hundred dollar license Claude"}]}],
 "entities": [],
 "tasks": [{"text": "Price the device as a company phone",
            "at": "00:00:00", "assignee": "Sam", "due": "Friday",
            "section": "Procurement"},
           {"text": "A task with a made-up section",
            "at": "00:00:00", "assignee": null, "due": null,
            "section": "This section does not exist"}]}"""


def test_the_valid_section_count_reaches_stats_and_open_todos():
    out = sb.brief_from_turns(_turns(), call_llm=_llm(_SECTIONED_BRIEF_JSON))
    assert out["stats"]["tasks_with_valid_section"] == 1
    by_text = {t["text"]: t["section"] for t in out["open_todos"]}
    assert by_text["Price the device as a company phone"] == "Procurement"
    assert by_text["A task with a made-up section"] is None


def test_a_valid_section_count_of_zero_is_still_logged(caplog):
    j = _BRIEF_JSON  # no `section` on its one task at all
    with caplog.at_level("INFO"):
        sb.brief_from_turns(_turns(), call_llm=_llm(j))
    assert any("task(s) carried a section that exists" in r.message
               for r in caplog.records)


# --- B2: the owner's display name reaches the prompt --------------------------
# Plumbing only (2026-09-17 plan, task B2): `build_brief_prompt` gains an
# `owner_name` keyword-only parameter so the recording owner's name can reach
# the model. What the prompt DOES with the name (the attribution rule) is
# task B3 -- these only pin that the name is conveyed when known, and that
# nothing about an owner leaks into the prompt when it is not.

def test_the_prompt_names_the_owner_when_one_is_known():
    prompt = sb.build_brief_prompt(_turns(), owner_name="Ben Lin")
    assert "Ben Lin" in prompt


def test_the_prompt_says_nothing_about_an_owner_when_none_is_known():
    prompt = sb.build_brief_prompt(_turns(), owner_name=None)
    assert "owner" not in prompt.lower()
    assert "None" not in prompt
    assert "{owner" not in prompt
