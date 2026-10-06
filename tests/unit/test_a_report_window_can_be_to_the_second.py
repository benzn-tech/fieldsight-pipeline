"""A report can cover a spoken check's window to the second.

The Level 1 pre-pour check on prod 10-05 ran 10:59:09-11:02:14, and the Level
2 steel check began at 11:02:14: to the minute, the two windows share 11:02.
"""
import datetime

import pytest

sr = pytest.importorskip("lambda_session_report")
org = pytest.importorskip("lambda_org_api")


def test_THE_the_worker_reads_a_window_to_the_second():
    assert sr._clock("2026-10-05", "11:02:14") == datetime.datetime(2026, 10, 5, 11, 2, 14)
    assert sr._clock("2026-10-05", "11:02") == datetime.datetime(2026, 10, 5, 11, 2)


def test_the_request_keeps_a_good_window_and_drops_a_bad_end():
    assert org._report_window({"from": "10:59:09", "to": "11:02:14"}) == {"from": "10:59:09", "to": "11:02:14"}
    assert org._report_window({"from": "09:00", "to": "11:30"}) == {"from": "09:00", "to": "11:30"}
    assert org._report_window({}) == {"from": "00:00", "to": "23:59"}
    assert org._report_window({"from": "25:00", "to": "11:02:60"}) == {"from": "00:00", "to": "23:59"}
    assert org._report_window({"from": "x; drop", "to": None}) == {"from": "00:00", "to": "23:59"}


def test_both_report_routes_use_it():
    src = open(org.__file__, encoding="utf-8").read()
    assert src.count('"window": _report_window(body),') == 2


SEGS = [{"from": "11:02:03", "to": "11:02:21"}, {"from": "11:02:41", "to": "11:03:21"}]


def test_THE_an_interrupted_checks_stretches_travel_and_the_gap_is_left_out():
    w = org._report_window({"from": "11:02:03", "to": "11:03:21", "segments": SEGS})
    assert w == {"from": "11:02:03", "to": "11:03:21", "segments": SEGS}
    gaps = sr._segment_gaps("2026-10-05", w["segments"])
    assert gaps == [(datetime.datetime(2026, 10, 5, 11, 2, 21), datetime.datetime(2026, 10, 5, 11, 2, 41))]


def test_one_bad_stretch_and_the_window_is_plain():
    for bad in ([SEGS[1], SEGS[0]],                                  # out of order
                [SEGS[0], {"from": "11:03:00", "to": "11:02:50"}],    # backwards
                [SEGS[0], {"from": "nope", "to": "11:04"}],
                [SEGS[0]]):                                          # one is not "stretches"
        w = org._report_window({"from": "11:02:03", "to": "11:03:21", "segments": bad})
        assert w == {"from": "11:02:03", "to": "11:03:21"}, bad


def test_the_steel_check_between_the_stretches_is_not_offered():
    topics = [{"time_range": "11:01 – 11:02", "topic_title": "pre-pour"},
              {"time_range": "11:05 – 11:06", "topic_title": "steel"},     # in the gap
              {"time_range": "11:08 – 11:09", "topic_title": "pre-pour again"}]
    day = "2026-10-05"
    stretches = [(sr._clock(day, "11:01:00"), sr._clock(day, "11:03:00")),
                 (sr._clock(day, "11:08:00"), sr._clock(day, "11:10:00"))]
    kept = [t["topic_title"] for t in topics
            if any(sr._in_window(t, day, a, b) for a, b in stretches)]
    assert kept == ["pre-pour", "pre-pour again"]
    src = open(sr.__file__, encoding="utf-8").read()
    assert "transcript_window.clip_to_window(turns, win_from, win_to, gaps)" in src
    assert "if (any(test(t, date, a, b) for a, b in stretches) if stretches" in src



def test_THE_a_checks_report_reads_only_the_speech_inside_its_window():
    """TEST, 2026-10-06: the pre-pour check ended 11:02:14, and the Level 2
    PPE talk later in the same audio file answered its PPE item."""
    day = "2026-10-05"
    w = {"from": "10:59:09", "to": "11:02:14"}
    assert sr._precise(w) and not sr._precise({"from": "09:00", "to": "11:30"})
    steel = {"time_range": "11:02 – 11:03"}
    hvac = {"time_range": "10:59 – 11:02"}
    a, b = sr._clock(day, w["from"]), sr._clock(day, w["to"])
    assert not sr._mostly_in(steel, day, a, b), "14 of its 120 seconds"
    assert sr._mostly_in(hvac, day, a, b)
    src = open(sr.__file__, encoding="utf-8").read()
    assert "transcript_window.clip_to_window(turns, win_from, win_to, gaps)" in src
    assert "precise=_precise(window)" in src


