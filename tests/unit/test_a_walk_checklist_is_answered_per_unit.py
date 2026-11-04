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
