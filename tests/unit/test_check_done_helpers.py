"""Check done (step A), the pure parts: which checklist, how far the stored
transcript reaches, which chunks still need hearing, and what is said aloud.

Spec: docs/superpowers/specs/2026-10-20-check-done-gaps-design.md
"""
import io
import json
import wave

import check_done as cd

SID = "9f8c1e2a4b6d47f0a1b2c3d4e5f60718"
OTHER = "0" * 32
PREPOUR = {"id": "t-pp", "name": "Pre-pour Inspection Checklist", "scope": "org",
           "body": {"sections": [
               {"title": "A. Formwork", "kind": "checklist", "items": ["Formwork braced", "Edge protection"]},
               {"title": "Notes", "kind": "prose"},
               {"title": "B. Steel", "kind": "checklist", "items": ["Cover to steel checked"],
                "children": [{"title": "B1", "kind": "checklist", "items": ["Bar chairs"]}]}]}}
STEEL = {"id": "t-st", "name": "Reinforcing Steel", "scope": "org",
         "body": {"sections": [{"title": "S", "kind": "checklist", "items": ["Laps"]}]}}
PROSE_ONLY = {"id": "t-pr", "name": "Daily Diary", "scope": "org",
              "body": {"sections": [{"title": "Notes", "kind": "prose"}]}}


def chunk(i, hhmmss="10-00-00", sid=SID, ext=".wav"):
    return "users/Ben/audio/2026-10-20/F2SP01_2026-10-20_%s_sid%s_c%04d%s" % (hhmmss, sid, i, ext)


def tkey(i, suffix="_off0.0_to29.5_srcwav.json", sid=SID):
    return "transcripts/Ben/2026-10-20/F2SP01_2026-10-20_10-00-00_sid%s_c%04d%s" % (sid, i, suffix)


# ---- which check ------------------------------------------------------------

def test_done_words_are_stripped():
    assert cd.strip_done_words("pre-pour check done") == "pre-pour"
    assert cd.strip_done_words("Pre-pour checklist finished.") == "Pre-pour"
    assert cd.strip_done_words("what's left on the steel check") == "the steel"
    assert cd.strip_done_words("fire stopping inspection complete") == "fire stopping inspection"
    assert cd.strip_done_words("done") == ""
    assert cd.strip_done_words(None) == ""


def test_the_spoken_check_picks_its_template():
    tpl, window, closest = cd.choose_template("pre-pour check done", [STEEL, PREPOUR], [])
    assert tpl["id"] == "t-pp" and window is None


def test_the_live_window_of_the_matched_template_gives_the_start():
    windows = [{"template_id": "t-pp", "start_at": "10:01:00"},
               {"template_id": "t-st", "start_at": "10:20:00"},
               {"template_id": "t-pp", "start_at": "10:30:00"}]
    tpl, window, _ = cd.choose_template("pre-pour done", [STEEL, PREPOUR], windows)
    assert tpl["id"] == "t-pp" and window["start_at"] == "10:30:00"


def test_no_spoken_match_falls_back_to_the_live_window():
    windows = [{"template_id": None, "start_at": "09:00:00"},
               {"template_id": "t-st", "start_at": "10:20:00"}]
    tpl, window, _ = cd.choose_template("done", [STEEL, PREPOUR], windows)
    assert tpl["id"] == "t-st" and window["start_at"] == "10:20:00"


def test_no_template_names_the_two_closest():
    tpl, window, closest = cd.choose_template("scaffold check done", [STEEL, PREPOUR, PROSE_ONLY], [])
    assert tpl is None
    assert len(closest) == 2 and "Daily Diary" not in closest


def test_a_template_without_checklist_items_is_no_template():
    windows = [{"template_id": "t-pr", "start_at": "10:00:00"}]
    tpl, _, _ = cd.choose_template("done", [PROSE_ONLY], windows)
    assert tpl is None


def test_every_checklist_section_is_read_in_template_order():
    assert cd.checklist_items(PREPOUR) == ["Formwork braced", "Edge protection",
                                           "Cover to steel checked", "Bar chairs"]


