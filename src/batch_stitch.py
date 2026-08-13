"""Join four consecutive chunks into one transcription request, and remember where
everything was.

Pure Python by the same rule as `chunk_stitch`: no boto3, no environment, nothing at import
time. Bundled into every function automatically (`CodeUri: src/`).

Spec:  docs/superpowers/specs/2026-08-11-batched-transcription.md
Plan:  docs/superpowers/plans/2026-08-11-batched-transcription.md (phase 1)

Two things in here fail silently if they are wrong, and they are why the module exists as a
separate, tested unit rather than as code inside the transcriber:

**The time map.** The provider returns `word.start` counted from the first sample of the
concatenated file — an origin that appears in no filename. If the map is off, every timestamp
in the meeting shifts by a second or two, and reports, minutes and photo binding all still
look completely reasonable.

**The filename.** The batch key has to be read correctly by parsers that know nothing about
batching. `build_batch_name` is asserted against those real parsers in the tests rather than
against a second copy of the format.
"""
from __future__ import annotations

import logging
import re
from datetime import timedelta

from transcript_utils import extract_base_time_from_filename

_log = logging.getLogger()

BYTES_PER_SAMPLE = 2                     # PCM s16le mono, the capture format throughout
DEFAULT_MAX_BATCH = 4                    # four ~30 s chunks ≈ two minutes
DEFAULT_CEILING_SEC = 2.0                # the device configures overlapBytesFor(2)

_BATCH_RE = re.compile(r'_sid[0-9a-f]{32}_c\d+_bn(\d+)(?=[_.]|$)')


# ============================================================
# Measuring the overlap — never trusting a constant
# ============================================================

