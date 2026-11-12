"""A site-wide answer is answered once, in General (spec 2026-11-13, R53).

A General finding said before he walked into the first unit is site-wide: each
unit's row for that item points to it -- state "site_wide", neither not checked
nor failed in the unit -- unless the unit answered the item itself. All-good
never fills it, the final check does not ask it per unit, and the summary shows
it as "<answer> (G)". A General finding said inside a unit's stretch stays
General-only, as before.
"""
import checklist
import check_done
import walk_answers_overlay as overlay
import walk_checklist as wc
import walk_report

ITEMS = ["Silt fence in place", "Consent documents on site", "Smoke alarm installed",
         "Handrail fixed"]
OPENING = {"names": ["Unit 1", "Unit 2", "Unit 3"], "count": 3}
TRANSCRIPT = "\n".join([
    "[09:00:05] Pre-roof today, units one to three. Silt fence by the gate is down in one "
    "spot. Consent documents are in the site box. The scaffold tags are all current.",
    "[09:01:00] Starting in unit one now. The smoke alarm is installed and tested.",
    "[09:03:00] Walking into unit two now. Handrail on the stairs is loose. The skip bin is "
    "overflowing again. The scaffold tags are all current. Everything else in this one is "
    "all good.",
    "[09:05:00] Into unit three. No silt fence needed on this side.",
])
MARKS = [{"unit": "Unit 1", "at": "09:01", "at_s": "09:01:00", "quote": "Starting in unit one now."},
         {"unit": "Unit 2", "at": "09:03", "at_s": "09:03:00", "quote": "Walking into unit two now."},
         {"unit": "Unit 3", "at": "09:05", "at_s": "09:05:00", "quote": "Into unit three."}]
STRETCHES = [{"unit": None, "from": "09:00:00", "to": "09:01:00"},
             {"unit": "Unit 1", "from": "09:01:00", "to": "09:03:00"},
             {"unit": "Unit 2", "from": "09:03:00", "to": "09:05:00"},
             {"unit": "Unit 3", "from": "09:05:00", "to": None}]
HEADER = "| Unit | Item no | Answer | Comment | Responsible | Due | Evidence |\n|---|---|---|---|---|---|---|\n"
SILT = "| General | 1 | No | silt fence down | | | Silt fence by the gate is down in one spot |\n"
CONSENT = "| General | 2 | Yes | in the site box | | | Consent documents are in the site box |\n"
ROWS = HEADER + SILT + CONSENT + "".join([
    "| Unit 1 | 3 | Yes | | | | The smoke alarm is installed and tested |\n",
    "| Unit 2 | 4 | No | loose | | | Handrail on the stairs is loose |\n",
    "| Unit 2 | ALL | Yes | | | | Everything else in this one is all good |\n",
    "| General | - | No | skip bin full | | | The skip bin is overflowing again |\n",
])
SW = "Site-wide — see General"


def _build(rows=ROWS, transcript=TRANSCRIPT, stretches=None, entered=MARKS):
    return wc.build(rows, ITEMS, transcript, OPENING, stretches=stretches, entered=entered)


def _unit(got, name):
    return next(u for u in got["units"] if u["unit"] == name)


def _row(unit, n):
    return unit["rows"][n - 1]


def _states(unit):
    return [r["state"] for r in unit["rows"]]


# ---- General keeps its item and answer (task 1) ----------------------------------

def test_a_numbered_general_row_keeps_its_item_and_answer():
    got = _build()
    assert got["general"] == [
        {"n": 1, "item": "Silt fence in place", "answer": "No", "comment": "silt fence down",
         "evidence": "Silt fence by the gate is down in one spot"},
        {"n": 2, "item": "Consent documents on site", "answer": "Yes",
         "comment": "in the site box", "evidence": "Consent documents are in the site box"},
        {"n": None, "item": "", "answer": "No", "comment": "skip bin full",
         "evidence": "The skip bin is overflowing again"},
    ]
    assert got["general_dropped"] == []


def test_the_same_item_twice_in_general_keeps_the_first():
    rows = ROWS + "| General | 1 | Yes | fence fine | | | Consent documents are in the site box |\n"
    got = _build(rows)
    assert [g["answer"] for g in got["general"] if g["n"] == 1] == ["No"]
    assert got["general_dropped"] == [{"comment": "fence fine",
                                       "evidence": "Consent documents are in the site box",
                                       "reason": "answered twice"}]


