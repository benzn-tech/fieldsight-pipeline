"""Unit: the two pure helpers that shape a match request, neither of which had ever run.

A stub census over the speaker tests turned these up: `_split_for_budget` is monkeypatched or
bypassed everywhere and called for real nowhere, and `_label_map` likewise. That is the same
shape as this project's worst defect of the week — `invoke_writer` was stubbed twenty times
and exercised zero, while the whole match path did nothing in production.

Neither touches S3 or a database, so there was never a reason for the gap beyond nobody
looking. Both fail silently if they are wrong:

* `_split_for_budget` drops or reorders turns from a long session, and the only symptom is a
  meeting that comes back with fewer names than it should have;
* `_label_map` feeds label inheritance, which is the one tier whose answer for a turn depends
  on another turn. A malformed map makes inheritance quietly name nothing.
"""
import pytest

org = pytest.importorskip("lambda_org_api")
import turn_name_overlay  # noqa: E402


def _turn(start, end, src="a_c0000.wav", label="spk_0"):
    return {"source_filename": src, "start_sec": start, "end_sec": end,
            "speaker_label": label}


# ---- _split_for_budget --------------------------------------------------


def test_every_turn_survives_the_split_exactly_once_and_in_order():
    """The property that matters, and the one whose failure is invisible: a session comes
    back with fewer names than it should, which is indistinguishable from a quiet meeting."""
    turns = [_turn(i * 7.0, i * 7.0 + 6.0) for i in range(200)]
    runs = org._split_for_budget(turns)
    flat = [t for run in runs for t in run]
    assert flat == turns, "the split lost, duplicated or reordered turns"
    assert all(runs), "an empty run would become an invocation that does nothing"


def test_a_single_turn_longer_than_the_budget_gets_its_own_run():
    """Never splits a turn — the docstring's promise. A turn longer than the whole budget
    must still travel, or whole-chunk transcription's hundred-second turns vanish."""
    turns = [_turn(0.0, org.MATCH_SECONDS_PER_RUN * 3)]
    runs = org._split_for_budget(turns)
    assert runs == [turns]


def test_the_turn_count_cap_is_honoured():
    runs = org._split_for_budget([_turn(i, i + 0.5) for i in range(org.MATCH_TURNS_PER_RUN * 2 + 5)])
    assert all(len(r) <= org.MATCH_TURNS_PER_RUN for r in runs), \
        [len(r) for r in runs]


def test_turns_without_times_do_not_crash_or_vanish():
    """`end_sec`/`start_sec` are absent on nothing today, and a KeyError here would take out
    the whole request rather than the one turn."""
    turns = [{"source_filename": "a.wav"}, _turn(0.0, 5.0)]
    assert [t for run in org._split_for_budget(turns) for t in run] == turns


def test_no_turns_produces_no_runs():
    """Not one empty run: an empty artifact is an invocation that reads a model, fetches
    profiles and names nothing."""
    assert org._split_for_budget([]) == []


# ---- _label_map ---------------------------------------------------------


def test_the_map_keys_agree_with_what_the_overlay_looks_names_up_by():
    """Both sides derive `turn_ref` from the same function, and this asserts they still do.
    If they ever diverge, inheritance looks up keys nothing wrote and names nobody, with no
    error anywhere — the shape that made label inheritance unreachable code once already."""
    turns = [_turn(41.203, 55.0, src="Benl1_2026-04-29_sid00_c0000_srcwav.json")]
    entry = org._label_map(turns)[0]
    assert entry["turn_ref"] == turn_name_overlay.turn_ref(
        turns[0]["source_filename"], turns[0]["start_sec"])


def test_a_turn_with_no_speaker_label_maps_to_none_rather_than_being_dropped():
    """The writer groups on (source_filename, label) and skips entries with no label. Dropping
    them here instead would make the two halves disagree about how many turns exist."""
    entries = org._label_map([_turn(0.0, 5.0, label=None), _turn(6.0, 9.0)])
    assert len(entries) == 2
    assert entries[0]["speaker_label"] is None
    assert entries[1]["speaker_label"] == "spk_0"


def test_the_map_carries_no_audio_and_no_vectors():
    """It travels in an S3 artifact with a 7-day expiry and no other protection. The vector
    ban on that path is the defect this design has relocated four times."""
    entries = org._label_map([_turn(0.0, 5.0)])
    assert set(entries[0]) == {"turn_ref", "source_filename", "speaker_label"}


def test_source_filename_is_kept_because_the_label_alone_is_not_a_person():
    """Speaker labels are per transcription call, and batching merges namespaces on purpose,
    so two calls' `spk_0` are two different people. Grouping without the filename would merge
    them."""
    entries = org._label_map([_turn(0.0, 5.0, src="a.wav"), _turn(0.0, 5.0, src="b.wav")])
    assert {e["source_filename"] for e in entries} == {"a.wav", "b.wav"}


def test_no_turns_maps_to_no_entries():
    assert org._label_map([]) == []
    assert org._label_map(None) == []
