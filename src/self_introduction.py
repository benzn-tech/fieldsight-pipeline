"""Self-introduction -> name suggestion: the pattern matcher.

A person introducing themselves ("Hi, this is Petros from Cassidy") is the best
enrolment material there is -- named, single-speaker, and something people do on site
every day. This module finds those moments in a session's turns. It does nothing with
them: no I/O, no database, no logging of transcript text. `lambda_extract_session` calls
`find()` and rides the result on the extraction artifact (spec §2); `lambda_item_writer`
stores it (spec §4); org-api turns a confirmation into a `speaker_corrections` call
(spec §5). The queue this feeds asks a human "who is this new voice?" -- it never names
anyone by itself (design doc, "What it is not").

**Reads `speaker`, not `speaker_label`.** `transcript_utils._build_turn` emits
`{speaker, text, start_sec, end_sec, ...}`; `assemble_session_turns`
(`lambda_extract_session.py`) adds `source_filename` and does not rename the key --
that rename happens only on the way OUT, onto the artifact's `speaker_turns` field. A
version of this module (or of `lambda_extract_session`'s own `speaker_turns` field, once)
that read `speaker_label` off the INPUT got an empty result on every session, silently --
see the plan's "Spec corrections" #1 and `lambda_extract_session.py`'s own comment beside
`speaker_turns`. `find()` emits `speaker_label` on its OUTPUT because that is the
downstream vocabulary (the artifact, the repository, the bell).
"""
import re

# ---------------------------------------------------------------------------
# English
# ---------------------------------------------------------------------------

_NAME_TOKEN = r"[A-Z][a-zA-Z'\u2019\-]*"
_NAME_GROUP = rf"({_NAME_TOKEN}(?:\s+{_NAME_TOKEN}){{0,2}})"

_GREETING_RE = re.compile(
    r"(?i:^\s*(hi|hello|hey|morning|g'day|good\s+(morning|afternoon))\b)")
_MY_NAME_RE = re.compile(rf"(?i:\bmy name(?:'s|\s+is))\s+{_NAME_GROUP}")
_IM_RE = re.compile(rf"(?i:\bI(?:'m|\s+am))\s+{_NAME_GROUP}")
_THIS_IS_RE = re.compile(rf"(?i:\bthis is)\s+{_NAME_GROUP}")
_INTRODUCE_MYSELF_RE = re.compile(r"(?i:introduce myself)")
_FOLLOW_CONTEXT_RE = re.compile(r"^\s*(,|\.|(?i:from\b|with\b|at\b))")
# A company name may carry an ampersand ("Smith & Sons") that a person's name never does,
# so this is its own pattern rather than a reuse of _NAME_GROUP.
_COMPANY_RE = re.compile(
    rf"(?i:\b(?:from|with|at)\b)\s+([A-Z][a-zA-Z&'\u2019\-]*(?:\s+[A-Z][a-zA-Z&'\u2019\-]*){{0,2}})")

# Filler/verb words that are never a name, even capitalised at a sentence start. A single-
# token candidate matching one of these is rejected outright (spec correction 7: "I'm
# here" is a stop phrase, not a following-context word).
_STOP_PHRASES = {"going", "sure", "here", "sorry", "done", "not", "just", "off", "back",
                 "good", "fine", "on", "in", "at"}

# Places and things the ASR capitalises the way it capitalises a name (spec correction 7,
# measured against real site speech: "Hi, this is Level 2 east", "this is Block C").
_STOP_FIRST_TOKENS = {"level", "block", "site", "stage", "grid", "zone", "room", "unit",
                       "team", "bay", "tower", "recording",
                       "monday", "tuesday", "wednesday", "thursday", "friday",
                       "saturday", "sunday"}

# Decision 3 (owner, 2026-09-29): a capitalised phrase ending in a company word is a
# company, not a person -- "Hello, this is Cassidy Construction" is rejected, not offered
# for a human to reject.
_COMPANY_SUFFIXES = {"construction", "builders", "electrical", "plumbing",
                      "ltd", "limited", "group", "services"}


def _validate_latin_name(raw, rest_of_text):
    """`raw` is the regex capture; `rest_of_text` is everything after it in the turn.

    Returns the cleaned name, or None if it fails any of the site-speech guards above.
    """
    tokens = raw.split()
    if not tokens or len(tokens) > 3:
        return None
    first = tokens[0].lower().strip("'\u2019")
    if len(tokens) == 1 and first in _STOP_PHRASES:
        return None
    if first in _STOP_FIRST_TOKENS:
        return None
    last = tokens[-1].lower().rstrip(".").strip("'\u2019")
    if last in _COMPANY_SUFFIXES:
        return None
    # A digit immediately after the name means it was never a name ("Level 2", "Block 5").
    if re.match(r"\s*\d", rest_of_text):
        return None
    return " ".join(tokens)


def _find_company(text, pos):
    m = _COMPANY_RE.search(text, pos)
    return m.group(1) if m else None


