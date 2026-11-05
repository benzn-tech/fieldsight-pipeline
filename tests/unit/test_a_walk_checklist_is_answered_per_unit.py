"""A walk's checklist is answered per unit (spec 2026-11-03).

The model writes checklist rows that carry a Unit column; code turns them into
one checklist per unit, each validated by the same evidence rule as an
ordinary check. "This one's all good" answers only its own unit's open items
and says so; a finding that names no unit goes to General, never guessed into
a unit; silence is never Yes.
"""
import checklist
import walk_checklist as wc

ITEMS = ["Smoke alarm installed", "Handrail fixed", "Window seals complete"]
OPENING = {"names": ["Unit 1", "Unit 2", "Unit 3"], "count": 3}
TRANSCRIPT = "\n".join([
    "[10:00:05] Ben Lin (spk_1): Doing units one two and three this morning.",
    "[10:00:40] Ben Lin (spk_1): The skip bin by the gate is overflowing again.",
    "[10:01:10] Ben Lin (spk_1): In unit one now, the smoke alarm is installed and tested.",
    "[10:02:30] Ben Lin (spk_1): Handrail on the stairs is loose, needs fixing.",
    "[10:05:15 – 10:05:40] Ben Lin (spk_1): Unit two, this one is all good, nothing to report.",
    "[10:10:02] Ben Lin (spk_1): Now in unit three.",
    "[10:11:20] Ben Lin (spk_1): Back to unit two, the window seal in the bedroom is missing.",
    "[10:12:00] Ben Lin (spk_1): This unit is all good otherwise.",
])
STRETCHES = [
    {"unit": None, "from": "10:00:00", "to": "10:01:00"},
    {"unit": "Unit 1", "from": "10:01:00", "to": "10:05:00"},
    {"unit": "Unit 2", "from": "10:05:00", "to": "10:10:00"},
    {"unit": "Unit 3", "from": "10:10:00", "to": None},
]
HEADER = "| Unit | Item no | Answer | Comment | Responsible | Due | Evidence |\n|---|---|---|---|---|---|---|\n"


def _unit(got, name):
    return next(u for u in got["units"] if u["unit"] == name)


def _row(unit, n):
    return unit["rows"][n - 1]


def test_silence_is_not_checked_in_every_unit():
    got = wc.build("", ITEMS, TRANSCRIPT, OPENING)
    assert [u["unit"] for u in got["units"]] == ["Unit 1", "Unit 2", "Unit 3"]
    for u in got["units"]:
        assert u["flagged_unlisted"] is False
        assert [r["state"] for r in u["rows"]] == ["not_checked"] * 3
        assert [r["answer"] for r in u["rows"]] == ["", "", ""]
        assert [r["comment"] for r in u["rows"]] == [checklist.NOT_COVERED] * 3
        assert u["paragraphs"][2:] == ["%s |  | %s |  | " % (it, checklist.NOT_COVERED) for it in ITEMS]
        assert [g["n"] for g in u["report"]["gaps"]["not_covered"]] == [1, 2, 3]
    assert got["general"] == []


def test_all_good_answers_only_its_own_unit_and_says_so():
    text = HEADER + "| Unit 2 | ALL | Yes | all good | | | this one is all good, nothing to report |\n"
    got = wc.build(text, ITEMS, TRANSCRIPT, OPENING, stretches=STRETCHES)
    u2 = _unit(got, "Unit 2")
    for r in u2["rows"]:
        assert r["answer"] == "Yes"
        assert r["state"] == "answered"
        assert r["comment"] == "covered by 'this one is all good, nothing to report' (10:05)"
        assert r["comment"].startswith("covered by '")
        assert r["evidence"] == "this one is all good, nothing to report"
    assert u2["paragraphs"][2] == ("Smoke alarm installed | Yes | covered by "
                                   "'this one is all good, nothing to report' (10:05) |  | ")
    assert u2["report"]["all_good"] == {"quote": "this one is all good, nothing to report", "at": "10:05"}
    assert u2["report"]["gaps"]["not_covered"] == []
    assert u2["report"]["answered"] == 3
    for other in ("Unit 1", "Unit 3"):
        u = _unit(got, other)
        assert [r["state"] for r in u["rows"]] == ["not_checked"] * 3
        assert "all_good" not in u["report"]


