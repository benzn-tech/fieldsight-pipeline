"""A multi-unit walk's checklist, answered per unit (spec 2026-11-03).

The model writes one checklist table for the whole walk, each row carrying the
unit it is ABOUT. This module turns it into one checklist per unit, and every
unit's rows go through `checklist.rebuild` -- the same evidence rule, the same
customer wording and order as an ordinary check. On top of that:

  * "this one's all good" (Item no ALL, Answer Yes) fills only that unit's
    unanswered items, each saying so: covered by '<quote>' (HH:MM). It never
    overwrites a No, and with unit stretches it must have been said inside
    that unit's stretch;
  * a finding that names no unit goes to General -- kept, never guessed into
    a unit -- if its evidence is in the transcript;
  * a unit named in the opening with no rows still gets its section, every
    item not checked; a unit not named in the opening is flagged.

`rows` is the structured form of each unit's table (each row's `origin` is
"recording"; the on-site overlay changes it); `render` turns rows into
the table and `summary` into the unit-by-item grid, so a later step that
rewrites rows (the on-site overlay) re-renders the same way.

Pure: no database, no network.
"""
import re

import checklist
import units as units_mod

MODEL_COLUMNS = ["Unit", "Item no", "Answer", "Comment", "Responsible", "Due", "Evidence"]
GENERAL = "General"
ALL = "ALL"
OUTSIDE = "all good outside the unit"
NO_EVIDENCE = "evidence not in the transcript"
NOT_YES = "all good answer is not Yes"
_SUMMARY_CELL = {"not_checked": "Not checked", "check": "Check"}
_TIME_RE = re.compile(r"^\[(\d\d:\d\d(?::\d\d)?)")


def shape_sentence():
    """How the model is asked to write a walk's checklist. Our words."""
    return ("as a markdown table with exactly these columns, in this order: "
            "%s. One row per (unit, checklist item) THE RECORDING ACTUALLY ADDRESSED. "
            "Unit is the unit the finding is ABOUT, not the unit he happens to be "
            "standing in: a finding for \"back to Unit 2\" is Unit 2. Write the unit as "
            "its name and number, e.g. Unit 2 or Lot 150. Use Unit General for a "
            "finding that names no unit. Item no is the item's number from the list "
            "under the heading. When he says a unit is all good (\"this one's all "
            "good\"), write one row for that unit with Item no ALL and Answer Yes. "
            "Answer is Yes, No or N/A. Comment says briefly what was said about the "
            "item -- for a No, what is wrong. Responsible and Due are who will put "
            "right what is wrong and by when, only when that was said; otherwise leave "
            "them empty. Evidence is the exact words from the transcript that answer "
            "the item, copied verbatim. Leave out every item the recording did not "
            "address -- do not guess, and do not answer from general expectation"
            % " | ".join(MODEL_COLUMNS))


# ---- the model's rows, by unit --------------------------------------------------

def _header_cell(c):
    return " ".join(re.sub(r"[^a-z ]", " ", c.lower()).split())


def _is_header(cells):
    """"| Unit | Item no |", "| unit | Item No. |" and the like."""
    return [_header_cell(c) for c in cells[:2]] == ["unit", "item no"]


def _parsed_rows(text):
    """[(normalized unit or "General", pipe row without the Unit cell)], in
    row order."""
    out = []
    for line in (text or "").splitlines():
        if "|" not in line or re.match(r"^[\s|:-]+$", line):
            continue
        cells = checklist._cells(line)
        if _is_header(cells):
            continue
        cells = (cells + [""] * len(MODEL_COLUMNS))[:len(MODEL_COLUMNS)]
        unit = units_mod.normalize_unit(cells[0]) or GENERAL
        out.append((unit, "| " + " | ".join(cells[1:]) + " |"))
    return out


def rows_by_unit(text):
    """{normalized unit or "General": [pipe rows without the Unit cell]}, in
    the order units are first seen. Rows whose unit does not normalize go to
    General."""
    out = {}
    for unit, row in _parsed_rows(text):
        out.setdefault(unit, []).append(row)
    return out


# ---- rendering ------------------------------------------------------------------

def render(unit):
    """(paragraphs, line_refs) of a unit's table from its rows -- byte for byte
    what checklist.rebuild writes for the same answers."""
    paragraphs = [" | ".join(checklist.DOC_COLUMNS), " | ".join(["---"] * len(checklist.DOC_COLUMNS))]
    line_refs = [[], []]
    for r in unit["rows"]:
        row = [r["item"], r["answer"], r["comment"], r["responsible"], r["due"]]
        paragraphs.append(" | ".join(c.replace("|", "/") for c in row))
        line_refs.append(list(r["refs"]))
    return paragraphs, line_refs


def summary(units, items):
    """The unit-by-item grid: Yes / No / N/A when answered, else Not checked or
    Check."""
    rows = []
    for u in units:
        rows.append([u["unit"]] + [_SUMMARY_CELL.get(r["state"], r["answer"]) for r in u["rows"]])
    return {"columns": list(items), "rows": rows}


# ---- all good -------------------------------------------------------------------

def _matching_lines(quote, transcript_text):
    """[(line index, "HH:MM:SS" / "HH:MM" or None)] of every transcript line
    holding the quote, in transcript order."""
    out = []
    for i, line in enumerate((transcript_text or "").splitlines()):
        if checklist.evidence_found(quote, checklist.transcript_words(line)):
            m = _TIME_RE.match(line.strip())
            out.append((i, m.group(1) if m else None))
    return out


