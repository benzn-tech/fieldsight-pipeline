"""On-site answers laid over a walk's per-unit checklist (spec 2026-11-03).

The final check asks the inspector on site; `walk_answers.for_walk` keeps his
answers, oldest first. `apply` lays them over `walk_checklist.build`'s output:

  * fill -- the recording did not check the item: the on-site answer goes in,
    "<comment> (on site, HH:MM)", its quote as the evidence;
  * agree -- the recording said the same: the recording's row stays, with
    "(confirmed on site HH:MM)";
  * conflict -- they differ: neither silently wins. Answer is empty, the
    Comment names both, the summary cell is Check, and the item is listed in
    `conflicts` for the inspector to choose;
  * move -- `move_to_unit` is set: the source unit's No goes to the named
    unit's same item (replacing a Yes or a not-checked row, merged into a No),
    and the source item becomes not checked.

Answers match by unit and by item TEXT (whitespace and case folded), never by
number, so they survive a template version that reorders its items. What
cannot be placed -- an unknown unit, an item no longer in the template, a
move whose source is not a No -- is kept under `unmatched_answers`.

Each row keeps its BASE (what the recording said, or what a move brought;
None when not checked) apart from the latest on-site answer, and is redrawn
from the two whenever an answer lands. So a later answer replaces an earlier
one; an answer on a conflicted item is judged against the recording again; an
answer on an item an earlier on-site answer filled is a new fill, not a
conflict.

Pure: no database, no network. The input is not mutated.
"""
import copy

import checklist
import units as units_mod
import walk_checklist as wc

UNKNOWN_UNIT = "unit not in the walk"
UNKNOWN_ITEM = "item not in the template"
NOT_A_NO = "moved item is not a No"
NO_ANSWER = "not an answer or a move"
_ANSWERS = ("Yes", "No", "N/A")


def _fold(text):
    return " ".join((text or "").split()).casefold()


def _unit_key(text):
    return units_mod.normalize_unit(text) or _fold(text)


def _hhmm(answer):
    return (answer.get("answered_at") or "")[:5]


def _with(comment, note):
    return " ".join(p for p in ((comment or "").strip(), note) if p)


def _not_checked(row):
    row.update(answer="", comment=checklist.NOT_COVERED, responsible="", due="",
               evidence="", refs=[], state="not_checked")


def _draw(row, base, site, report):
    """Redraw `row` from its base ({"row", "on_site"} or None) and the latest
    on-site answer (or None). Returns the conflict, or None."""
    report["evidence"].pop(row["n"], None)
    if site is None:
        if base is None:
            _not_checked(row)
        else:
            row.update(copy.deepcopy(base["row"]))
    elif base is None or base["on_site"]:
        at = _hhmm(site)
        row.update(answer=site["answer"], state="answered", responsible="", due="",
                   comment=_with(site.get("comment"), "(on site%s)" % (", " + at if at else "")),
                   evidence=site.get("quote") or "", refs=[])
    elif base["row"]["answer"] == site["answer"]:
        at = _hhmm(site)
        row.update(copy.deepcopy(base["row"]))
        row["comment"] = _with(row["comment"], "(confirmed on site%s)" % (" " + at if at else ""))
    else:
        rec = base["row"]
        at = _hhmm(site)
        row.update(copy.deepcopy(rec))
        row.update(answer="", state="check",
                   comment="On site: %s%s. Recording: %s — '%s'. Inspector to choose." % (
                       site["answer"], " (%s)" % at if at else "", rec["answer"], rec["evidence"]))
        if row["evidence"]:
            report["evidence"][row["n"]] = row["evidence"]
        return {"unit": None, "item": row["item"], "on_site": site["answer"],
                "recording": rec["answer"]}
    if row["evidence"]:
        report["evidence"][row["n"]] = row["evidence"]
    return None


def _recount(unit):
    rows, report = unit["rows"], unit["report"]
    report["answered"] = sum(1 for r in rows if r["state"] == "answered")
    report["gaps"] = {
        "not_covered": [{"n": r["n"], "text": r["item"]} for r in rows if r["state"] == "not_checked"],
        "failed": [{"n": r["n"], "text": r["item"], "comment": r["comment"] or "",
                    "responsible": r["responsible"] or "", "due": r["due"] or ""}
                   for r in rows if r["state"] == "answered" and r["answer"] == "No"],
    }
    unit["paragraphs"], unit["line_refs"] = wc.render(unit)