def test_a_windowed_report_lists_only_its_topics_actions():
    content = {"topics": [
        {"action_items": [{"action": "HVAC email", "responsible": "John"}]},
        {"action_items": [{"action": "PPE on level two"}]}]}
    offer = [{"ref": "t0"}]
    assert [a["action"] for a in sr._action_items_for_prompt(content, offer)] == ["HVAC email"]
    assert len(sr._action_items_for_prompt(content)) == 2, "no window, every action as before"
    src = open(sr.__file__, encoding="utf-8").read()
    assert src.count("_action_items_for_prompt(content, topic_offer)") == 2



# ---- to the word -------------------------------------------------------------------

import transcript_window as tw  # noqa: E402


def _turn(start, words):
    """One 90-second single-speaker turn, as a one-person recording makes."""
    base = datetime.datetime(2026, 10, 6, 17, 14, 22)
    ws = [(w, base + datetime.timedelta(seconds=o)) for w, o in words]
    return {"at": ws[0][1], "until": ws[-1][1], "line": "[17:14:22 – 17:16:10] ben (spk_0): " +
            " ".join(w for w, _ in words), "words": ws}


WALK = _turn(0, [("Formwork", 0), ("set", 1), ("out", 2), ("fine.", 3),
                 ("Steel", 66), ("sixteens", 67), ("at", 68), ("two", 69), ("hundred.", 70),
                 ("Reo", 81), ("twelves", 82), ("at", 83), ("two", 84), ("hundred.", 85)])


def test_THE_a_gap_cuts_the_words_in_it_not_the_whole_turn():
    """TEST 2026-10-06: the steel check (17:15:27-17:15:42) sat inside one
    90-second turn; dropping the turn lost the formwork and reo items around it."""
    day = datetime.datetime(2026, 10, 6)
    gap = (day.replace(hour=17, minute=15, second=27), day.replace(hour=17, minute=15, second=42))
    out = tw.clip_to_window([WALK], day.replace(hour=17, minute=12, second=52),
                            day.replace(hour=17, minute=17, second=33), [gap])
    assert len(out) == 1
    line = out[0]["line"]
    assert "Formwork set out fine." in line and "Reo twelves at two hundred." in line
    assert "sixteens" not in line
    assert line.startswith("[17:14:22 – 17:15:47] ben (spk_0): ")


def test_a_window_end_cuts_the_rest_of_the_file():
    day = datetime.datetime(2026, 10, 6)
    out = tw.clip_to_window([WALK], day.replace(hour=17, minute=14), day.replace(hour=17, minute=15, second=27))
    assert "Formwork" in out[0]["line"] and "Steel" not in out[0]["line"]


def test_a_turn_without_word_times_is_kept_whole_or_not_at_all():
    day = datetime.datetime(2026, 10, 6)
    old = dict(WALK, words=[])
    gap = (day.replace(hour=17, minute=15, second=27), day.replace(hour=17, minute=15, second=42))
    assert tw.clip_to_window([old], day.replace(hour=17), day.replace(hour=18)) == [old]
    assert tw.clip_to_window([old], day.replace(hour=17), day.replace(hour=18), [gap]) == []


def test_headings_written_without_hashes_are_still_the_templates_sections():
    """TEST 2026-10-06: muse-spark wrote the section titles as plain lines."""
    text = ("Pour Details\nLevel two slab, Thursday 7am. [t0]\n"
            "**A. Documents & Approvals**\n| Item no | Answer |\n| 1 | Yes |\n"
            "B. Formwork & Falsework:\nsomething [t1]")
    secs = sr._prose_sections(text, titles=["Pour Details", "A. Documents & Approvals",
                                            "B. Formwork & Falsework"])
    assert [s["title"] for s in secs] == ["Pour Details", "A. Documents & Approvals",
                                          "B. Formwork & Falsework"]
    assert secs[1]["paragraphs"][0].startswith("| Item no")
    assert [s["title"] for s in sr._prose_sections("Pour Details\nx")] == [""], "no titles, as before"
