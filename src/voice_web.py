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

# Spoken when the records cannot answer a general question and the web is offered
# instead of being used unasked (spec 2026-10-15 voice offer, section 4.1).
OFFER_NO_RECORDS = "Your site records don't cover that. Want me to search online?"
OFFER_ALSO = "Want me to check online as well?"
# The screen's offer (spec 2026-10-16 section 3): the button is the offer, so the
# text asks no question the user could only answer by typing "yes". There is no
# screen counterpart to OFFER_ALSO; a partial answer carries the flag alone.
SCREEN_OFFER_NO_RECORDS = "Your site records don't cover that."
DECLINED = "Okay."

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
    # Narrower than the spec list: "check online induction status" is ordinary
    # site speech, so this only counts before a clause word or at the end.
    r"check\s+online(?=\s+(?:what|whether|if|how|for|that|the|about)\b|[\s.,;:!?]*$)",
)
# (?<![-\w]) / (?![-\w]) rather than \b: "the internet-connected panel" must not match.
_ALT = r"(?<![-\w])(?:%s)(?![-\w])" % "|".join(_PHRASES)
_INTENT = re.compile(_ALT, re.I)
# Commas and spaces before the phrase, any punctuation after it: the phrase
# usually ends a sentence of its own ("Searching online. Find out ...").
_STRIP = re.compile(r"[\s,]*" + _ALT + r"[\s,.;:!?]*", re.I)
_LEADING_CONNECTIVE = re.compile(r"^(?:and\b|[\s,.;:])+", re.I)
# A remainder ending on one of these is a fragment ("Did the survey say to"),
# so the whole transcript is the better question.
_DANGLING = frozenset("to you it is the a about can me us and of for what".split())


def detect_intent(transcript: str) -> tuple[bool, str]:
    """(web_intent, question for retrieval and answering).

    The device still shows and logs the unstripped transcript. If stripping
    leaves a fragment (under three words, or ending on a stopword) the phrase
    was most of the utterance, so the original goes through unchanged."""
    text = transcript if isinstance(transcript, str) else ""
    if not _INTENT.search(text):
        return False, text
    stripped = _STRIP.sub(" ", text)
    stripped = _LEADING_CONNECTIVE.sub("", " ".join(stripped.split())).strip()
    words = stripped.split()
    if len(words) < 3 or words[-1].strip(".,;:!?\"'").lower() in _DANGLING:
        return True, text
    return True, stripped


def speakable(text) -> bool:
    """Model-free guard run on the final text before it is spoken."""
    if not isinstance(text, str) or not text.strip():
        return False
    low = text.lower()
    if "http" in low or "www." in low or "](" in text:
        return False
    # A marker needs whitespace after it: "-5 degrees overnight" is a number, not a bullet.
    if re.search(r"^\s*[-*•]\s", text, re.M):
        return False
    return len(text.split()) <= 60


# --- Replies to an offer ("Want me to search online?") ---------------------
# Phrases are matched longest first so "go ahead" counts as one yes, not two
# filler words. Every word of a reply must be in one of the three sets, so a
# real question that merely starts with "yes" is never swallowed.
_YES_PHRASES = (("look", "it", "up"), ("go", "ahead"), ("do", "it"),
                ("search", "it"), ("search", "online"))
_NO_PHRASES = (("never", "mind"),)
_YES_WORDS = frozenset("yes yeah yep yup sure".split())
_NO_WORDS = frozenset("no nope nah cancel stop don't dont".split())
_FILLER = frozenset(
    "please thanks thank you ok okay it that go ahead do search online look up for".split())
_REPLY_MAX_WORDS = 6
_REPLY_STRIP = re.compile(r"[^\w\s']")


def reply_kind(transcript: str):
    """'yes' / 'no' for a short reply to the web offer, else None.

    Model-free. Filler alone ("thank you", "okay") is None: right after an
    answer that is far more likely a new question than a yes or a no."""
    if not isinstance(transcript, str):
        return None
    words = _REPLY_STRIP.sub(" ", transcript.lower()).split()
    if not words or len(words) > _REPLY_MAX_WORDS:
        return None
    yes = no = False
    i = 0
    while i < len(words):
        for phrases, kind in ((_YES_PHRASES, "yes"), (_NO_PHRASES, "no")):
            hit = next((p for p in phrases if tuple(words[i:i + len(p)]) == p), None)
            if hit:
                yes, no = yes or kind == "yes", no or kind == "no"
                i += len(hit)
                break
        else:
            w = words[i]
            if w in _YES_WORDS:
                yes = True
            elif w in _NO_WORDS:
                no = True
            elif w not in _FILLER:
                return None
            i += 1
    if yes == no:
        return None
    return "yes" if yes else "no"


def is_bare_search_request(transcript: str) -> bool:
    """True for a "search online" with nothing to search for ("Search online.").

    With an offer live that is a yes on the offered question; with a real
    remainder it is an ordinary web-intent question."""
    if not isinstance(transcript, str) or not _INTENT.search(transcript):
        return False
    return detect_intent(transcript)[1] == transcript
