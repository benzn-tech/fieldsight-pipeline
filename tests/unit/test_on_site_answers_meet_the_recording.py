"""On-site answers meet the recording (spec 2026-11-03, Task 7).

The final check asks the inspector on site; his answers are laid over the
walk's per-unit checklist. An item the recording did not check takes the
on-site answer; an agreeing answer is noted; a disagreement keeps both and
flags the item for the inspector -- neither silently wins; a wrong-unit No
moves to the named unit. Answers match by item TEXT, never by number.
"""
import copy

import checklist
import walk_answers_overlay as overlay
import walk_checklist as wc

ITEMS = ["Smoke alarm installed", "Handrail fixed", "Window seals complete"]
OPENING = {"names": ["Unit 1", "Unit 2", "Unit 3"], "count": 3}
TRANSCRIPT = "\n".join([
    "[10:00:05] Ben Lin (spk_1): Doing units one two and three this morning.",
    "[10:01:10] Ben Lin (spk_1): In unit one now, the smoke alarm is installed and tested.",
    "[10:02:30] Ben Lin (spk_1): Handrail on the stairs is loose, needs fixing.",
    "[10:05:15] Ben Lin (spk_1): Unit two, no smoke alarm fitted in the hallway.",
    "[10:06:40] Ben Lin (spk_1): The window seal in the bedroom is missing.",
    "[10:10:02] Ben Lin (spk_1): Now in unit three, smoke alarm is in and working.",
    "[10:11:20] Ben Lin (spk_1): Lounge window seal is cracked along the bottom.",
])
HEADER = "| Unit | Item no | Answer | Comment | Responsible | Due | Evidence |\n|---|---|---|---|---|---|---|\n"
MODEL = HEADER + (
    "| Unit 1 | 1 | Yes | alarm tested | | | the smoke alarm is installed and tested |\n"
    "| Unit 1 | 2 | No | handrail loose | Joe | Friday | Handrail on the stairs is loose, needs fixing |\n"
    "| Unit 2 | 1 | No | no alarm in hallway | | | no smoke alarm fitted in the hallway |\n"
    "| Unit 2 | 3 | No | bedroom seal missing | | | The window seal in the bedroom is missing |\n"
    "| Unit 3 | 1 | Yes | alarm working | | | smoke alarm is in and working |\n"
    "| Unit 3 | 3 | No | lounge seal cracked | | | Lounge window seal is cracked along the bottom |\n")


def _walk(items=ITEMS):
    return wc.build(MODEL, items, TRANSCRIPT, OPENING)


def _ans(unit, item, answer, at="10:20:05", comment="", quote="", move=None):
    return {"unit": unit, "item_text": item, "answer": answer, "comment": comment,
            "move_to_unit": move, "source": "device", "quote": quote,
            "voice_note_id": "vn-1", "answered_at": at}


def _unit(got, name):
    return next(u for u in got["units"] if u["unit"] == name)


def _row(unit, n):
    return unit["rows"][n - 1]


def test_the_fixture_walk_is_what_the_tests_assume():
    w = _walk()
    assert [r["answer"] for r in _unit(w, "Unit 1")["rows"]] == ["Yes", "No", ""]
    assert [r["answer"] for r in _unit(w, "Unit 2")["rows"]] == ["No", "", "No"]
    assert [r["answer"] for r in _unit(w, "Unit 3")["rows"]] == ["Yes", "", "No"]


def test_on_site_answer_fills_a_not_checked_item():
    walk = _walk()
    before = copy.deepcopy(walk)
    got = overlay.apply(walk, [_ans("Unit 3", "Handrail fixed", "Yes", comment="handrail solid",
                                    quote="yes the handrail is solid")], ITEMS)
    assert walk == before  # pure: the input is not touched
    u3 = _unit(got, "Unit 3")
    r = _row(u3, 2)
    assert r["answer"] == "Yes"
    assert r["state"] == "answered"
    assert r["comment"] == "handrail solid (on site, 10:20)"
    assert r["evidence"] == "yes the handrail is solid"
    assert r["refs"] == []
    assert u3["report"]["evidence"][2] == "yes the handrail is solid"
    assert u3["report"]["gaps"]["not_covered"] == []
    assert u3["report"]["answered"] == 3
    assert u3["paragraphs"][3] == "Handrail fixed | Yes | handrail solid (on site, 10:20) |  | "
    assert got["summary"]["rows"][2] == ["Unit 3", "Yes", "Yes", "No"]
    assert got["conflicts"] == []
    assert got["unmatched_answers"] == []