def test_a_general_all_good_is_dropped_outside_the_unit():
    rows = ROWS + "| General | ALL | Yes | all fine | | | The scaffold tags are all current |\n"
    got = _build(rows)
    assert {"comment": "all fine", "evidence": "The scaffold tags are all current",
            "reason": wc.OUTSIDE} in got["general_dropped"]
    assert all(g["evidence"] != "The scaffold tags are all current" for g in got["general"])


def test_general_shows_a_numbered_row_as_item_answer_comment():
    got = _build()
    sec = walk_report.general_section(got["general"])
    assert sec["paragraphs"] == [
        "Silt fence in place: No — silt fence down",
        "Consent documents on site: Yes — in the site box",
        'skip bin full — "The skip bin is overflowing again"',
    ]


def test_a_numbered_row_with_no_valid_answer_stays_a_note_and_is_not_linked():
    rows = HEADER + "| General | 1 | Maybe | not sure | | | Silt fence by the gate is down in one spot |\n"
    got = _build(rows)
    assert got["general"][0]["answer"] == ""
    assert walk_report.general_section(got["general"])["paragraphs"] == [
        'not sure — "Silt fence by the gate is down in one spot"']
    assert _row(_unit(got, "Unit 1"), 1)["state"] == "not_checked"


# ---- the selector (task 2) -------------------------------------------------------

def test_a_general_answer_said_before_the_first_unit_is_site_wide_in_every_unit():
    got = _build()
    for name in ("Unit 1", "Unit 2", "Unit 3"):
        u = _unit(got, name)
        silt, consent = _row(u, 1), _row(u, 2)
        assert (silt["state"], silt["origin"], silt["answer"]) == ("site_wide", "general", "No")
        assert silt["comment"] == SW
        assert silt["evidence"] == "Silt fence by the gate is down in one spot"
        assert silt["refs"] == [] and silt["responsible"] == "" and silt["due"] == ""
        assert (consent["state"], consent["answer"]) == ("site_wide", "Yes")


def test_a_general_answer_said_inside_a_units_stretch_is_not_linked():
    rows = HEADER + "| General | 4 | No | loose | | | The skip bin is overflowing again |\n"
    got = _build(rows)
    for name in ("Unit 1", "Unit 2", "Unit 3"):
        assert _row(_unit(got, name), 4)["state"] == "not_checked"
    assert got["general"][0]["n"] == 4      # still shown in General


def test_evidence_said_before_and_inside_a_unit_is_not_site_wide():
    rows = HEADER + "| General | 2 | Yes | tags current | | | The scaffold tags are all current |\n"
    got = _build(rows)
    assert _row(_unit(got, "Unit 1"), 2)["state"] == "not_checked"


def test_without_placeable_marks_or_stretches_nothing_is_linked():
    marks = [dict(MARKS[0], quote="now standing in unit one")] + MARKS[1:]
    for entered in (None, marks):
        got = _build(entered=entered)
        assert _states(_unit(got, "Unit 1")) == ["not_checked", "not_checked", "answered",
                                                 "not_checked"]


def test_a_later_mark_that_is_not_found_does_not_disable_the_selector():
    marks = MARKS[:2] + [dict(MARKS[2], quote="now standing in unit three")]
    got = _build(entered=marks)
    assert _row(_unit(got, "Unit 3"), 1)["state"] == "site_wide"


def test_with_stretches_and_no_marks_a_general_stretch_before_the_first_unit_is_site_wide():
    got = _build(stretches=STRETCHES, entered=None)
    assert _row(_unit(got, "Unit 1"), 1)["state"] == "site_wide"
    rows = HEADER + "| General | 4 | No | loose | | | The skip bin is overflowing again |\n"
    got = _build(rows, stretches=STRETCHES, entered=None)
    assert _row(_unit(got, "Unit 1"), 4)["state"] == "not_checked"


def test_stretches_with_no_unit_stretch_link_nothing():
    got = _build(stretches=[{"unit": None, "from": "09:00:00", "to": None}], entered=None)
    assert _row(_unit(got, "Unit 1"), 1)["state"] == "not_checked"


def test_an_untimed_line_under_stretches_is_not_site_wide():
    untimed = "\n".join(line.split("] ", 1)[1] for line in TRANSCRIPT.splitlines())
    got = _build(transcript=untimed, stretches=STRETCHES, entered=None)
    assert _row(_unit(got, "Unit 1"), 1)["state"] == "not_checked"


def test_an_opening_mis_marked_as_entering_unit_1_links_nothing():
    marks = [dict(MARKS[0], quote="Pre-roof today, units one to three.")] + MARKS[1:]
    got = _build(entered=marks)
    assert _row(_unit(got, "Unit 1"), 1)["state"] == "not_checked"