def _is_all(row):
    return checklist._cells(row)[0].upper() == ALL


def _all_good(model_rows, t_words, transcript_text, unit, stretches, claimed, dropped):
    """The first surviving ALL row as (quote, "HH:MM" or None), or None. Each
    ALL row that does not survive is added to `dropped`.

    The same words ("this one is all good") are often said in several units,
    so the line that times a quote is chosen per unit: with stretches, the
    first matching line inside this unit's stretch; without, the first
    matching line no earlier unit's all-good has claimed (`claimed`, line
    indices, updated here)."""
    chosen = None
    for line in model_rows:
        if not _is_all(line):
            continue
        cells = (checklist._cells(line) + [""] * 6)[:6]
        answer, quote = cells[1].strip().lower(), cells[5]
        at, why = None, None
        if checklist.ANSWERS.get(answer) != "Yes":
            why = NOT_YES
        elif not checklist.evidence_found(quote, t_words):
            why = NO_EVIDENCE
        else:
            lines = _matching_lines(quote, transcript_text)
            if stretches is not None:
                # An all-good that cannot be placed inside this unit's
                # stretch is not shown to be about this unit.
                hit = next((ln for ln in lines
                            if ln[1] and units_mod.unit_at(stretches, ln[1]) == unit), None)
                if hit is None:
                    why = OUTSIDE
            else:
                hit = next((ln for ln in lines if ln[0] not in claimed), None)
            if not why and chosen is None and hit is not None:
                claimed.add(hit[0])
                at = hit[1]
        if why:
            dropped.append({"item": ALL, "reason": why})
            continue
        if chosen is None:
            chosen = (quote, at[:5] if at else None)
    return chosen


def _apply_all_good(rows, quote, at, report):
    comment = "covered by '%s'" % quote + (" (%s)" % at if at else "")
    for r in rows:
        if r["state"] == "not_checked":
            r.update(answer="Yes", comment=comment, evidence=quote, refs=[], state="answered")
            report["evidence"][r["n"]] = quote
    report["answered"] = sum(1 for r in rows if r["state"] == "answered")
    report["gaps"]["not_covered"] = [{"n": r["n"], "text": r["item"]}
                                     for r in rows if r["state"] == "not_checked"]
    report["all_good"] = {"quote": quote, "at": at}


# ---- the walk -------------------------------------------------------------------

def _unit_section(unit, model_rows, items, transcript_text, good, all_dropped, report_date):
    section = {"paragraphs": list(model_rows), "line_refs": [[] for _ in model_rows]}
    paragraphs, line_refs, report = checklist.rebuild(section, items, transcript_text, report_date)
    rows = []
    for n, item in enumerate(items, start=1):
        _, answer, comment, responsible, due = paragraphs[n + 1].split(" | ")
        rows.append({"n": n, "item": item, "answer": answer, "comment": comment,
                     "responsible": responsible, "due": due,
                     "evidence": report["evidence"].get(n, ""), "refs": list(line_refs[n + 1]),
                     "state": "answered" if answer else "not_checked", "origin": "recording"})
    report["dropped"].extend(all_dropped)
    out = {"unit": unit, "rows": rows, "report": report}
    if good:
        _apply_all_good(rows, good[0], good[1], report)
        paragraphs, line_refs = render(out)
    out["paragraphs"], out["line_refs"] = paragraphs, line_refs
    return out


def _general(model_rows, t_words):
    kept, dropped = [], []
    for line in model_rows:
        cells = (checklist._cells(line) + [""] * 6)[:6]
        entry = {"comment": cells[2], "evidence": cells[5]}
        if checklist.evidence_found(entry["evidence"], t_words):
            kept.append(entry)
        else:
            dropped.append(dict(entry, reason=NO_EVIDENCE))
    return kept, dropped


def build(model_text, items, transcript_text, opening, stretches=None, report_date=None):
    """The walk's checklists: {"units", "general", "general_dropped", "summary"}.

    `opening` is the window's units dict ({"names", "count"}); only names are
    flagged against. Units come in walk order: the opening's names, then units
    first seen in the rows."""
    parsed = _parsed_rows(model_text)
    grouped = {}
    for u, row in parsed:
        grouped.setdefault(u, []).append(row)
    names = []
    for n in (opening or {}).get("names") or []:
        u = units_mod.normalize_unit(n)
        if u and u not in names:
            names.append(u)
    order = names + [u for u in grouped if u != GENERAL and u not in names]
    t_words = checklist.transcript_words(transcript_text)
    # All-good rows are timed in row order, so an earlier unit claims its
    # line before a later unit that said the same words.
    goods, all_dropped, claimed = {}, {}, set()
    for u in dict.fromkeys(u for u, row in parsed if u in order and _is_all(row)):
        all_dropped[u] = []
        goods[u] = _all_good(grouped[u], t_words, transcript_text, u, stretches,
                             claimed, all_dropped[u])
    walk = []
    for u in order:
        sec = _unit_section(u, grouped.get(u, []), items, transcript_text, goods.get(u),
                            all_dropped.get(u, []), report_date)
        sec["flagged_unlisted"] = bool(names) and u not in names
        walk.append({k: sec[k] for k in ("unit", "paragraphs", "line_refs", "report",
                                         "flagged_unlisted", "rows")})
    general, general_dropped = _general(grouped.get(GENERAL, []), t_words)
    return {"units": walk, "general": general, "general_dropped": general_dropped,
            "summary": summary(walk, items)}
