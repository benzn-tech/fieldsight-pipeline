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


def test_stitch_tolerates_empty_and_none():
    assert cs.stitch_chunks([]) == []
    assert cs.stitch_chunks(None) == []
    assert cs.stitch_chunks([(0, None), (1, W("hi"))]) == W("hi")
