"""
Tests for src/chunking.py — pure chunker module (Phase 4a ingestion).

Ports the gate-approved sample chunker's logic (topic blocks + transcript
windows) into a pure, IO-free module consumed by the ingest lambda.
"""
from datetime import datetime

from chunking import chunk_report, chunk_transcripts, parse_time_range


# ---------------------------------------------------------------------------
# Synthetic-data helpers
# ---------------------------------------------------------------------------

def make_report(topics, **overrides):
    report = {
        "report_date": "2026-07-01",
        "user_name": "Test_User",
        "site": "Test Site",
        "topics": topics,
    }
    report.update(overrides)
    return report


def _dt(hh, mm, ss, date="2026-07-01"):
    y, mo, d = (int(x) for x in date.split("-"))
    return datetime(y, mo, d, hh, mm, ss)


def make_turn(hh, mm, ss, speaker, text, src, end=None):
    """Build a turn in normalize_transcript's speaker_turns shape + 'src'."""
    abs_start = _dt(hh, mm, ss)
    end_hh, end_mm, end_ss = end if end else (hh, mm, ss)
    abs_end = _dt(end_hh, end_mm, end_ss)
    return {
        "speaker": speaker,
        "text": text,
        "abs_start": abs_start,
        "abs_start_str": abs_start.strftime("%H:%M:%S"),
        "abs_end_str": abs_end.strftime("%H:%M:%S"),
        "src": src,
    }


# ---------------------------------------------------------------------------
# parse_time_range
# ---------------------------------------------------------------------------

def test_parse_time_range_empty_and_none_return_none():
    assert parse_time_range("") is None
    assert parse_time_range(None) is None


def test_parse_time_range_single_value_collapses_to_a_point():
    # BUG-09 dirty-data case: a single 'HH:MM' (no range) collapses to (v, v)
    assert parse_time_range("09:05") == (9 * 3600 + 5 * 60, 9 * 3600 + 5 * 60)


def test_parse_time_range_handles_en_dash_and_hyphen_ranges():
    expected = (9 * 3600, 9 * 3600 + 10 * 60)
    assert parse_time_range("09:00 – 09:10") == expected  # en dash
    assert parse_time_range("09:00-09:10") == expected          # bare hyphen
    assert parse_time_range("09:00 - 09:10") == expected        # spaced hyphen


# ---------------------------------------------------------------------------
# chunk_report — topic blocks
# ---------------------------------------------------------------------------

def test_chunk_report_builds_one_topic_chunk_with_expected_text_and_metadata():
    topic = {
        "topic_id": 0,
        "time_range": "09:00 – 09:05",
        "topic_title": "Safety Briefing",
        "category": "safety",
        "participants": ["Alice", "Bob"],
        "summary": "Discussed PPE requirements.",
        "key_decisions": ["All workers must wear hi-vis"],
        "action_items": [
            {"action": "Order more hard hats", "responsible": "Bob", "deadline": "Friday"}
        ],
        "safety_flags": [
            {"risk_level": "medium", "observation": "Missing barrier tape",
             "recommended_action": "Install tape"}
        ],
    }
    report = {
        "report_date": "2026-07-01",
        "user_name": "Test_User",
        # deliberately no 'site' key -> metadata must default to ''
        "topics": [topic],
    }

    chunks = chunk_report(report)

    assert len(chunks) == 1
    c = chunks[0]
    assert c["chunk_type"] == "topic"
    assert c["topic_seq"] == 0
    assert c["chunk_text"] == (
        "[09:00 – 09:05] Safety Briefing (safety)\n"
        "Participants: Alice, Bob\n"
        "Discussed PPE requirements.\n"
        "Decision: All workers must wear hi-vis\n"
        "Action: Order more hard hats — Bob, due Friday\n"
        "Safety (medium): Missing barrier tape → Install tape"
    )
    m = c["metadata"]
    assert m["user_name"] == "Test_User"
    assert m["site"] == ""
    assert m["report_date"] == "2026-07-01"
    assert m["topic_seq"] == 0
    assert m["time_range"] == "09:00 – 09:05"
    assert m["category"] == "safety"
    assert m["participants"] == ["Alice", "Bob"]


