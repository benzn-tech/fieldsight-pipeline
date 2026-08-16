"""Every payload the embedder can send, replayed into the writer.

Three separate defects tonight had one shape: the embedder and the writer are two halves of
one contract that nobody had written down, each half's tests exercised its own side, and both
stayed green while the seam between them was broken.

* the embedder built `label_map` and the writer read it — and the hop between them dropped
  the key, so label inheritance was unreachable code that reported success;
* the embedder began sending a refused enrolment as `{"voiceprint_id", "refused"}` and the
  writer crashed on it, so every refusal on TEST died and the outcome it existed to record
  was never recorded;
* the precedence table was keyed on a source string the writer had stopped using.

None of these is a logic error either side could have caught alone. They are all the same
question — *does what one half sends survive what the other half does with it* — and nothing
asked it.

So this asks it mechanically: drive the embedder over the paths that produce a payload,
capture every payload it actually hands to `invoke_writer`, and feed each one back through
the writer's own `lambda_handler`. The database is stubbed; what is under test is the shape
of the message, not what it stores.

Adding a field to one side and not the other is now a red test rather than a silent
production failure.
"""
import json

import numpy as np
import pytest

import lambda_speaker_embed as se
import lambda_voiceprint_writer as vw

CO = "11111111-1111-1111-1111-111111111111"
SID = "sid" + "c" * 32
SRC = f"x_{SID}_c0000.wav"
AUDIO_KEY = f"users/u/audio/2026-08-13/x_{SID}_c0000.wav"


class FakeS3:
    def __init__(self, objects):
        self.objects = objects

    def get_object(self, Bucket, Key):
        import io as _io
        return {"Body": _io.BytesIO(self.objects[Key])}


def _wav(seconds=40.0, sr=16000, silent=False):
    import io as _io
    import wave
    n = int(seconds * sr)
    if silent:
        samples = np.zeros(n, dtype="<i2")
    else:
        t = np.arange(n, dtype=np.float64) / sr
        samples = (np.sin(2 * np.pi * 220.0 * t) * 8000).astype("<i2")
    buf = _io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(sr)
        w.writeframes(samples.tobytes())
    return buf.getvalue()


def _turns(n=3):
    return [{"source_filename": SRC, "start_sec": float(i * 12),
             "end_sec": float(i * 12 + 11), "speaker_label": "spk_0"} for i in range(n)]


def _artifact(**over):
    base = {"request_id": "r1", "session_base": SID, "company_id": CO,
            "user_folder": "u", "date": "2026-08-13",
            "correction": {"source_filename": SRC, "start_sec": 0.0, "end_sec": 11.0,
                           "display_name": "Ben L"},
            "turns": _turns(),
            "label_map": [{"turn_ref": f"{SRC}@{t['start_sec']}",
                           "source_filename": SRC, "speaker_label": "spk_0"}
                          for t in _turns()]}
    base.update(over)
    return base


@pytest.fixture
def captured(monkeypatch):
    """Runs the embedder for real over its S3 entry point, recording every writer payload."""
    sent = []
    # TWO profiles, not one. A single-profile pool names nobody by design — there is no
    # runner-up to beat — so a one-profile stub would make the match path produce no payload
    # at all and this file would assert on an empty list.
    pool = [{"person_key": "vp-1", "display_name": "Ben L", "status": "confirmed",
             "embedding": [1.0] * 192},
            {"person_key": "vp-2", "display_name": "Zoe", "status": "confirmed",
             "embedding": [1.0] + [-1.0] * 191}]
    monkeypatch.setattr(se, "invoke_writer", lambda p: sent.append(p) or
                        ({"profiles": pool} if p.get("op") == "profiles"
                         else {"written": 0}))
    monkeypatch.setattr(se, "embed_audio",
                        lambda audio, sr: np.ones(192, dtype=np.float32))
    return sent


def _run_embedder(monkeypatch, artifact, audio=None):
    key = "voiceprint_requests/c/s/r1.json"
    se._AUDIO_CACHE.clear()
    monkeypatch.setattr(se, "s3", lambda: FakeS3({
        key: json.dumps(artifact).encode(),
        AUDIO_KEY: audio if audio is not None else _wav()}))
    se.lambda_handler({"Records": [{"s3": {"bucket": {"name": "b"},
                                           "object": {"key": key}}}]}, None)