def _moved(src_row, target_row, source, at):
    """The row a move brings to the target: the source's No, merged into a
    target No (comments joined with "; "; the target's evidence, refs,
    Responsible and Due are kept, the source's fill what the target lacks)."""
    note = "(moved from %s on site%s)" % (source, ", " + at if at else "")
    moved = copy.deepcopy(src_row)
    moved["comment"] = _with(moved["comment"], note)
    if target_row["answer"] != "No":
        return moved
    merged = copy.deepcopy(target_row)
    merged["comment"] = "; ".join(c for c in (target_row["comment"], moved["comment"]) if c)
    merged["refs"] = list(target_row["refs"]) + [r for r in moved["refs"] if r not in target_row["refs"]]
    for k in ("responsible", "due", "evidence"):
        merged[k] = target_row[k] or moved[k]
    return merged


def apply(walk, answers, items):
    """The walk with the on-site answers laid over its rows; adds
    `conflicts` [{"unit", "item", "on_site", "recording"}] and
    `unmatched_answers` (each answer that could not be placed, with a
    `reason`). `items` is the template's checklist, in its current order."""
    walk = copy.deepcopy(walk)
    walk["conflicts"], walk["unmatched_answers"] = [], []
    by_unit = {_unit_key(u["unit"]): u for u in walk["units"]}
    known = {_fold(i) for i in items}
    state = {}  # (unit name, n) -> [base, site]
    for u in walk["units"]:
        for r in u["rows"]:
            base = None if r["state"] == "not_checked" else {"row": copy.deepcopy(r), "on_site": False}
            state[(u["unit"], r["n"])] = [base, None]
    conflicts = {}  # (unit name, n) -> conflict, in the order first raised
    touched = set()

    def row_of(unit, text):
        return next(r for r in unit["rows"] if _fold(r["item"]) == text)

    def redraw(unit, row):
        base, site = state[(unit["unit"], row["n"])]
        c = _draw(row, base, site, unit["report"])
        key = (unit["unit"], row["n"])
        if c:
            c["unit"] = unit["unit"]
            conflicts[key] = c
        else:
            conflicts.pop(key, None)
        touched.add(unit["unit"])

    for a in answers:
        unit = by_unit.get(_unit_key(a.get("unit")))
        text = _fold(a.get("item_text"))
        move = a.get("move_to_unit")
        why = None
        if unit is None:
            why = UNKNOWN_UNIT
        elif text not in known or not any(_fold(r["item"]) == text for r in unit["rows"]):
            why = UNKNOWN_ITEM
        elif move:
            target = by_unit.get(_unit_key(move))
            src = row_of(unit, text) if target is not None else None
            if target is None or target is unit:
                why = UNKNOWN_UNIT
            elif src["answer"] != "No" or src["state"] != "answered":
                why = NOT_A_NO
            else:
                dst = row_of(target, text)
                s_base = state[(unit["unit"], src["n"])][0]
                d_base = state[(target["unit"], dst["n"])][0]
                on_site = (s_base is None or s_base["on_site"]) and (
                    dst["answer"] != "No" or d_base is None or d_base["on_site"])
                state[(target["unit"], dst["n"])] = [
                    {"row": _moved(src, dst, unit["unit"], _hhmm(a)), "on_site": on_site}, None]
                state[(unit["unit"], src["n"])] = [None, None]
                redraw(unit, src)
                redraw(target, dst)
        elif a.get("answer") in _ANSWERS:
            row = row_of(unit, text)
            state[(unit["unit"], row["n"])][1] = a
            redraw(unit, row)
        else:
            why = NO_ANSWER
        if why:
            walk["unmatched_answers"].append(dict(a, reason=why))

    for u in walk["units"]:
        u["report"]["conflicts"] = [c for (name, _), c in conflicts.items() if name == u["unit"]]
        if u["unit"] in touched:
            _recount(u)
    walk["conflicts"] = [c for u in walk["units"] for (name, _), c in conflicts.items()
                         if name == u["unit"]]
    walk["summary"] = wc.summary(walk["units"], items)
    return walk
