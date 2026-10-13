"""
Tests for src/session_index.py -- the pure chunk builder for the session index.

Turns here have the shape `assemble_session_turns` returns (abs_start datetime,
abs_start_str / abs_end_str, speaker, text, and the segment filename).
"""
from datetime import datetime

import session_index as si


def turn(hms, text, src="Ben_2026-10-12_11-49-00_sidaaaa_c0001.json", spk="spk_0"):
    h, m, s = map(int, hms.split(":"))
    return {"speaker": spk, "text": text, "abs_start": datetime(2026, 10, 12, h, m, s),
            "abs_start_str": hms, "abs_end_str": hms, "src": src}


def make_req(**overrides):
    req = {"extraction_key": "extractions/Ben/2026-10-12/sidaaaa.json", "extracted_at": "t1",
           "tier": "final", "site_id": "s", "user_id": "u", "owner_company_id": "c",
           "report_date": "2026-10-12", "user_folder": "Ben", "session_base": "sidaaaa",
           "member_bases": [], "replaces": [],
           "topics": [
               {"id": "T1", "title": "Ceiling Services", "summary": "Hangers need rework.",
                "time_range": "11:52 – 11:54", "category": "quality", "participants": ["Ben"],
                "decisions": [{"decision": "Rework hangers", "rationale": "spacing"}],
                "questions": [{"question": "Who signs off?"}],
                "action_items": [{"action": "Book inspector", "responsible": "Sam", "deadline": "Fri"}],
                "findings": [{"observation": "Hanger spacing 1.4m", "severity": "medium", "domain": "quality"}],
                "work_class": "work"},
               {"id": "T2", "title": "Banter", "summary": "chat", "time_range": "12:02 – 12:03",
                "category": "progress", "participants": [], "decisions": [], "questions": [],
                "action_items": [], "findings": [], "work_class": "non_work"}]}
    req.update(overrides)
    return req


REQ = make_req()


def windows(chunks):
    return [c for c in chunks if c["chunk_type"] == "transcript_window"]


def test_topic_text_layout():
    c = [x for x in si.build_chunks(REQ, []) if x["chunk_type"] == "topic"]
    assert len(c) == 1 and c[0]["topic_id"] == "T1"
    t = c[0]["chunk_text"]
    assert t.splitlines()[0] == "[11:52 – 11:54] Ceiling Services (quality)"
    assert "Participants: Ben" in t
    assert "Hangers need rework." in t
    assert "Decision: Rework hangers — spacing" in t
    assert "Action: Book inspector (responsible: Sam, due: Fri)" in t
    assert "Open question: Who signs off?" in t
    assert "Finding (medium): Hanger spacing 1.4m" in t
    assert c[0]["metadata"] == {
        "index": "session", "tier": "final", "time_range": "11:52 – 11:54",
        "source_files": ["sidaaaa"], "topic_title": "Ceiling Services"}


def test_empty_fields_are_omitted():
    req = make_req(topics=[{"id": "T9", "title": "Bare", "summary": "", "time_range": "10:00 - 10:05",
                            "category": "progress", "participants": [], "decisions": [],
                            "questions": [], "action_items": [], "findings": [],
                            "work_class": "work"}])
    t = [x for x in si.build_chunks(req, []) if x["chunk_type"] == "topic"][0]["chunk_text"]
    assert t == "[10:00 - 10:05] Bare (progress)"


def test_action_without_responsible_or_deadline():
    req = make_req(topics=[dict(REQ["topics"][0], decisions=[], questions=[], findings=[],
                                action_items=[{"action": "Call", "responsible": "", "deadline": None}])])
    t = [x for x in si.build_chunks(req, []) if x["chunk_type"] == "topic"][0]["chunk_text"]
    assert "Action: Call" in t.splitlines()
    assert "responsible" not in t and "due" not in t


def test_non_work_span_is_excluded_from_windows():
    chunks = si.build_chunks(REQ, [turn("12:02:30", "private chat"), turn("11:53:00", "hangers")])
    assert not any(c["topic_id"] == "T2" for c in chunks)
    texts = " ".join(c["chunk_text"] for c in windows(chunks))
    assert "private chat" not in texts and "hangers" in texts


def test_non_work_exclusion_is_exact_not_buffered():
    # 12:03:30 is inside the non_work topic's +-120 s buffer but outside its span.
    chunks = si.build_chunks(REQ, [turn("12:03:30", "after banter")])
    texts = " ".join(c["chunk_text"] for c in windows(chunks))
    assert "after banter" in texts


def test_turn_after_topic_end_buffer():
    chunks = si.build_chunks(REQ, [turn("11:55:30", "near"), turn("11:57:20", "far")])
    w = windows(chunks)
    near = [c for c in w if "near" in c["chunk_text"]][0]
    far = [c for c in w if "far" in c["chunk_text"]][0]
    assert near["topic_id"] == "T1"
    assert far["topic_id"] is None
    assert far["metadata"]["block"] == {"from": "11:57", "to": "11:57"}


