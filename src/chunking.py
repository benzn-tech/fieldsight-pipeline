"""
chunking.py — Pure chunking logic for Phase 4a ingestion (gate-approved params).

Ports the gate-approved sample chunker's LOGIC into a pure, IO-free module.
No S3 / file reads / glob live here — the caller (ingest lambda) supplies:
  - `report`: the parsed daily_report.json dict
  - `turns`:  flattened `transcript_utils.normalize_transcript()` speaker_turns
              (across all of a day's transcript files), each with a caller-added
              'src' key (the source transcript filename)

Chunking strategy (gate-approved 2026-07-06, two-day tested incl. dirty
time_range data):
  A. topic chunks (chunk_type='topic') — one chunk per topic; text = title/
     time_range/category line + participants + summary + decisions/actions/
     safety lines. Oversize topics (> TOPIC_SPLIT_CHARS) split into two
     overlapping parts (repeated title line, 2-line overlap).
  B. transcript_window chunks (chunk_type='transcript_window') — speaker turns
     bucketed by topic time_range (+- ASSIGN_BUFFER_SEC), packed into
     ~TARGET_CHARS windows on turn boundaries (never mid-sentence), adjacent
     windows overlap by OVERLAP_TURNS turns. Turns outside every topic's time
     window fall into an "unassigned" bucket (still ingested, still searchable).
"""

TARGET_CHARS = 2600      # transcript window target size (chars)
OVERLAP_TURNS = 2        # turns carried over between adjacent windows
TOPIC_SPLIT_CHARS = 4500 # topic chunk oversize threshold (chars)
ASSIGN_BUFFER_SEC = 120  # +- buffer (seconds) around a topic's time_range


def parse_time_range(tr):
    """Parse a topic's 'time_range' string into (start_sec, end_sec) seconds-
    of-day. Handles real dirty data:
      - '' / None            -> None (topic does not participate in
                                 transcript assignment)
      - single 'HH:MM'       -> (v, v)  (BUG-09 collapse case)
      - 'HH:MM - HH:MM'      -> (start_sec, end_sec) — either en dash (–)
        or plain hyphen, with or without surrounding spaces
    """
    parts = [x.strip() for x in (tr or "").replace("–", "-").split("-") if x.strip()]

    def sec(hms):
        # Real lake data mixes 'HH:MM' and 'HH:MM:SS' — accept both.
        p = hms.split(":")
        return int(p[0]) * 3600 + int(p[1]) * 60 + (int(p[2]) if len(p) > 2 else 0)

    try:
        if len(parts) == 2:
            return sec(parts[0]), sec(parts[1])
        if len(parts) == 1:
            v = sec(parts[0])
            return v, v
    except (ValueError, IndexError):
        # Unparseable time_range → same rule as empty: topic does not
        # participate in transcript assignment.
        return None
    return None


def _topic_text(t):
    # Defensive .get: one real lake report (2026-04-07 Ben_Test) carries a
    # truncated final topic with no topic_title — a missing key must degrade
    # to an untitled chunk, not a KeyError that fails the whole report.
    header = (f"[{t.get('time_range', '')}] {t.get('topic_title') or '(untitled)'}"
              f" ({t.get('category', '')})")
    parts = [header]
    if t.get("participants"):
        parts.append("Participants: " + ", ".join(t["participants"]))
    parts.append(t.get("summary", ""))
    for d in t.get("key_decisions", []):
        parts.append(f"Decision: {d}")
    for a in t.get("action_items", []):
        parts.append(
            f"Action: {a.get('action', '')} — {a.get('responsible', '?')}"
            + (f", due {a['deadline']}" if a.get("deadline") else "")
        )
    for s in t.get("safety_flags", []):
        parts.append(
            f"Safety ({s.get('risk_level', '?')}): {s.get('observation', '')}"
            + (f" → {s['recommended_action']}" if s.get("recommended_action") else "")
        )
    return "\n".join(p for p in parts if p)


def _topic_meta(report, t, extra=None):
    m = {
        "user_name": report.get("user_name", ""),
        "site": report.get("site", ""),
        "report_date": report.get("report_date", ""),
        "topic_seq": t.get("topic_id"),
        "time_range": t.get("time_range", ""),
        "category": t.get("category", ""),
        "participants": t.get("participants", []),
    }
    if extra:
        m.update(extra)
    return m


