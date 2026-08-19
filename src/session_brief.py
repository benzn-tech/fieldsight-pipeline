"""session_brief.py — the session's narrative record, and the structure derived FROM it.

The pipeline's ordering is inverted today: a session is turned into
`topics[] -> {action_items, findings, ...}` and that JSON is what every surface
reads, while the transcript it came from is never read again. Measured on one
real 71-minute session, the extraction keeps 5.0% of the transcript's characters
and two of its numbers; the names, prices and channels the meeting was actually
about have no field to land in.

This module writes the narrative FIRST and lets structure fall out of it. It is a
drop-in for `lambda_rolling_summary.summarize_turns` -- same call shape, and the
returned dict still carries `summary` and `open_todos`, so the confirmation email
needs no change at all. Everything else in the dict is additional.

Pure at import: `llm_utils` is imported lazily inside the call, mirroring
rolling_summary, so importing this never reads an API key.
"""
import difflib
import json
import logging
import re

logger = logging.getLogger()

MAX_TOKENS = 8000
TRANSCRIPT_LIMIT = 300000     # matches lambda_extract_session's head+tail budget

# Alias validation (see validate_aliases). A word list is a maintenance
# liability and wrong in a second language, so "common" is measured against the
# corpus at hand rather than hardcoded -- a token appearing in this share of the
# session's turns is a word, not a name.
COMMON_DF_SHARE = 0.02
_TOKEN = re.compile(r"[a-z][a-z'-]{2,}")


def render_turns(turns, limit=TRANSCRIPT_LIMIT):
    """`[HH:MM:SS] speaker: text` lines, head+tail elided if over budget."""
    lines = [f"[{t.get('abs_start_str', '')}] {t.get('speaker', '?')}: {t.get('text', '')}"
             for t in turns]
    text = "\n".join(lines)
    if len(text) <= limit:
        return text
    head, tail = int(limit * 0.6), limit - int(limit * 0.6)
    return text[:head] + "\n[... middle of the session elided to fit ...]\n" + text[-tail:]


def build_brief_prompt(turns):
    return f"""You are briefing a senior colleague who was NOT in this meeting. They have a
few minutes, and afterwards they have to be able to discuss any part of it.

Below is the full transcript, one line per speaker turn as [HH:MM:SS] speaker: text.
It comes from automatic speech recognition, so expect misheard words and filler.

Return ONLY JSON, no code fence and no commentary:

{{
  "headline": "One sentence on what this meeting was actually about and what came out of it. Concrete -- not 'discussed product strategy'.",
  "sections": [
    {{
      "title": "Short subject line for this section, 3-8 words",
      "bullets": [
        {{
          "text": "One or two sentences. Carry the actual names, companies, products, numbers and amounts -- those ARE the information, not decoration. A reader must finish this line knowing what happened, without opening anything.",
          "at": "HH:MM:SS where this sits in the transcript",
          "quote": "One supporting line copied VERBATIM from the transcript above. Do not rewrite it."
        }}
      ]
    }}
  ],
  "entities": [
    {{
      "name": "The CORRECT spelling of a proper noun, e.g. PB Tech, even if the transcript misspells it",
      "aliases": ["the spelling that ACTUALLY appears in the transcript, e.g. PV Tech. Empty array if it was heard correctly"],
      "kind": "product | company | person | standard | place | metric",
      "note": "One sentence on its role in this meeting, with the relevant numbers"
    }}
  ],
  "tasks": [
    {{
      "text": "One sentence on what needs doing, written for whoever will do it.",
      "why": "What happened in the meeting that produced this",
      "at": "HH:MM:SS",
      "assignee": "The name it was given to, or null. Do not guess.",
      "due": "When, in the words used, or null",
      "basis": "committed if someone took it on; inferred if you concluded it should be done"
    }}
  ]
}}

How to write it:

1. **Cover the whole session.** Any subject discussed for more than about two
   minutes needs its own section or at least a bullet. Missing a whole stretch of
   the conversation is the worst thing this brief can do.
2. **No telegraphic style.** "Procurement strategy -- productize as standard IT"
   is a failure: the reader cannot tell what to do with it.
3. **Numbers, amounts, model names, standard codes and proper nouns stay exactly
   as spoken.** Losing one is losing the line.
4. **entities: be generous.** Anything someone might later search for belongs
   here, including a name said once. Aliases matter -- recognition mangles proper
   nouns constantly, and a literal search for the correct spelling then finds
   nothing.
5. **tasks: only what a specific person could finish and tick off.** Test the
   verb. "consult Xiao Han", "replace the damaged doors", "call the electrician"
   are tasks. "focus on X", "consider Y", "explore Z" are directions -- nobody can
   ever tick them, and they belong in a bullet. A discussion that reached no act
   yields NO tasks; an empty array is correct, and two real tasks are worth more
   than six invented ones, because invented ones bury the real ones.
6. Write in the language the meeting was mostly held in.

---

{render_turns(turns)}
"""