def test_two_turn_overlap_between_windows():
    turns = [turn(f"11:52:{i:02d}", f"line{i} " + "x" * 700) for i in range(10)]
    w = [c for c in windows(si.build_chunks(REQ, turns)) if c["topic_id"] == "T1"]
    assert len(w) >= 2
    for c in w:
        assert len(c["chunk_text"]) <= si.TARGET_CHARS
    a, b = w[0]["chunk_text"].splitlines(), w[1]["chunk_text"].splitlines()
    assert a[-2:] == b[:2]
    assert [c["metadata"]["turns"] for c in w] == [len(c["chunk_text"].splitlines()) for c in w]


def test_unassigned_turns_group_by_hour():
    turns = [turn("09:10:05", "early"), turn("09:40:00", "early two"), turn("10:20:00", "late")]
    w = windows(si.build_chunks(REQ, turns))
    assert len(w) == 2 and all(c["topic_id"] is None for c in w)
    assert w[0]["metadata"]["block"] == {"from": "09:10", "to": "09:40"}
    assert w[1]["metadata"]["block"] == {"from": "10:20", "to": "10:20"}
    assert w[0]["chunk_text"] == "[09:10:05] spk_0: early\n[09:40:00] spk_0: early two"


def test_window_metadata():
    t1 = turn("11:52:10", "a", src="f2.json")
    t2 = turn("11:52:40", "b", src="f1.json")
    t2["abs_end_str"] = "11:52:50"
    w = windows(si.build_chunks(REQ, [t1, t2]))[0]
    assert w["metadata"] == {"index": "session", "tier": "final",
                             "window_span": "11:52:10–11:52:50", "turns": 2,
                             "source_files": ["f1.json", "f2.json"]}


def test_turn_without_src_is_tolerated():
    t = turn("11:52:10", "a")
    del t["src"]
    w = windows(si.build_chunks(REQ, [t]))[0]
    assert w["metadata"]["source_files"] == []


def test_real_turn_key_source_filename_is_used():
    # assemble_session_turns stamps `source_filename`, not `src`.
    t = turn("11:52:10", "a")
    del t["src"]
    t["source_filename"] = "seg.json"
    w = windows(si.build_chunks(REQ, [t]))[0]
    assert w["metadata"]["source_files"] == ["seg.json"]


def test_digest_lists_only_work_topics():
    d = [c for c in si.build_chunks(REQ, [turn("11:49:00", "a"), turn("12:10:30", "b")])
         if c["chunk_type"] == "session_digest"]
    assert len(d) == 1 and d[0]["topic_id"] is None
    assert d[0]["chunk_text"] == ("Session 11:49–12:10\n"
                                  "- [11:52 – 11:54] Ceiling Services")
    assert d[0]["metadata"] == {"index": "session", "tier": "final", "source_files": ["sidaaaa"]}


def test_no_digest_without_work_topics():
    req = make_req(topics=[REQ["topics"][1]])
    assert not [c for c in si.build_chunks(req, [turn("11:49:00", "a")])
                if c["chunk_type"] == "session_digest"]


def test_oversize_topic_splits_and_repeats_header():
    req = make_req(topics=[dict(REQ["topics"][0], summary="s" * 5000)])
    c = [x for x in si.build_chunks(req, []) if x["chunk_type"] == "topic"]
    assert len(c) == 2
    for part in c:
        assert part["chunk_text"].splitlines()[0] == "[11:52 – 11:54] Ceiling Services (quality)"
        assert len(part["chunk_text"]) <= si.TOPIC_SPLIT_CHARS
        assert part["topic_id"] == "T1"
    assert sum(p["chunk_text"].count("s") for p in c) >= 5000


def test_source_files_topic_digest_vs_window():
    req = make_req(member_bases=["sidbbbb"])
    chunks = si.build_chunks(req, [turn("11:52:10", "a", src="seg_c0001.json")])
    by = {c["chunk_type"]: c for c in chunks}
    assert by["topic"]["metadata"]["source_files"] == ["sidaaaa", "sidbbbb"]
    assert by["session_digest"]["metadata"]["source_files"] == ["sidaaaa", "sidbbbb"]
    assert by["transcript_window"]["metadata"]["source_files"] == ["seg_c0001.json"]


def test_huge_header_terminates_and_every_part_fits():
    # A header that alone exceeds a part used to leave `room` <= 0 and loop forever.
    import threading
    req = make_req(topics=[dict(REQ["topics"][0], title="T" * 5000, summary="s" * 6000)])
    out = {}
    th = threading.Thread(target=lambda: out.update(c=si.build_chunks(req, [])), daemon=True)
    th.start()
    th.join(10)
    assert not th.is_alive(), "_split_topic_text did not terminate"
    parts = [x for x in out["c"] if x["chunk_type"] == "topic"]
    assert len(parts) >= 2
    for part in parts:
        assert len(part["chunk_text"]) <= si.TOPIC_SPLIT_CHARS
        assert len(part["chunk_text"].splitlines()[0]) <= si.HEADER_MAX_CHARS
