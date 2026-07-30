"""chunk_stitch.py — assemble a device recording session from its overlapping
audio chunks (mobile chunk-session contract).

The GrandTime device chops a recording into ~30s audio chunks and carries the
last ~2s of PCM from one chunk into the next (AudioSegmentation.PcmRingBuffer:
"a sentence crossing a boundary appears whole in both"), so every raw-media /
transcript basename is tokenized `{device}_{date}_{HH-MM-SS}_sid{32hex}_c{NNNN}`.
The overlap SIZE is deliberately NOT on the wire, so the backend dedups by
CONTENT: the tail of chunk N and the head of chunk N+1 transcribe the same audio,
so they share a run of words — find the longest such boundary run and drop the
duplicate from the incoming chunk.

This is the upstream session-assembly `session_scope` anticipates ("session
assembly moves UPSTREAM … SUPERSEDES the merge"): one press-record→stop becomes
ONE clean word stream feeding both the rolling Tier-1 summary and the Tier-2
final extraction.

Pure: no boto3 / psycopg / env — importable from any lambda.
"""
import re

# `_sid{32hex}_c{NNNN}` on a raw-media / transcript basename. NNNN is >=4 digits
# (zero-padded, zero-based); followed by a '.', '_', or end so `_c0012` never
# eats into a longer trailing token.
CHUNK_TOKENS_RE = re.compile(r"_sid([0-9a-f]{32})_c(\d{4,})(?:[._]|$)")

# How far back into the running stream to look for a boundary repeat. ~2s of
# speech is a handful of words; a generous window tolerates a bigger device
# overlap (capped at 10s upstream) without matching unrelated repeats far apart.
DEFAULT_MAX_WINDOW = 40


def parse_chunk_key(key):
    """(session_id, chunk_index) for a key carrying the mobile chunk tokens,
    else None. session_id = the 32-hex sid; chunk_index = int (zero-based)."""
    if not key:
        return None
    m = CHUNK_TOKENS_RE.search(key)
    if not m:
        return None
    return m.group(1), int(m.group(2))


def _norm(word):
    """Comparison key for one word: lowercased, alphanumerics only. Absorbs the
    trivial ASR differences at a boundary (case, trailing punctuation) so the
    same spoken word in two chunks matches."""
    text = word.get("text") if isinstance(word, dict) else str(word)
    return re.sub(r"[^0-9a-z]+", "", (text or "").lower())


def dedup_overlap(tail, head, max_window=DEFAULT_MAX_WINDOW):
    """Drop the boundary words at the start of `head` that repeat the end of
    `tail` (same audio, transcribed twice because of the device overlap). Returns
    a copy of `head` with the longest matching prefix removed; `head` unchanged
    when there is no boundary repeat (conservative — a little duplication beats
    dropping real speech). The KEPT words are `head`'s own objects, so their
    per-chunk timestamps survive for the caller to re-base."""
    tail = tail or []
    head = head or []
    lim = min(len(tail), len(head), max_window)
    for k in range(lim, 0, -1):
        if [_norm(w) for w in tail[-k:]] == [_norm(w) for w in head[:k]]:
            return list(head[k:])
    return list(head)


def stitch_chunks(chunks, max_window=DEFAULT_MAX_WINDOW):
    """Merge a session's per-chunk word lists into one stream. `chunks` is an
    iterable of (chunk_index, words); it is ordered by index here (never by
    arrival), adjacent overlaps are deduped, and the survivors concatenated. A
    None chunk or None word list is treated as empty."""
    ordered = sorted((c for c in (chunks or []) if c is not None),
                     key=lambda c: c[0])
    out = []
    for _, words in ordered:
        words = list(words or [])
        if not out:
            out = words
        else:
            out.extend(dedup_overlap(out, words, max_window))
    return out
