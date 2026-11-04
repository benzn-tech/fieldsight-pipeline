"""A check he says he is starting opens a window; "finished" -- or the end of
the recording -- closes it (voice-triggered checklists, owner 2026-09-30).

Extraction's half: the model names the check and quotes the words; code
times both ends to the second from the words, and closes an unfinished check
where the recording stopped. Matching the check to the company's checklist
template happens in item-writer, which has the database.
"""
import pytest

from transcript_utils import normalize_transcript

es = pytest.importorskip("lambda_extract_session")

FILE = ("ben_lin_test_2026-10-05_11-02-01_sid37168e5632af4360b3784c752823f46c_c0007"
        "_off0.0_to109.0_srcwav.json")
WORDS = [("Starting", 2.0), ("the", 2.3), ("pre-pour", 2.5), ("check", 3.0), ("on", 3.2),
         ("level", 3.4), ("one.", 3.7), ("Pre-pour", 40.0), ("check", 40.4), ("done.", 40.8),
         ("Now", 50.0), ("the", 50.2), ("steel", 50.4), ("inspection.", 50.8), ("Bye.", 80.0)]


def turns():
    data = {"results": {"transcripts": [{"transcript": " ".join(w for w, _ in WORDS)}],
                        "items": [{"type": "pronunciation", "start_time": str(t),
                                   "end_time": str(t + 0.3), "speaker_label": "spk_0",
                                   "alternatives": [{"content": w, "confidence": "1.0"}]}
                                  for w, t in WORDS]}}
    return normalize_transcript(data, FILE)["speaker_turns"]


RAW = [{"name": "Level 1 pre-pour check", "kind": "pre-pour", "start_at": "11:02",
        "start_quote": "Starting the pre-pour check on level one.", "end_at": "11:02",
        "end_quote": "Pre-pour check done."},
       {"name": "steel inspection", "kind": "steel", "start_at": "11:02",
        "start_quote": "Now the steel inspection.", "end_at": None, "end_quote": None},
       {"name": "", "start_at": "11:03"},                       # no name: dropped
       "prose where a check goes"]


def test_THE_a_said_start_and_finish_bound_the_window_to_the_second():
    out = es._timed_inspections(RAW, turns())
    pre, steel = out
    assert (pre["start_at_s"], pre["end_at_s"], pre["end_source"]) == ("11:02:03", "11:02:41", "said")
    assert pre["kind"] == "pre-pour"


def test_a_check_never_finished_ends_where_the_recording_stopped():
    steel = es._timed_inspections(RAW, turns())[1]
    assert steel["start_at_s"] == "11:02:51"
    assert (steel["end_at_s"], steel["end_source"], steel["end_at"]) == ("11:03:21", "recording_stop", "11:03")


def test_a_check_with_no_name_or_start_is_not_a_window():
    assert len(es.clean_inspections(RAW)) == 2
    assert es.clean_inspections(None) == [] and es.clean_inspections("none") == []


def test_timing_that_fails_keeps_the_checks():
    out = es._timed_inspections(RAW, [{"abs_start": "not a time", "words": [("x", 1.0)]}])
    assert [i["name"] for i in out] == ["Level 1 pre-pour check", "steel inspection"]


def test_the_model_is_asked_for_checks_started_not_checks_discussed():
    prompt = es.EXTRACTION_SCHEMA
    assert '"inspections"' in prompt and '"start_quote"' in prompt
    src = open(es.__file__, encoding="utf-8").read()
    assert "0b. INSPECTIONS." in src and "only discussed, planned or described" in src
    assert "'inspections': _timed_inspections(parsed.get('inspections'), turns)," in src


def test_starting_the_next_check_ends_the_one_before():
    """Prod 10-05: no check was ever said to be finished; without this the
    Level 1 pre-pour window ran through the Level 2 steel inspection."""
    raw = [{"name": "L1 pre-pour", "kind": "pre-pour", "start_at": "11:02",
            "start_quote": "Starting the pre-pour check on level one."},
           {"name": "steel inspection", "kind": "steel", "start_at": "11:02",
            "start_quote": "Now the steel inspection."}]
    pre, steel = es._timed_inspections(raw, turns())
    assert (pre["end_at_s"], pre["end_source"]) == ("11:02:51", "next_check")
    assert (steel["end_at_s"], steel["end_source"]) == ("11:03:21", "recording_stop")


