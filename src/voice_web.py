"""Web answers in voice mode (spec 2026-10-15 section 8).

Three model-free pieces for the device Ask: spotting an explicit "search online"
request, the instruction that makes a web answer speakable, and the guard that
checks the text really is speakable. The guard is code, not prompt, because a
prompt asking for "no URLs" is a request and this check is a guarantee.

Nothing here logs question or answer text.
"""
from __future__ import annotations

import re

NO_CLEAR_ANSWER = "I couldn't get a clear answer online for that."

# Appended to the web_answer prompts when the answer will be read out by a device.
# The [n] exemption is there because compose() rejects any output that drops the
# records answer's citation markers, so telling the model to drop symbols would
# make a composed answer fail that check.
VOICE_RULES = (
    "This answer will be read aloud by a voice assistant. "
    "Use at most two short spoken sentences. "
    "Name the standard or organisation in words, for example \"according to NZS 4541\". "
    "Do not use URLs, symbols, headings, bullets or lists. "
    "If you could not confirm something online, say so in words instead of writing [unverified]. "
    "Keep any [1]-style citation markers already in the records text exactly as written."
)

# Spec section 8 phrase list. "searching" is accepted as well as "search": a
# spoken "Searching online. Find out ..." is the same request.
_PHRASES = (
    r"search(?:ing)?\s+(?:it\s+)?online",
    r"search(?:ing)?\s+the\s+(?:web|internet)",
    r"look\s+(?:it\s+|that\s+)?up\s+online",
    r"google\s+(?:it|that)",
    r"on\s+the\s+internet",
    r"check\s+online",
)
_INTENT = re.compile(r"\b(?:%s)\b" % "|".join(_PHRASES), re.I)
# Commas and spaces before the phrase, any punctuation after it: the phrase
# usually ends a sentence of its own ("Searching online. Find out ...").
_STRIP = re.compile(r"[\s,]*\b(?:%s)\b[\s,.;:!?]*" % "|".join(_PHRASES), re.I)
_LEADING_CONNECTIVE = re.compile(r"^(?:and\b|[\s,.;:])+", re.I)


def detect_intent(transcript: str) -> tuple[bool, str]:
    """(web_intent, question for retrieval and answering).

    The device still shows and logs the unstripped transcript. If stripping
    leaves under two words the phrase WAS the whole utterance ("google that"),
    so the original goes through unchanged rather than an empty question."""
    text = transcript if isinstance(transcript, str) else ""
    if not _INTENT.search(text):
        return False, text
    stripped = _STRIP.sub(" ", text)
    stripped = _LEADING_CONNECTIVE.sub("", " ".join(stripped.split())).strip()
    if len(stripped.split()) < 2:
        return True, text
    return True, stripped


def speakable(text) -> bool:
    """Model-free guard run on the final text before it is spoken."""
    if not isinstance(text, str) or not text.strip():
        return False
    low = text.lower()
    if "http" in low or "www." in low or "](" in text:
        return False
    if any(line.lstrip().startswith(("-", "*", "•")) for line in text.splitlines()):
        return False
    return len(text.split()) <= 60
