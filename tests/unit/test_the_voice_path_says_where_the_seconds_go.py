"""Unit: the voice ask reports which stage owned the wait.

Before this, `_voice_answer` logged NOTHING on the success path. Every latency
claim about voice was therefore either inferred from the screen path -- a
different prompt, a different token budget, one fewer Lambda hop -- or read off
Lambda `Duration`, which is the sum and says nothing about which stage owns it.

That absence has already cost something. A design review asserted "STT finishes
at about 0.8s so we can speak a restatement before retrieval": there was no
measurement behind it, and there was no way to get one.

These tests deliberately assert NO NUMBERS. A test that pins a duration measures
the CI runner. What must not silently disappear is the line itself and its
fields, because the next person to argue about voice latency will be reading it.
"""
import logging

import pytest

aa = pytest.importorskip(
    "lambda_ask_agent",
    reason="requires the ask agent's dependencies (installed in CI)")


@pytest.fixture
def voiced(monkeypatch):
    """A whole voice ask with every external call replaced."""
    import types
    fake = types.ModuleType("dashscope_utils")
    fake.stt = lambda audio, fmt: "how is level three going"
    fake.tts = lambda text: b"RIFF" + b"\0" * 200
    monkeypatch.setitem(__import__("sys").modules, "dashscope_utils", fake)
    monkeypatch.setattr(aa, "_rag_answer",
                        lambda req: {"answer": "Level three is on programme.",
                                     "basis": {"k": 5}})
    monkeypatch.setattr(aa, "_invoke_voice_audit", lambda *a, **k: None)
    return fake


def _body():
    import base64
    return {"caller_sub": "u-1", "format": "m4a",
            "audio": base64.b64encode(b"fake audio bytes").decode("ascii")}


def test_a_successful_ask_says_where_the_seconds_went(voiced, caplog):
    with caplog.at_level(logging.INFO):
        res = aa._voice_answer(_body())
    assert "error" not in res, res
    line = [r.getMessage() for r in caplog.records if "voice ask:" in r.getMessage()]
    assert line, "the success path must leave exactly one timing line"
    for field in ("stt=", "rag=", "tts=", "total=", "clip_bytes=",
                  "transcript_words=", "answer_words=", "answer_chars=",
                  "audio_bytes="):
        assert field in line[0], (field, line[0])


def test_the_answer_length_is_reported_not_the_answer(voiced, caplog):
    """Length, never content. This line goes to CloudWatch, which is not a place
    to put what a worker asked or what the site was told -- and the question of
    the moment is how LONG a spoken answer is, which is a number."""
    with caplog.at_level(logging.INFO):
        aa._voice_answer(_body())
    line = [r.getMessage() for r in caplog.records if "voice ask:" in r.getMessage()][0]
    assert "Level three is on programme" not in line
    assert "how is level three going" not in line
    assert "answer_words=5" in line


def test_a_failed_stage_does_not_pretend_to_have_timings(voiced, caplog, monkeypatch):
    """A stage that never ran must not contribute a number. Returning early is
    the existing contract and the instrumentation must not change it -- the
    device's error cue depends on the shape of what comes back."""
    import types
    broken = types.ModuleType("dashscope_utils")
    broken.stt = lambda audio, fmt: (_ for _ in ()).throw(RuntimeError("boom"))
    broken.tts = lambda text: b""
    monkeypatch.setitem(__import__("sys").modules, "dashscope_utils", broken)
    with caplog.at_level(logging.INFO):
        res = aa._voice_answer(_body())
    assert res == {"error": "Speech recognition failed"}
    assert not [r for r in caplog.records if "voice ask:" in r.getMessage()]


def test_measuring_did_not_change_the_response(voiced):
    """The whole point of this change is that it is inert. The device parses
    these keys; an extra one is a client-side surprise and a missing one is a
    silent failure."""
    res = aa._voice_answer(_body())
    assert set(res) == {"transcript", "basis", "answerText",
                        "audioBase64", "audioFormat"}
    assert res["audioFormat"] == "wav"
    assert res["transcript"] == "how is level three going"


# ── continuity step 2: received and counted, used for nothing ────────────────
#
# The whole value of an inert step is that it can be observed in production
# before the retrieval half exists. These three tests are what makes the
# observation trustworthy: the count is real, it does not leak into retrieval,
# and it survives a client that sends garbage.


def test_the_turn_count_is_reported(voiced, caplog):
    """The only evidence of whether any real device sends history. Without this
    number, "the device half shipped" and "the device half shipped and is
    sending nothing" look identical for as long as nobody looks."""
    with caplog.at_level(logging.INFO):
        aa._voice_answer(dict(_body(), history=[
            {"question": "q1", "answer": "a1"},
            {"question": "q2", "answer": "a2"}]))
    line = [r.getMessage() for r in caplog.records if "voice ask:" in r.getMessage()][0]
    assert "history_turns=2" in line, line


def test_no_history_counts_zero_rather_than_omitting_the_field(voiced, caplog):
    """A field that disappears when it is zero cannot be aggregated. The whole
    point of this line is a CloudWatch query over many requests."""
    with caplog.at_level(logging.INFO):
        aa._voice_answer(_body())
    line = [r.getMessage() for r in caplog.records if "voice ask:" in r.getMessage()][0]
    assert "history_turns=0" in line, line


def test_history_reaches_the_log_but_not_retrieval(voiced, caplog, monkeypatch):
    """THE inert test, and the one that would catch step 4 being started by
    accident. Retrieval must receive exactly what it received before: the
    question, the caller, the mode, k and tz. A `history` key arriving here early
    means the union-of-embeddings change went in without its measurement."""
    seen = {}

    def _rag(req):
        seen.update(req)
        return {"answer": "Level three is on programme.", "basis": {"k": 5}}

    monkeypatch.setattr(aa, "_rag_answer", _rag)
    with caplog.at_level(logging.INFO):
        aa._voice_answer(dict(_body(), history=[{"question": "q", "answer": "a"}]))
    assert "history" not in seen, seen
    line = [r.getMessage() for r in caplog.records if "voice ask:" in r.getMessage()][0]
    assert "history_turns=1" in line


def test_a_history_the_agent_cannot_count_is_zero_not_a_crash(voiced, caplog):
    """The gateway sanitises, but this function is also invoked directly -- by
    the other three call sites in the API lambda, by scripts, and by anyone
    testing. It counts and trusts nothing."""
    for bad in ("two turns", 7, {"question": "q"}, None):
        caplog.clear()
        with caplog.at_level(logging.INFO):
            res = aa._voice_answer(dict(_body(), history=bad))
        assert "error" not in res, (bad, res)
        line = [r.getMessage() for r in caplog.records if "voice ask:" in r.getMessage()][0]
        assert "history_turns=0" in line, (bad, line)
