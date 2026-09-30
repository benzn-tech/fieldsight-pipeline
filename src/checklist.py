"""Checklist sections: the customer's questions, answered only from the recording.

Owner, 2026-09-30: inspection checklists (Penina site inspection, BICL site
establishment) are fixed question lists -- Yes / No / N/A, comment, person
responsible, target date. A checklist filled from a recording is only worth
anything if every answer can be traced to what was said, and an item nobody
addressed stays EMPTY. Filling an unchecked item with "Yes" is a false
compliance record, which is worse than no record.

The split (owner, 2026-09-29): the model reads the transcript and proposes
answers, each with the exact words that answer it; this module decides:

  * an answer whose evidence is not in the transcript is discarded;
  * an answer that is not Yes / No / N/A is discarded;
  * the table is rebuilt here, in the customer's order, with the customer's
    own item text -- never the model's paraphrase of it -- and every item the
    recording did not address left blank.

The model's row carries the item's number, not its text, so an item cannot be
reworded into something the customer did not ask.
"""
import difflib
import re

ANSWERS = {"yes": "Yes", "y": "Yes", "no": "No", "n": "No", "n/a": "N/A", "na": "N/A",
           "not applicable": "N/A"}
MODEL_COLUMNS = ["Item no", "Answer", "Comment", "Responsible", "Due", "Evidence"]
DOC_COLUMNS = ["Item", "Answer", "Comment", "Responsible", "Due"]
MAX_ITEMS = 120
MAX_ITEM_CHARS = 300
MIN_EVIDENCE_WORDS = 3
EVIDENCE_MATCH = 0.9

_WORD_RE = re.compile(r"[a-z0-9]+")
_TAG_RE = re.compile(r"\s*\[\s*(t\d+(?:\s*(?:,|and|&)\s*t\d+)*)\s*\]\s*$", re.IGNORECASE)


def shape_sentence():
    """How the model is asked to write a checklist section. Our words."""
    return ("as a markdown table with exactly these columns, in this order: "
            "%s. One row per checklist item THE RECORDING ACTUALLY ADDRESSED, "
            "where Item no is the item's number from the list under the heading. Answer "
            "is Yes, No or N/A. Evidence is the exact words from the transcript that "
            "answer the item, copied verbatim. Leave out every item the recording "
            "did not address -- do not guess, and do not answer from general "
            "expectation" % " | ".join(MODEL_COLUMNS))


def items_block(items):
    """The customer's items, numbered, for the plan (customer data)."""
    return "Checklist items:\n" + "\n".join("%d. %s" % (i + 1, it) for i, it in enumerate(items))


def validate_items(items, where):
    if not isinstance(items, list) or not items:
        return "%s: a checklist needs at least one item" % where
    if len(items) > MAX_ITEMS:
        return "%s: a checklist may have at most %d items" % (where, MAX_ITEMS)
    for i, it in enumerate(items):
        if not isinstance(it, str) or not it.strip():
            return "%s: checklist item %d is empty" % (where, i + 1)
        if len(it) > MAX_ITEM_CHARS:
            return "%s: checklist item %d is longer than %d characters" % (where, i + 1, MAX_ITEM_CHARS)
    return None


# ---- evidence -----------------------------------------------------------------

def _words(text):
    return _WORD_RE.findall((text or "").lower())


def evidence_found(quote, transcript_words):
    """True when `quote` is in the transcript: word for word after
    normalising case and punctuation, or near enough (a window of the same
    length at >= EVIDENCE_MATCH similarity) to forgive a dropped filler."""
    q = _words(quote)
    if len(q) < MIN_EVIDENCE_WORDS:
        return False
    n = len(q)
    joined_q = " ".join(q)
    t = transcript_words
    for start in range(0, max(1, len(t) - n + 1)):
        window = t[start:start + n]
        if window == q:
            return True
    best = 0.0
    sm = difflib.SequenceMatcher(autojunk=False)
    sm.set_seq2(joined_q)
    for start in range(0, max(1, len(t) - n + 1)):
        sm.set_seq1(" ".join(t[start:start + n]))
        if sm.real_quick_ratio() < EVIDENCE_MATCH or sm.quick_ratio() < EVIDENCE_MATCH:
            continue
        best = max(best, sm.ratio())
        if best >= EVIDENCE_MATCH:
            return True
    return False


