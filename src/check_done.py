"""Check done (step A): the device says which checklist items are still open.

Spec: docs/superpowers/specs/2026-10-20-check-done-gaps-design.md

At the moment he says "pre-pour check done" the last minutes of the check are
not transcribed yet: the open chunk is still on the device, the batch window
holds up to ~150 s, and the live extraction is throttled to 90 s. A window
written by the normal pipeline appears 2-5 minutes after the words, when he
has left the pour. So this does not wait. It reads what is already
transcribed, hears the session's uploaded chunks that no stored transcript
covers yet itself -- ephemeral, never written back, so the batch ledger (whose
failure mode is audio that silently vanishes) is not touched -- and says how
far its evidence reaches.

The gap rule is step B's exactly: the model proposes rows with verbatim
evidence and `checklist.rebuild` keeps only those whose evidence is in the
same transcript text the model was shown. A "not covered" here is the same
"not covered" the report will show tomorrow.

Everything outside this process (the DB hop, S3, the STT vendor, the model) is
injected, so the rules are tested without AWS or the network.
"""
import calendar
import difflib
import io
import json
import logging
import re
import time
import wave
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta

import batch_stitch
import checklist
import inspection_match
import nz_time
from transcript_utils import (extract_base_time_from_filename, extract_chunk_index_from_filename,
                              extract_session_id_from_filename)

logger = logging.getLogger()
logger.setLevel(logging.INFO)

# Newest uncovered chunks heard per ask: 8 x 30 s = 4 min, the longest the
# batch window plus the device's open chunk can leave unwritten. Older
# uncovered audio means something upstream is stuck; the answer says partial.
MAX_NEW_CHUNKS = 8
# One STT post per chunk, single attempt. A lost question is re-askable, and a
# retry spends the seconds the answer needs (elevenlabs_utils.stt_short).
STT_TIMEOUT_S = 12.0
# The whole STT phase, read against one clock: futures are collected with
# result(timeout=what is left), so eight slow posts cost 14 s, not 8 x 12.
STT_PHASE_S = 14.0
LLM_DEADLINE_S = 12
LLM_MAX_TOKENS = 4000
# With no live window to start from, the model looks for the start of the
# check in this much transcript before the newest evidence.
LOOKBACK_MIN = 45
# Evidence older than this before the press means the answer is "as of" then.
STALE_S = 20
SAY_MAX_ITEMS = 5
CLAUSE_MAX_WORDS = 12

# Not in inspection_match.GENERIC (they never appear in a template name, so
# they never needed to be), but every "done" phrase carries one -- and an
# unmatched spoken word halves the match score.
_DONE_PHRASES = re.compile(r"\b(what'?s|what is)\s+left\s+(on|of|in)\b", re.IGNORECASE)
_DONE_WORDS = re.compile(r"\b(done|finished|finish|complete|completed|checklists?|checks?)\b",
                         re.IGNORECASE)
_CLAUSE_END = re.compile(r"[,;:.(]|\s[-–—]\s")


# ---- which check ------------------------------------------------------------

def strip_done_words(name):
    """The spoken check name without the words that say it is finished."""
    text = _DONE_PHRASES.sub(" ", name or "")
    text = _DONE_WORDS.sub(" ", text)
    return " ".join(text.replace(".", " ").split()).strip(" ,;:!?")


def checklist_items(template):
    """Every checklist item of the template, sections in template order (one
    numbered list: the gap pass is one model call, spec "Gap pass")."""
    out = []
    for items in checklist.checklist_sections((template or {}).get("body")).values():
        out.extend(items)
    return out


def _usable(template):
    return bool(template) and bool(checklist_items(template))


def _closest(spoken, templates, n=2):
    """The names of the n checklist templates nearest to what he said."""
    sw = inspection_match.words(spoken)
    ranked = sorted(
        (t for t in templates if _usable(t)),
        key=lambda t: (-inspection_match.score(sw, inspection_match.template_words(t)),
                       -difflib.SequenceMatcher(None, (spoken or "").lower(),
                                                (t.get("name") or "").lower()).ratio(),
                       t.get("name") or ""))
    return [t.get("name") for t in ranked[:n]]


def _latest(windows, template_id=None):
    hits = [w for w in windows or []
            if w.get("template_id") and (template_id is None or str(w["template_id"]) == str(template_id))]
    return max(hits, key=lambda w: w.get("start_at") or "") if hits else None


def choose_template(check_name, templates, live_windows):
    """(template, live window, closest names). The spoken name first, matched the
    way item-writer matches a spoken check (inspection_match); then the session's
    latest live window that matched a template. A template with no checklist
    items cannot have gaps, so it is no template."""
    name = strip_done_words(check_name)
    tpl = None
    if inspection_match.words(name):
        tpl, _score = inspection_match.match({"kind": name, "name": name}, templates or [])
    if tpl is not None:
        window = _latest(live_windows, tpl.get("id"))
    else:
        window = _latest(live_windows)
        if window is not None:
            tpl = next((t for t in templates or [] if str(t.get("id")) == str(window["template_id"])),
                       None)
    if not _usable(tpl):
        return None, None, _closest(name, templates or [])
    return tpl, window, []


# ---- how far the stored transcript reaches -----------------------------------