def test_a_site_wide_remark_after_the_first_mark_stays_general_only():
    transcript = TRANSCRIPT.replace("Starting in unit one now.",
                                    "Starting in unit one now. Silt fence by the gate is down in one spot.")
    transcript = transcript.replace("Silt fence by the gate is down in one spot. Consent", "Consent", 1)
    got = _build(transcript=transcript)
    for name in ("Unit 1", "Unit 3"):
        assert _row(_unit(got, name), 1)["state"] == "not_checked"
    assert _row(_unit(got, "Unit 2"), 1)["comment"].startswith("covered by '")   # as today
    assert got["general"][0]["n"] == 1
    asked = [(q["unit"], q["item_text"]) for q in check_done.walk_questions(got, ITEMS)]
    assert ("Unit 1", ITEMS[0]) in asked and ("Unit 3", ITEMS[0]) in asked


# ---- the link (task 3) -----------------------------------------------------------

def test_a_units_own_answer_wins_over_general():
    rows = ROWS + "| Unit 3 | 1 | N/A | none needed | | | No silt fence needed on this side |\n"
    got = _build(rows)
    r = _row(_unit(got, "Unit 3"), 1)
    assert (r["state"], r["answer"], r["origin"]) == ("answered", "N/A", "recording")


def test_all_good_does_not_fill_a_site_wide_row():
    u2 = _unit(_build(), "Unit 2")
    assert [(r["answer"], r["state"]) for r in u2["rows"]] == [
        ("No", "site_wide"), ("Yes", "site_wide"), ("Yes", "answered"), ("No", "answered")]
    assert _row(u2, 3)["comment"].startswith("covered by '")
    assert u2["report"]["all_good"]["quote"] == "Everything else in this one is all good"


def test_the_summary_marks_a_site_wide_cell_with_g():
    got = _build()
    assert got["summary"]["rows"] == [
        ["Unit 1", "No (G)", "Yes (G)", "Yes", "Not checked"],
        ["Unit 2", "No (G)", "Yes (G)", "Yes", "No"],
        ["Unit 3", "No (G)", "Yes (G)", "Not checked", "Not checked"],
    ]


def test_gaps_leave_out_site_wide_rows():
    u1, u2 = _unit(_build(), "Unit 1"), _unit(_build(), "Unit 2")
    assert [g["n"] for g in u1["report"]["gaps"]["not_covered"]] == [4]
    assert u1["report"]["gaps"]["failed"] == []
    assert u1["report"]["answered"] == 1 and u1["report"]["site_wide"] == 2
    assert [g["n"] for g in u2["report"]["gaps"]["failed"]] == [4]
    assert u1["report"]["evidence"] == {
        1: "Silt fence by the gate is down in one spot",
        2: "Consent documents are in the site box",
        3: "The smoke alarm is installed and tested"}


def test_a_site_wide_row_renders_its_answer_and_points_to_general():
    u1 = _unit(_build(), "Unit 1")
    assert u1["paragraphs"][2] == "Silt fence in place | No | %s |  | " % SW
    assert u1["paragraphs"] == wc.render(u1)[0]


def test_a_walk_without_general_rows_is_unchanged():
    rows = HEADER + "| Unit 1 | 3 | Yes | | | | The smoke alarm is installed and tested |\n"
    u1 = _unit(_build(rows), "Unit 1")
    assert _states(u1) == ["not_checked", "not_checked", "answered", "not_checked"]
    assert u1["report"]["site_wide"] == 0
    assert u1["paragraphs"][2] == "Silt fence in place |  | %s |  | " % checklist.NOT_COVERED


# ---- on-site answers (task 4) ----------------------------------------------------

def _ans(unit, item, answer=None, at="09:30:05", move=None, kind=None):
    a = {"unit": unit, "item_text": item, "answer": answer, "comment": "", "move_to_unit": move,
         "source": "device", "quote": "", "voice_note_id": "vn-1", "answered_at": at}
    if kind:
        a["asked_kind"] = kind
    return a


def test_an_on_site_answer_on_a_site_wide_row_is_a_fill():
    for kind in (None, "confirm_no"):
        got = overlay.apply(_build(), [_ans("Unit 1", ITEMS[0], "Yes", kind=kind)], ITEMS)
        r = _row(_unit(got, "Unit 1"), 1)
        assert (r["answer"], r["state"], r["origin"]) == ("Yes", "answered", "on_site")
        assert r["comment"] == "(on site, 09:30)"
        assert got["conflicts"] == []
        assert got["summary"]["rows"][0][1] == "Yes"