# ---- rebuilding the section -----------------------------------------------------

def _cells(line):
    return [c.strip() for c in line.strip().strip("|").split("|")]


def _model_rows(paragraphs, line_refs):
    """(item number, cells, refs) for each data row of the model's table."""
    out = []
    for text, refs in zip(paragraphs, line_refs):
        if "|" not in text or re.match(r"^[\s|:-]+$", text):
            continue
        cells = _cells(text)
        if not cells or not re.match(r"^\d+$", cells[0]):
            continue                      # the header row, or anything unnumbered
        out.append((int(cells[0]), cells, refs))
    return out


def rebuild(section, items, transcript_text):
    """The checklist section rebuilt from the model's rows. Returns
    (paragraphs, line_refs, report). `section` is a _prose_sections dict."""
    t_words = _words(transcript_text)
    answered, report = {}, {"items": len(items), "answered": 0, "dropped": [],
                            "evidence": {}}
    for num, cells, refs in _model_rows(section.get("paragraphs") or [],
                                        section.get("line_refs") or []):
        cells = (cells + [""] * len(MODEL_COLUMNS))[:len(MODEL_COLUMNS)]
        _, answer, comment, responsible, due, quote = cells
        why = None
        if not 1 <= num <= len(items):
            why = "no such item"
        elif num in answered:
            why = "answered twice"
        elif (answer or "").strip().lower() not in ANSWERS:
            why = "answer is not Yes, No or N/A"
        elif not evidence_found(quote, t_words):
            why = "evidence not in the transcript"
        if why:
            report["dropped"].append({"item": num, "reason": why})
            continue
        answered[num] = (ANSWERS[answer.strip().lower()], comment, responsible, due, refs)
        report["evidence"][num] = quote
    report["answered"] = len(answered)
    paragraphs = [" | ".join(DOC_COLUMNS), " | ".join(["---"] * len(DOC_COLUMNS))]
    line_refs = [[], []]
    for i, item in enumerate(items, start=1):
        a = answered.get(i)
        if a:
            ans, comment, responsible, due, refs = a
            row = [item, ans, comment, responsible, due]
        else:
            refs, row = [], [item, "", "", "", ""]
        paragraphs.append(" | ".join(c.replace("|", "/") for c in row))
        line_refs.append(list(refs))
    return paragraphs, line_refs, report


def checklist_sections(template):
    """{lower-cased title: items} for every checklist section of a template,
    sub-sections included."""
    out = {}
    def walk(sections):
        for s in sections or []:
            if str(s.get("kind") or "").strip().lower() == "checklist" and s.get("items"):
                out[(s.get("title") or "").strip().lower()] = list(s["items"])
            walk(s.get("children"))
    walk((template or {}).get("sections"))
    return out


def apply(prose, template, transcript_text):
    """Rebuild every checklist section of the answer in place. Returns a report
    per section title, for the result's provenance.

    A checklist the model left out entirely (nothing in the recording
    addressed it) is still a form the customer expects to see whole: it is
    added here with every item blank, before the catch-all heading."""
    wanted = checklist_sections(template)
    reports, seen = {}, set()
    for sec in prose:
        key = (sec.get("title") or "").strip().lower()
        items = wanted.get(key)
        if not items:
            continue
        seen.add(key)
        paragraphs, line_refs, report = rebuild(sec, items, transcript_text)
        sec["paragraphs"], sec["line_refs"] = paragraphs, line_refs
        reports[sec["title"]] = report
    catch_all = ((template or {}).get("catch_all") or {}).get("title", "").strip().lower()
    for key, items in wanted.items():
        if key in seen:
            continue
        title = next((s.get("title") for s in _all(template) if (s.get("title") or "").strip().lower() == key), key)
        empty = {"title": title, "paragraphs": [], "line_refs": [], "level": 1, "covers": []}
        empty["paragraphs"], empty["line_refs"], report = rebuild(empty, items, transcript_text)
        report["omitted_by_model"] = True
        at = next((i for i, s in enumerate(prose)
                   if (s.get("title") or "").strip().lower() == catch_all), len(prose))
        prose.insert(at, empty)
        reports[title] = report
    return reports


def _all(template):
    out = []
    def walk(sections):
        for s in sections or []:
            out.append(s)
            walk(s.get("children"))
    walk((template or {}).get("sections"))
    return out
