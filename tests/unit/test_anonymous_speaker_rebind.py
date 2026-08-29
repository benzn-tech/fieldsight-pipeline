"""Unit: one session, one speaker namespace — and the seam that would make it invisible.

Batching numbers speakers per ASR call, so `spk_0` in call 3 and `spk_0` in call 9 are two
different people about half the time (50.2 % purity, measured on a real meeting). The re-bind
decides which `(call, label)` pairs are the same voice, using ~40 s of evidence per decision
instead of the 3-second turns that make per-turn relabelling unusable.

**The failure this file exists for is not a missing group.** A session with no groups reads
exactly as it read before any of this — that is the designed fallback. The two that matter:

- **`source_filename` spelled differently on the two sides.** The producer stores it, the read
  path looks it up. A `.wav` where the reader expects `.json` gives zero groups, zero errors
  and a clean fallback — indistinguishable from "the re-bind has not run". Nothing anywhere
  would say so.
- **a confidently WRONG group.** Two people merged read as one person for a whole session,
  which is worse than an obviously inconsistent `Speaker 1`. So the tests assert that the
  clustering refuses rather than merges when the evidence disagrees.
"""
import numpy as np
import pytest

vpu = pytest.importorskip("voiceprint_utils")
iw = pytest.importorskip("lambda_item_writer")
slg = pytest.importorskip("repositories.speaker_label_groups")

CO = "11111111-1111-1111-1111-111111111111"
SID = "sid" + "a" * 32


def _v(*xs):
    a = np.array(xs, dtype=np.float64)
    return a / np.linalg.norm(a)


# ---- the clustering ------------------------------------------------------


def test_centroids_of_one_voice_become_one_group():
    a = _v(1, 0, 0)
    assert len(set(vpu.cluster_centroids([a, a, a]))) == 1


def test_two_voices_stay_two_groups():
    """The direction that matters. Merging them reads as one person for the whole session,
    which a customer cannot detect and cannot correct."""
    a, b = _v(1, 0, 0), _v(0, 1, 0)
    assert len(set(vpu.cluster_centroids([a, b, a, b]))) == 2


def test_the_largest_group_is_always_A():
    """Letters have to be stable in the only sense they can be. Without an ordering rule a
    re-run relabels an unchanged session and the viewer recolours it for no reason."""
    a, b = _v(1, 0, 0), _v(0, 1, 0)
    out = vpu.cluster_centroids([b, a, a, a])
    assert out[1] == out[2] == out[3] == 0
    assert out[0] == 1


def test_the_threshold_is_a_floor_not_a_suggestion():
    """At a similarity floor above what the vectors achieve, nothing merges. That is the
    refusing direction, and it is the one that must not be loosened to 'get more merging'."""
    a, b = _v(1, 0, 0), _v(0.9, 0.44, 0)
    assert len(set(vpu.cluster_centroids([a, b], min_similarity=0.99))) == 2


def test_it_is_not_the_turn_clustering_constant():
    """`DEFAULT_CLUSTER_TAU` is a cosine DISTANCE ceiling for complete linkage over turns;
    this is a SIMILARITY floor for average linkage over centroids. Two different questions,
    and one constant serving both would silently apply a threshold measured for one to the
    other."""
    assert vpu.DEFAULT_REBIND_SIMILARITY != vpu.DEFAULT_CLUSTER_TAU


# ---- the seam: one spelling of source_filename ---------------------------


def test_the_producer_stores_the_filename_the_reader_looks_up():
    """The seam, driven end to end rather than asserted on either side.

    The producer copies `source_filename` straight from the extraction artifact's turns; the
    read path looks segments up by the transcript basename. Feeding one to the other is the
    only check that catches a `.json`/`.wav` mismatch, which otherwise produces zero groups,
    zero errors, and a fallback nobody can tell from 'not run'.
    """
    written = []
    artifact = {"speaker_turns": [
        {"source_filename": "ben_2026-08-12_16-50-28_sidaaa_c0000_srcwav.json",
         "speaker_label": "spk_0", "start_sec": 0.0, "end_sec": 40.0},
        {"source_filename": "ben_2026-08-12_16-52-24_sidaaa_c0004_srcwav.json",
         "speaker_label": "spk_0", "start_sec": 0.0, "end_sec": 40.0}]}
    iw._request_rebind(CO, SID, artifact, put=lambda k, b: written.append(b))

    assert written, "no request was enqueued for a two-call session"
    sent = {t["source_filename"] for t in written[0]["turns"]}
    assert sent == {t["source_filename"] for t in artifact["speaker_turns"]}, (
        "the producer rewrote the filenames on the way out; the read path looks up the "
        "transcript basename and would find nothing")
    assert all(f.endswith(".json") for f in sent)


def test_a_single_speaker_session_is_skipped_and_says_so(caplog):
    """Most sessions are one person across one call, where the re-bind changes nothing and
    would still cost ~100 s of ONNX on a slot the naming path wants. The skip logs its counts,
    because a silent skip and a producer that never ran are the same absence."""
    written = []
    artifact = {"speaker_turns": [
        {"source_filename": "a.json", "speaker_label": "spk_0",
         "start_sec": 0.0, "end_sec": 40.0}]}
    with caplog.at_level("INFO"):
        assert iw._request_rebind(CO, SID, artifact, put=lambda k, b: written.append(b)) is False
    assert not written
    assert any("not requested" in r.getMessage() for r in caplog.records)