INTERRUPTED = [("Starting", 2.0), ("the", 2.3), ("pre-pour", 2.5), ("check.", 3.0),
               ("Quick", 20.0), ("steel", 20.3), ("inspection", 20.6), ("here.", 21.0),
               ("Back", 40.0), ("to", 40.2), ("the", 40.4), ("pre-pour.", 40.6), ("Bye.", 80.0)]


def interrupted_turns():
    data = {"results": {"transcripts": [{"transcript": " ".join(w for w, _ in INTERRUPTED)}],
                        "items": [{"type": "pronunciation", "start_time": str(t),
                                   "end_time": str(t + 0.3), "speaker_label": "spk_0",
                                   "alternatives": [{"content": w, "confidence": "1.0"}]}
                                  for w, t in INTERRUPTED]}}
    return normalize_transcript(data, FILE)["speaker_turns"]


def test_THE_an_interrupted_check_is_two_stretches_and_the_other_check_is_between():
    """Owner, 2026-10-05: "if it ends where another check is inserted, how do
    the interrupted parts join up?" -- as segments of one check."""
    raw = [{"name": "pre-pour", "kind": "pre-pour", "start_at": "11:02",
            "start_quote": "Starting the pre-pour check.",
            "resumed": [{"at": "11:02", "quote": "Back to the pre-pour."}]},
           {"name": "steel", "kind": "steel", "start_at": "11:02",
            "start_quote": "Quick steel inspection here."}]
    pre, steel = es._timed_inspections(raw, interrupted_turns())
    assert pre["segments"] == [{"from": "11:02:03", "to": "11:02:21"},
                               {"from": "11:02:41", "to": "11:03:21"}]
    assert (pre["start_at_s"], pre["end_at_s"], pre["end_source"]) == ("11:02:03", "11:03:21", "recording_stop")
    assert steel["segments"] == [{"from": "11:02:21", "to": "11:02:41"}]
    assert steel["end_source"] == "next_check"


def test_coming_back_without_anything_between_is_one_stretch():
    raw = [{"name": "pre-pour", "kind": "pre-pour", "start_at": "11:02",
            "start_quote": "Starting the pre-pour check.",
            "resumed": [{"at": "11:02", "quote": "Back to the pre-pour."}]}]
    pre = es._timed_inspections(raw, interrupted_turns())[0]
    assert pre["segments"] == [{"from": "11:02:03", "to": "11:03:21"}]


# Multi-unit walks (spec 2026-11-03): the opening's units, and each unit he
# walks INTO, ride on the check so the report can give each unit its checklist.

WALK = [("Starting", 2.0), ("the", 2.3), ("framing", 2.5), ("check,", 3.0), ("lot", 3.2),
        ("150", 3.4), ("and", 3.6), ("lot", 3.8), ("151.", 4.0),
        ("Now", 30.0), ("in", 30.3), ("lot", 30.6), ("151.", 30.9), ("Bye.", 80.0)]


def walk_turns():
    """Two turns: the opening, then (another speaker label from 30 s) "Now in lot 151." """
    data = {"results": {"transcripts": [{"transcript": " ".join(w for w, _ in WALK)}],
                        "items": [{"type": "pronunciation", "start_time": str(t),
                                   "end_time": str(t + 0.3),
                                   "speaker_label": "spk_0" if t < 30 else "spk_1",
                                   "alternatives": [{"content": w, "confidence": "1.0"}]}
                                  for w, t in WALK]}}
    return normalize_transcript(data, FILE)["speaker_turns"]