def _detect_english(text):
    if _GREETING_RE.match(text):
        m = _THIS_IS_RE.search(text)
        if m:
            name = _validate_latin_name(m.group(1), text[m.end():])
            if name:
                return {"heard_name": name, "company_name": _find_company(text, m.end())}

    m = _MY_NAME_RE.search(text)
    if m:
        name = _validate_latin_name(m.group(1), text[m.end():])
        if name:
            return {"heard_name": name, "company_name": _find_company(text, m.end())}

    m = _IM_RE.search(text)
    if m:
        name = _validate_latin_name(m.group(1), text[m.end():])
        if name:
            followed = bool(_FOLLOW_CONTEXT_RE.match(text[m.end():]))
            introduced = bool(_INTRODUCE_MYSELF_RE.search(text[:m.start()]))
            if followed or introduced:
                return {"heard_name": name, "company_name": _find_company(text, m.end())}

    return None


# ---------------------------------------------------------------------------
# Mandarin
# ---------------------------------------------------------------------------

_HAN = "\u4e00-\u9fff"
_WOJIAO_RE = re.compile(rf"\u6211\u53eb([{_HAN}]{{2,4}})")            # 我叫X
_WO_MINGZI_RE = re.compile(rf"\u6211\u7684\u540d\u5b57(?:\u662f|\u53eb)([{_HAN}]{{2,4}})")  # 我的名字(是|叫)X
_WOSHI_RE = re.compile(                                                # 我是X, only before 来自/从/，/。
    rf"\u6211\u662f([{_HAN}]{{2,4}})(?=\u6765\u81ea|\u4ece|\uff0c|\u3002)")
_COMPANY_HAN_RE = re.compile(rf"(?:\u6765\u81ea|\u4ece)([{_HAN}]{{2,6}})")  # (来自|从)company

# A name may not start with a pronoun -- "我叫他过来" ("I told him to come over") passes
# every English-shaped rule above translated literally; this is the guard the design doc's
# review added for it. 大家 ("everyone") gets the same treatment as a single-char pronoun.
_HAN_PRONOUNS = ("\u4ed6", "\u5979", "\u4f60", "\u5b83", "\u6211", "\u5927\u5bb6")


def _strip_han_spaces(text):
    """ASR sometimes emits Mandarin with a space between every character -- collapse
    only runs BETWEEN Han characters, so an English/Han mixed turn keeps its real spaces."""
    return re.sub(rf"(?<=[{_HAN}])\s+(?=[{_HAN}])", "", text)


def _valid_han_name(name):
    if not name or len(name) < 2:
        return False
    return not any(name.startswith(p) for p in _HAN_PRONOUNS)


def _detect_mandarin(text):
    t = _strip_han_spaces(text)
    for rx in (_WOJIAO_RE, _WO_MINGZI_RE):
        m = rx.search(t)
        if m and _valid_han_name(m.group(1)):
            cm = _COMPANY_HAN_RE.search(t, m.end())
            return {"heard_name": m.group(1), "company_name": cm.group(1) if cm else None}
    m = _WOSHI_RE.search(t)
    if m and _valid_han_name(m.group(1)):
        cm = _COMPANY_HAN_RE.search(t, m.end())
        return {"heard_name": m.group(1), "company_name": cm.group(1) if cm else None}
    return None


def _detect(text):
    if not text:
        return None
    return _detect_english(text) or _detect_mandarin(text)


def find(turns, min_turn_sec=3.0):
    """One suggestion per (source_filename, speaker_label): the first hit wins.

    `turns` is `assemble_session_turns` output -- each turn carries `speaker` (not
    `speaker_label`; see the module docstring), `text`, `start_sec`, `end_sec` and
    `source_filename`.

    Turns under `min_turn_sec` are skipped for DETECTION, not enrolment -- the confirm
    path (org-api) enrols on the cluster's longest turn regardless of which turn tripped
    detection (spec correction 5). 3 s is enough to catch a short "Hi, I'm Petros" and
    still ask a real question; the homogeneity guard is what refuses a window too short to
    enrol on, and it is never asked to judge 3 s of audio.
    """
    results = []
    seen = set()
    for t in turns:
        speaker = t.get("speaker")
        source_filename = t.get("source_filename")
        if not speaker or not source_filename:
            continue
        key = (source_filename, speaker)
        if key in seen:
            continue
        start_sec = t.get("start_sec")
        end_sec = t.get("end_sec")
        if start_sec is None or end_sec is None or (end_sec - start_sec) < min_turn_sec:
            continue
        text = t.get("text") or ""
        hit = _detect(text)
        if hit is None:
            continue
        seen.add(key)
        results.append({
            "source_filename": source_filename,
            "speaker_label": speaker,
            "start_sec": start_sec,
            "end_sec": end_sec,
            "heard_name": hit["heard_name"],
            "company_name": hit.get("company_name"),
            "quote": text[:200],
        })
    return results