def test_a_fill_with_no_comment_still_says_on_site():
    got = overlay.apply(_walk(), [_ans("Unit 3", "Handrail fixed", "N/A")], ITEMS)
    assert _row(_unit(got, "Unit 3"), 2)["comment"] == "(on site, 10:20)"
    assert _row(_unit(got, "Unit 3"), 2)["answer"] == "N/A"


def test_agreeing_answers_keep_the_recording_and_note_the_confirmation():
    got = overlay.apply(_walk(), [_ans("Unit 1", "Smoke alarm installed", "Yes",
                                       quote="smoke alarm is fine")], ITEMS)
    r = _row(_unit(got, "Unit 1"), 1)
    assert r["answer"] == "Yes"
    assert r["state"] == "answered"
    assert r["comment"] == "alarm tested (confirmed on site 10:20)"
    assert r["evidence"] == "the smoke alarm is installed and tested"
    assert got["conflicts"] == []


def test_a_disagreement_keeps_both_and_flags_it():
    got = overlay.apply(_walk(), [_ans("Unit 1", "Handrail fixed", "Yes",
                                       quote="handrail is fine now")], ITEMS)
    u1 = _unit(got, "Unit 1")
    r = _row(u1, 2)
    assert r["answer"] == ""
    assert r["state"] == "check"
    assert r["comment"] == ("On site: Yes (10:20). Recording: No — "
                            "'Handrail on the stairs is loose, needs fixing'. Inspector to choose.")
    assert got["summary"]["rows"][0] == ["Unit 1", "Yes", "Check", "Not checked"]
    conflict = {"unit": "Unit 1", "item": "Handrail fixed", "on_site": "Yes", "recording": "No"}
    assert got["conflicts"] == [conflict]
    assert u1["report"]["conflicts"] == [conflict]
    # A conflict is neither a gap nor a failure: it is in conflicts.
    assert u1["report"]["gaps"]["failed"] == []
    assert [g["n"] for g in u1["report"]["gaps"]["not_covered"]] == [3]
    assert u1["report"]["answered"] == 1


def test_a_move_takes_the_no_to_the_named_unit_and_clears_the_source():
    got = overlay.apply(_walk(), [_ans("Unit 2", "Window seals complete", None, at="10:21:30",
                                       move="unit 1")], ITEMS)
    u1, u2 = _unit(got, "Unit 1"), _unit(got, "Unit 2")
    t = _row(u1, 3)
    assert t["answer"] == "No"
    assert t["state"] == "answered"
    assert t["comment"] == "bedroom seal missing (moved from Unit 2 on site, 10:21)"
    assert t["evidence"] == "The window seal in the bedroom is missing"
    assert u1["report"]["evidence"][3] == "The window seal in the bedroom is missing"
    assert [g["n"] for g in u1["report"]["gaps"]["failed"]] == [2, 3]
    s = _row(u2, 3)
    assert (s["answer"], s["state"], s["comment"], s["evidence"]) == (
        "", "not_checked", checklist.NOT_COVERED, "")
    assert 3 not in u2["report"]["evidence"]
    assert [g["n"] for g in u2["report"]["gaps"]["not_covered"]] == [2, 3]
    assert [g["n"] for g in u2["report"]["gaps"]["failed"]] == [1]
    assert got["summary"]["rows"][:2] == [["Unit 1", "Yes", "No", "No"],
                                          ["Unit 2", "No", "Not checked", "Not checked"]]


def test_a_move_replaces_a_yes_in_the_target():
    got = overlay.apply(_walk(), [_ans("Unit 2", "Smoke alarm installed", None, move="Unit 3")],
                        ITEMS)
    t = _row(_unit(got, "Unit 3"), 1)
    assert t["answer"] == "No"
    assert t["comment"] == "no alarm in hallway (moved from Unit 2 on site, 10:20)"
    assert t["evidence"] == "no smoke alarm fitted in the hallway"
    assert _row(_unit(got, "Unit 2"), 1)["state"] == "not_checked"


def test_a_move_onto_a_no_merges_the_comments():
    got = overlay.apply(_walk(), [_ans("Unit 2", "Window seals complete", None, move="Unit 3")],
                        ITEMS)
    t = _row(_unit(got, "Unit 3"), 3)
    assert t["answer"] == "No"
    assert t["comment"] == ("lounge seal cracked; bedroom seal missing "
                            "(moved from Unit 2 on site, 10:20)")


