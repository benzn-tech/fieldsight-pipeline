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

# ── Block-level (multi-chunk) assembly ─────────────────────────────────────
# A BLOCK is several consecutive chunks joined into ONE transcription unit (design:
# docs/superpowers/specs/2026-07-31-multi-chunk-block-transcription-design.md). It
# replaces the per-VAD-segment jobs that fragment diarization + language-ID and cost
# more (15 s Transcribe minimum). ~2 min per block at 30 s chunks = 4 chunks; consecutive
# blocks SHARE one whole chunk so a sentence cut at a block boundary is whole in one of
# them, and the shared audio (transcribed twice) is reused to CHAIN local speaker labels
# into one global identity across the session.
DEFAULT_BLOCK_SIZE = 4
DEFAULT_BLOCK_OVERLAP = 1
# A ~30 s overlap chunk holds far more words than the ~2 s chunk overlap, so the boundary
# dedup / speaker-chaining search window is correspondingly larger.
DEFAULT_BLOCK_WINDOW = 256


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
    same spoken word in two chunks matches. Accepts both the transcript_utils
    word shape (`{'word': ...}`) and a plain `{'text': ...}`/string."""
    if isinstance(word, dict):
        text = word.get("word") or word.get("text") or ""
    else:
        text = str(word)
    return re.sub(r"[^0-9a-z]+", "", text.lower())


def _overlap_len(tail, head, max_window=DEFAULT_MAX_WINDOW):
    """Length k of the LONGEST run where the last k words of `tail` equal the first k
    words of `head` (normalised compare), else 0. This is the boundary repeat the device/
    block overlap creates — the same audio transcribed twice. Split out of dedup_overlap
    so speaker chaining can reuse the aligned overlap without dropping it."""
    tail = tail or []
    head = head or []
    lim = min(len(tail), len(head), max_window)
    for k in range(lim, 0, -1):
        if [_norm(w) for w in tail[-k:]] == [_norm(w) for w in head[:k]]:
            return k
    return 0


def dedup_overlap(tail, head, max_window=DEFAULT_MAX_WINDOW):
    """Drop the boundary words at the start of `head` that repeat the end of
    `tail` (same audio, transcribed twice because of the device overlap). Returns
    a copy of `head` with the longest matching prefix removed; `head` unchanged
    when there is no boundary repeat (conservative — a little duplication beats
    dropping real speech). The KEPT words are `head`'s own objects, so their
    per-chunk timestamps survive for the caller to re-base."""
    head = head or []
    k = _overlap_len(tail, head, max_window)
    return list(head[k:])


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


def plan_blocks(chunk_indices, block_size=DEFAULT_BLOCK_SIZE,
                overlap=DEFAULT_BLOCK_OVERLAP):
    """Group a session's PRESENT chunk indices into overlapping blocks. Each block is a
    list of chunk indices — a contiguous slice of the *present* indices, NOT of the integer
    range: VAD skips silent chunks, so indices legitimately have gaps and a block groups
    whatever survived. Consecutive blocks share `overlap` chunk(s), so a sentence cut at a
    block boundary lands whole in one block and the shared chunk anchors speaker chaining.

    block_size is clamped to >=1 and overlap to 0..block_size-1 (an overlap == block_size
    would never advance). Duplicate/unsorted indices are normalised. Returns [] for no
    input."""
    idxs = sorted({int(i) for i in (chunk_indices or [])})
    if not idxs:
        return []
    block_size = max(1, int(block_size))
    overlap = max(0, min(int(overlap), block_size - 1))
    step = block_size - overlap
    blocks = []
    i = 0
    n = len(idxs)
    while i < n:
        blocks.append(idxs[i:i + block_size])
        if i + block_size >= n:      # this block reached the tail — stop (avoids a
            break                    # dangling all-overlap block that adds nothing)
        i += step
    return blocks


def _spk(word):
    """The local speaker label on a word (`speaker` or Transcribe's `speaker_label`),
    else None. Local to ONE block's transcription job until chain_speakers globalises it."""
    if isinstance(word, dict):
        return word.get("speaker") or word.get("speaker_label")
    return None


def stitch_blocks(blocks, max_window=DEFAULT_BLOCK_WINDOW):
    """Merge per-BLOCK word streams into one, deduping the shared-chunk overlap between
    consecutive blocks. `blocks` is an ORDERED iterable of word lists (each already the
    transcription of one block; block order == session order). Same mechanism as
    stitch_chunks, but the overlap is a whole chunk so the default window is larger. Run
    chain_speakers FIRST if you want global speaker ids to survive onto the stitched
    stream (dedup keeps each survivor word object, global_speaker included)."""
    out = []
    for words in (blocks or []):
        words = list(words or [])
        if not out:
            out = words
        else:
            out.extend(dedup_overlap(out, words, max_window))
    return out


def chain_speakers(blocks, max_window=DEFAULT_BLOCK_WINDOW):
    """Assign a STABLE GLOBAL speaker id to every word across blocks, from the per-block
    LOCAL labels (`spk_0/1/…` are local to each block's job — the same person is a
    different label in a different block, which is the measured "one person judged as
    many speakers" defect).

    Consecutive blocks share an overlap chunk transcribed twice, so within the overlap the
    same real speaker appears under both blocks' local labels. Vote over the aligned
    overlap words to map block N+1's labels onto block N's already-global ids; chain the
    per-boundary maps across the session. A label with no confident overlap vote (speaks
    only outside the overlap, or the boundary has no detectable overlap) gets a fresh
    global id — honest divergence rather than a wrong merge.

    Mutates each dict word in place, adding `global_speaker` (str, or None when the word
    had no local label), and returns `blocks` (as lists). Pure otherwise."""
    from collections import Counter
    blocks = [list(b or []) for b in (blocks or [])]
    gid_of = {}                       # (block_index, local_label) -> global id
    counter = [0]

    def new_gid():
        g = "speaker_%d" % counter[0]
        counter[0] += 1
        return g

    for bi, words in enumerate(blocks):
        seen = []
        for w in words:
            lab = _spk(w)
            if lab is not None and lab not in seen:
                seen.append(lab)
        if bi == 0:
            for lab in seen:
                gid_of[(0, lab)] = new_gid()
        else:
            prev = blocks[bi - 1]
            k = _overlap_len(prev, words, max_window)
            votes = {}                # cur_local_label -> Counter(prev_local_label)
            base = len(prev) - k
            for j in range(k):
                a = _spk(prev[base + j])
                b = _spk(words[j])
                if a is not None and b is not None:
                    votes.setdefault(b, Counter())[a] += 1
            for lab in seen:
                mapped = None
                if votes.get(lab):
                    a_label = votes[lab].most_common(1)[0][0]
                    mapped = gid_of.get((bi - 1, a_label))
                gid_of[(bi, lab)] = mapped or new_gid()

    for bi, words in enumerate(blocks):
        for w in words:
            if isinstance(w, dict):
                lab = _spk(w)
                w["global_speaker"] = gid_of.get((bi, lab)) if lab is not None else None
    return blocks