@pytest.fixture
def writer_db(monkeypatch):
    """The writer with its database replaced. What is under test is whether it can READ the
    message, not what it stores."""
    class Conn:
        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

    calls = {"turns": [], "samples": [], "attempts": []}
    monkeypatch.setattr(vw, "get_connection", lambda: Conn())
    monkeypatch.setattr(vw, "record_turn_name",
                        lambda conn, co, **kw: calls["turns"].append(kw) or {"id": "t"})
    monkeypatch.setattr(vw, "add_sample",
                        lambda conn, co, *a, **kw: calls["samples"].append(kw) or {"id": "s"})
    monkeypatch.setattr(vw, "record_attempt",
                        lambda conn, co, vp, outcome, detail=None:
                        calls["attempts"].append(outcome))
    monkeypatch.setattr(vw, "live_turn_names", lambda conn, co, sb: [])
    monkeypatch.setattr(vw, "rejected_names", lambda conn, co, sb: set())
    monkeypatch.setattr(vw, "profiles_for_matching", lambda conn, co, site_id=None: [])
    return calls


# --- the paths that produce a payload -------------------------------------


CASES = {
    "correction, no enrolment": dict(artifact={}, audio=None),
    "correction with enrolment": dict(
        artifact={"enrol": {"voiceprint_id": "vp-1"}}, audio=None),
    "correction whose enrolment is refused": dict(
        artifact={"enrol": {"voiceprint_id": "vp-1"}}, audio=_wav(silent=True)),
    "match run": dict(artifact={"op": "match", "mode": "on", "site_id": "st-1"},
                      audio=None),
}


@pytest.mark.parametrize("name", sorted(CASES))
def test_every_payload_the_embedder_sends_is_one_the_writer_understands(
        name, captured, writer_db, monkeypatch):
    """The seam, asked directly. Three defects tonight lived here and none of them was
    visible from either side alone."""
    case = CASES[name]
    _run_embedder(monkeypatch, _artifact(**case["artifact"]), case["audio"])
    assert captured, f"{name}: the embedder sent nothing, so this proves nothing"
    for payload in captured:
        # Raises on an unknown op, a missing key, or a shape the writer cannot read — which
        # is exactly what happened in production, twice, while every unit test was green.
        vw.lambda_handler(payload, None)


@pytest.mark.parametrize("name", sorted(CASES))
def test_nothing_the_artifact_carries_is_dropped_at_the_hop(name, captured, monkeypatch):
    """The embedder is a middle hop, and a middle hop that does not speak a key silently
    removes the feature that key exists for.

    `label_map` was built by org-api and read by the writer, and this hop did not forward it:
    label inheritance was unreachable code that reported success, and each end's tests passed
    because each end worked.

    Asserted on the KEY surviving rather than on any behaviour it produces — a dropped key
    raises nothing and changes no result that a stub can see."""
    case = CASES[name]
    art = _artifact(**case["artifact"])
    _run_embedder(monkeypatch, art, case["audio"])
    forwarded = [p for p in captured if p.get("op") in ("propagation", "match_names")]
    assert forwarded, f"{name}: nothing was forwarded"
    for key in ("label_map",):
        if art.get(key) is None:
            continue
        assert any(p.get(key) is not None for p in forwarded), (
            f"{name}: the artifact carried {key!r} and no payload did")


def test_a_refused_enrolment_never_carries_the_vector_it_was_refused_for(captured,
                                                                        monkeypatch):
    """Keyed on the CASE — silent audio, which the guard must refuse — not on a field of the
    payload. A first version asked `if enrol.get("refused")`, so a change that dropped that
    field skipped the assertion entirely: the guard hung on the thing it was checking."""
    _run_embedder(monkeypatch,
                  _artifact(enrol={"voiceprint_id": "vp-1"}), _wav(silent=True))
    assert captured
    for payload in captured:
        blob = json.dumps(payload)
        assert "embedding" not in blob, (
            "a window the guard refused had its vector sent anyway — biometric data on a "
            "path not designed to hold it")
        assert (payload.get("enrol") or {}).get("refused"), (
            "the refusal reached the writer without a reason, so the profile stays empty "
            "and unexplained")


@pytest.mark.parametrize("name", sorted(CASES))
def test_every_harvested_sample_belongs_to_a_profile(name, captured, monkeypatch):
    case = CASES[name]
    _run_embedder(monkeypatch, _artifact(**case["artifact"]), case["audio"])
    for payload in captured:
        for h in payload.get("harvest") or []:
            assert h.get("voiceprint_id"), (
                f"{name}: a harvested sample belongs to no profile")


def test_the_writer_rejects_an_op_it_does_not_know(writer_db):
    """The counterpart to the contract test: a payload the writer cannot handle must RAISE,
    not be silently ignored. A quiet no-op here is how a dropped key looks like success."""
    with pytest.raises(ValueError):
        vw.lambda_handler({"op": "something_nobody_sends", "company_id": CO}, None)