def measure_overlap(prev_pcm: bytes, next_pcm: bytes, rate: int,
                    ceiling_sec: float = DEFAULT_CEILING_SEC) -> float:
    """Seconds of audio at the head of `next_pcm` that repeat the tail of `prev_pcm`.

    Measured, not configured. The sources disagree — the mobile code says 2.0 s, a direct
    measurement on real files said 1.50 s — and the trim is the dangerous edit: too little
    and the transcript stutters, too much and real speech is deleted with no trace, which is
    exactly what PR #314 was.

    **Run this on the raw upload, never on a normalised segment.** `NORMALISE_AUDIO` applies
    `acompressor` + single-pass `loudnorm`, both time-varying, so identical source samples
    emerge as different bytes in the two chunks and this returns 0 — which, with the
    caller's "zero means do not trim" rule, would silently switch all trimming off.

    Returns 0.0 when nothing matches. That is a signal, not a measurement: the caller must
    keep the audio and record that the seam was unmeasured.
    """
    if rate <= 0 or not prev_pcm or not next_pcm:
        return 0.0
    # Whole samples only. A truncated upload can leave half a sample behind, and reading it
    # as a whole one shifts the grid by half a period — after which nothing ever matches.
    prev_pcm = prev_pcm[: len(prev_pcm) - (len(prev_pcm) % BYTES_PER_SAMPLE)]
    next_pcm = next_pcm[: len(next_pcm) - (len(next_pcm) % BYTES_PER_SAMPLE)]

    max_samples = int(ceiling_sec * rate)
    limit = min(max_samples, len(prev_pcm) // BYTES_PER_SAMPLE,
                len(next_pcm) // BYTES_PER_SAMPLE)
    if limit <= 0:
        return 0.0
    span = limit * BYTES_PER_SAMPLE
    head, tail = next_pcm[:span], prev_pcm[-span:]

    # Precomputed once rather than per candidate. A silent seam produces a candidate at
    # EVERY length up to the silence, and re-scanning each one is quadratic — 1.5 s of
    # zeros took 73 seconds in the tests before this line existed.
    need = _first_informative_length(head)
    for n in _candidate_overlaps(head, tail):
        if need is not None and n >= need:
            return n / rate
    # An overlap LONGER than the ceiling is invisible here: the match is a suffix/prefix
    # one, so every truncated comparison inside the ceiling fails and this returns 0. That
    # is the safe direction — the duplicate survives into the transcript and
    # `_dedup_turn_boundaries` still removes it, whereas capping would trim a length
    # nothing verified.
    return 0.0


def _candidate_overlaps(head: bytes, tail: bytes):
    """Every n where `tail[-n:] == head[:n]`, longest first, in linear time.

    The obvious loop — compare every length from the ceiling down — is O(n²) on two seconds
    of 16 kHz audio and takes minutes. This is the KMP prefix function of
    `head + sentinel + tail`: its final value is the longest such n, and walking the failure
    links from there enumerates the shorter ones in order, which is what the
    information guard needs when the longest match turns out to be a constant.
    """
    import array

    h = array.array("h")
    h.frombytes(head)
    t = array.array("h")
    t.frombytes(tail)
    SENTINEL = 1 << 20                      # cannot occur in 16-bit PCM
    s = list(h) + [SENTINEL] + list(t)

    pi = [0] * len(s)
    k = 0
    for i in range(1, len(s)):
        while k and s[i] != s[k]:
            k = pi[k - 1]
        if s[i] == s[k]:
            k += 1
        pi[i] = k

    n = pi[-1]
    while n > 0:
        yield n
        n = pi[n - 1]


def _first_informative_length(pcm: bytes, distinct_needed: int = 8):
    """Shortest prefix of `pcm` (in samples) that is audio rather than a constant, or None
    if the whole thing is a constant.

    This is what stops a silent seam being mistaken for the ring-buffer copy. Two chunks
    that both run into silence match for the whole length of the silence, and that match is
    not the overlap — trimming it deletes real audio. The case is concrete on this device: a
    refused microphone returns a positive read length and a buffer of zeros, which is
    indistinguishable from a quiet room to everything downstream.

    Returned as a length rather than a yes/no because distinct values only accumulate, so
    one scan answers the question for every candidate overlap at once.
    """
    seen = set()
    for i in range(0, len(pcm), BYTES_PER_SAMPLE):
        seen.add(pcm[i:i + BYTES_PER_SAMPLE])
        if len(seen) >= distinct_needed:
            return i // BYTES_PER_SAMPLE + 1
    return None


# ============================================================
# Which chunks may travel together
# ============================================================

# `plan_batches` — runs of consecutive indices, split at every gap — was removed on
# 2026-08-13 rather than left beside its replacement. Its rule made a VAD-dropped chunk a
# batch boundary, which shattered 31% of real sessions into short runs, and a dead function
# with a plausible name is how a superseded rule comes back.

DEFAULT_WINDOW_SEC = 120.0               # two minutes of wall clock — the actual target


def plan_windows(members, window_sec: float = DEFAULT_WINDOW_SEC,
                 cap: int = DEFAULT_MAX_BATCH, consumed=None) -> list[list[int]]:
    """Chunk indices grouped into wall-clock windows. Supersedes `plan_batches`.

    `members` maps chunk index -> that chunk's own base time (the device clock out of its
    filename). Only ever compared to each other, never to `time.time()`: the two are
    different clocks, ~13 hours apart, and mixing them seals a backlogged session's first
    chunk alone with zero effective grace.

    A window opens at the earliest unconsumed member and takes everything strictly inside
    `anchor + window_sec`. **A VAD-dropped chunk is not a boundary.** That is the whole
    point: batching's one measured benefit is a shared speaker namespace (33 labels -> 9),
    and splitting at a 30-second silence hands the same person an unrelated label on each
    side of it — in 37% of real sessions.

    Anchored greedily on the window's own first member, not on a grid aligned to the
    session start: a grid cuts dense stretches at its boundaries (480 requests against 470
    measured over every session in the lake) and can split a run of four that a boundary
    happens to straddle.

    `consumed` are members already sealed into a batch, and excluding them is load-bearing
    rather than tidy. The seal record is keyed by a window's FIRST index, so a chunk that
    arrives late and earlier than a sealed window's anchor would shift that anchor, claim a
    key nobody holds, and transcribe — and bill — every member of that window a second
    time. A late chunk forms its own window.

    `cap` is a safety net, not the rule. Nothing reaches it while chunks are 30 s; a device
    emitting much shorter ones would otherwise build one unbounded request.
    """
    consumed = consumed or set()
    ordered = sorted((i for i in members if i not in consumed),
                     key=lambda i: (members[i], i))
    out: list[list[int]] = []
    window: list[int] = []
    anchor = None
    for i in ordered:
        if anchor is not None and len(window) < cap:
            elapsed = (members[i] - anchor).total_seconds() \
                if hasattr(members[i], "timestamp") else members[i] - anchor
            if elapsed < window_sec:
                window.append(i)
                continue
        if window:
            out.append(window)
        window, anchor = [i], members[i]
    if window:
        out.append(window)
    return out


# ============================================================
# The filename contract
# ============================================================

def build_batch_name(first_member_stem: str, count: int, head_trim_sec: float,
                     duration_sec: float) -> str:
    """The batch object's filename.

        {device}_{date}_{HH-MM-SS}_sid{32hex}_c{NNNN}_bn{K}_off{T}_to{E}_srcwav.wav

    Everything before `_bn` is the FIRST member chunk's own stem, so every existing parser
    reads the batch as that chunk: the session id, the chunk index, the device and the base
    clock all come out right without any of them knowing batching happened.

    `_off{T}` carries the first chunk's measured head trim, so `compute_segment_base_time`
    — which is what the rest of the pipeline uses — lands on the true first sample rather
    than on the chunk's nominal start.
    """
    end = round(head_trim_sec + duration_sec, 3)
    return (f"{first_member_stem}_bn{count}"
            f"_off{round(head_trim_sec, 3)}_to{end}_srcwav.wav")


def is_batch_key(key: str) -> bool:
    """Is this object a batch rather than one chunk's audio?

    Anchored to `_sid{32hex}_c{N}_bn{K}` rather than to `_bn` alone: a device named
    `x_bn2_y` would otherwise be read as a batch, and the cost of that mistake is two
    minutes of audio never transcribed — with no error anywhere, the session just reads
    quieter.
    """
    return bool(key) and _BATCH_RE.search(key) is not None


# ============================================================
# The map — the only sanctioned way to turn a batch offset into a real time
# ============================================================

def map_key_for_audio(batch_audio_key: str) -> str:
    """Where the map for this batch WAV lives: beside the audio that produced it.

    One function so the writer and the reader cannot drift apart. They already did:
    the seal wrote the map under `audio_segments/` while the extractor looked for it
    under `transcripts/`, so every batched session in test fell back to filename
    arithmetic — and the tests agreed with the reader instead of with the writer,
    which is why nothing failed.
    """
    return f"{batch_audio_key.rsplit('.', 1)[0]}_batch_map.json"


def map_key_for_transcript(transcript_key: str, audio_prefix: str = 'audio_segments'):
    """The map belonging to a batched transcript, or None if the key is not shaped like one.

    A transcript key is `{transcripts}/{user}/{date}/{stem}.json` and its audio is
    `{audio_prefix}/{user}/{date}/{stem}.wav` — the same swap `batch_seal.raw_key_for`
    makes to reach the device's own upload.
    """
    parts = transcript_key.split('/')
    if len(parts) < 4:
        return None
    stem = parts[-1].rsplit('.', 1)[0]
    return f"{audio_prefix}/{'/'.join(parts[1:-1])}/{stem}_batch_map.json"


def member(chunk_index: int, chunk_key: str, abs_start: str, trimmed_head_sec: float,
           kept_duration_sec: float, trim_measured: bool = True,
           seam: str = "adjacent") -> dict:
    """One member of a batch. `abs_start` is the chunk's own filename clock, ISO-8601."""
    return {
        "chunk_index": chunk_index,
        "chunk_key": chunk_key,
        "abs_start": abs_start,
        "trimmed_head_sec": round(float(trimmed_head_sec), 6),
        "kept_duration_sec": round(float(kept_duration_sec), 6),
        # False means the seam measured zero unexpectedly: the audio was kept whole and this
        # says so, rather than leaving a 0.0 that reads as "there was no overlap".
        "trim_measured": bool(trim_measured),
        # "first" | "adjacent" | "gap". A gap seam bridges a chunk VAD dropped: those two
        # were never adjacent, so zero overlap is the CORRECT answer and must be kept out of
        # the unmeasured-seam alarm, which exists to catch the byte comparison breaking.
        "seam": seam,
    }


def build_map(session_id: str, members: list[dict], sealed_by: str,
              created_at: str | None = None) -> dict:
    """The sidecar written next to the batch audio.

    `batch_offset_sec` is filled in here rather than by the caller: it is the running sum of
    the kept durations, and a caller computing it independently is a second implementation
    of the one piece of arithmetic that must not drift.

    `sealed_by` is kept because `gap` and `session_close` mean different things when someone
    later asks why a transcript looks short.
    """
    out, offset = [], 0.0
    for m in members:
        entry = dict(m)
        entry["batch_offset_sec"] = round(offset, 6)
        offset += float(m["kept_duration_sec"])
        out.append(entry)
    doc = {"schema": 1, "session_id": session_id, "members": out, "sealed_by": sealed_by}
    if created_at is not None:
        doc["created_at"] = created_at
    return doc


def _parse_abs_start(value):
    """ISO-8601 from the map, or the chunk filename it came from.

    Both are accepted because the map is written from a filename and read back as ISO, and
    a caller that hands over the filename it already has must not silently get None — a
    None here is a word with no timestamp, which downstream reads as a word with no time
    rather than as an error.
    """
    if not value:
        return None
    try:
        from datetime import datetime
        return datetime.fromisoformat(value)
    except (TypeError, ValueError):
        return extract_base_time_from_filename(value)


def resolve_abs_time(batch_map: dict, t_sec: float):
    """Absolute time of second `t_sec` of the concatenated audio.

    The one sanctioned rule, and it is never re-derived elsewhere: find the last member
    whose `batch_offset_sec` is at or before `t_sec`, then

        absolute = abs_start + trimmed_head_sec + (t − batch_offset_sec)

    A `t` past the end clamps to the final member. Providers do return a word fractionally
    beyond the declared duration, and extrapolating would place it inside a chunk that does
    not exist.
    """
    members = batch_map.get("members") or []
    if not members:
        return None
    chosen = members[0]
    for m in members:
        if m["batch_offset_sec"] <= t_sec:
            chosen = m
        else:
            break
    base = _parse_abs_start(chosen["abs_start"])
    if base is None:
        return None
    within = t_sec - chosen["batch_offset_sec"]
    within = max(0.0, min(within, float(chosen["kept_duration_sec"])))
    return base + timedelta(seconds=chosen["trimmed_head_sec"] + within)


EMBEDDED_MAP_KEY = "fieldsight_batch_map"


def rebase_turns_from_embedded_map(normalized, transcript_data):
    """Re-time a batched transcript's turns using the map carried inside it.

    A batch's `word.start` counts from the first sample of the concatenated file — an origin
    that appears in no filename. Filename arithmetic gets the batch's start right and then
    drifts by the trimmed overlap at every seam, and since 2026-08-13 by the whole of any
    VAD-dropped chunk the window bridges. A second or two used to be invisible; thirty is not.

    **No S3 client, by design.** Four consumers need this, and the alternative — each one
    fetching the sidecar — is one new `audio_segments/*` grant per reader behind one
    `except`-and-log. Three missing grants have already broken this feature silently. The map
    travels inside the object every reader already holds, so a reader cannot lose one.

    Absent key: return `normalized` untouched. Per-chunk transcripts have no map and must
    cost nothing, and a batch transcript written before this change still has its sidecar for
    the one consumer that fetches it.

    `start_sec`/`end_sec` stay batch-relative on purpose: they are the in-file offsets the
    evidence and playback paths seek `source_filename` with, and that file is the batch WAV.
    """
    doc = (transcript_data or {}).get(EMBEDDED_MAP_KEY)
    if not doc or not (doc.get("members") if isinstance(doc, dict) else None):
        return normalized
    turns = (normalized or {}).get("speaker_turns") or []
    crossings = 0
    for turn in turns:
        if turn.get("abs_start") is None:
            continue                       # no time to re-base; do not invent one
        start = resolve_abs_time(doc, turn.get("start_sec") or 0.0)
        if start is None:
            continue
        turn["abs_start"] = start
        turn["abs_start_str"] = start.strftime("%H:%M:%S")
        end = resolve_abs_time(doc, turn.get("end_sec") or 0.0)
        if end is not None:
            turn["abs_end"] = end
            turn["abs_end_str"] = end.strftime("%H:%M:%S")
        turn["crosses_gap"] = spans_a_gap(doc, turn.get("start_sec") or 0.0,
                                          turn.get("end_sec") or 0.0)
        crossings += 1 if turn["crosses_gap"] else 0
    # Logged unconditionally, zero included. A guard that speaks only on failure cannot be
    # told apart from a guard that was never reached — which is how three missing IAM grants
    # each looked like nothing at all.
    _log.info("batch: rebased %d turns through the embedded map, batch_splice_turns=%d",
              len(turns), crossings)
    return normalized


def spans_a_gap(batch_map: dict, start_sec: float, end_sec: float) -> bool:
    """True when a turn runs across a member boundary that is discontinuous in time.

    A bridged gap splices audio that was never adjacent, and the provider cannot know that:
    it can emit one turn whose text runs across the join, fusing utterances up to a window
    apart into a single interval that covers time when nobody spoke. Each end of that turn
    is individually correct after rebasing, which is exactly why nothing downstream notices.
    Photo binding and claim provenance both consume the interval.

    Only DISCONTINUOUS boundaries count. Every batch has boundaries; flagging them all would
    make the signal mean "this is a batch" and bury the cases that matter.
    """
    members = batch_map.get("members") or []
    for prev, nxt in zip(members, members[1:]):
        boundary = float(nxt["batch_offset_sec"])
        if not (start_sec < boundary < end_sec):
            continue
        a, b = _parse_abs_start(prev["abs_start"]), _parse_abs_start(nxt["abs_start"])
        if a is None or b is None:
            continue
        expected_end = a + timedelta(seconds=float(prev["trimmed_head_sec"])
                                     + float(prev["kept_duration_sec"]))
        actual_start = b + timedelta(seconds=float(nxt["trimmed_head_sec"]))
        # A second of slack. A seam trim is measured to the sample and a real gap is a whole
        # dropped chunk, so nothing legitimate lands between those two scales.
        if (actual_start - expected_end).total_seconds() > 1.0:
            return True
    return False


# ============================================================
# Concatenation
# ============================================================

def concat_wavs(pcm_and_rates, trims_sec) -> tuple[bytes, int]:
    """Join raw PCM payloads, dropping `trims_sec[i]` from the head of each.

    Refuses to mix sample rates rather than resampling: resampling is a quality change, and
    hiding one inside a function whose job is to join bytes is how it would never be found.

    Refuses a trim longer than its chunk. That means the measurement and the audio disagree,
    and silently emptying the chunk would delete a whole segment of speech.
    """
    if not pcm_and_rates:
        return b"", 0
    rate = pcm_and_rates[0][1]
    parts = []
    for (pcm, r), trim in zip(pcm_and_rates, trims_sec):
        if r != rate:
            raise ValueError(f"cannot join {r} Hz audio to {rate} Hz — resample deliberately")
        drop = int(round(trim * rate)) * BYTES_PER_SAMPLE
        if drop > len(pcm):
            raise ValueError(
                f"trim of {trim}s exceeds the chunk ({len(pcm) / (rate * BYTES_PER_SAMPLE):.3f}s)")
        parts.append(pcm[drop:])
    return b"".join(parts), rate