def test_all_good_without_a_timed_line_says_no_time():
    transcript = "this one is all good, nothing to report"
    text = HEADER + "| Unit 2 | ALL | Yes | | | | this one is all good, nothing to report |\n"
    got = wc.build(text, ITEMS, transcript, OPENING)
    u2 = _unit(got, "Unit 2")
    assert _row(u2, 1)["comment"] == "covered by 'this one is all good, nothing to report'"
    assert u2["report"]["all_good"] == {"quote": "this one is all good, nothing to report", "at": None}


def test_all_good_does_not_overwrite_a_no():
    text = HEADER + (
        "| Unit 1 | 2 | No | handrail loose | | | Handrail on the stairs is loose, needs fixing |\n"
        "| Unit 1 | ALL | Yes | | | | the smoke alarm is installed and tested |\n")
    got = wc.build(text, ITEMS, TRANSCRIPT, OPENING, stretches=STRETCHES)
    u1 = _unit(got, "Unit 1")
    assert _row(u1, 2)["answer"] == "No"
    assert _row(u1, 2)["comment"] == "handrail loose"
    assert _row(u1, 1)["answer"] == _row(u1, 3)["answer"] == "Yes"
    assert _row(u1, 1)["comment"] == "covered by 'the smoke alarm is installed and tested' (10:01)"
    assert [g["n"] for g in u1["report"]["gaps"]["failed"]] == [2]
    assert u1["report"]["gaps"]["not_covered"] == []


def test_a_finding_for_another_unit_goes_to_that_unit():
    # Spoken at 10:11:20, inside Unit 3's stretch -- but it is ABOUT Unit 2.
    text = HEADER + ("| Unit 2 | 3 | No | bedroom window seal missing | | | "
                     "the window seal in the bedroom is missing |\n")
    got = wc.build(text, ITEMS, TRANSCRIPT, OPENING, stretches=STRETCHES)
    assert _row(_unit(got, "Unit 2"), 3)["answer"] == "No"
    assert _row(_unit(got, "Unit 2"), 3)["evidence"] == "the window seal in the bedroom is missing"
    assert _row(_unit(got, "Unit 3"), 3)["state"] == "not_checked"


def test_a_row_without_evidence_is_dropped_per_unit():
    text = HEADER + (
        "| Unit 1 | 1 | Yes | alarm ok | | | the smoke alarm is installed and tested |\n"
        "| Unit 3 | 1 | Yes | alarm ok | | | smoke alarm beeps on test in unit three |\n"
        "| Unit 3 | ALL | Yes | | | | everything in unit three is perfect |\n")
    got = wc.build(text, ITEMS, TRANSCRIPT, OPENING)
    assert _row(_unit(got, "Unit 1"), 1)["answer"] == "Yes"
    u3 = _unit(got, "Unit 3")
    assert [r["state"] for r in u3["rows"]] == ["not_checked"] * 3
    assert u3["report"]["dropped"] == [
        {"item": 1, "reason": "evidence not in the transcript"},
        {"item": "ALL", "reason": "evidence not in the transcript"},
    ]


def test_an_unlisted_unit_gets_a_flagged_section():
    text = HEADER + "| Unit 4 | 1 | Yes | | | | the smoke alarm is installed and tested |\n"
    got = wc.build(text, ITEMS, TRANSCRIPT, OPENING)
    assert [u["unit"] for u in got["units"]] == ["Unit 1", "Unit 2", "Unit 3", "Unit 4"]
    assert [u["flagged_unlisted"] for u in got["units"]] == [False, False, False, True]
    assert _row(_unit(got, "Unit 4"), 1)["answer"] == "Yes"
    # A count with no names flags nothing.
    got = wc.build(text, ITEMS, TRANSCRIPT, {"names": [], "count": 3})
    assert [(u["unit"], u["flagged_unlisted"]) for u in got["units"]] == [("Unit 4", False)]


def test_general_rows_are_kept_not_guessed():
    text = HEADER + (
        "| General | - | No | skip bin overflowing | | | The skip bin by the gate is overflowing |\n"
        "| the gate | 1 | No | made up | | | the gate was left wide open all night |\n")
    got = wc.build(text, ITEMS, TRANSCRIPT, OPENING)
    assert got["general"] == [{"comment": "skip bin overflowing",
                               "evidence": "The skip bin by the gate is overflowing"}]
    assert got["general_dropped"] == [{"comment": "made up",
                                       "evidence": "the gate was left wide open all night",
                                       "reason": "evidence not in the transcript"}]
    for u in got["units"]:
        assert [r["state"] for r in u["rows"]] == ["not_checked"] * 3


