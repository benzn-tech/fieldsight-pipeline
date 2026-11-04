"""A confirmation answer from the device (multi-unit walks, Task 11).

After the final check asks "Unit 3: wall bracing -- pass?", the inspector holds
the Ask key and answers. This parses the answer, has the in-VPC hop store it as
report evidence (walk_answers, with the transcript as its quote), and asks for
the walk report to be queued again. The device keeps the question list and asks
the next question itself.

It is never general Ask: an answer that is not pass / fail / N/A / a move is
skipped, and the question stays on the phone. On a not-checked question a Yes
is a WHOLE acceptance ("pass", "all good"), never an answer that merely starts
with one ("okay, hang on"), and a hedge ("no idea") is never a No (R46).

Pure apart from the injected `stt`, `db_write`, `requeue` and `now`. It logs
counts and status only, never what was said.
"""
import base64
import binascii
import logging
import re

import nz_time
import units as units_mod

logger = logging.getLogger(__name__)

KINDS = ("not_checked", "confirm_no")

SAY_SAVED = "Got it."
SAY_MOVED = "Moved to %s."
SAY_SKIP = "Skipping this one — it's on your phone."

#: The hop's refusal of a move to a unit that is not one of the walk's. The hop
#: is the authority on the walk's units; handle then answers by the next rule.
MOVE_NOT_IN_WALK = "move_to_unit not in walk"

# Rule 1, anchored: the WHOLE answer is a move -- an optional no, an optional
# "that was", the unit, and nothing else but filler. A unit mentioned in passing
# ("No, the nogging in Unit 2 is missing") is not a move.
_MOVE = re.compile(r"^(?:(?:no|nope|wrong(?: one)?) )?"
                   r"(?:(?:that was|thats|that is|it was|its|it is|should be|belongs to|it belongs to) )?"
                   r"(?P<unit>.+)$")
_FILLER = {"actually", "sorry"}
_NUMBER_WORDS = {"one", "two", "three", "four", "five", "six", "seven", "eight", "nine",
                 "ten", "eleven", "twelve", "thirteen", "fourteen", "fifteen", "sixteen",
                 "seventeen", "eighteen", "nineteen", "twenty", "thirty", "forty", "fifty",
                 "sixty", "seventy", "eighty", "ninety", "hundred", "and", "oh", "zero", "o"}
_UNIT_TOKEN = re.compile(r"[a-z]+|\d+[a-z](?![a-z])|\d+")   # units._TOKEN's split
# R46: on a not-checked question the WHOLE answer, filler aside, must be one of
# these -- "okay, hang on" is not a pass.
_ACCEPT = {"pass", "passed", "yes", "yep", "good", "all good", "ok", "okay", "fine"}
_ACCEPT_FILLER = {"um", "uh", "er", "erm", "ah", "hmm", "mm", "actually", "sorry"}
# R46: on a not-checked question a No needs one of these first, and no hedge.
_NO_FIRST = {"no", "fail", "failed"}
# R50: "no worries" / "no issues" / "not yet" / "I haven't" are not a No -- and
# "no issues" (it's fine) is not a Yes either: they go to the phone.
_HEDGE = re.compile(r"^no (?:idea|wait)\b|\bnot sure\b|\bdont know\b|\bdunno\b"
                    r"|\bno (?:worries|problems?|dramas?|issues?)\b|\bnot yet\b"
                    r"|\bi (?:havent|didnt)\b")
# R48: a move naming two units ("unit 2 and 3", "units 2 and 3") is a skip.
_JOIN = {"and", "or", "to", "plus"}
TWO_UNITS = object()
# Heard text that is a non-speech marker, not an answer. There is no ASR
# hallucination classifier in this repo: VAD gating drops silent recording
# chunks before STT (not applicable to one answer clip), and the only text
# filter is lambda_extraction_backlog._NON_SPEECH -- its markers are copied here
# (that Lambda is not importable from CheckGaps), plus any bracketed audio-event
# tag the STT writes ("(wind)", "[music]").
_NON_SPEECH = ("recording started", "recording stopped")   # the bracketed ones: _TAG
_TAG = re.compile(r"\([^)]*\)|\[[^\]]*\]")
# R32: on a read-back No an acknowledgement confirms the No; only an explicit
# pass turns it into Yes.
_CONFIRM = {"right", "correct", "yes", "yeah", "yep", "ok", "okay", "good"}
_PASS = re.compile(r"^(?:actually )?(?:it )?(?:pass|passed|passes)\b")
_PASS_WORD = re.compile(r"\b(?:pass|passed|passes)\b")
_NO = {"fail", "failed", "fails", "no"}       # rule 3 on a read-back No, as before
# Rule 4, anchored to the start like rules 2 and 3.
_NA = re.compile(r"^(?:(?:it|that|this|its|thats) (?:is )?)?"
                 r"(?:not applicable|n a|na|doesnt apply|does not apply)\b")