def test_the_opening_units_are_kept_normalized():
    raw = [{"name": "framing check", "kind": "framing", "start_at": "11:02",
            "start_quote": "Starting the framing check", "units_named": ["lot 150", "Lot 151"],
            "unit_count": None}]
    assert es.clean_inspections(raw)[0]["units"] == {"names": ["Lot 150", "Lot 151"], "count": 2}


def test_a_count_alone_is_kept_when_no_unit_is_named():
    raw = [{"name": "framing check", "start_at": "11:02", "units_named": [], "unit_count": 5}]
    assert es.clean_inspections(raw)[0]["units"] == {"names": [], "count": 5}


def test_an_entered_mark_without_a_unit_is_dropped():
    raw = [{"name": "framing check", "start_at": "11:02",
            "entered": [{"at": "11:03", "unit": "Kitchen", "quote": "into the kitchen"},
                        {"at": "11:04", "unit": None, "quote": "next one"},
                        {"unit": "Unit 3", "quote": "no time"},
                        "prose",
                        {"at": "11:05", "unit": "unit two", "quote": "now in unit two"}]}]
    assert es.clean_inspections(raw)[0]["entered"] == [
        {"unit": "Unit 2", "at": "11:05", "quote": "now in unit two"}]


def test_entered_marks_are_capped_at_40():
    raw = [{"name": "walk", "start_at": "11:02",
            "entered": [{"at": "11:03", "unit": "Unit %d" % i, "quote": "q"} for i in range(1, 60)]}]
    assert len(es.clean_inspections(raw)[0]["entered"]) == 40


def test_entered_marks_are_timed_to_the_second():
    turns = walk_turns()
    assert len(turns) >= 2 and "Now in lot 151" in " ".join(w for w, _ in turns[1]["words"])
    raw = [{"name": "framing check", "kind": "framing", "start_at": "11:02",
            "start_quote": "Starting the framing check, lot 150 and lot 151.",
            "units_named": ["Lot 150", "Lot 151"],
            "entered": [{"at": "11:02", "unit": "lot 151", "quote": "Now in lot 151."},
                        {"at": "11:02", "unit": "Lot 150", "quote": "words nobody said"}]}]
    check = es._timed_inspections(raw, turns)[0]
    found, unfound = check["entered"]
    assert found == {"unit": "Lot 151", "at": "11:02", "at_s": "11:02:31",
                     "quote": "Now in lot 151."}
    assert unfound == {"unit": "Lot 150", "at": "11:02", "quote": "words nobody said"}
    assert check["units"] == {"names": ["Lot 150", "Lot 151"], "count": 2}


def test_a_check_without_units_cleans_as_before():
    raw = [{"name": "Level 1 pre-pour check", "kind": "pre-pour", "start_at": "11:02",
            "start_quote": "Starting the pre-pour check on level one.", "end_at": "11:02",
            "end_quote": "Pre-pour check done."}]
    assert es.clean_inspections(raw) == [
        {"name": "Level 1 pre-pour check", "kind": "pre-pour", "start_at": "11:02",
         "start_quote": "Starting the pre-pour check on level one.", "end_at": "11:02",
         "end_quote": "Pre-pour check done.", "resumed": [],
         "units": {"names": [], "count": None}, "entered": []}]


def test_timing_that_fails_keeps_the_entered_minutes():
    raw = [{"name": "walk", "start_at": "11:02",
            "entered": [{"at": "11:03", "unit": "Unit 2", "quote": "now in unit two"}]}]
    out = es._timed_inspections(raw, [{"abs_start": "not a time", "words": [("x", 1.0)]}])
    assert out[0]["entered"] == [{"unit": "Unit 2", "at": "11:03", "quote": "now in unit two"}]


def test_the_model_is_asked_for_units_entered_not_units_mentioned():
    schema = es.EXTRACTION_SCHEMA
    assert '"units_named"' in schema and '"unit_count"' in schema and '"entered"' in schema
    src = open(es.__file__, encoding="utf-8").read()
    assert "walks INTO a unit" in src and "not a unit he only mentions" in src
    assert "an ordinary check in one room leaves it empty" in src