def test_the_summary_has_a_row_per_unit_and_a_cell_per_item():
    text = HEADER + (
        "| Unit 1 | 1 | Yes | | | | the smoke alarm is installed and tested |\n"
        "| Unit 1 | 2 | No | loose | | | Handrail on the stairs is loose, needs fixing |\n"
        "| Unit 2 | ALL | Yes | | | | this one is all good, nothing to report |\n"
        "| Unit 3 | 3 | N/A | | | | This unit is all good otherwise |\n")
    got = wc.build(text, ITEMS, TRANSCRIPT, OPENING, stretches=STRETCHES)
    assert got["summary"] == {
        "columns": ITEMS,
        "rows": [
            ["Unit 1", "Yes", "No", "Not checked"],
            ["Unit 2", "Yes", "Yes", "Yes"],
            ["Unit 3", "Not checked", "Not checked", "N/A"],
        ],
    }


def test_a_check_row_reads_check_in_the_summary():
    got = wc.build("", ITEMS, TRANSCRIPT, {"names": ["Unit 1"], "count": 1})
    got["units"][0]["rows"][1]["state"] = "check"
    assert wc.summary(got["units"], ITEMS)["rows"] == [["Unit 1", "Not checked", "Check", "Not checked"]]


def test_all_good_outside_its_stretch_is_dropped():
    # "this unit is all good otherwise" was said at 10:12:00, in Unit 3's stretch.
    text = HEADER + "| Unit 1 | ALL | Yes | | | | This unit is all good otherwise |\n"
    got = wc.build(text, ITEMS, TRANSCRIPT, OPENING, stretches=STRETCHES)
    u1 = _unit(got, "Unit 1")
    assert [r["state"] for r in u1["rows"]] == ["not_checked"] * 3
    assert u1["report"]["dropped"] == [{"item": "ALL", "reason": "all good outside the unit"}]
    assert "all_good" not in u1["report"]
    # Without stretches the same row is trusted to its Unit cell.
    got = wc.build(text, ITEMS, TRANSCRIPT, OPENING)
    assert _row(_unit(got, "Unit 1"), 1)["comment"] == "covered by 'This unit is all good otherwise' (10:12)"


def test_render_is_byte_identical_to_rebuild():
    rows = ("| 1 | Yes | alarm ok | Sam | Friday | the smoke alarm is installed and tested |\n"
            "| 2 | No | rail a|b loose | | | Handrail on the stairs is loose, needs fixing |\n")
    text = HEADER + "".join("| Unit 1 " + r for r in rows.splitlines(keepends=True))
    got = wc.build(text, ITEMS, TRANSCRIPT, OPENING)
    u1 = _unit(got, "Unit 1")
    section = {"paragraphs": rows.splitlines(), "line_refs": [[], []]}
    paragraphs, line_refs, _ = checklist.rebuild(section, ITEMS, TRANSCRIPT)
    assert wc.render(u1) == (paragraphs, line_refs)
    assert (u1["paragraphs"], u1["line_refs"]) == (paragraphs, line_refs)


def test_rows_by_unit_strips_the_unit_cell():
    text = HEADER + ("| unit two | 3 | No | x | | | quote |\n"
                     "| General | | No | y | | | q |\n"
                     "| U2 | ALL | Yes | | | | z |\n")
    assert wc.rows_by_unit(text) == {
        "Unit 2": ["| 3 | No | x |  |  | quote |", "| ALL | Yes |  |  |  | z |"],
        "General": ["|  | No | y |  |  | q |"],
    }
    assert "Unit" in wc.shape_sentence() and "ALL" in wc.shape_sentence() and "General" in wc.shape_sentence()


def test_the_model_is_told_every_way_he_says_the_rest_of_a_unit_is_fine():
    # D3 (TEST smoke 2026-11-06): "Everything else in this one is good" got no
    # ALL row from either model. Only the text can be pinned here; the real
    # check is the TEST re-run.
    s = wc.shape_sentence()
    assert ("When he says the rest of a unit is fine (\"this one's all good\", \"everything "
            "else in this one is good\", \"the rest is fine\", \"no other issues in here\"), "
            "write one row for that unit with Item no ALL and Answer Yes. Evidence is those "
            "words. Rows for items he did address in that unit stay as their own rows.") in s
    assert "When he says a unit is all good" not in s