def test_a_move_whose_source_is_not_a_no_is_listed_not_applied():
    walk = _walk()
    a = _ans("Unit 1", "Smoke alarm installed", None, move="Unit 2")
    got = overlay.apply(walk, [a], ITEMS)
    assert [u["rows"] for u in got["units"]] == [u["rows"] for u in walk["units"]]
    assert [x["item_text"] for x in got["unmatched_answers"]] == ["Smoke alarm installed"]
    assert got["unmatched_answers"][0]["reason"]


def test_answers_survive_a_new_template_version_by_item_text():
    items = list(reversed(ITEMS))  # the template's next version reorders the items
    got = overlay.apply(_walk(items), [_ans("unit 3", "  HANDRAIL   fixed ", "No",
                                            comment="still loose")], items)
    u3 = _unit(got, "Unit 3")
    r = next(r for r in u3["rows"] if r["item"] == "Handrail fixed")
    assert r["answer"] == "No"
    assert r["comment"] == "still loose (on site, 10:20)"
    # The answer for an item no longer in the template is kept, not dropped.
    got = overlay.apply(_walk(), [_ans("Unit 3", "Gutters clear", "Yes")], ITEMS)
    assert [x["item_text"] for x in got["unmatched_answers"]] == ["Gutters clear"]
    assert [u["rows"] for u in got["units"]] == [u["rows"] for u in _walk()["units"]]


def test_an_answer_for_a_unit_not_in_the_walk_is_unmatched():
    got = overlay.apply(_walk(), [_ans("Unit 9", "Handrail fixed", "Yes"),
                                  _ans("Unit 2", "Handrail fixed", None, move="Unit 9")], ITEMS)
    assert [x["unit"] for x in got["unmatched_answers"]] == ["Unit 9", "Unit 2"]


def test_a_later_answer_replaces_an_earlier_one():
    got = overlay.apply(_walk(), [
        _ans("Unit 3", "Handrail fixed", "Yes", at="10:20:05", comment="ok"),
        _ans("Unit 3", "Handrail fixed", "No", at="10:25:00", comment="actually loose"),
    ], ITEMS)
    r = _row(_unit(got, "Unit 3"), 2)
    # It replaces the earlier on-site fill; it is not a conflict with the recording.
    assert (r["answer"], r["comment"], r["state"]) == ("No", "actually loose (on site, 10:25)",
                                                       "answered")
    assert got["conflicts"] == []
    # A later answer on a conflicted item is judged against the recording again.
    got = overlay.apply(_walk(), [
        _ans("Unit 1", "Handrail fixed", "Yes", at="10:20:05"),
        _ans("Unit 1", "Handrail fixed", "No", at="10:26:00"),
    ], ITEMS)
    r = _row(_unit(got, "Unit 1"), 2)
    assert (r["answer"], r["state"]) == ("No", "answered")
    assert r["comment"] == "handrail loose (confirmed on site 10:26)"
    assert got["conflicts"] == []
    assert _unit(got, "Unit 1")["report"]["conflicts"] == []
    # A move, then an answer on the target: the move first, then the answer.
    got = overlay.apply(_walk(), [
        _ans("Unit 2", "Window seals complete", None, at="10:21:00", move="Unit 1"),
        _ans("Unit 1", "Window seals complete", "Yes", at="10:22:00"),
    ], ITEMS)
    r = _row(_unit(got, "Unit 1"), 3)
    assert r["state"] == "check"
    assert r["comment"].startswith("On site: Yes (10:22). Recording: No — "
                                   "'The window seal in the bedroom is missing'.")


# ---- where each row's answer came from (R25) -------------------------------------

def _origins(got, name):
    return [r["origin"] for r in _unit(got, name)["rows"]]


def test_every_row_the_recording_wrote_says_so():
    w = _walk()
    for u in w["units"]:
        assert {r["origin"] for r in u["rows"]} == {"recording"}


def test_a_fill_and_an_agreement_are_on_site():
    got = overlay.apply(_walk(), [_ans("Unit 1", "Window seals complete", "Yes"),
                                  _ans("Unit 1", "Handrail fixed", "No")], ITEMS)
    assert _origins(got, "Unit 1") == ["recording", "on_site", "on_site"]


def test_a_disagreement_stays_the_recordings():
    got = overlay.apply(_walk(), [_ans("Unit 1", "Handrail fixed", "Yes")], ITEMS)
    assert _row(_unit(got, "Unit 1"), 2)["state"] == "check"
    assert _origins(got, "Unit 1")[1] == "recording"