_NO_REST = re.compile(r"^\W*(?:failed|fails|fail|no)\b[\W_]*(.*)$", re.IGNORECASE | re.DOTALL)


def _norm(text):
    """Lower case, apostrophes dropped ("that's" -> "thats"), every other
    punctuation mark a space ("n/a" -> "n a"), spaces collapsed."""
    t = (text or "").lower().replace("'", "").replace("’", "")
    return " ".join(re.sub(r"[^\w\s]|_", " ", t).split())


def _move_phrase(norm):
    """The unit the answer moves to when the whole answer is a move, else None:
    [no | nope | wrong] [that was | that's | it was | it's | should be |
    belongs to] <unit>, with only filler ("actually", "sorry") besides.
    TWO_UNITS when the move names two or more units ("unit 2 and 3", "units 2
    and 3", "unit 2 unit 3") -- R48: never a move to the first of them."""
    words = " ".join(w for w in norm.split() if w not in _FILLER)
    m = _MOVE.match(words)
    if not m:
        return None
    tok = _UNIT_TOKEN.findall(m.group("unit"))
    if not tok or len(tok) < 2:
        return None
    plural = tok[0].endswith("s") and tok[0][:-1] in units_mod.PREFIXES
    if tok[0] not in units_mod.PREFIXES and not plural:
        return None
    rest = tok[1:]
    if not all(t[0].isdigit() or t in _NUMBER_WORDS or t in units_mod.PREFIXES or t in _JOIN
               for t in rest):
        return None
    # "and" only joins a number after "hundred" ("one hundred and five").
    joined = any(t in _JOIN and not (t == "and" and i and rest[i - 1] == "hundred")
                 for i, t in enumerate(rest))
    if plural or joined or any(t in units_mod.PREFIXES for t in rest):
        return TWO_UNITS
    return units_mod.normalize_unit(m.group("unit"))


def _move_to(norm, units):
    """The walk unit the whole answer moves to, or None."""
    walk = {units_mod.normalize_unit(u) for u in units or [] if isinstance(u, str)}
    walk.discard(None)
    u = _move_phrase(norm)
    return u if u and u is not TWO_UNITS and u in walk else None


def _accepted(norm):
    """The whole answer, filler aside, is one acceptance (R46)."""
    return " ".join(w for w in norm.split() if w not in _ACCEPT_FILLER) in _ACCEPT


def _not_speech(text):
    """Heard text that carries a non-speech marker or an audio-event tag, or
    has no word made of letters ("...") -- not an answer, whatever else it
    says (R46). Wind STT'd as "(wind) okay" must not pass an item."""
    low = (text or "").lower()
    if _TAG.search(low) or any(m in low for m in _NON_SPEECH):
        return True
    return not re.search(r"[a-z]", low)


def parse_answer(text, kind, units):
    """The answer in `text`, by the first rule that applies, or None (skip):

    1. the whole answer is a move to a unit of the walk -- [no / wrong / nope]
       [that was / that's / it was / it's / should be / belongs to] <unit> --
       (a read-back No only); a move naming two or more units -> None (R48);
    2. on a not-checked question (R46) the WHOLE answer, filler ("um",
       "actually") aside, is pass / passed / yes / yep / good / all good / ok
       / okay / fine -> Yes; an acknowledgement followed by more words ("okay
       hang on") -> None. For a read-back No (R32) an acknowledgement (right /
       correct / yes / yeah / ok / okay / good / that's right / yep) confirms
       the No, and only an explicit pass ("pass", "it passes", "actually it
       passes") is Yes; both at once ("yes it passes") is ambiguous -> None
       (R34);
    3. it starts with fail / failed / no (on a read-back No also fails) -> No,
       the rest is its comment; on a not-checked question a hedge ("no idea",
       "no wait", "not sure", "don't know", "no worries", "no issues",
       "not yet", "I haven't", "I didn't") anywhere -> None (R46, R50);
    4. it starts with not applicable / n a / na / doesn't apply -> N/A;
    5. anything else -> None."""
    norm = _norm(text)
    if not norm:
        return None
    if kind == "confirm_no":
        if _move_phrase(norm) is TWO_UNITS:
            return None
        target = _move_to(norm, units)
        if target:
            return {"move_to": target}
    first = norm.split()[0]
    if kind == "confirm_no":
        if _PASS.match(norm):
            return {"answer": "Yes"}
        if first in _CONFIRM or norm.startswith("thats right"):
            # R34: an acknowledgement that also says it passes ("yes it
            # passes") is ambiguous -- skipped to the phone, never guessed.
            if _PASS_WORD.search(norm):
                return None
            return {"answer": "No", "confirmed": True}
    else:
        if _accepted(norm):
            return {"answer": "Yes"}
        if _HEDGE.search(norm):
            return None
    if first in (_NO if kind == "confirm_no" else _NO_FIRST):
        m = _NO_REST.match((text or "").strip())
        return {"answer": "No", "comment": (m.group(1) if m else "").strip()}
    if _NA.match(norm):
        return {"answer": "N/A"}
    return None