TWICE = "\n".join([
    "[10:01:10] Ben Lin (spk_1): Unit one, this one is all good.",
    "[10:05:30] Ben Lin (spk_1): Unit two, the handrail on the stairs is loose.",
    "[10:10:02] Ben Lin (spk_1): Unit three, this one is all good.",
])
TWICE_ROWS = HEADER + ("| Unit 1 | ALL | Yes | | | | this one is all good |\n"
                       "| Unit 3 | ALL | Yes | | | | this one is all good |\n")


def test_the_same_all_good_in_two_stretches_fills_both_units_with_their_own_time():
    got = wc.build(TWICE_ROWS, ITEMS, TWICE, OPENING, stretches=STRETCHES)
    assert _unit(got, "Unit 1")["report"]["all_good"] == {"quote": "this one is all good", "at": "10:01"}
    assert _unit(got, "Unit 3")["report"]["all_good"] == {"quote": "this one is all good", "at": "10:10"}
    assert _row(_unit(got, "Unit 3"), 1)["comment"] == "covered by 'this one is all good' (10:10)"
    assert _unit(got, "Unit 3")["report"]["dropped"] == []
    assert [r["state"] for r in _unit(got, "Unit 2")["rows"]] == ["not_checked"] * 3


def test_the_same_all_good_without_stretches_takes_the_next_unclaimed_line():
    got = wc.build(TWICE_ROWS, ITEMS, TWICE, OPENING)
    assert _row(_unit(got, "Unit 1"), 1)["comment"] == "covered by 'this one is all good' (10:01)"
    assert _row(_unit(got, "Unit 3"), 1)["comment"] == "covered by 'this one is all good' (10:10)"


def test_r45_one_all_good_never_covers_two_units_without_stretches():
    # C2: said once, in Unit 1; the model tagged ALL for Unit 1 and Unit 2.
    once = "[10:01:10] Ben Lin (spk_1): Unit one, this ones all good."
    rows = HEADER + ("| Unit 1 | ALL | Yes | | | | this ones all good |\n"
                     "| Unit 2 | ALL | Yes | | | | this ones all good |\n")
    got = wc.build(rows, ITEMS, once, {"names": ["Unit 1", "Unit 2"], "count": 2})
    assert [r["answer"] for r in _unit(got, "Unit 1")["rows"]] == ["Yes"] * 3
    u2 = _unit(got, "Unit 2")
    assert [r["state"] for r in u2["rows"]] == ["not_checked"] * 3
    assert "all_good" not in u2["report"]
    assert {"item": "ALL", "reason": wc.NOT_PLACED} in u2["report"]["dropped"]
    assert wc.NOT_PLACED == "all good not placed"


def test_r45_a_third_unit_saying_it_again_with_no_line_left_is_dropped():
    got = wc.build(TWICE_ROWS + "| Unit 2 | ALL | Yes | | | | this one is all good |\n",
                   ITEMS, TWICE, OPENING)
    u2 = _unit(got, "Unit 2")
    assert [r["state"] for r in u2["rows"]] == ["not_checked"] * 3
    assert {"item": "ALL", "reason": wc.NOT_PLACED} in u2["report"]["dropped"]


# ---- R44: the walk's units are the opening's, the entered ones and the rows' --

ENTERED_ONLY = "\n".join([
    "[10:00:05] Ben Lin (spk_1): Doing four units today.",
    "[10:01:10] Ben Lin (spk_1): Unit one, the smoke alarm is installed and tested.",
    "[10:05:10] Ben Lin (spk_1): Into unit two now.",
])


def test_r44_an_entered_unit_with_no_rows_gets_its_section_every_item_not_checked():
    rows = HEADER + "| Unit 1 | 1 | Yes | | | | the smoke alarm is installed and tested |\n"
    got = wc.build(rows, ITEMS, ENTERED_ONLY, {"names": [], "count": 4},
                   known_units=["Unit 1", "unit two"])
    assert [u["unit"] for u in got["units"]] == ["Unit 1", "Unit 2"]
    u2 = _unit(got, "Unit 2")
    assert [r["state"] for r in u2["rows"]] == ["not_checked"] * 3
    assert [g["n"] for g in u2["report"]["gaps"]["not_covered"]] == [1, 2, 3]
    assert u2["flagged_unlisted"] is False       # no names to flag against
    assert [row[0] for row in got["summary"]["rows"]] == ["Unit 1", "Unit 2"]


