"""chunk_stitch — assemble a recording session from its overlapping audio chunks
(mobile chunk-session contract). The device (GrandTime) uploads ~30s audio chunks
with a ~2s tail overlap (PcmRingBuffer: "a sentence crossing a boundary appears
whole in both"), keyed `{device}_{date}_{HH-MM-SS}_sid{32hex}_c{NNNN}.{ext}`. The
overlap amount is NOT carried on the wire, so the backend dedups by CONTENT.

Pure module (no boto3/psycopg/env)."""
import chunk_stitch as cs

SID = "a1b2c3d4e5f60718293a4b5c6d7e8f90"


def W(*texts):
    return [{"text": t} for t in texts]


# ---- parse_chunk_key -----------------------------------------------------

def test_parse_chunk_key_extracts_sid_and_zero_based_index():
    k = f"users/Ada_L/audio/2026-07-25/Benl1_2026-07-25_13-00-11_sid{SID}_c0003.wav"
    assert cs.parse_chunk_key(k) == (SID, 3)
    k0 = f"transcripts/Ada_L/2026-07-25/Benl1_2026-07-25_13-00-11_sid{SID}_c0000.json"
    assert cs.parse_chunk_key(k0) == (SID, 0)


def test_parse_chunk_key_none_for_non_chunk_keys():
    assert cs.parse_chunk_key("extractions/Ada_L/2026-07-25/Benl1_2026-07-25_13-00-11.json") is None
    assert cs.parse_chunk_key("users/Ada_L/audio/2026-07-25/Benl1_2026-07-25_13-00-11.wav") is None
    assert cs.parse_chunk_key("") is None
    assert cs.parse_chunk_key(None) is None


# ---- dedup_overlap -------------------------------------------------------

def test_dedup_drops_the_duplicated_boundary_words():
    tail = W("pour", "the", "slab", "before", "lunch")
    head = W("before", "lunch", "then", "check", "rebar")
    assert cs.dedup_overlap(tail, head) == W("then", "check", "rebar")


def test_dedup_no_overlap_returns_head_unchanged():
    assert cs.dedup_overlap(W("a", "b", "c"), W("x", "y", "z")) == W("x", "y", "z")


def test_dedup_is_case_and_punctuation_insensitive():
    tail = W("Slab", "poured.")
    head = W("slab", "Poured", "then", "cured")
    assert cs.dedup_overlap(tail, head) == W("then", "cured")


def test_dedup_prefers_the_longest_boundary_match():
    tail = W("the", "the", "slab")
    head = W("the", "slab", "cracked")
    assert cs.dedup_overlap(tail, head) == W("cracked")   # k=2 ("the slab"), not k=1


def test_dedup_preserves_the_kept_side_word_objects_times():
    tail = [{"text": "slab", "start_time": 28.0}]
    head = [{"text": "slab", "start_time": 0.1}, {"text": "cured", "start_time": 0.6}]
    out = cs.dedup_overlap(tail, head)
    assert out == [{"text": "cured", "start_time": 0.6}]   # dup dropped, survivor keeps its time


# ---- stitch_chunks -------------------------------------------------------

def test_stitch_orders_by_index_and_dedups_adjacent_overlaps():
    c0 = (0, W("morning", "check", "the", "slab"))
    c1 = (1, W("the", "slab", "needs", "rebar"))
    c2 = (2, W("needs", "rebar", "by", "friday"))
    out = cs.stitch_chunks([c2, c0, c1])   # deliberately unordered
    assert [w["text"] for w in out] == \
        ["morning", "check", "the", "slab", "needs", "rebar", "by", "friday"]


def test_stitch_single_chunk_passthrough():
    assert cs.stitch_chunks([(0, W("just", "one"))]) == W("just", "one")


def test_dedup_works_on_transcript_utils_word_shape():
    # transcript_utils.parse_transcript words use key 'word' (not 'text') + times.
    tail = [{"word": "slab", "start_time": 28.0, "end_time": 28.4, "speaker": "spk_0"}]
    head = [{"word": "Slab", "start_time": 0.1}, {"word": "cured", "start_time": 0.6}]
    out = cs.dedup_overlap(tail, head)
    assert out == [{"word": "cured", "start_time": 0.6}]   # dup dropped, survivor kept whole


def test_stitch_tolerates_empty_and_none():
    assert cs.stitch_chunks([]) == []
    assert cs.stitch_chunks(None) == []
    assert cs.stitch_chunks([(0, None), (1, W("hi"))]) == W("hi")


# ---- dedup_overlap: the seam, and what it can and cannot recover ---------
#
# The overlap is the SAME ~2s of audio transcribed twice, and the engine does not
# return the same words both times. Measured on prod session
# sidfb57faf959ed40d68ca8b02797605a20 (2026-08-08).


def test_a_misheard_word_at_the_seam_leaves_the_duplicate_in_place():
    """Documented limitation, not an aspiration. c0003 ended "…back around the
    gully" and c0004 began "Back around the galley.", so no exact run exists and
    the duplicate survives into the extraction prompt.

    A fuzzy variant was written and measured against this session, then REMOVED:
    the engine also glues a boundary word to the next sentence ("galley。12"), so
    the fuzzy run dropped that whole token and took the batch number with it,
    turning "Batch 12 bricks" into "that batch of bricks". Losing a number is
    worse than carrying a fragment. The real fix is timestamp-based and needs
    word-level times on a turn, which speaker_turns does not carry today.
    """
    tail = W("cut", "the", "slab", "back", "around", "the", "gully")
    head = W("Back", "around", "the", "galley.", "12", "bricks", "south")
    assert cs.dedup_overlap(tail, head) == head


def test_exact_match_still_wins_and_is_unchanged():
    tail = W("booked", "for", "Monday", "we", "push", "it", "to", "Wednesday")
    head = W("push", "it", "to", "Wednesday.", "next", "topic")
    assert cs.dedup_overlap(tail, head) == W("next", "topic")


def test_bare_punctuation_is_never_a_match():
    """A token that normalises to nothing carries no evidence of a repeat. Letting
    emptiness match is exactly how the CJK defect below did its damage."""
    assert cs.dedup_overlap(W("slab", "."), W(".", "pour", "monday")) == W(".", "pour", "monday")


def test_unrelated_chinese_is_not_a_boundary_repeat():
    """The defect these tests were written for, and it was silent data loss.

    `_norm` erased every non-ASCII character to the empty string, so any two
    Chinese runs compared EQUAL and the longest-first loop deleted up to
    `max_window` characters from the head of every chunk. Measured 2026-08-08:
    these two unrelated sentences lost 7 of the head's 11 characters, at every
    seam, on every mixed-language session, with nothing logged.
    """
    tail = [{"text": c} for c in "今天天气不错啊"]
    head = [{"text": c} for c in "钢筋合格证还没到今天再催"]
    assert cs.dedup_overlap(tail, head) == head


def test_a_real_chinese_repeat_is_still_removed():
    """Fixing the above must not stop it deduping when the repeat is genuine."""
    tail = [{"text": c} for c in "剩下那一段要等电气"]
    head = [{"text": c} for c in "要等电气套管预埋完"]
    assert cs.dedup_overlap(tail, head) == [{"text": c} for c in "套管预埋完"]
