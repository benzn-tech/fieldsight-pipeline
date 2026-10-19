"""
Pure chunk builder for the session search index.

Turns one extraction request (the topics the extraction produced) plus the
session's speaker turns into the chunks that get embedded: one `topic` chunk
per work topic (split when oversize), `transcript_window` chunks packed from
the turns, and one `session_digest` chunk. No IO -- the caller feeds it the
request and the turns and does the embedding / writing.

Sizing constants and the time_range parser come from chunking.py so the
session index and the report index cut text the same way.

Personal talk is never indexed: a `non_work` topic yields no topic chunk and
its exact time span (no assignment buffer) keeps its turns out of every
window.
"""
from chunking import (
    ASSIGN_BUFFER_SEC,
    OVERLAP_TURNS,
    TARGET_CHARS,
    TOPIC_SPLIT_CHARS,
    parse_time_range,
)

INDEX = "session"
# The header repeats on every part, so it must leave room for body text.
HEADER_MAX_CHARS = 300


def _turn_sec(turn):
    d = turn["abs_start"]
    return d.hour * 3600 + d.minute * 60 + d.second


def _turn_src(turn):
    # `assemble_session_turns` stamps `source_filename`; chunking.py's own
    # fixtures use `src`. A turn with neither contributes no filename.
    return turn.get("src") or turn.get("source_filename") or ""


def _topic_lines(t):
    """Header plus body lines of a topic chunk; empty parts are omitted."""
    header = f"[{t.get('time_range', '')}] {t.get('title') or '(untitled)'} ({t.get('category', '')})"
    lines = []
    if t.get("participants"):
        lines.append("Participants: " + ", ".join(t["participants"]))
    if t.get("summary"):
        lines.append(t["summary"])
    for d in t.get("decisions") or []:
        line = f"Decision: {d.get('decision', '')}"
        if d.get("rationale"):
            line += f" — {d['rationale']}"
        lines.append(line)
    for a in t.get("action_items") or []:
        extras = []
        if a.get("responsible"):
            extras.append(f"responsible: {a['responsible']}")
        if a.get("deadline"):
            extras.append(f"due: {a['deadline']}")
        lines.append(f"Action: {a.get('action', '')}" + (f" ({', '.join(extras)})" if extras else ""))
    for q in t.get("questions") or []:
        lines.append(f"Open question: {q.get('question', '')}")
    for f in t.get("findings") or []:
        lines.append(f"Finding ({f.get('severity') or '?'}): {f.get('observation', '')}")
    return header, [ln for ln in lines if ln.strip()]


def _split_topic_text(header, body):
    """Texts for one topic: a single text when it fits, else parts that each
    repeat the header line. A line longer than a part is cut by characters."""
    header = header[:HEADER_MAX_CHARS]
    text = "\n".join([header] + body)
    if len(text) <= TOPIC_SPLIT_CHARS:
        return [text]
    room = TOPIC_SPLIT_CHARS - len(header) - 1
    parts, cur, size = [], [], 0
    for ln in body:
        while ln:
            avail = max(room - size - (1 if cur else 0), 0)
            if len(ln) <= avail:
                size += len(ln) + (1 if cur else 0)
                cur.append(ln)
                break
            if len(ln) <= room and cur:
                parts.append(cur)
                cur, size = [], 0
                continue
            # Longer than a whole part: fill what is left of this one, carry on.
            cut = ln[:avail]
            if cut:
                cur.append(cut)
            parts.append(cur)
            cur, size = [], 0
            ln = ln[len(cut):]
    if cur:
        parts.append(cur)
    return ["\n".join([header] + part) for part in parts]


def _pack_windows(turns):
    """Pack turns into <= TARGET_CHARS windows on turn boundaries, carrying
    OVERLAP_TURNS turns into the next window. A window is only emitted if it
    holds at least one turn not already in the previous window.

    A last window under TARGET_CHARS // 2 is back-filled with the turns
    before it, stopping before the first that would take it over
    TARGET_CHARS: a 639-char tail of a few turns was cosine rank 65 for a
    question whose words sat in the window before it, and rank 20 once
    back-filled (2026-10-16 spec §3b). It then overlaps its predecessor by
    more than OVERLAP_TURNS; window count is unchanged."""
    out, window, size, fresh = [], [], 0, 0
    for turn in turns:
        n = len(_window_line(turn)) + 1
        if window and fresh and size + n > TARGET_CHARS:
            out.append(window)
            window = window[-OVERLAP_TURNS:]
            size = sum(len(_window_line(t)) + 1 for t in window)
            fresh = 0
        window.append(turn)
        size += n
        fresh += 1
    if window and fresh:
        out.append(window)
    if len(out) >= 2 and _window_size(out[-1]) < TARGET_CHARS // 2:
        out[-1] = _backfill(turns, out[-1])
    return out