def _named_units(text):
    """The unit a whole-answer move names, as the one candidate a move may go
    to. Whether it is the walk's is the hop's to say (it reads the walk)."""
    u = _move_phrase(_norm(text))
    return [u] if u and u is not TWO_UNITS else []


def _out(status, say):
    return {"status": status, "say": say, "next": None}


def _heard(event, stt):
    """(transcript, None) or (None, reason)."""
    text = event.get("text")
    if isinstance(text, str) and (text.strip() or not event.get("audio_b64")):
        return text.strip(), None
    audio = event.get("audio_b64")
    if not isinstance(audio, str) or not audio:
        return None, "no answer"
    if stt is None:
        return None, "no stt"
    try:
        data = base64.b64decode(audio, validate=True)
    except (binascii.Error, ValueError):
        return None, "bad audio"
    try:
        return (stt(data, "answer.wav") or "").strip(), None
    except Exception as e:                            # noqa: BLE001 -- a person is waiting
        logger.warning("walk-confirm: stt failed: %s", type(e).__name__)
        return None, "stt"


def _row(event, parsed, quote, answered_at):
    move = parsed.get("move_to")
    return {"action": "write_walk_answer", "caller_sub": event["caller_sub"],
            "walk_key": event["walk_key"], "unit": event["unit"],
            "item_text": event["item_text"],
            "answer": None if move else parsed["answer"],
            "comment": parsed.get("comment") or "", "move_to_unit": move,
            "quote": quote, "answered_at": answered_at,
            # R36: an answer to a read-back No is the inspector's choice over the recording.
            "asked_kind": event["kind"]}


def handle(event, *, stt, db_write, now, requeue=None):
    """One answer. Returns {"status": "saved" | "skipped" | "error", "say", "next": None}.

    Only these fields of the event are used: caller_sub, walk_key, unit,
    item_text, kind, text / audio_b64. The company, folder and walk facts are
    the hop's, from the caller's own record -- never the event's."""
    event = event or {}
    kind = event.get("kind")
    if kind not in KINDS or not all(isinstance(event.get(k), str) and event[k].strip()
                                    for k in ("caller_sub", "walk_key", "unit", "item_text")):
        logger.info("walk-confirm: status=error reason=bad_request")
        return _out("error", SAY_SKIP)
    quote, why = _heard(event, stt)
    if quote is None:
        logger.info("walk-confirm: status=error reason=%s", why)
        return _out("error", SAY_SKIP)
    if _not_speech(quote):
        logger.info("walk-confirm: status=skipped kind=%s reason=not_speech", kind)
        return _out("skipped", SAY_SKIP)

    # The current unit is not somewhere to move to: "no, that was Unit 3" while
    # on Unit 3 is a No, by rule 3.
    here = units_mod.normalize_unit(event["unit"])
    parsed = parse_answer(quote, kind, [u for u in _named_units(quote) if u != here])
    if parsed is None:
        logger.info("walk-confirm: status=skipped kind=%s chars=%d", kind, len(quote))
        return _out("skipped", SAY_SKIP)

    answered_at = nz_time.to_nz(now()).strftime("%H:%M:%S")
    try:
        saved = db_write(_row(event, parsed, quote, answered_at))
        if parsed.get("move_to") and (saved or {}).get("error") == MOVE_NOT_IN_WALK:
            # Not one of the walk's units: then it is not a move (rule 1 does
            # not apply), and the next rule answers.
            parsed = parse_answer(quote, kind, [])
            if parsed is None:
                logger.info("walk-confirm: status=skipped kind=%s chars=%d", kind, len(quote))
                return _out("skipped", SAY_SKIP)
            saved = db_write(_row(event, parsed, quote, answered_at))
    except Exception as e:                            # noqa: BLE001 -- a person is waiting
        logger.warning("walk-confirm: write failed: %s", type(e).__name__)
        return _out("error", SAY_SKIP)
    if not isinstance(saved, dict) or saved.get("status") != "saved":
        logger.info("walk-confirm: status=error reason=hop:%s",
                    (saved or {}).get("error") if isinstance(saved, dict) else "shape")
        return _out("error", SAY_SKIP)

    if requeue is not None:
        try:
            requeue(saved)
        except Exception:                             # noqa: BLE001 -- the answer is kept
            logger.warning("walk-confirm: walk report requeue failed; the answer is saved",
                           exc_info=True)
    move = parsed.get("move_to")
    logger.info("walk-confirm: status=saved kind=%s answer=%s move=%s chars=%d",
                kind, parsed.get("answer"), bool(move), len(quote))
    return _out("saved", SAY_MOVED % move if move else SAY_SAVED)
