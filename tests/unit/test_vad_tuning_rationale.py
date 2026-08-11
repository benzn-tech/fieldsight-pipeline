"""The VAD defaults are a measurement result, not a guess. This test says so.

Every assertion here points at data in
docs/superpowers/specs/2026-08-10-vad-and-capture-measurements.md. If one of
them fails, the change may still be right — but it is contradicting a measured
result, and the person making it should know which one before they proceed.

The values were chosen by sweeping 45 combinations of threshold x merge_gap x
min_duration through the shipping Silero model and the shipping merge function,
scored against material with known answers: distant speech that must be found,
and walking silence that must not be. Exactly three combinations passed, and all
three are the ones below.
"""
import importlib
import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "src"))
os.environ.setdefault("AWS_DEFAULT_REGION", "ap-southeast-2")


@pytest.fixture()
def vad(monkeypatch):
    for k in ("VAD_THRESHOLD", "MERGE_GAP", "MIN_SPEECH_DURATION",
              "DROP_SILENT_CHUNKS", "TRANSCRIBE_WHOLE_CHUNK"):
        monkeypatch.delenv(k, raising=False)
    import lambda_vad
    return importlib.reload(lambda_vad)


def test_min_speech_duration_stays_at_one_second(vad):
    """0.5 s and 0.3 s both started emitting segments on walking silence, at
    every threshold tried. 1.0 s was the only value that separated distant
    speech from footsteps."""
    assert vad.MIN_SPEECH_DURATION == 1.0


def test_merge_gap_default(vad):
    """2.0, 3.0 and 5.0 all behaved identically at min_duration 1.0 — the gap is
    not what decides this, so it stays where it was rather than being changed
    for no measured reason."""
    assert vad.MERGE_GAP == 2.0


def test_the_retry_threshold_is_derived_not_constant(vad):
    """A hardcoded retry only looks permissive next to the default it was
    written against. When both environments moved to 0.2, the old constant 0.25
    became STRICTER than the primary pass, so the retry could never succeed and
    every uncertain chunk fell straight through it."""
    src = open(os.path.join(os.path.dirname(__file__), "..", "..", "src",
                            "lambda_vad.py"), encoding="utf-8").read()
    assert "VAD_THRESHOLD / 2" in src, "the retry must derive from the configured threshold"
    assert "threshold=0.25" not in src, "a constant retry threshold is the bug this replaced"


def test_whole_chunk_emits_the_whole_chunk_when_any_speech_is_found(vad):
    """This is why a mixed chunk needs no special handling: one nearby speaker
    opens the gate and the entire 30 s goes to ASR, distant voices included. The
    gap is only a chunk with NO nearby speech for its whole duration — much
    narrower than it looks."""
    assert vad.plan_emission([(3.0, 7.0)], 30.0, whole_chunk=True) == [(0.0, 30.0)]
    assert vad.plan_emission([], 30.0, whole_chunk=True) == []


def test_normalisation_runs_after_the_gate_not_before(vad):
    """Measured 2026-08-10: enhancing first lifts the 4 m and 6 m chunks from
    0.276/0.200 to 0.964/0.977 and they pass — and then transcribe as invented
    sentences, because loudness does not tell the VAD which seconds are speech.
    The gate says "speech everywhere", whole-chunk mode ships all 30 s of
    footsteps, and ASR turns them into fluent text.

    Missing data is recoverable. Fabricated data is not, and nothing downstream
    can tell them apart. If this ordering is ever changed, that is the result
    being contradicted."""
    src = open(os.path.join(os.path.dirname(__file__), "..", "..", "src",
                            "lambda_vad.py"), encoding="utf-8").read()
    # Anchor on the CALL sites, not the first textual occurrence -- both
    # functions are defined near the top of the module, so a plain index() finds
    # the definitions and compares nothing meaningful.
    vad_call = src.index("raw_segments = run_silero_vad(")
    norm_call = src.index("= normalise_for_asr(")
    assert vad_call < norm_call, "VAD must run before normalise_for_asr"


def test_drop_silent_chunks_default_is_documented_as_provider_specific(vad):
    """It defaults on because AWS Transcribe invented 10.7% of its words from
    silence. ElevenLabs does not — four stretches of real walking silence came
    back as [pause] / [clicking] / [background noise] with zero invented words.

    That does NOT make turning it off safe: EL fabricates happily on a raw 30 s
    chunk that is mostly non-speech (§3 of the spec). The distinction is "a clip
    that is mostly speech" versus "a chunk with a few seconds of distant speech
    in it", not "silence" versus "noise"."""
    assert vad.DROP_SILENT_CHUNKS is True