def parse_brief(raw):
    """Tolerant JSON parse -- fences, or an object embedded in prose. Mirrors
    lambda_rolling_summary.parse_rolling_summary's tolerance because the same
    models produce the same wrappers. Returns a dict or None."""
    if not raw:
        return None
    text = raw.strip()
    if text.startswith("```"):
        text = re.sub(r"^```[a-zA-Z]*\n?", "", text)
        text = re.sub(r"\n?```$", "", text).strip()
    for candidate in (text, None):
        if candidate is None:
            m = re.search(r"\{.*\}", text, re.S)
            candidate = m.group(0) if m else None
            if candidate is None:
                break
        try:
            data = json.loads(candidate)
        except (ValueError, TypeError):
            continue
        if isinstance(data, dict):
            return data
    logger.warning("session_brief: no usable JSON object in the model's reply")
    return None


def _doc_frequency(turns):
    """How many turns each word appears in. This is the corpus itself telling us
    which words are common, so the alias rule below needs no maintained list and
    does not fall over in a second language."""
    df = {}
    for t in turns:
        for w in set(_TOKEN.findall((t.get("text") or "").lower())):
            df[w] = df.get(w, 0) + 1
    return df, max(len(turns), 1)


def validate_aliases(entities, turns):
    """Drop alias guesses that are not spellings of their entity.

    The model is good at spotting that `PV Tech` in the transcript means PB Tech,
    and that recovery matters -- a literal index answers zero for the string a
    person would type. But left unchecked it also offered `Claude` as a spelling
    of `Plaud`, which folds every mention of the AI tool into a competitor
    device, and `record include` as a spelling of `Riccarton clinic`.

    Two rules remove both classes:
      1. an alias may not BE another entity's canonical name;
      2. an alias made only of words this session uses constantly is a misheard
         phrase, not a name.

    Returns (entities, rejected) with rejected carrying the reason, so a bad rule
    shows up in a log line instead of silently eating mentions.
    """
    canon = {(e.get("name") or "").strip().lower()
             for e in entities if (e.get("name") or "").strip()}
    df, n_turns = _doc_frequency(turns)
    common = {w for w, c in df.items() if c >= max(2, n_turns * COMMON_DF_SHARE)}

    out, rejected = [], []
    for e in entities:
        name = (e.get("name") or "").strip()
        if not name:
            continue
        kept = []
        for alias in (e.get("aliases") or []):
            a = (alias or "").strip()
            low = a.lower()
            if len(a) < 2 or low == name.lower():
                continue
            if low in canon:
                rejected.append({"name": name, "alias": a,
                                 "reason": "collides with another entity"})
                continue
            words = _TOKEN.findall(low)
            if words and all(w in common for w in words):
                rejected.append({"name": name, "alias": a,
                                 "reason": "made only of words common in this session"})
                continue
            kept.append(a)
        out.append(dict(e, name=name, aliases=kept))
    return out, rejected


def _snap_to_quote(quote, turns):
    """The timestamp of the turn this quote came from, or None."""
    probe = re.sub(r"\s+", " ", (quote or "").lower()).strip(" .\"\u201c\u201d")[:70]
    if len(probe) < 12:
        return None
    best, score = None, 0.0
    for t in turns:
        r = difflib.SequenceMatcher(None, probe, (t.get("text") or "").lower()[:160]).ratio()
        if r > score:
            best, score = t, r
    return best.get("abs_start_str") if best is not None and score >= 0.45 else None