_BATCH_COUNT = re.compile(r"_bn(\d+)(?=[_.]|$)")


def chunk_index(key):
    return extract_chunk_index_from_filename((key or "").rsplit("/", 1)[-1])


def last_covered_index(stored_keys, batch_maps):
    """The highest chunk index any stored transcript covers, or None.

    By index, not by presence: with DROP_SILENT_CHUNKS a silent chunk never gets
    a transcript, so a hole below the highest index is silence, not a gap. A
    batch covers every member in its map; a batch whose map is unreadable is
    credited only with what its name proves (first index + count - 1), which can
    make us hear a chunk twice but never skip one."""
    best = None
    for key in stored_keys or []:
        idx = chunk_index(key)
        if idx is None:
            continue
        top = idx
        if batch_stitch.is_batch_key(key):
            members = ((batch_maps or {}).get(key) or {}).get("members") or []
            listed = [int(m["chunk_index"]) for m in members
                      if isinstance(m, dict) and m.get("chunk_index") is not None]
            if listed:
                top = max(listed)
            else:
                m = _BATCH_COUNT.search(key)
                top = idx + max(int(m.group(1)), 1) - 1 if m else idx
        best = top if best is None else max(best, top)
    return best


# ---- which chunks still need hearing ----------------------------------------

def select_new_chunks(audio_keys, session_id, covered):
    """(chosen, skipped): this session's raw .wav chunks above `covered`, the
    newest MAX_NEW_CHUNKS in index order, and how many older ones were left."""
    mine = []
    for key in audio_keys or []:
        name = key.rsplit("/", 1)[-1]
        if not name.lower().endswith(".wav"):
            continue
        if extract_session_id_from_filename(name) != session_id:
            continue
        idx = extract_chunk_index_from_filename(name)
        if idx is None or (covered is not None and idx <= covered):
            continue
        mine.append((idx, key))
    mine.sort()
    chosen = [k for _, k in mine[-MAX_NEW_CHUNKS:]]
    return chosen, max(0, len(mine) - MAX_NEW_CHUNKS)


def wav_seconds(data):
    """Duration from the WAV header, or None when it is not a readable WAV."""
    try:
        with wave.open(io.BytesIO(data), "rb") as w:
            rate = w.getframerate()
            return w.getnframes() / float(rate) if rate else None
    except Exception:                                 # noqa: BLE001 -- any unreadable header
        return None


def _epoch_from_nz(local):
    utc = nz_time.from_nz(local)
    return calendar.timegm(utc.timetuple()) if utc is not None else None


def chunk_start_local(key):
    """The chunk's own NZ wall-clock start (its filename's HH-MM-SS is that
    chunk's true start, transcript_utils' chunk/session note)."""
    return extract_base_time_from_filename((key or "").rsplit("/", 1)[-1])


def chunk_end_epoch(key, seconds):
    start = chunk_start_local(key)
    if start is None:
        return None
    return _epoch_from_nz(start + timedelta(seconds=seconds or 0.0))


# ---- what is said -------------------------------------------------------------

def first_clause(text):
    clause = _CLAUSE_END.split(text or "", maxsplit=1)[0].strip() or (text or "").strip()
    return " ".join(clause.split()[:CLAUSE_MAX_WORDS])


def _items(n, word="item"):
    return "%d %s%s" % (n, word, "" if n == 1 else "s")


def say_ok(label, total, not_covered, failed, partial):
    """Built here, not by the model: the count and the item wording are the
    customer's, and a model paraphrasing them aloud is the same false record
    checklist.py refuses to write."""
    read_open = not_covered[:SAY_MAX_ITEMS]
    read_failed = failed[:max(0, SAY_MAX_ITEMS - len(read_open))]
    unread = (len(not_covered) - len(read_open)) + (len(failed) - len(read_failed))
    parts = []
    if not_covered:
        line = "%s open on %s: %s" % (_items(len(not_covered)), label,
                                      "; ".join(first_clause(i["text"]) for i in read_open))
        if len(not_covered) > len(read_open):
            line += "; and %d more" % (len(not_covered) - len(read_open))
        parts.append(line + ".")
    else:
        parts.append("All %s on %s are covered." % (_items(total), label))
    if failed:
        line = ("And %d marked No" if not_covered else "%d marked No") % len(failed)
        if read_failed:
            line += ": " + "; ".join(first_clause(i["text"]) for i in read_failed)
            if len(failed) > len(read_failed):
                line += "; and %d more" % (len(failed) - len(read_failed))
        parts.append(line + ".")
    if unread:
        parts.append("See the app for the rest.")
    said = " ".join(parts)
    if partial:
        said = "As of a minute ago, " + said[:1].lower() + said[1:]
    return said


def say_no_template(name, closest):
    if not closest:
        return "I couldn't find a checklist set up for this check."
    what = " and ".join(closest) if len(closest) == 2 else closest[0]
    return ("I couldn't find a checklist for %s. The closest %s %s."
            % (name or "that check", "are" if len(closest) == 2 else "is", what))


def say_no_recording():
    return "I can't find a recording running for you just now, so there's nothing to check yet."


def say_error():
    return "I couldn't check the list just now; it will be in today's report."