def test_r44_walk_order_is_names_then_entered_then_rows_and_only_names_flag():
    rows = HEADER + ("| Unit 9 | 1 | Yes | | | | the smoke alarm is installed and tested |\n"
                     "| Unit 1 | 1 | Yes | | | | the smoke alarm is installed and tested |\n")
    got = wc.build(rows, ITEMS, ENTERED_ONLY, {"names": ["Unit 1", "Unit 3"], "count": 2},
                   known_units=["Unit 2", "Unit 1", "Kitchen", None])
    assert [u["unit"] for u in got["units"]] == ["Unit 1", "Unit 3", "Unit 2", "Unit 9"]
    assert [u["flagged_unlisted"] for u in got["units"]] == [False, False, True, True]


def test_a_header_with_another_spelling_is_not_a_finding():
    text = ("| Unit | Item No. | Answer | Comment | Responsible | Due | Evidence |\n"
            "|---|---|---|---|---|---|---|\n"
            "| UNIT | item  no | Answer | Comment | Responsible | Due | Evidence |\n")
    got = wc.build(text, ITEMS, TRANSCRIPT, OPENING)
    assert got["general"] == [] and got["general_dropped"] == []
    assert wc.rows_by_unit(text) == {}


# ---- one long line holds several units (TEST smoke 2026-11-06, D2) -------------
# One speaker's consecutive words are one long turn: the real TEST walk's second
# turn holds Unit 1's tail, all of Unit 2 and all of Unit 3. An all-good is
# placed by WHERE it was said -- the unit whose entered quote came last before
# it -- not by its line.

LONG = "\n".join([
    "[00:15:02] Morning. Fine day, a bit of wind. With me on site is Ben from RCP. Pre-pour "
    "inspection today, unit one to unit three. Starting in unit one. Formwork setout checked "
    "against the drawings, gridlines and levels are right. Formwork is clean, no debris or "
    "standing water. Release agent is on all the form faces. Props and bracing are in, and "
    "the supervisor has checked them. Bar sizes and spacing match the reinforcement drawings",
    "[00:16:01] Bar chairs are in, but the cover at the north edge is only 30 millimetres. It "
    "needs 50. That's a fail. Plumbing sleeves are in position and taped. Electrical conduits "
    "are fixed and capped. Everything else in this one is good. Walking into unit two now. "
    "Edge boards on the east side are about 10 millimetres low. That's a fail. Everything else "
    "in this one is all good. Into unit three. Formwork setout matches the drawings. Formwork "
    "is clean. Bar sizes and spacing are right. Laps and starter bars are the right length. "
    "Cast-in hold-downs on the west wall are not secured yet. Fail. Oh, back to unit one. I "
    "forgot to say the engineer has not signed off the reinforcement there yet, so that's a "
    "fail. Unit four, next door, looks the same from here. We will do that one next week. "
    "That's it for today.",
])
LONG_ITEMS = ["Formwork setout", "Bar chairs and cover", "Edge boards", "Hold-downs secured"]
LONG_OPENING = {"names": ["Unit 1", "Unit 2", "Unit 3"], "count": 3}
LONG_MARKS = [{"unit": "Unit 1", "at": "00:15", "at_s": "00:15:20", "quote": "Starting in unit one."},
              {"unit": "Unit 2", "at": "00:16", "at_s": "00:16:31",
               "quote": "Walking into unit two now."},
              {"unit": "Unit 3", "at": "00:16", "at_s": "00:16:54", "quote": "Into unit three."}]
LONG_STRETCHES = [{"unit": None, "from": "00:15:10", "to": "00:15:20"},
                  {"unit": "Unit 1", "from": "00:15:20", "to": "00:16:31"},
                  {"unit": "Unit 2", "from": "00:16:31", "to": "00:16:54"},
                  {"unit": "Unit 3", "from": "00:16:54", "to": None}]
U1_ALL = "Everything else in this one is good."
U2_ALL = "Everything else in this one is all good."
LONG_ROWS = HEADER + "".join([
    "| Unit 1 | 1 | Yes | | | | Formwork setout checked against the drawings |\n",
    "| Unit 1 | 2 | No | 30 mm | | | the cover at the north edge is only 30 millimetres |\n",
    "| Unit 1 | ALL | Yes | | | | %s |\n" % U1_ALL,
    "| Unit 2 | 3 | No | 10 mm low | | | Edge boards on the east side are about 10 millimetres low |\n",
    "| Unit 2 | ALL | Yes | | | | %s |\n" % U2_ALL,
    "| Unit 3 | 1 | Yes | | | | Formwork setout matches the drawings |\n",
    "| Unit 3 | 4 | No | west wall | | | Cast-in hold-downs on the west wall are not secured yet |\n",
])