def test_a_move_is_on_site_and_its_cleared_source_is_not():
    got = overlay.apply(_walk(), [_ans("Unit 2", "Smoke alarm installed", None, move="Unit 3")],
                        ITEMS)
    assert _origins(got, "Unit 3")[0] == "on_site"
    assert _origins(got, "Unit 2")[0] == "recording"


def test_a_move_merged_into_a_no_keeps_that_nos_source():
    # Onto the recording's own No: still the recording's, so still read back.
    got = overlay.apply(_walk(), [_ans("Unit 2", "Window seals complete", None, move="Unit 3")],
                        ITEMS)
    assert _origins(got, "Unit 3")[2] == "recording"
    # Onto a No given on site: on site.
    got = overlay.apply(_walk(), [_ans("Unit 3", "Handrail fixed", "No", comment="loose"),
                                  _ans("Unit 1", "Handrail fixed", None, move="Unit 3")], ITEMS)
    row = _row(_unit(got, "Unit 3"), 2)
    assert row["answer"] == "No" and row["origin"] == "on_site"


# ---- R36: an answer to a read-back No is the inspector's choice ------------------

def test_a_yes_to_a_read_back_no_overrides_the_recording_and_keeps_its_basis():
    got = overlay.apply(_walk(), [dict(_ans("Unit 1", "Handrail fixed", "Yes", comment="fixed now",
                                            quote="pass"), asked_kind="confirm_no")], ITEMS)
    u1 = _unit(got, "Unit 1")
    r = _row(u1, 2)
    assert (r["answer"], r["state"], r["origin"]) == ("Yes", "answered", "on_site")
    assert r["comment"] == ("fixed now (on site, 10:20; recording: No — "
                            "'Handrail on the stairs is loose, needs fixing')")
    assert r["evidence"] == "pass"
    assert got["conflicts"] == [] and u1["report"]["conflicts"] == []
    assert u1["report"]["gaps"]["failed"] == []
    assert got["summary"]["rows"][0][2] == "Yes"


def test_a_read_back_answer_with_no_comment_still_names_the_recording():
    got = overlay.apply(_walk(), [dict(_ans("Unit 2", "Smoke alarm installed", "N/A"),
                                       asked_kind="confirm_no")], ITEMS)
    r = _row(_unit(got, "Unit 2"), 1)
    assert (r["answer"], r["state"]) == ("N/A", "answered")
    assert r["comment"] == ("(on site, 10:20; recording: No — "
                            "'no smoke alarm fitted in the hallway')")


def test_only_an_answer_to_a_not_checked_question_raises_a_conflict():
    for kind in (None, "not_checked"):
        a = _ans("Unit 1", "Handrail fixed", "Yes")
        if kind:
            a["asked_kind"] = kind
        got = overlay.apply(_walk(), [a], ITEMS)
        assert _row(_unit(got, "Unit 1"), 2)["state"] == "check", kind


def test_a_read_back_no_answered_on_site_is_not_asked_again():
    import check_done
    import walk_report
    got = overlay.apply(_walk(), [dict(_ans("Unit 1", "Handrail fixed", "Yes"),
                                       asked_kind="confirm_no")], ITEMS)
    asked = [(q["unit"], q["item_text"]) for q in check_done.walk_questions(got, ITEMS)]
    assert ("Unit 1", "Handrail fixed") not in asked
    stored = walk_report.result(got, "walk:k")
    opened = check_done.open_from_result(stored, ITEMS, [], None)
    assert ("Handrail fixed" not in
            [i["item_text"] for u in opened if u["unit"] == "Unit 1" for i in u["items"]])


def test_a_read_back_answer_keeps_the_recordings_line_refs():
    """The comment names the recording's basis; its line refs stay with it."""
    walk = _walk()
    _row(_unit(walk, "Unit 1"), 2)["refs"] = ["L3"]       # the recording's line
    got = overlay.apply(walk, [dict(_ans("Unit 1", "Handrail fixed", "Yes"),
                                    asked_kind="confirm_no")], ITEMS)
    assert _row(_unit(got, "Unit 1"), 2)["refs"] == ["L3"]


def test_the_overlay_has_no_line_longer_than_the_house_limit():
    import walk_answers_overlay
    longest = max(len(line) for line in open(walk_answers_overlay.__file__, encoding="utf-8"))
    assert longest <= 120                     # pyproject line-length