def test_chunk_report_splits_oversize_topic_into_two_overlapping_parts():
    filler = "X" * 800
    decisions = [f"D{i}{filler}" for i in range(1, 7)]  # 6 long decision lines
    topic = {
        "topic_id": 3,
        "time_range": "09:00 – 09:05",
        "topic_title": "Big Topic",
        "category": "quality",
        "participants": [],
        "summary": "",
        "key_decisions": decisions,
        "action_items": [],
        "safety_flags": [],
    }
    report = make_report([topic])

    chunks = chunk_report(report)

    assert len(chunks) == 2
    head = "[09:00 – 09:05] Big Topic (quality)"
    for i, c in enumerate(chunks, start=1):
        assert c["chunk_type"] == "topic"
        assert c["topic_seq"] == 3
        assert c["metadata"]["part"] == f"{i}/2"
        assert c["chunk_text"].startswith(head + f"  (part {i}/2)\n")

    # 2-line overlap: the middle decisions (D3, D4) must appear in BOTH parts
    assert f"Decision: D3{filler}" in chunks[0]["chunk_text"]
    assert f"Decision: D4{filler}" in chunks[0]["chunk_text"]
    assert f"Decision: D3{filler}" in chunks[1]["chunk_text"]
    assert f"Decision: D4{filler}" in chunks[1]["chunk_text"]
    # first part starts at D1, second part ends at D6 (no content lost)
    assert f"Decision: D1{filler}" in chunks[0]["chunk_text"]
    assert f"Decision: D6{filler}" in chunks[1]["chunk_text"]
    assert f"Decision: D1{filler}" not in chunks[1]["chunk_text"]
    assert f"Decision: D6{filler}" not in chunks[0]["chunk_text"]


# ---------------------------------------------------------------------------
# chunk_transcripts — transcript windows
# ---------------------------------------------------------------------------

def test_chunk_transcripts_packs_windows_by_target_chars_and_carries_overlap():
    topic = {
        "topic_id": 0,
        "time_range": "09:00 – 09:10",
        "topic_title": "Site Walk",
        "category": "progress",
        "participants": ["Jarley Trainor"],
    }
    report = make_report([topic])

    text900 = "T" * 900
    t1 = make_turn(9, 1, 0, "spk_0", text900, "fileA.json")
    t2 = make_turn(9, 1, 5, "spk_1", text900, "fileA.json")
    t3 = make_turn(9, 1, 10, "spk_0", text900, "fileB.json")
    t4 = make_turn(9, 1, 15, "spk_1", text900, "fileB.json")

    # Passed unsorted -> chunk_transcripts must sort by abs_start itself.
    chunks = chunk_transcripts(report, [t3, t1, t4, t2])

    assert len(chunks) == 2
    w0, w1 = chunks

    assert w0["chunk_type"] == "transcript_window"
    assert w0["topic_seq"] == 0
    assert w0["metadata"]["window_index"] == 0
    assert w0["metadata"]["turns"] == 3
    assert w0["chunk_text"] == (
        f"[09:01:00] spk_0: {text900}\n"
        f"[09:01:05] spk_1: {text900}\n"
        f"[09:01:10] spk_0: {text900}"
    )
    assert w0["metadata"]["window_span"] == "09:01:00–09:01:10"
    assert w0["metadata"]["source_files"] == ["fileA.json", "fileB.json"]
    assert w0["metadata"]["time_range"] == "09:00 – 09:10"
    assert w0["metadata"]["category"] == "progress"
    assert w0["metadata"]["participants"] == ["Jarley Trainor"]

    # window 1 carries the last OVERLAP_TURNS(2) turns from window 0, then t4.
    # (The leftover carried-only remainder after this is NOT flushed — see the
    # sole-window test below for the other half of the final-window rule.)
    assert w1["metadata"]["window_index"] == 1
    assert w1["metadata"]["turns"] == 3
    assert w1["chunk_text"] == (
        f"[09:01:05] spk_1: {text900}\n"
        f"[09:01:10] spk_0: {text900}\n"
        f"[09:01:15] spk_1: {text900}"
    )
    assert w1["metadata"]["source_files"] == ["fileA.json", "fileB.json"]