# ---- how far the stored transcript reaches -----------------------------------

def test_last_covered_is_the_highest_plain_chunk_index():
    keys = [tkey(0), tkey(1), tkey(4), tkey(4, "_off10.0_to20.0_srcwav.json")]
    assert cd.last_covered_index(keys, {}) == 4


def test_a_batch_counts_every_member_from_its_map():
    b = tkey(2, "_bn3_off0.4_to88.0_srcwav.json")
    maps = {b: {"members": [{"chunk_index": 2}, {"chunk_index": 3}, {"chunk_index": 5}]}}
    assert cd.last_covered_index([tkey(0), b], maps) == 5


def test_a_batch_without_its_map_counts_only_what_its_name_proves():
    b = tkey(2, "_bn3_off0.4_to88.0_srcwav.json")
    assert cd.last_covered_index([b], {}) == 4


def test_nothing_stored_covers_nothing():
    assert cd.last_covered_index([], {}) is None


# ---- which chunks still need hearing ----------------------------------------

def test_only_this_sessions_uncovered_raw_wavs_are_chosen():
    keys = [chunk(i) for i in range(6)] + [chunk(9, sid=OTHER), chunk(7, ext=".json"),
                                            chunk(8, ext=".mp4")]
    chosen, skipped = cd.select_new_chunks(keys, SID, covered=3)
    assert [cd.chunk_index(k) for k in chosen] == [4, 5]
    assert skipped == 0


def test_a_silent_gap_is_judged_by_index_not_presence():
    # c0002 never got a transcript (silent), c0003 did: 2 is covered.
    chosen, _ = cd.select_new_chunks([chunk(i) for i in range(5)], SID, covered=3)
    assert [cd.chunk_index(k) for k in chosen] == [4]


def test_at_most_eight_newest_and_the_rest_counted():
    chosen, skipped = cd.select_new_chunks([chunk(i) for i in range(12)], SID, covered=None)
    assert [cd.chunk_index(k) for k in chosen] == list(range(4, 12))
    assert skipped == 4


# ---- how long a chunk is ------------------------------------------------------

def _wav(seconds, rate=16000):
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(rate)
        w.writeframes(b"\x00\x00" * int(rate * seconds))
    return buf.getvalue()


def test_wav_seconds_come_from_the_header():
    assert abs(cd.wav_seconds(_wav(2.5)) - 2.5) < 0.01
    assert cd.wav_seconds(b"not a wav") is None


def test_chunk_end_is_its_nz_start_plus_its_length_as_epoch():
    # 2026-10-20 10:00:30 NZDT (+13) == 2026-10-19 21:00:30 UTC
    import calendar
    want = calendar.timegm((2026, 10, 19, 21, 0, 30)) + 29
    assert cd.chunk_end_epoch(chunk(1, "10-00-30"), 29.0) == want


# ---- what is said -------------------------------------------------------------

def test_first_clause_and_twelve_words():
    assert cd.first_clause("Formwork braced, plumb and clean") == "Formwork braced"
    long = " ".join("w%d" % i for i in range(20))
    assert cd.first_clause(long) == " ".join("w%d" % i for i in range(12))


def test_say_none_open():
    s = cd.say_ok("the pre-pour check", 18, [], [], partial=False)
    assert s == "All 18 items on the pre-pour check are covered."


def test_say_some_open_and_failed():
    s = cd.say_ok("the pre-pour check", 18,
                  [{"n": 1, "text": "Formwork braced"}, {"n": 4, "text": "Bar chairs"}],
                  [{"n": 7, "text": "Edge protection, all sides", "comment": "north"}],
                  partial=False)
    assert s == ("2 items open on the pre-pour check: Formwork braced; Bar chairs. "
                 "And 1 marked No: Edge protection.")


def test_say_one_open_is_singular():
    s = cd.say_ok("the pre-pour check", 3, [{"n": 1, "text": "Formwork braced"}], [], partial=False)
    assert s == "1 item open on the pre-pour check: Formwork braced."