def _snap_to_terms(text, turns, df):
    """For an item with no quote: the turn carrying the most of its rarest words."""
    words = [w for w in _TOKEN.findall((text or "").lower()) if 0 < df.get(w, 0) <= 25]
    if not words:
        return None
    probe = sorted(set(words), key=lambda w: df.get(w, 0))[:3]
    best, hits = None, 0
    for t in turns:
        low = (t.get("text") or "").lower()
        n = sum(1 for w in probe if w in low)
        if n > hits:
            best, hits = t, n
    return best.get("abs_start_str") if hits >= 2 else None


def reanchor(brief, turns):
    """Re-derive every `at` from the transcript instead of trusting the model.

    The model writes timestamps from recollection and gets them wrong: on the
    session this was built from, the photo-linking discussion came back at
    13:33:11 when the audio has it at 14:33:11. An anchor that jumps an hour
    makes the feature it exists for useless, so each one is matched back -- by
    the quote the model itself cited, or for a task by its rarest words.

    Anything unmatchable keeps its stated time and is COUNTED, so drift stays
    visible rather than silently plausible.
    """
    df, _ = _doc_frequency(turns)
    fixed = missed = 0
    for section in brief.get("sections") or []:
        for b in section.get("bullets") or []:
            at = _snap_to_quote(b.get("quote"), turns)
            if at is None:
                missed += 1
            elif at != b.get("at"):
                b["at"] = at
                fixed += 1
    for task in brief.get("tasks") or []:
        at = _snap_to_terms(f"{task.get('text', '')} {task.get('why', '')}", turns, df)
        if at is None:
            missed += 1
        elif at != task.get("at"):
            task["at"] = at
            fixed += 1
    return {"reanchored": fixed, "unmatched": missed}


def to_session_summary(brief):
    """The two keys the confirmation email already reads, derived from the brief.

    Keeping these means `_complete_summary` can hand a brief straight to
    `build_confirmation_email` and nothing downstream changes. `open_todos` takes
    the same {text, responsible, due} shape `_clean_todos` normalises.
    """
    todos = [{"text": (t.get("text") or "").strip(),
              "responsible": t.get("assignee") or None,
              "due": t.get("due") or None}
             for t in (brief.get("tasks") or []) if (t.get("text") or "").strip()]
    return {"summary": (brief.get("headline") or "").strip(), "open_todos": todos}


def brief_from_turns(turns, call_llm=None):
    """Drop-in for lambda_rolling_summary.summarize_turns.

    Returns the brief WIDENED with `summary` and `open_todos`, or None on any
    failure so the caller falls back exactly as it does today. `call_llm` is
    injectable for tests; llm_utils is imported lazily so this module stays pure
    at import.
    """
    if not turns:
        return None
    if call_llm is None:
        import llm_utils
        call_llm = llm_utils.call_llm
    # Thinking ON, explicitly, not inherited. SessionFinalizeFunction carries
    # QWEN_ENABLE_THINKING=false -- set when the summariser here was the terse
    # rolling one and 54s of reasoning blew a 2-minute budget for two sentences.
    # A brief is the opposite trade: measured at ~100-125s with thinking on for
    # a 70-minute session, and that is where its density comes from. Inheriting
    # the env would mean every number this was designed against was measured on
    # a configuration that never shipped.
    raw, _err = call_llm(build_brief_prompt(turns), max_tokens=MAX_TOKENS,
                         force_json=True, enable_thinking=True)
    brief = parse_brief(raw)
    if not brief:
        return None
    entities, rejected = validate_aliases(brief.get("entities") or [], turns)
    brief["entities"] = entities
    anchor_stats = reanchor(brief, turns)
    brief["stats"] = dict(anchor_stats, aliases_rejected=len(rejected))
    if rejected:
        logger.info("session_brief: rejected %d alias guess(es): %s",
                    len(rejected), "; ".join(f"{r['name']}<-{r['alias']} ({r['reason']})"
                                             for r in rejected))
    logger.info("session_brief: %d section(s), %d bullet(s), %d entit(ies), %d task(s); "
                "%d anchor(s) corrected, %d unmatched",
                len(brief.get("sections") or []),
                sum(len(s.get("bullets") or []) for s in brief.get("sections") or []),
                len(entities), len(brief.get("tasks") or []),
                anchor_stats["reanchored"], anchor_stats["unmatched"])
    brief.update(to_session_summary(brief))
    return brief