def test_chunk_transcripts_flushes_sole_window_even_if_under_target_chars():
    topic = {
        "topic_id": 7,
        "time_range": "14:00 – 14:01",
        "topic_title": "Quick Chat",
        "category": "progress",
        "participants": [],
    }
    report = make_report([topic])

    turns = [
        make_turn(14, 0, 5, "spk_0", "Short remark.", "fileC.json"),
        make_turn(14, 0, 8, "spk_1", "Even shorter.", "fileC.json"),
    ]

    chunks = chunk_transcripts(report, turns)

    assert len(chunks) == 1
    c = chunks[0]
    assert c["topic_seq"] == 7
    assert c["metadata"]["window_index"] == 0
    assert c["metadata"]["turns"] == 2
    assert c["chunk_text"] == (
        "[14:00:05] spk_0: Short remark.\n"
        "[14:00:08] spk_1: Even shorter."
    )


def test_chunk_transcripts_keeps_unassigned_turns_with_note_and_dedupes_source_files():
    topic = {
        "topic_id": 0,
        "time_range": "09:00 – 09:02",  # +-120s buffer -> 08:58:00-09:04:00
        "topic_title": "Corner Bead Fix",
        "category": "quality",
        "participants": ["Jarley Trainor"],
    }
    no_range_topic = {
        "topic_id": 1,
        "time_range": "",  # dirty data: does not participate in assignment
        "topic_title": "Untimed Topic",
        "category": "progress",
        "participants": [],
    }
    report = make_report([topic, no_range_topic])

    inside = make_turn(9, 1, 0, "spk_0", "Inside the window.", "fileA.json")
    outside = make_turn(9, 10, 0, "spk_1", "Long after the topic ends.", "fileA.json")
    far_gap = make_turn(13, 0, 0, "spk_0", "Unrelated chit-chat.", "fileD.json")

    chunks = chunk_transcripts(report, [outside, inside, far_gap])

    by_topic_seq = {c["topic_seq"]: c for c in chunks if c["topic_seq"] is not None}
    unassigned = [c for c in chunks if c["topic_seq"] is None]

    assert 0 in by_topic_seq
    assert by_topic_seq[0]["metadata"]["turns"] == 1
    assert "Inside the window." in by_topic_seq[0]["chunk_text"]

    assert len(unassigned) == 1
    u = unassigned[0]
    assert u["chunk_type"] == "transcript_window"
    assert u["metadata"]["topic_seq"] is None
    assert u["metadata"].get("note")
    assert u["metadata"]["turns"] == 2
    assert u["metadata"]["source_files"] == ["fileA.json", "fileD.json"]
    # unassigned metadata carries no topic-specific fields
    assert "time_range" not in u["metadata"]
    assert "category" not in u["metadata"]


def test_parse_time_range_more_dirty_lake_formats():
    """Real lake data (2026-07-07 batch prep): seconds-bearing and garbage ranges."""
    from chunking import parse_time_range
    assert parse_time_range("09:56:40 - 10:02:11") == (9*3600+56*60+40, 10*3600+2*60+11)
    assert parse_time_range("09:56:40") == (9*3600+56*60+40,)*2 or parse_time_range("09:56:40") == (9*3600+56*60+40, 9*3600+56*60+40)
    assert parse_time_range("unknown") is None
    assert parse_time_range("morning session") is None


def test_topic_missing_title_degrades_not_crashes():
    """Real lake data: 2026-04-07 Ben_Test final topic lacks topic_title."""
    from chunking import chunk_report
    report = {"user_name": "U", "site": "S", "report_date": "2026-04-07",
              "topics": [{"topic_id": 0, "time_range": "09:00 - 09:05",
                          "category": "general", "summary": "truncated topic"}]}
    chunks = chunk_report(report)
    assert len(chunks) == 1
    assert "(untitled)" in chunks[0]["chunk_text"]
