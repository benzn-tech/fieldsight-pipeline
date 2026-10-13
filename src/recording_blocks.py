"""Recording blocks: a day's recorded segments merged on silence. PURE -- no I/O.

Design: docs/superpowers/specs/2026-09-15-reports-over-any-stretch-of-the-day-design.md
§5.4/§5.5 as amended by §11 F1 and F9.

lambda_recording_segments stores raw segments; org-api's GET /sessions is the only
caller of this module and merges at read time, so a threshold change reaches every
day, finished or not (F9). Deleted recordings and wholly-excluded sessions are
filtered here at read time, never in storage (F1).

All times are seconds since midnight on the device's wall clock -- the clock
transcript filenames and topics.time_range carry. Nothing here converts timezones.
"""
from photo_binding import parse_time_range

_LAST_SECOND_OF_DAY = 86399


def _hhmm(seconds):
    """Floor to the minute. Clamped to 23:59: a segment whose VAD offset carries it past
    midnight keeps its raw numbers but must not render as "24:03"."""
    s = min(max(int(seconds), 0), _LAST_SECOND_OF_DAY)
    return f"{s // 3600:02d}:{(s % 3600) // 60:02d}"


def _is_number(value):
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def merge_segments(segments, gap_seconds, long_block_seconds):
    """Merge segments whose silence between them is <= gap_seconds.

    Returns blocks ordered by start:
      {"from": "HH:MM", "to": "HH:MM", "start": float, "end": float, "minutes": int,
       "selectable_as_whole": bool, "session_ids": [sorted unique non-null]}

    `selectable_as_whole` is end - start <= long_block_seconds (D1: a long block is
    chosen by its topics, not as a whole). A segment without numeric start/end is
    ignored rather than guessed.
    """
    ordered = sorted(
        (s for s in segments if _is_number(s.get("start")) and _is_number(s.get("end"))),
        key=lambda s: (float(s["start"]), float(s["end"])),
    )
    blocks = []
    current = None
    for seg in ordered:
        start = float(seg["start"])
        end = max(float(seg["end"]), start)
        if current is not None and start - current["end"] <= gap_seconds:
            current["end"] = max(current["end"], end)
        else:
            current = {"start": start, "end": end, "sids": set()}
            blocks.append(current)
        if seg.get("session_id"):
            current["sids"].add(seg["session_id"])
    return [{
        "from": _hhmm(b["start"]),
        "to": _hhmm(b["end"]),
        "start": b["start"],
        "end": b["end"],
        "minutes": int(round((b["end"] - b["start"]) / 60)),
        "selectable_as_whole": (b["end"] - b["start"]) <= long_block_seconds,
        "session_ids": sorted(b["sids"]),
    } for b in blocks]


def _tombstone_candidates(segment):
    """Every key a recording tombstone could name for this segment.

    The live tombstone shape is `extractions/{folder}/{date}/sid{32hex}` (a chunk
    session). A legacy whole-file recording's base is its filename minus `.json` and
    minus the `_off` suffix -- the rule lambda_extract_session.session_base_from_key
    applies -- so that is offered too. The transcript key itself is included so a
    tombstone written against the raw key also matches.
    """
    key = segment.get("key") or ""
    out = [key] if key else []
    parts = key.split("/")
    if len(parts) == 4 and parts[0] == "transcripts":
        folder, date, name = parts[1], parts[2], parts[3]
        sid = segment.get("session_id")
        if sid:
            out.append(f"extractions/{folder}/{date}/sid{sid}")
        elif name.endswith(".json"):
            out.append(f"extractions/{folder}/{date}/{name[:-len('.json')].split('_off')[0]}")
    return out


def filter_segments(segments, excluded_session_ids, deleted_prefixes):
    """Drop what the picker must not advertise (F1), at read time.

    - a segment whose session_id is in `excluded_session_ids` (a session all of whose
      topics were excluded by build_day_sessions);
    - a segment any of whose tombstone candidates starts with a deleted source prefix
      (redactions.deleted_source_prefixes -- prefix match, as DELETED_SOURCE_PREDICATE).
    """
    excluded = set(excluded_session_ids or ())
    prefixes = [p for p in (deleted_prefixes or ()) if p]
    kept = []
    for seg in segments:
        if seg.get("session_id") and seg["session_id"] in excluded:
            continue
        candidates = _tombstone_candidates(seg)
        if any(c.startswith(p) for c in candidates for p in prefixes):
            continue
        kept.append(seg)
    return kept


def topic_ids_in_block(block, topic_rows):
    """Ids (as str) of the topic rows whose time_range overlaps the block.

    time_range is minute precision ("11:30 – 11:36"), so a topic covers from the start
    of its first minute to the end of its last: "12:07 – 12:07" is 12:07:00-12:07:59,
    not a zero-width instant. Overlap, not containment -- the frontend's window rule.
    A topic whose range does not parse, or ends before it starts, is placed in no block.
    """
    ids = []
    for row in topic_rows:
        parsed = parse_time_range(row.get("time_range"))
        if parsed is None:
            continue
        start_min, end_min = parsed
        if end_min < start_min:
            continue
        topic_start = start_min * 60
        topic_end = end_min * 60 + 59
        if topic_start <= block["end"] and topic_end >= block["start"]:
            ids.append(str(row["id"]))
    return ids
