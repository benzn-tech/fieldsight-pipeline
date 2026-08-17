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


# ---- _session_turns -----------------------------------------------------
#
# Reads S3 through `_read_org_transcripts`, which is stubbed here — everything below is
# about what happens to the rows AFTER they arrive, which is where the recorded defect was.

SID_A = "Benl1_2026-04-29_08-14-32_sid" + "a" * 32 + "_c0000_srcwav.json"
SID_B = "Benl1_2026-04-29_09-20-00_sid" + "b" * 32 + "_c0000_srcwav.json"


def _payload(*segs):
    return {"speaker_segments": list(segs)}


def _seg(src, start, duration=5.0, label="spk_0"):
    return {"source_filename": src, "chunk_start": start, "duration": duration,
            "speaker_label": label}


def test_turns_from_another_session_at_the_same_offset_are_not_included(monkeypatch):
    """The defect this function's docstring records: a day holds several sessions and two
    routinely have turns at the same offset, so a turn from the wrong session looked like a
    matching failure. It cost two rounds of debugging and nothing pinned the fix."""
    monkeypatch.setattr(org, "_read_org_transcripts",
                        lambda *a, **k: _payload(_seg(SID_A, 10.0), _seg(SID_B, 10.0)))
    turns = org._session_turns("Benl1", "2026-04-29", SID_A)
    assert [t["source_filename"] for t in turns] == [SID_A]


def test_the_session_is_matched_as_a_session_not_as_a_string(monkeypatch):
    """Both sides go through `session_base`. Comparing a normalised filename against the raw
    URL parameter is how one of these ends up matching nothing — the two spellings of a
    session are not equal as strings, only as sessions."""
    monkeypatch.setattr(org, "_read_org_transcripts",
                        lambda *a, **k: _payload(_seg(SID_A, 3.0)))
    # The caller passes the session id, not the full filename.
    assert org._session_turns("Benl1", "2026-04-29",
                              turn_name_overlay.session_base(SID_A))


def test_a_turn_with_no_offset_is_dropped_rather_than_defaulted_to_zero(monkeypatch):
    """A missing `chunk_start` defaulted to 0.0 would cut audio from the top of the file and
    hand the embedder somebody else's voice under this turn's reference."""
    monkeypatch.setattr(org, "_read_org_transcripts", lambda *a, **k: _payload(
        {"source_filename": SID_A, "duration": 5.0}, _seg(SID_A, 7.0)))
    assert [t["start_sec"] for t in org._session_turns("Benl1", "2026-04-29", SID_A)] == [7.0]


def test_the_end_is_derived_from_the_duration(monkeypatch):
    monkeypatch.setattr(org, "_read_org_transcripts",
                        lambda *a, **k: _payload(_seg(SID_A, 12.0, duration=9.5)))
    t = org._session_turns("Benl1", "2026-04-29", SID_A)[0]
    assert (t["start_sec"], t["end_sec"]) == (12.0, 21.5)


def test_the_transcribers_own_label_is_carried_through(monkeypatch):
    """It is the whole input to label inheritance, and it says "these turns are one voice"
    about turns too short for any acoustic judgement."""
    monkeypatch.setattr(org, "_read_org_transcripts",
                        lambda *a, **k: _payload(_seg(SID_A, 1.0, label="spk_3")))
    assert org._session_turns("Benl1", "2026-04-29", SID_A)[0]["speaker_label"] == "spk_3"


def test_a_transcript_read_that_fails_yields_no_turns_rather_than_raising(monkeypatch):
    """A correction on a day whose transcripts cannot be read still names the one turn the
    user pointed at; it must not take the whole request down."""
    def _boom(*a, **k):
        raise RuntimeError("S3 said no")
    monkeypatch.setattr(org, "_read_org_transcripts", _boom)
    assert org._session_turns("Benl1", "2026-04-29", SID_A) == []


def test_a_session_id_that_normalises_to_nothing_matches_no_turns(monkeypatch):
    """A legacy RealPTT filename has no sid. `want` is then None, and `not want` must refuse
    everything — without that guard the comparison would be None == None and every turn in
    the day would be swept into one 'session'."""
    monkeypatch.setattr(org, "_read_org_transcripts",
                        lambda *a, **k: _payload(_seg("Benl1_2026-04-29_off0.5_srcwav.json", 4.0)))
    assert org._session_turns("Benl1", "2026-04-29",
                              "Benl1_2026-04-29_off0.5_srcwav.json") == []