def chunk_report(report):
    """Build one (or more, if oversize) 'topic' chunk per report topic."""
    chunks = []
    for t in report.get("topics", []):
        if t.get("work_class") == "non_work":
            continue                       # spec §6: personal talk never embedded
        text = _topic_text(t)
        if len(text) <= TOPIC_SPLIT_CHARS:
            chunks.append({
                "chunk_type": "topic",
                "chunk_text": text,
                "topic_seq": t.get("topic_id"),
                "metadata": _topic_meta(report, t),
            })
        else:
            # Oversize: split into two overlapping parts (2-line overlap),
            # each part repeats the title line (small-to-big rollup anchor).
            head = text.split("\n")[0]
            body = text.split("\n")[1:]
            half = len(body) // 2
            for i, seg in enumerate([body[: half + 1], body[half - 1 :]]):
                chunks.append({
                    "chunk_type": "topic",
                    "chunk_text": head + f"  (part {i + 1}/2)\n" + "\n".join(seg),
                    "topic_seq": t.get("topic_id"),
                    "metadata": _topic_meta(report, t, {"part": f"{i + 1}/2"}),
                })
    return chunks


def _turn_sec(turn):
    d = turn["abs_start"]
    return d.hour * 3600 + d.minute * 60 + d.second


def _window_metadata(report, topic, index, window):
    source_files = sorted({turn["src"] for turn in window})
    window_span = f"{window[0]['abs_start_str']}–{window[-1]['abs_end_str']}"
    if topic is not None:
        return _topic_meta(report, topic, {
            "window_index": index,
            "turns": len(window),
            "window_span": window_span,
            "source_files": source_files,
        })
    return {
        "user_name": report.get("user_name", ""),
        "site": report.get("site", ""),
        "report_date": report.get("report_date", ""),
        "topic_seq": None,
        "note": "unassigned: no owning topic (gap between topics / casual talk) "
                "— still ingested, searchable",
        "window_index": index,
        "turns": len(window),
        "window_span": window_span,
        "source_files": source_files,
    }


def _build_window_chunk(report, topic, tid, index, window):
    text = "\n".join(f"[{turn['abs_start_str']}] {turn['speaker']}: {turn['text']}" for turn in window)
    return {
        "chunk_type": "transcript_window",
        "chunk_text": text,
        "topic_seq": tid,
        "metadata": _window_metadata(report, topic, index, window),
    }


def chunk_transcripts(report, turns):
    """Bucket normalized speaker turns by owning topic (via time_range +-
    ASSIGN_BUFFER_SEC), then pack each bucket into ~TARGET_CHARS windows on
    turn boundaries, with OVERLAP_TURNS carried into the next window.
    """
    ordered_turns = sorted(turns, key=lambda turn: turn["abs_start"])

    topic_ranges = []
    for t in report.get("topics", []):
        pr = parse_time_range(t.get("time_range"))
        if pr:
            topic_ranges.append((t.get("topic_id"), pr[0], pr[1], t))

    def owner(turn):
        s = _turn_sec(turn)
        for tid, start_sec, end_sec, t in topic_ranges:
            if start_sec - ASSIGN_BUFFER_SEC <= s <= end_sec + ASSIGN_BUFFER_SEC:
                return tid, t
        return None, None

    buckets = {}
    for turn in ordered_turns:
        tid, topic = owner(turn)
        bucket = buckets.setdefault(tid, {"topic": topic, "turns": []})
        bucket["turns"].append(turn)

    chunks = []
    for tid, bucket in sorted(buckets.items(), key=lambda kv: (kv[0] is None, kv[0])):
        window, size, index = [], 0, 0
        for turn in bucket["turns"]:
            window.append(turn)
            size += len(turn["text"])
            if size >= TARGET_CHARS:
                chunks.append(_build_window_chunk(report, bucket["topic"], tid, index, window))
                index += 1
                window = window[-OVERLAP_TURNS:]
                size = sum(len(t["text"]) for t in window)
        # Final-window rule: flush the remainder only if it's the only window
        # for this bucket, or it holds more than the carried-over overlap.
        if window and (index == 0 or len(window) > OVERLAP_TURNS):
            chunks.append(_build_window_chunk(report, bucket["topic"], tid, index, window))

    return chunks
