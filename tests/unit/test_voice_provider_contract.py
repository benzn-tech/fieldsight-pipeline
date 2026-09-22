"""One shape, every voice vendor.

`_stt` and `_tts` in lambda_ask_agent are if/else branches on an env var, and
each vendor module parses its own wire format before returning. Nothing
enforces that the modules agree: dashscope_utils and elevenlabs_utils are
tested separately, each against its own vendor's shape, and both suites stay
green while the join between them rots. That is the exact failure this repo
has paid for before -- two components agreeing on a contract neither of them
stands on.

So this file drives EVERY registered provider through ONE set of assertions.
Adding a third vendor means adding a row to `PROVIDERS`; if the new module
does not behave like the two that exist, the red test is here, now, and not
a silent Ask that stopped speaking three months from now.

The contract, in full:

  * a missing key RAISES -- it never returns empty. An empty return would be
    indistinguishable from "the user said nothing" / "there was nothing to
    say", so a deploy that forgot a secret would present as a device that
    simply stopped answering. (Measured shape of this exact hazard: prod
    carried NO Ask STT key at all until 2026-09-21 and nothing failed.)
  * with a key present, empty input returns empty WITHOUT calling the vendor.
  * stt returns `str`, tts returns `bytes` -- the dispatcher relies on both
    and neither module declares it.
  * whatever the vendor's audio container, `_tts` hands the device WAV.
  * every value the template will accept has a branch.
"""
import importlib
import re

import pytest

laa = pytest.importorskip("lambda_ask_agent")

TEMPLATE = "src/template.yaml"


# module, stt entry point, tts entry point, the module attribute holding each
# key (read at import time, so tests patch the ATTRIBUTE, never the env var).
PROVIDERS = {
    "dashscope": {
        "module": "dashscope_utils",
        "stt": ("stt", "DASHSCOPE_API_KEY"),
        "tts": ("tts", "DASHSCOPE_API_KEY"),
    },
    "elevenlabs": {
        "module": "elevenlabs_utils",
        "stt": ("stt_short", "ELEVENLABS_ASK_API_KEY"),
        "tts": ("tts", "ELEVENLABS_TTS_API_KEY"),
    },
}


def _mod(name):
    return importlib.import_module(PROVIDERS[name]["module"])


def _template_allowed_values(parameter):
    """The AllowedValues list the template will accept for one Parameter."""
    text = open(TEMPLATE, encoding="utf-8").read()
    block = text[text.index("\n  %s:\n" % parameter):]
    m = re.search(r"^\s*AllowedValues:\s*\[([^\]]*)\]", block, re.MULTILINE)
    assert m, "%s declares no AllowedValues" % parameter
    return {v.strip() for v in m.group(1).split(",") if v.strip()}


# ---- the registry is the template's list, not a copy of it ----------------

@pytest.mark.parametrize("parameter", ["AskSttProvider", "AskTtsProvider"])
def test_every_value_the_template_accepts_is_a_provider_here(parameter):
    """A vendor added to the template but not to this file would deploy, be
    selectable, and never be held to any of the assertions below."""
    assert _template_allowed_values(parameter) == set(PROVIDERS)


# ---- a missing key is loud -------------------------------------------------

@pytest.mark.parametrize("name", sorted(PROVIDERS))
@pytest.mark.parametrize("kind", ["stt", "tts"])
def test_a_missing_key_raises_instead_of_returning_empty(monkeypatch, name, kind):
    mod = _mod(name)
    fn_name, key_attr = PROVIDERS[name][kind]
    monkeypatch.setattr(mod, key_attr, "")
    arg = b"audio" if kind == "stt" else "some answer"
    with pytest.raises(RuntimeError):
        getattr(mod, fn_name)(arg)


# ---- empty input costs nothing --------------------------------------------

@pytest.mark.parametrize("name", sorted(PROVIDERS))
def test_no_audio_returns_empty_text_without_asking_the_vendor(monkeypatch, name):
    mod = _mod(name)
    fn_name, key_attr = PROVIDERS[name]["stt"]
    monkeypatch.setattr(mod, key_attr, "k")
    _forbid_network(monkeypatch, mod)
    out = getattr(mod, fn_name)(b"")
    assert out == "", "%s: expected the empty transcript" % name
    assert isinstance(out, str)


@pytest.mark.parametrize("name", sorted(PROVIDERS))
@pytest.mark.parametrize("text", ["", "   "])
def test_no_text_returns_empty_audio_without_asking_the_vendor(
        monkeypatch, name, text):
    mod = _mod(name)
    fn_name, key_attr = PROVIDERS[name]["tts"]
    monkeypatch.setattr(mod, key_attr, "k")
    _forbid_network(monkeypatch, mod)
    out = getattr(mod, fn_name)(text)
    assert out == b"", "%s: expected no audio" % name
    assert isinstance(out, bytes)


def _forbid_network(monkeypatch, mod):
    """Any HTTP from here is the failure being tested for, not a side issue."""
    if hasattr(mod, "urllib3"):
        class Loud:
            def __init__(self, *a, **kw):
                raise AssertionError("the vendor was called for empty input")
        monkeypatch.setattr(mod.urllib3, "PoolManager", Loud)


# ---- the dispatcher hides the vendor from the device ----------------------

def test_the_device_is_handed_wav_whichever_vendor_spoke(monkeypatch):
    """dashscope_utils.tts returns WAV; elevenlabs_utils.tts returns bare PCM
    (no header), because Android's MediaPlayer cannot play headerless audio
    and one of the two has to add it. `_tts` is where that is reconciled, so
    a vendor whose container differs must be normalised THERE -- not left for
    the device to discover."""
    import dashscope_utils
    import elevenlabs_utils

    pcm = b"\x01\x00" * 16
    monkeypatch.setattr(elevenlabs_utils, "tts", lambda text: pcm)
    monkeypatch.setattr(dashscope_utils, "tts",
                        lambda text: dashscope_utils._pcm_to_wav(pcm))

    for name in PROVIDERS:
        monkeypatch.setenv("ASK_TTS_PROVIDER", name)
        out = laa._tts("anything")
        assert out.startswith(b"RIFF"), "%s: the device needs a container" % name
        assert out.endswith(pcm), "%s: the audio itself must survive" % name


@pytest.mark.parametrize("name", sorted(PROVIDERS))
def test_each_provider_value_reaches_its_own_module(monkeypatch, name):
    import dashscope_utils
    import elevenlabs_utils

    monkeypatch.setattr(dashscope_utils, "stt",
                        lambda audio, fmt=None: "dashscope")
    monkeypatch.setattr(elevenlabs_utils, "stt_short",
                        lambda audio, filename=None: "elevenlabs")
    monkeypatch.setenv("ASK_STT_PROVIDER", name)
    assert laa._stt(b"clip", "m4a") == name


def test_an_unknown_provider_falls_back_rather_than_crashing(monkeypatch):
    """A typo in a repo variable must not take the voice path down. It reads
    as the incumbent, which is the documented behaviour of that branch."""
    import dashscope_utils
    monkeypatch.setattr(dashscope_utils, "stt", lambda audio, fmt=None: "dashscope")
    monkeypatch.setenv("ASK_STT_PROVIDER", "elevnlabs")
    assert laa._stt(b"clip", "m4a") == "dashscope"
