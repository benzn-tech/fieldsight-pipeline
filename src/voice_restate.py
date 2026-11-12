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
DEFAULT_PROVIDER = "groq"
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

# Spoken numbers. Speech-to-text writes "three one oh one" where the model then
# writes "3101" (TEST 2026-11-12: the restatement was refused as guard_number and
# the fixed line played). The guard still allows only numbers the caller said:
# these are the readings of what they said, not new numbers.
_DIGIT_WORDS = {"zero": 0, "oh": 0, "one": 1, "two": 2, "three": 3, "four": 4,
                "five": 5, "six": 6, "seven": 7, "eight": 8, "nine": 9}
_TEEN_WORDS = {"ten": 10, "eleven": 11, "twelve": 12, "thirteen": 13, "fourteen": 14,
               "fifteen": 15, "sixteen": 16, "seventeen": 17, "eighteen": 18, "nineteen": 19}
_TENS_WORDS = {"twenty": 20, "thirty": 30, "forty": 40, "fifty": 50, "sixty": 60,
               "seventy": 70, "eighty": 80, "ninety": 90}
_SCALE_WORDS = {"hundred": 100, "thousand": 1000}


def _is_num_word(t):
    return t in _DIGIT_WORDS or t in _TEEN_WORDS or t in _TENS_WORDS


def _chunks(run):
    """"three one oh one" -> "3101", "thirty six oh four" -> "3604", "twenty four" -> "24"."""
    out, i = "", 0
    while i < len(run):
        t = run[i]
        if t in _TENS_WORDS and i + 1 < len(run) and run[i + 1] in _DIGIT_WORDS and _DIGIT_WORDS[run[i + 1]]:
            out += str(_TENS_WORDS[t] + _DIGIT_WORDS[run[i + 1]])
            i += 2
            continue
        out += str(_DIGIT_WORDS.get(t, _TEEN_WORDS.get(t, _TENS_WORDS.get(t))))
        i += 1
    return out


def _cardinal(run):
    """"twenty four hundred" -> 2400, "two thousand five hundred" -> 2500."""
    total, cur = 0, 0
    for t in run:
        if t == "thousand":
            total += (cur or 1) * 1000
            cur = 0
        elif t == "hundred":
            cur = (cur or 1) * 100
        else:
            cur += _DIGIT_WORDS.get(t, _TEEN_WORDS.get(t, _TENS_WORDS.get(t, 0)))
    return str(total + cur)


def _readings(run):
    if "point" in run:
        k = run.index("point")
        whole, frac = run[:k], run[k + 1:]
        if not frac or any(t not in _DIGIT_WORDS for t in frac) or "point" in frac:
            return []
        heads = _readings(whole) if whole else ["0"]
        return [h + "." + "".join(str(_DIGIT_WORDS[t]) for t in frac) for h in heads]
    if not run or run[-1] in _SCALE_WORDS and len(run) == 1:
        return []
    if any(t in _SCALE_WORDS for t in run):
        return [_cardinal(run)]
    return [_chunks(run)]


def spoken_numbers(text) -> list:
    """Digit strings for the numbers written out as words in `text`."""
    toks = re.findall(r"[a-z]+", str(text).lower())
    out, i = [], 0
    while i < len(toks):
        if not _is_num_word(toks[i]):
            i += 1
            continue
        j = i
        while j < len(toks) and (_is_num_word(toks[j]) or toks[j] in _SCALE_WORDS or toks[j] == "point"):
            j += 1
        out.extend(_readings(toks[i:j]))
        i = j
    return out


def _said_numbers(question):
    """Every number the caller said, as digits; "2,400" and "2400" are one number."""
    return {n.replace(",", "") for n in _NUMBER.findall(question)} | set(spoken_numbers(question))


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
    q_numbers = _said_numbers(q)
    if any(n.replace(",", "") not in q_numbers for n in _NUMBER.findall(m)):
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


def provider_pin():
    """OpenRouter `provider` preference for the restate call, or None.

    Unpinned, OpenRouter spread llama-3.1-8b over Cloudflare and DeepInfra at about 22
    tok/s: measured 2026-11-12 (n=10) total p50 1.8-2.0 s, max 6.6 s, which is the
    owner's 2.27 s restate timeout that morning. Pinned to Groq the same prompt took
    p50 0.31-0.42 s (about 444 tok/s); CoreWeave 0.55-0.62 s. Fallbacks stay allowed:
    a Groq outage should cost speed, not the restatement. VOICE_RESTATE_PROVIDER="none"
    removes the pin."""
    raw = (os.environ.get("VOICE_RESTATE_PROVIDER") or DEFAULT_PROVIDER).strip()
    if not raw or raw.lower() == "none":
        return None
    return {"order": [p.strip() for p in raw.split(",") if p.strip()],
            "allow_fallbacks": True}


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
    payload = {
        "model": model,
        "messages": [{"role": "system", "content": RESTATE_SYSTEM},
                     {"role": "user", "content": "Current question: " + question}],
        "max_tokens": 40,
        "temperature": 0,
    }
    pin = provider_pin()
    if pin:
        payload["provider"] = pin
    body = json.dumps(payload).encode("utf-8")
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
    # urlopen's timeout bounds each socket read, not the whole call, so a slow
    # trickle could overrun; a restatement that arrives late is worthless.
    if time.monotonic() - started > timeout_s:
        return _result(None, "timeout", started)
    middle = re.sub(r"^[\"'“]+|[\"'”]+$", "", content.strip()).strip()
    g = guard_middle(question, middle)
    if not g["ok"]:
        return _result(None, g["reason"], started)
    return _result(assemble(g["middle"], web_intent), None, started)