def test_a_site_wide_row_with_no_answer_stays_site_wide_through_the_overlay():
    got = overlay.apply(_build(), [_ans("Unit 1", ITEMS[3], "Yes")], ITEMS)
    u1 = _unit(got, "Unit 1")
    assert _row(u1, 1)["state"] == "site_wide"
    assert u1["report"]["site_wide"] == 2
    assert [g["n"] for g in u1["report"]["gaps"]["not_covered"]] == []
    assert u1["report"]["gaps"]["failed"] == []
    assert u1["report"]["evidence"][1] == "Silt fence by the gate is down in one spot"


def test_a_move_off_a_site_wide_row_is_refused():
    got = overlay.apply(_build(), [_ans("Unit 1", ITEMS[0], move="Unit 3")], ITEMS)
    assert [a["reason"] for a in got["unmatched_answers"]] == [overlay.NOT_A_NO]
    assert _row(_unit(got, "Unit 3"), 1)["state"] == "site_wide"


def test_a_site_wide_row_filled_on_site_then_moved_away_shows_general_again():
    answers = [_ans("Unit 1", ITEMS[0], "No"), _ans("Unit 1", ITEMS[0], move="Unit 3", at="09:31:00")]
    got = overlay.apply(_build(), answers, ITEMS)
    r1 = _row(_unit(got, "Unit 1"), 1)
    assert (r1["state"], r1["answer"], r1["comment"]) == ("site_wide", "No", SW)
    assert got["summary"]["rows"][0][1] == "No (G)"
    r3 = _row(_unit(got, "Unit 3"), 1)
    assert (r3["state"], r3["answer"], r3["origin"]) == ("answered", "No", "on_site")
    assert r3["comment"] == "(on site, 09:30) (moved from Unit 1 on site, 09:31)"


def test_a_move_into_a_site_wide_no_replaces_it():
    rows = ROWS + "| Unit 3 | 1 | No | flat behind | | | No silt fence needed on this side |\n"
    got = overlay.apply(_build(rows), [_ans("Unit 3", ITEMS[0], move="Unit 1")], ITEMS)
    r1 = _row(_unit(got, "Unit 1"), 1)
    assert (r1["state"], r1["answer"], r1["origin"]) == ("answered", "No", "on_site")
    assert r1["comment"] == "flat behind (moved from Unit 3 on site, 09:30)"
    assert SW not in r1["comment"]
    assert r1["evidence"] == "No silt fence needed on this side"
    u1 = _unit(got, "Unit 1")
    assert [g["n"] for g in u1["report"]["gaps"]["failed"]] == [1]
    assert u1["report"]["site_wide"] == 1


# ---- the final check (task 5) ----------------------------------------------------

def test_the_final_check_asks_nothing_for_a_site_wide_row():
    got = overlay.apply(_build(), [], ITEMS)
    qs = check_done.walk_questions(got, ITEMS)
    assert [(q["unit"], q["item_text"], q["kind"]) for q in qs] == [
        ("Unit 1", ITEMS[3], "not_checked"),
        ("Unit 2", ITEMS[3], "confirm_no"),
        ("Unit 3", ITEMS[2], "not_checked"), ("Unit 3", ITEMS[3], "not_checked")]


def test_the_walk_result_leaves_site_wide_rows_out_of_failed_and_not_covered():
    walk = {"key": "w1", "units": OPENING, "entered": MARKS, "answers": []}
    out = check_done._walk_result(walk, {"name": "Pre-roof"}, ITEMS, ROWS.splitlines(), TRANSCRIPT,
                                  "Pre-roof", "09:06", False, [])
    assert [(g["unit"], g["n"]) for g in out["failed"]] == [("Unit 2", 4)]
    assert [(g["unit"], g["n"]) for g in out["not_covered"]] == [
        ("Unit 1", 4), ("Unit 3", 3), ("Unit 3", 4)]
    assert len(out["questions"]) == 4


def test_the_phone_list_from_a_stored_report_does_not_ask_a_site_wide_row():
    laid = overlay.apply(_build(), [], ITEMS)
    stored = walk_report.result(laid, "w1")
    got = check_done.open_from_result(stored, ITEMS, [], None)
    asked = [(u["unit"], i["item_text"]) for u in got for i in u["items"]]
    assert (("Unit 1", ITEMS[0]) not in asked) and (("Unit 1", ITEMS[1]) not in asked)
    assert ("Unit 1", ITEMS[3]) in asked