def test_say_more_than_five_points_to_the_app():
    opened = [{"n": i, "text": "Item %d" % i} for i in range(1, 8)]
    s = cd.say_ok("the pre-pour check", 18, opened, [{"n": 9, "text": "Rails"}], partial=False)
    assert s == ("7 items open on the pre-pour check: Item 1; Item 2; Item 3; Item 4; Item 5; "
                 "and 2 more. And 1 marked No. See the app for the rest.")


def test_say_none_open_but_failed():
    s = cd.say_ok("the pre-pour check", 18, [], [{"n": 2, "text": "Rails"}], partial=False)
    assert s == "All 18 items on the pre-pour check are covered. 1 marked No: Rails."


def test_say_partial_is_prefixed():
    s = cd.say_ok("the pre-pour check", 18, [], [], partial=True)
    assert s == "As of a minute ago, all 18 items on the pre-pour check are covered."


def test_say_no_template_and_error_and_no_recording():
    assert cd.say_no_template("scaffold", ["Pre-pour Inspection Checklist", "Reinforcing Steel"]) == (
        "I couldn't find a checklist for scaffold. The closest are "
        "Pre-pour Inspection Checklist and Reinforcing Steel.")
    assert cd.say_no_template("", []) == "I couldn't find a checklist set up for this check."
    assert cd.say_error() == "I couldn't check the list just now; it will be in today's report."
    assert "recording" in cd.say_no_recording()


# ---- holes just below the covered index -----------------------------------------

def test_covered_indices_list_every_chunk_a_stored_transcript_holds():
    b = tkey(5, "_bn3_off0.4_to88.0_srcwav.json")
    maps = {b: {"members": [{"chunk_index": 5}, {"chunk_index": 7}]}}
    assert cd.covered_indices([tkey(0), tkey(2), b], maps) == {0, 2, 5, 7}
    assert cd.covered_indices([tkey(9, "_bn2_off0.0_to50.0_srcwav.json")], {}) == {9, 10}


def test_a_hole_just_below_the_covered_index_is_heard_too():
    # c0005 and c0006 have no transcript yet (a batch still open, or a failed
    # one) while c0007 has: they are not silence until we know it.
    keys = [chunk(i) for i in range(9)]
    chosen, skipped = cd.select_new_chunks(keys, SID, covered=7, covered_set={0, 1, 2, 3, 4, 7})
    assert [cd.chunk_index(k) for k in chosen] == [5, 6, 8]
    assert skipped == 0


def test_only_the_last_few_indices_are_searched_for_holes():
    keys = [chunk(i) for i in range(12)]
    chosen, _ = cd.select_new_chunks(keys, SID, covered=10, covered_set={10})
    assert [cd.chunk_index(k) for k in chosen] == [7, 8, 9, 11]


def test_wav_seconds_trust_the_bytes_not_the_frame_count():
    # A streaming writer that never patched its header claims 0x7fffffff bytes of
    # data; the length of what was actually uploaded is the truth.
    data = bytearray(_wav(3.0))
    at = data.index(b"data")
    data[at + 4:at + 8] = (0x7FFFFFF0).to_bytes(4, "little")
    data[4:8] = (0x7FFFFFF0).to_bytes(4, "little")
    assert abs(cd.wav_seconds(bytes(data)) - 3.0) < 0.01


def test_wav_seconds_are_clamped_to_a_chunk():
    assert cd.wav_seconds(_wav(90.0, rate=8000)) == cd.MAX_CHUNK_SECONDS


def test_a_named_check_that_matches_nothing_is_no_template_even_with_a_live_window():
    # He said "scaffold check done" during a pre-pour window: answering about the
    # pre-pour checklist would read the wrong list aloud as if it were his.
    windows = [{"template_id": "t-pp", "start_at": "10:30:00"}]
    tpl, window, closest = cd.choose_template("scaffold check done", [STEEL, PREPOUR], windows)
    assert tpl is None and window is None and closest
