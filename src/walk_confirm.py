"""A confirmation answer from the device (multi-unit walks, Task 11).

After the final check asks "Unit 3: wall bracing -- pass?", the inspector holds
the Ask key and answers. This parses the answer, has the in-VPC hop store it as
report evidence (walk_answers, with the transcript as its quote), and asks for
the walk report to be queued again. The device keeps the question list and asks
the next question itself.

It is never general Ask: an answer that is not pass / fail / N/A / a move is
skipped, and the question stays on the phone.

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

_MOVE_AFTER = re.compile(r"\b(?:no|wrong|that was|thats)\b")
_YES = {"pass", "passed", "passes", "yes", "good", "ok", "okay", "right", "correct"}
_CONFIRM = {"right", "correct", "yes"}
_NO = {"fail", "failed", "fails", "no"}
_NA = re.compile(r"\bnot applicable\b|\bn a\b|\bna\b|\bdoesnt apply\b|\bdoes not apply\b")
_NO_REST = re.compile(r"^\W*(?:failed|fails|fail|no)\b[\W_]*(.*)$", re.IGNORECASE | re.DOTALL)


def _norm(text):
    """Lower case, apostrophes dropped ("that's" -> "thats"), every other
    punctuation mark a space ("n/a" -> "n a"), spaces collapsed."""
    t = (text or "").lower().replace("'", "").replace("’", "")
    return " ".join(re.sub(r"[^\w\s]|_", " ", t).split())


def _move_to(norm, units):
    """The walk unit named after "no" / "wrong" / "that was" / "that's", or None."""
    walk = {units_mod.normalize_unit(u) for u in units or [] if isinstance(u, str)}
    walk.discard(None)
    for m in _MOVE_AFTER.finditer(norm):
        u = units_mod.normalize_unit(norm[m.end():])
        if u and u in walk:
            return u
    return None


def parse_answer(text, kind, units):
    """The answer in `text`, by the first rule that applies, or None (skip):

    1. a unit of the walk named after no / wrong / that was / that's -> a move
       (a read-back No only);
    2. it starts with pass / yes / good / ok / right / correct -> Yes; for a
       read-back No, right / correct / yes confirm the No;
    3. it starts with fail / no / failed -> No, the rest is its comment;
    4. not applicable / n a / na / doesn't apply -> N/A;
    5. anything else -> None."""
    norm = _norm(text)
    if not norm:
        return None
    if kind == "confirm_no":
        target = _move_to(norm, units)
        if target:
            return {"move_to": target}
    first = norm.split()[0]
    if kind == "confirm_no" and first in _CONFIRM:
        return {"answer": "No", "confirmed": True}
    if first in _YES:
        return {"answer": "Yes"}
    if first in _NO:
        m = _NO_REST.match((text or "").strip())
        return {"answer": "No", "comment": (m.group(1) if m else "").strip()}
    if _NA.search(norm):
        return {"answer": "N/A"}
    return None


def _named_units(text):
    """Every unit the answer names after a move word: the candidates a move may
    go to. Whether one is the walk's is the hop's to say (it reads the walk)."""
    norm = _norm(text)
    out = []
    for m in _MOVE_AFTER.finditer(norm):
        u = units_mod.normalize_unit(norm[m.end():])
        if u and u not in out:
            out.append(u)
    return out


def _out(status, say):
    return {"status": status, "say": say, "next": None}


def _heard(event, stt):
    """(transcript, None) or (None, reason)."""
    if isinstance(event.get("text"), str):
        return event["text"].strip(), None
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
    except Exception:                                 # noqa: BLE001 -- a person is waiting
        logger.warning("walk-confirm: stt failed", exc_info=True)
        return None, "stt"


def _row(event, parsed, quote, answered_at):
    move = parsed.get("move_to")
    return {"action": "write_walk_answer", "caller_sub": event["caller_sub"],
            "walk_key": event["walk_key"], "unit": event["unit"],
            "item_text": event["item_text"],
            "answer": None if move else parsed["answer"],
            "comment": parsed.get("comment") or "", "move_to_unit": move,
            "quote": quote, "answered_at": answered_at}


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
    except Exception:                                 # noqa: BLE001 -- a person is waiting
        logger.warning("walk-confirm: write failed", exc_info=True)
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
