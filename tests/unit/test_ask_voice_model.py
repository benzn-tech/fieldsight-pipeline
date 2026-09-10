"""Unit: the spoken answer may use a different model, and never without low effort.

A spoken answer and a screen answer are the same question asked of two different
products. Measured 2026-09-09 on the voice-shaped prompt, three runs each:

    meta/muse-spark-1.3-contributor   6.53s   651 completion, 599 REASONING
    google/gemini-3.8-flash           3.33s    32 completion,   0 reasoning

Nearly twice as fast because it stops THINKING, not because it writes less --
599 of muse's 651 tokens were never spoken aloud.

THE TRAP THIS FILE EXISTS FOR: the same measurement, run without an explicit
effort, gave gemini 538 reasoning tokens and 7.22s -- SLOWER than the incumbent.
A model swap that forgets the effort is a regression wearing an optimisation's
name, and it is the same shape as DashScope defaulting `enable_thinking` ON when
the field is omitted, which cost 10x once already.

So `test_the_model_never_travels_without_low_effort` is the load-bearing test
here. The speed cannot be checked in CI; the pairing can.
"""
import pytest

llm_utils = pytest.importorskip("llm_utils")
aa = pytest.importorskip(
    "lambda_ask_agent",
    reason="requires the ask agent's dependencies (installed in CI)")


@pytest.fixture
def rag(monkeypatch):
    """Enough of _rag_answer's world to reach the call_llm line."""
    calls = []

    def _fake_call_llm(prompt, max_tokens=None, force_json=False,
                       enable_thinking=None, model=None):
        calls.append({"enable_thinking": enable_thinking, "model": model})
        return "Level three is on programme.", None

    # `llm_utils` is imported lazily inside the function, so patch the MODULE,
    # not an attribute of lambda_ask_agent -- there is no such attribute.
    monkeypatch.setattr(llm_utils, "call_llm", _fake_call_llm)
    monkeypatch.setattr(aa, "build_rag_prompt", lambda *a, **k: "PROMPT")
    monkeypatch.setattr(aa, "_rerank_chunks", lambda q, chunks, k: chunks)

    # Retrieval is a synchronous invoke of the in-VPC search lambda; stub the
    # client rather than a helper, so the test does not depend on the shape of
    # a private function it is not about.
    class _Payload:
        @staticmethod
        def read():
            import json as _j
            return _j.dumps({"chunks": [{"text": "chunk", "id": "c1",
                                         "source_s3_key": "k"}]}).encode()

    class _Client:
        @staticmethod
        def invoke(**kw):
            return {"Payload": _Payload()}

    # The question is embedded before the search invoke; stub the vendor call
    # rather than set a fake key, so nothing in this test can reach the network.
    import types, sys
    ds = sys.modules.get("dashscope_utils") or types.ModuleType("dashscope_utils")
    monkeypatch.setattr(ds, "embed", lambda texts: [[0.0] * 8], raising=False)
    monkeypatch.setitem(sys.modules, "dashscope_utils", ds)

    monkeypatch.setattr(aa, "_get_lambda_client", lambda: _Client())
    monkeypatch.setenv("RAG_SEARCH_FUNCTION", "fake-search")
    return calls


def _ask(mode):
    return {"question": "how is level three", "caller_sub": "u-1", "mode": mode}


def test_the_screen_path_is_untouched(rag, monkeypatch):
    """Unset must mean unchanged, on both paths. The override exists for voice;
    the screen answer keeps the deploy's model and the deploy's effort."""
    monkeypatch.setenv("ASK_VOICE_MODEL", "google/gemini-3.8-flash")
    aa._rag_answer(_ask("screen"))
    assert rag, "call_llm was never reached"
    assert rag[-1] == {"enable_thinking": None, "model": None}


def test_voice_without_the_env_is_also_untouched(rag, monkeypatch):
    """The default is inert. A provider seam that switches itself on is a deploy
    that changes behaviour nobody asked it to change."""
    monkeypatch.delenv("ASK_VOICE_MODEL", raising=False)
    aa._rag_answer(_ask("voice"))
    assert rag[-1] == {"enable_thinking": None, "model": None}


def test_voice_uses_the_named_model(rag, monkeypatch):
    monkeypatch.setenv("ASK_VOICE_MODEL", "google/gemini-3.8-flash")
    aa._rag_answer(_ask("voice"))
    assert rag[-1]["model"] == "google/gemini-3.8-flash"


def test_the_model_never_travels_without_low_effort(rag, monkeypatch):
    """THE test. Measured: gemini with no explicit effort spends 538 reasoning
    tokens and 7.22s -- slower than the model it replaces. The two are one
    change, and separating them turns a measured win into a measured loss with
    no error anywhere to say so."""
    monkeypatch.setenv("ASK_VOICE_MODEL", "google/gemini-3.8-flash")
    aa._rag_answer(_ask("voice"))
    assert rag[-1]["enable_thinking"] is False, (
        "a voice model override must force the fast path; without it the model "
        "decides for itself and this becomes slower than doing nothing")


def test_whitespace_is_not_a_model(rag, monkeypatch):
    """A cleared stack parameter arrives as the word `none` -- an empty override
    renders a bare `AskVoiceModel=` and SAM exits 2 before CloudFormation runs,
    so the deploy passes a sentinel. An empty model on the wire is a 400 that
    reads as a vendor outage, so both spellings must mean "no override"."""
    for blank in ("", "   ", "none", "NONE"):
        monkeypatch.setenv("ASK_VOICE_MODEL", blank)
        aa._rag_answer(_ask("voice"))
        assert rag[-1] == {"enable_thinking": None, "model": None}, blank