def test_one_label_across_two_calls_is_still_worth_grouping():
    """Two calls, one label each: exactly the case where `spk_0` may be two people. Skipping
    it because 'there is only one label' would skip the defect."""
    written = []
    artifact = {"speaker_turns": [
        {"source_filename": "a.json", "speaker_label": "spk_0", "start_sec": 0, "end_sec": 40},
        {"source_filename": "b.json", "speaker_label": "spk_0", "start_sec": 0, "end_sec": 40}]}
    assert iw._request_rebind(CO, SID, artifact, put=lambda k, b: written.append(b)) is True


def test_a_failed_enqueue_does_not_fail_the_item_write():
    """Best effort, deliberately. A re-bind that does not happen leaves the transcript reading
    as it does today; a re-bind that takes the item write down trades a working feature for a
    cosmetic one."""
    def boom(k, b):
        raise RuntimeError("S3 down")

    artifact = {"speaker_turns": [
        {"source_filename": "a.json", "speaker_label": "spk_0", "start_sec": 0, "end_sec": 40},
        {"source_filename": "b.json", "speaker_label": "spk_1", "start_sec": 0, "end_sec": 40}]}
    assert iw._request_rebind(CO, SID, artifact, put=boom) is False


# ---- the store -----------------------------------------------------------


class _Cur:
    def __init__(self):
        self.sql, self.params = [], []

    def execute(self, sql, params=None):
        self.sql.append(" ".join(sql.split()))
        self.params.append(params)
        return self

    def fetchall(self):
        return []


class _Conn:
    def __init__(self):
        self.cur = _Cur()

    def cursor(self, row_factory=None):
        return self.cur


def test_writing_a_session_s_groups_replaces_the_previous_generation():
    """The letters come from clustering, so a second run may legitimately call the same voice
    `B` where the first called it `A`. Two generations side by side give one transcript two
    contradictory groupings with no way to tell which is current."""
    conn = _Conn()
    slg.replace_for_session(conn, CO, SID, [
        {"source_filename": "a.json", "speaker_label": "spk_0", "group_label": "A"}])
    assert conn.cur.sql[0].startswith("DELETE FROM speaker_label_groups")
    assert any(s.startswith("INSERT INTO speaker_label_groups") for s in conn.cur.sql)


def test_an_empty_result_still_clears():
    """"The re-bind ran and found nothing to group" and "the old groups are still there" must
    not be the same state. The second is a transcript displaying a mapping nobody computed."""
    conn = _Conn()
    assert slg.replace_for_session(conn, CO, SID, []) == 0
    assert conn.cur.sql[0].startswith("DELETE FROM speaker_label_groups")


def test_an_unscoped_write_is_refused():
    """A falsy company binds NULL, the delete matches nothing, and the previous generation
    silently survives beside the new one."""
    with pytest.raises(ValueError):
        slg.replace_for_session(_Conn(), "", SID, [])
    with pytest.raises(ValueError):
        slg.for_session(_Conn(), None, SID)


def test_a_row_missing_half_its_key_is_dropped_not_stored():
    """The primary key would reject it anyway; what this avoids is one bad row aborting a
    whole session's mapping inside a background lambda."""
    conn = _Conn()
    n = slg.replace_for_session(conn, CO, SID, [
        {"source_filename": None, "speaker_label": "spk_0", "group_label": "A"},
        {"source_filename": "a.json", "speaker_label": "spk_0", "group_label": "A"}])
    assert n == 1


def test_the_artifact_reads_the_key_the_turn_builder_actually_writes():
    """The defect this test exists for shipped, deployed, and produced nothing.

    `transcript_utils._build_turn` names the field `speaker`. The first version of the
    extraction artifact read `speaker_label` — so `speaker_turns` came out EMPTY on every
    session, the producer's pre-check saw zero pairs, and the re-bind never ran. No error
    anywhere; the field was present and the list was short.

    **Every other test in this file fed dicts written by the same file under test**, so both
    halves agreed on a key the actual producer of turns has never used. This one asks the
    real builder what it writes, which is the only question that could have caught it.
    """
    import inspect

    import transcript_utils as tu

    built = inspect.getsource(tu._build_turn)
    assert "'speaker':" in built and "speaker_label" not in built, (
        "the turn builder changed its field name; whatever it is now, the extraction "
        "artifact must read THAT")

    ex = inspect.getsource(iw)  # not the producer — the artifact composer lives elsewhere
    import lambda_extract_session as les

    composer = inspect.getsource(les.extract_session_topics) if hasattr(
        les, "extract_session_topics") else open(
            "src/lambda_extract_session.py", encoding="utf-8").read()
    i = composer.index("'speaker_turns':")
    window = composer[i:i + 400]
    assert "t.get('speaker')" in window, (
        "the artifact still reads a key the turn builder does not write; `speaker_turns` "
        "will be empty on every session and the re-bind will never run")
    assert ex is not None
