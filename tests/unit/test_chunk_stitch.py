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


# ---- _overlap_len (shared by dedup + speaker chaining) -------------------

def test_overlap_len_returns_matched_run_length():
    assert cs._overlap_len(W("a", "b", "c"), W("b", "c", "d")) == 2
    assert cs._overlap_len(W("a", "b"), W("x", "y")) == 0


# ---- plan_blocks (group chunks into overlapping blocks) ------------------

def test_plan_blocks_overlaps_by_one_chunk():
    assert cs.plan_blocks(range(10), block_size=4, overlap=1) == \
        [[0, 1, 2, 3], [3, 4, 5, 6], [6, 7, 8, 9]]


def test_plan_blocks_groups_present_indices_across_gaps():
    # VAD skipped 3,4,7 — blocks group the PRESENT indices, not the integer range.
    assert cs.plan_blocks([0, 1, 2, 5, 6, 8, 9], block_size=3, overlap=1) == \
        [[0, 1, 2], [2, 5, 6], [6, 8, 9]]


def test_plan_blocks_single_block_when_fewer_than_block_size():
    assert cs.plan_blocks([0, 1], block_size=4, overlap=1) == [[0, 1]]
    assert cs.plan_blocks([5], block_size=4, overlap=1) == [[5]]


def test_plan_blocks_overlap_zero_is_disjoint():
    assert cs.plan_blocks(range(8), block_size=4, overlap=0) == [[0, 1, 2, 3], [4, 5, 6, 7]]


def test_plan_blocks_normalises_unordered_and_duplicate_indices():
    assert cs.plan_blocks([3, 0, 1, 0, 2], block_size=2, overlap=1) == \
        [[0, 1], [1, 2], [2, 3]]


def test_plan_blocks_clamps_overlap_below_block_size():
    # overlap >= block_size would never advance; clamp to block_size-1.
    assert cs.plan_blocks(range(5), block_size=2, overlap=5) == \
        [[0, 1], [1, 2], [2, 3], [3, 4]]


def test_plan_blocks_empty():
    assert cs.plan_blocks([]) == []
    assert cs.plan_blocks(None) == []


# ---- stitch_blocks (whole-chunk overlap between blocks) ------------------

def test_stitch_blocks_dedups_a_whole_chunk_overlap():
    b0 = W("a", "b", "c", "d", "e", "f")
    b1 = W("d", "e", "f", "g", "h")          # shares d,e,f
    assert [w["text"] for w in cs.stitch_blocks([b0, b1])] == \
        ["a", "b", "c", "d", "e", "f", "g", "h"]


def test_stitch_blocks_tolerates_empty_and_none():
    assert cs.stitch_blocks([]) == []
    assert cs.stitch_blocks(None) == []
    assert cs.stitch_blocks([None, W("hi")]) == W("hi")


# ---- chain_speakers (global speaker id across blocks) --------------------

def test_chain_speakers_maps_labels_via_overlap():
    # A's tail overlaps B's head (same words x y z). In A that speaker is spk_0; in B the
    # SAME audio is labelled spk_1 (labels are local per job). Chaining must give both the
    # same global id, and B's other, non-overlap speaker a distinct one.
    A = [{"text": "hello", "speaker": "spk_0"},
         {"text": "x", "speaker": "spk_0"}, {"text": "y", "speaker": "spk_0"},
         {"text": "z", "speaker": "spk_0"}]
    B = [{"text": "x", "speaker": "spk_1"}, {"text": "y", "speaker": "spk_1"},
         {"text": "z", "speaker": "spk_1"}, {"text": "bye", "speaker": "spk_0"}]
    cs.chain_speakers([A, B])
    assert A[0]["global_speaker"] == B[0]["global_speaker"]     # same real person
    assert B[3]["global_speaker"] != B[0]["global_speaker"]     # "bye" is someone else


def test_chain_speakers_does_not_merge_same_local_label_of_a_different_person():
    # spk_0 appears in BOTH blocks, but the overlap proves B's spk_0 is A's spk_1 — a
    # different person than A's spk_0. Naive "same label == same person" would misattribute.
    A = [{"text": "a", "speaker": "spk_0"},
         {"text": "m", "speaker": "spk_1"}, {"text": "n", "speaker": "spk_1"}]
    B = [{"text": "m", "speaker": "spk_0"}, {"text": "n", "speaker": "spk_0"},
         {"text": "b", "speaker": "spk_0"}]
    cs.chain_speakers([A, B])
    assert B[0]["global_speaker"] == A[1]["global_speaker"]     # chains to A's spk_1
    assert B[0]["global_speaker"] != A[0]["global_speaker"]     # not A's spk_0


def test_chain_speakers_no_overlap_gives_fresh_ids():
    A = [{"text": "a", "speaker": "spk_0"}]
    B = [{"text": "b", "speaker": "spk_0"}]      # no shared words -> cannot chain
    cs.chain_speakers([A, B])
    assert A[0]["global_speaker"] != B[0]["global_speaker"]


def test_chain_speakers_single_block_and_missing_labels():
    A = [{"text": "a", "speaker": "spk_0"}, {"text": "b", "speaker": "spk_1"},
         {"text": "c"}]
    cs.chain_speakers([A])
    assert A[0]["global_speaker"] == "speaker_0"
    assert A[1]["global_speaker"] == "speaker_1"
    assert A[2]["global_speaker"] is None        # a word with no local label


def test_chain_speakers_chains_across_three_blocks():
    A = [{"text": "p", "speaker": "spk_0"}, {"text": "q", "speaker": "spk_0"}]
    B = [{"text": "q", "speaker": "spk_5"}, {"text": "r", "speaker": "spk_5"}]   # q ~ A
    C = [{"text": "r", "speaker": "spk_2"}, {"text": "s", "speaker": "spk_2"}]   # r ~ B
    cs.chain_speakers([A, B, C])
    assert A[0]["global_speaker"] == B[0]["global_speaker"] == C[0]["global_speaker"]


def test_chain_speakers_then_stitch_keeps_global_id_on_survivors():
    # the intended pipeline order: chain (needs the overlap) THEN stitch (drops it).
    A = [{"text": "x", "speaker": "spk_0"}, {"text": "y", "speaker": "spk_0"}]
    B = [{"text": "y", "speaker": "spk_3"}, {"text": "z", "speaker": "spk_3"}]
    chained = cs.chain_speakers([A, B])
    out = cs.stitch_blocks(chained)
    assert [w["text"] for w in out] == ["x", "y", "z"]
    assert len({w["global_speaker"] for w in out}) == 1     # one speaker throughout
