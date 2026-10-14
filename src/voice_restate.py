"""Python port of voice_llm/restate.mjs for the device two-phase Ask (spec 2026-10-15 section 7).

The device speaks a restatement of the question while the answer is still being
worked out, so the same model-free guards apply: a restatement may only rephrase
what the caller said, never add to it. The guards are pinned against the Node
version by tests/fixtures/restate_guard_cases.json, which both languages load.

Nothing here logs or retains question text.

The model call is one raw POST on purpose: llm_utils.call_llm's fallback ladder
alone would exceed the restate budget, and a late restatement is worthless.
"""
from __future__ import annotations

import json
import os
import re
import time
import urllib.error
import urllib.request

DEFAULT_MODEL = "meta-llama/llama-3.1-8b-instruct"
MAX_WORDS = 25

# Verbatim from voice_llm/restate.mjs; test_voice_restate.py parses the Node file
# and fails if the two differ.
RESTATE_SYSTEM = " ".join([
    "Restate the user's question as a short spoken phrase that starts with \"you're asking\".",
    "Use natural spoken English, at most 18 words.",
    "Keep any standard, product, quantity or location words exactly as the user said them.",
    "Never answer the question. Never add facts, names, dates or numbers that are not in the question.",
    "Output only the phrase.",
])

_CORRECTION = re.compile(r"^\s*(no\s*[,.!]|nope\b|i\s+mean(t)?\b)", re.I)
_REJECTED_MODEL = re.compile(r"gemini-3|-thinking|r1", re.I)
_PREFIX = re.compile(r"^you['’]re asking\b", re.I)
_TRAILING = re.compile(r"[\s.,;:!?]+$")
_NUMBER = re.compile(r"[0-9]+(?:[.,][0-9]+)*")   # ASCII digits, as JS \d
_POSSESSIVE = re.compile(r"['’]s$", re.I)


def is_correction(question) -> bool:
    """"No, I meant ..." re-asks something already restated; do not restate it."""
    return bool(_CORRECTION.search(str(question if question is not None else "")))


def model_rejected(model) -> bool:
    """Reasoning models burn the 40-token budget on thinking and answer with nothing."""
    return bool(_REJECTED_MODEL.search(str(model if model is not None else "")))


def _words(s):
    return str(s).split()


def _bare_word(w):
    # Python's re has no \p{L}; isalpha/isnumeric are the equivalent classes.
    start, end = 0, len(w)
    while start < end and not (w[start].isalpha() or w[start].isnumeric()):
        start += 1
    while end > start and not (w[end - 1].isalpha() or w[end - 1].isnumeric()):
        end -= 1
    return _POSSESSIVE.sub("", w[start:end])


def guard_middle(question, middle) -> dict:
    """{"ok": True, "middle"} (trimmed, trailing punctuation removed, leading
    "You're" lower-cased for the splice into "Got it, ...") or
    {"ok": False, "reason"}. Same semantics as guardMiddle in restate.mjs."""
    m = _TRAILING.sub("", middle.strip()) if isinstance(middle, str) else ""
    if not _PREFIX.search(m):
        return {"ok": False, "reason": "guard_prefix"}
    if len(_words(m)) > MAX_WORDS or re.search(r"\.\.\.|…|[\r\n]", m):
        return {"ok": False, "reason": "guard_words"}
    q = str(question if question is not None else "")
    q_numbers = _NUMBER.findall(q)
    if any(n not in q_numbers for n in _NUMBER.findall(m)):
        return {"ok": False, "reason": "guard_number"}
    known = {_bare_word(w).lower() for w in _words(q)}
    for w in _words(m)[1:]:   # word 0 is "You're"
        w = _bare_word(w)
        # str.isupper() is Python's stand-in for \p{Lu}; it also covers Otautahi-style macron capitals.
        if w[:1].isupper() and w != "I" and w.lower() not in known:
            return {"ok": False, "reason": "guard_name"}
    return {"ok": True, "middle": m[0].lower() + m[1:]}


def assemble(middle: str, web_intent: bool) -> str:
    """The device has no 141-char floor (that is the EL agent's TTS hold-back)."""
    tail = "Searching online now." if web_intent else "Checking your site records now."
    return "Got it, %s. %s" % (middle, tail)


def _result(text, reason, started):
    return {"text": text, "reason": reason, "ms": int((time.monotonic() - started) * 1000)}


def restate(question: str, *, web_intent: bool, timeout_s: float = 1.2) -> dict:
    """One non-streaming model call. Returns {"text", "reason", "ms"}; never raises.
    Only the current question is sent -- never answers or records."""
    started = time.monotonic()
    if os.environ.get("ASK_VOICE_RESTATE") != "true":
        return _result(None, "off", started)
    if is_correction(question):
        return _result(None, "correction", started)
    # Env is read at call time so a test (or a config change) is honoured without a reload.
    model = os.environ.get("VOICE_RESTATE_MODEL") or DEFAULT_MODEL
    if model_rejected(model):
        return _result(None, "model_rejected", started)
    key = os.environ.get("QWEN_API_KEY")
    base = os.environ.get("QWEN_BASE_URL")
    if not key or not base:
        return _result(None, "no_key", started)
    body = json.dumps({
        "model": model,
        "messages": [{"role": "system", "content": RESTATE_SYSTEM},
                     {"role": "user", "content": question}],
        "max_tokens": 40,
        "temperature": 0,
    }).encode("utf-8")
    req = urllib.request.Request(
        base.rstrip("/") + "/chat/completions", data=body, method="POST",
        headers={"Authorization": "Bearer " + key, "Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=timeout_s) as resp:
            if getattr(resp, "status", 200) != 200:
                return _result(None, "http", started)
            content = json.loads(resp.read())["choices"][0]["message"]["content"]
    except urllib.error.HTTPError:
        return _result(None, "http", started)
    except TimeoutError:
        return _result(None, "timeout", started)
    except urllib.error.URLError as e:
        return _result(None, "timeout" if isinstance(e.reason, TimeoutError) else "error", started)
    except Exception:  # noqa: BLE001 -- the caller speaks the filler on any failure
        return _result(None, "error", started)
    if not isinstance(content, str):
        return _result(None, "error", started)
    middle = re.sub(r"^[\"'“]+|[\"'”]+$", "", content.strip()).strip()
    g = guard_middle(question, middle)
    if not g["ok"]:
        return _result(None, g["reason"], started)
    return _result(assemble(g["middle"], web_intent), None, started)