def _states(unit):
    return [(r["answer"], r["state"]) for r in unit["rows"]]


def _assert_long_walk(got):
    u1, u2, u3 = (_unit(got, u) for u in ("Unit 1", "Unit 2", "Unit 3"))
    assert u1["report"]["all_good"] == {"quote": U1_ALL, "at": "00:16"}
    assert u2["report"]["all_good"] == {"quote": U2_ALL, "at": "00:16"}
    assert u1["report"]["dropped"] == [] and u2["report"]["dropped"] == []
    assert _states(u1) == [("Yes", "answered"), ("No", "answered"),
                           ("Yes", "answered"), ("Yes", "answered")]
    assert _states(u2) == [("Yes", "answered"), ("Yes", "answered"),
                           ("No", "answered"), ("Yes", "answered")]
    assert _row(u2, 1)["comment"] == "covered by '%s' (00:16)" % U2_ALL
    # Unit 3 said no all-good: what it did not address is still not checked.
    assert _states(u3) == [("Yes", "answered"), ("", "not_checked"),
                           ("", "not_checked"), ("No", "answered")]
    assert "all_good" not in u3["report"]


def test_two_units_all_goods_in_one_long_line_both_place_without_stretches():
    _assert_long_walk(wc.build(LONG_ROWS, LONG_ITEMS, LONG, LONG_OPENING, entered=LONG_MARKS))


def test_two_units_all_goods_in_one_long_line_both_place_with_stretches():
    # The line's time is 00:16:01, inside Unit 1's stretch: by line, Unit 2's
    # all-good was dropped OUTSIDE. By place it is Unit 2's.
    _assert_long_walk(wc.build(LONG_ROWS, LONG_ITEMS, LONG, LONG_OPENING,
                               stretches=LONG_STRETCHES, entered=LONG_MARKS))


def test_without_marks_the_line_rules_still_apply():
    got = wc.build(LONG_ROWS, LONG_ITEMS, LONG, LONG_OPENING)
    assert {"item": "ALL", "reason": wc.NOT_PLACED} in _unit(got, "Unit 2")["report"]["dropped"]


def test_a_mark_whose_quote_is_not_in_the_text_falls_back_to_the_line_rules():
    marks = LONG_MARKS[:2] + [dict(LONG_MARKS[2], quote="now standing in unit three")]
    got = wc.build(LONG_ROWS, LONG_ITEMS, LONG, LONG_OPENING, entered=marks)
    assert {"item": "ALL", "reason": wc.NOT_PLACED} in _unit(got, "Unit 2")["report"]["dropped"]
    got = wc.build(LONG_ROWS, LONG_ITEMS, LONG, LONG_OPENING, stretches=LONG_STRETCHES,
                   entered=marks)
    assert {"item": "ALL", "reason": wc.OUTSIDE} in _unit(got, "Unit 2")["report"]["dropped"]


def test_r45_one_all_good_in_a_long_line_never_fills_two_units():
    # Said once, in Unit 1; the model tagged it for Unit 2 as well.
    rows = LONG_ROWS.replace("| Unit 2 | ALL | Yes | | | | %s |" % U2_ALL,
                             "| Unit 2 | ALL | Yes | | | | %s |" % U1_ALL)
    for stretches, why in ((None, wc.NOT_PLACED), (LONG_STRETCHES, wc.OUTSIDE)):
        got = wc.build(rows, LONG_ITEMS, LONG, LONG_OPENING, stretches=stretches,
                       entered=LONG_MARKS)
        assert _unit(got, "Unit 1")["report"]["all_good"]["quote"] == U1_ALL
        u2 = _unit(got, "Unit 2")
        assert "all_good" not in u2["report"]
        assert {"item": "ALL", "reason": why} in u2["report"]["dropped"]
        assert _states(u2) == [("", "not_checked"), ("", "not_checked"),
                               ("No", "answered"), ("", "not_checked")]


def test_an_all_good_said_before_any_unit_was_entered_is_no_units():
    text = "[00:15:02] Everything else in this one is good. Starting in unit one. Formwork is clean."
    rows = HEADER + "| Unit 1 | ALL | Yes | | | | %s |\n" % U1_ALL
    got = wc.build(rows, LONG_ITEMS, text, LONG_OPENING, entered=LONG_MARKS[:1])
    u1 = _unit(got, "Unit 1")
    assert "all_good" not in u1["report"]
    assert {"item": "ALL", "reason": wc.NOT_PLACED} in u1["report"]["dropped"]