def _window_size(window):
    return sum(len(_window_line(t)) + 1 for t in window)


def _backfill(turns, tail):
    start = next(n for n, t in enumerate(turns) if t is tail[0])
    i, size = start, _window_size(tail)
    while i > 0:
        n = len(_window_line(turns[i - 1])) + 1
        if size + n > TARGET_CHARS:
            break
        i -= 1
        size += n
    return turns[i:start] + tail


def _window_line(turn):
    return f"[{turn['abs_start_str']}] {turn['speaker']}: {turn['text']}"


def _window_chunk(request, topic_id, window, extra_meta=None):
    meta = {
        "index": INDEX,
        "tier": request.get("tier"),
        "window_span": f"{window[0]['abs_start_str']}–{window[-1]['abs_end_str']}",
        "turns": len(window),
        "source_files": sorted({_turn_src(t) for t in window if _turn_src(t)}),
    }
    if extra_meta:
        meta.update(extra_meta)
    return {
        "chunk_type": "transcript_window",
        "chunk_text": "\n".join(_window_line(t) for t in window),
        "topic_id": topic_id,
        "metadata": meta,
    }


def _hhmm(turn):
    return turn["abs_start_str"][:5]


def build_chunks(request, turns):
    """Build the topic, transcript_window and session_digest chunks for one
    extraction request. `turns` are assemble_session_turns() dicts."""
    tier = request.get("tier")
    sources = [request.get("session_base"), *(request.get("member_bases") or [])]
    topics = request.get("topics") or []
    work = [t for t in topics if t.get("work_class") != "non_work"]
    chunks = []

    for t in work:
        header, body = _topic_lines(t)
        for text in _split_topic_text(header, body):
            chunks.append({
                "chunk_type": "topic",
                "chunk_text": text,
                "topic_id": t.get("id"),
                "metadata": {
                    "index": INDEX,
                    "tier": tier,
                    "time_range": t.get("time_range", ""),
                    "source_files": list(sources),
                    "topic_title": t.get("title"),
                },
            })

    # Personal talk is dropped from every window, on the topic's exact span.
    excluded = []
    for t in topics:
        if t.get("work_class") == "non_work":
            pr = parse_time_range(t.get("time_range"))
            if pr:
                excluded.append(pr)
    ranges = []
    for t in work:
        pr = parse_time_range(t.get("time_range"))
        if pr:
            ranges.append((t.get("id"), pr[0], pr[1]))

    ordered = sorted(turns, key=lambda t: t["abs_start"])
    by_topic = {t[0]: [] for t in ranges}
    by_hour = {}
    for turn in ordered:
        s = _turn_sec(turn)
        if any(lo <= s <= hi for lo, hi in excluded):
            continue
        for tid, lo, hi in ranges:
            if lo - ASSIGN_BUFFER_SEC <= s <= hi + ASSIGN_BUFFER_SEC:
                by_topic[tid].append(turn)
                break
        else:
            by_hour.setdefault(turn["abs_start"].hour, []).append(turn)

    for tid, owned in by_topic.items():
        for window in _pack_windows(owned):
            chunks.append(_window_chunk(request, tid, window))
    for hour in sorted(by_hour):
        for window in _pack_windows(by_hour[hour]):
            block = {"from": _hhmm(window[0]), "to": _hhmm(window[-1])}
            chunks.append(_window_chunk(request, None, window, {"block": block}))

    if work:
        if ordered:
            head = f"Session {_hhmm(ordered[0])}–{_hhmm(ordered[-1])}"
        else:
            head = "Session"
        lines = [head] + [f"- [{t.get('time_range', '')}] {t.get('title') or '(untitled)'}" for t in work]
        chunks.append({
            "chunk_type": "session_digest",
            "chunk_text": "\n".join(lines),
            "topic_id": None,
            "metadata": {"index": INDEX, "tier": tier, "source_files": list(sources)},
        })
    return chunks
