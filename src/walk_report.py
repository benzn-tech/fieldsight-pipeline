"""One report for a multi-unit walk (spec 2026-11-03, Task 8).

The worker writes a walk's report from two kinds of model call: the MAIN call
writes the template's other sections (header, prose, catch-all) from a plan
with its checklist taken out, and the WALK call(s) write one table for the
whole walk, each row carrying its unit (walk_checklist.shape_sentence). This
module is the assembly between them, in this order:

  1. the main call's sections, except the catch-all;
  2. General -- findings that name no unit -- when there are any;
  3. per unit, "<unit> -- <checklist title>", a unit not in the opening list
     saying so first;
  4. Summary, the unit-by-item grid;
  5. the catch-all.

Every unit table is drawn by walk_checklist.render from its rows, the same
way the on-site overlay redraws it. Each unit section carries its checklist
report (`report`) so the result's `checklists` provenance keeps one entry per
section title -- what the gaps reader already reads.

Pure: no database, no network.
"""
import copy

import checklist
import units as units_mod
import walk_checklist as wc

GENERAL = wc.GENERAL
SUMMARY = "Summary"
DASH = " — "
UNLISTED = "Not in the opening list — confirm this unit."
MAX_PART_WORDS = 6000


def _is_checklist(s):
    return str(s.get("kind") or "").strip().lower() == "checklist" and bool(s.get("items"))


def _all(sections):
    out = []
    for s in sections or []:
        out.append(s)
        out.extend(_all(s.get("children")))
    return out


def the_checklist(template):
    """(title, items) of the walk's checklist, the title as the customer wrote
    it. A walk template has exactly one checklist section; with none or more
    than one this raises rather than guessing a layout (NEEDS_CONTEXT)."""
    found = [s for s in _all((template or {}).get("sections")) if _is_checklist(s)]
    if len(found) != 1:
        raise ValueError("a walk report needs exactly one checklist section; "
                         "this template has %d" % len(found))
    return (found[0].get("title") or "").strip(), [str(i) for i in found[0]["items"]]


def without_checklists(template):
    """The plan the main call is given: the template with every checklist
    section removed, sub-sections included."""
    def strip(sections):
        out = []
        for s in sections or []:
            if _is_checklist(s):
                continue
            s = dict(s)
            if s.get("children"):
                s["children"] = strip(s["children"])
            out.append(s)
        return out
    out = copy.deepcopy(dict(template or {}))
    out["sections"] = strip(out.get("sections"))
    return out


# ---- the walk call(s) -------------------------------------------------------

def prompt(items, transcript_text, items_wrap=None):
    """Our shape sentence, the customer's items (wrapped by `items_wrap`, the
    caller's fence for a customer-written template) and the transcript."""
    block = checklist.items_block(items)
    return ("Write the checklist of this inspection walk %s.\n\n%s\n\nTranscript:\n%s\n"
            % (wc.shape_sentence(), items_wrap(block) if items_wrap else block,
               transcript_text))


def _stretch_index(stretches, hhmmss):
    if not hhmmss:
        return None
    t = units_mod._seconds_form(hhmmss)
    for i, s in enumerate(stretches or []):
        if s["from"] <= t and (s["to"] is None or t < s["to"]):
            return i
    return None


def parts(lines, stretches, max_words=MAX_PART_WORDS):
    """The walk transcript as one text, or -- over `max_words` -- several,
    each a run of whole stretches. `lines` are (HH:MM:SS, line) in transcript
    order; each line keeps its head. A part never ends inside a stretch, so a
    single stretch longer than `max_words` is a part of its own, over size."""
    lines = list(lines)
    words = lambda ls: sum(len(ln.split()) for _, ln in ls)  # noqa: E731
    if words(lines) <= max_words:
        return ["\n".join(ln for _, ln in lines)] if lines else []
    runs, key = [], object()
    for at, ln in lines:
        k = _stretch_index(stretches, at)
        if not runs or k != key:
            runs.append([])
            key = k
        runs[-1].append((at, ln))
    out, cur = [], []
    for run in runs:
        if cur and words(cur) + words(run) > max_words:
            out.append(cur)
            cur = []
        cur = cur + run
    if cur:
        out.append(cur)
    return ["\n".join(ln for _, ln in p) for p in out]


# ---- the report ---------------------------------------------------------------

def _section(title, paragraphs, line_refs, **extra):
    return dict({"title": title, "paragraphs": paragraphs, "level": 1, "covers": [],
                 "line_refs": line_refs}, **extra)


def _cell(text):
    return str(text).replace("|", "/")


def unit_title(unit, checklist_title):
    return unit + DASH + checklist_title


def general_section(entries):
    paragraphs = []
    for e in entries or []:
        comment, evidence = (e.get("comment") or "").strip(), (e.get("evidence") or "").strip()
        paragraphs.append('%s%s"%s"' % (comment, DASH if comment else "", evidence))
    return _section(GENERAL, paragraphs, [[] for _ in paragraphs])


def sections(template, main_prose, walk):
    """The walk report's sections (_prose_sections-shaped), in report order.

    `walk` is walk_checklist.build's output, after walk_answers_overlay.apply.
    A section the main call wrote under the checklist's own title is dropped:
    the customer's checklist is the walk's, never a table the model wrote
    beside it. Each unit section carries `unit` and its `report`."""
    title, _ = the_checklist(template)
    catch = ((template or {}).get("catch_all") or {}).get("title", "").strip().lower()
    head, tail = [], []
    for s in main_prose or []:
        t = (s.get("title") or "").strip().lower()
        if t and t == title.lower():
            continue
        (tail if catch and t == catch else head).append(s)
    out = list(head)
    if walk.get("general"):
        out.append(general_section(walk["general"]))
    for u in walk.get("units") or []:
        paragraphs, line_refs = wc.render(u)
        if u.get("flagged_unlisted"):
            paragraphs, line_refs = [UNLISTED] + paragraphs, [[]] + line_refs
        out.append(_section(unit_title(u["unit"], title), paragraphs, line_refs,
                            unit=u["unit"], report=u["report"]))
    summ = walk.get("summary") or {"columns": [], "rows": []}
    cols = ["Unit"] + [_cell(c) for c in summ["columns"]]
    paragraphs = [" | ".join(cols), " | ".join(["---"] * len(cols))]
    paragraphs += [" | ".join(_cell(c) for c in row) for row in summ["rows"]]
    out.append(_section(SUMMARY, paragraphs, [[] for _ in paragraphs]))
    return out + tail


def reports(secs):
    """{unit section title: its checklist report}, for the result's
    `checklists` provenance."""
    return {s["title"]: s["report"] for s in secs if "report" in s}


def place_photos(secs, streams_by_ref, offer, stretches, photo_time):
    """Each photograph into the section of the unit he was in when it was
    taken (units.unit_at); General when that was no unit, or a unit with no
    section -- never guessed into a unit. A General section is added, before
    the first unit, when a photograph needs one and there was none.

    Returns (placed, left): the count placed here, and the streams_by_ref of
    the photographs whose time is not known, for the ordinary placement."""
    by_unit = {s["unit"]: s for s in secs if "unit" in s}
    general = next((s for s in secs if s.get("title") == GENERAL and "unit" not in s), None)
    placed, left = 0, {}
    for t in offer or []:
        ref = t["ref"]
        streams = list((streams_by_ref or {}).get(ref) or [])
        names = list(t.get("photo_names") or [])
        for i, stream in enumerate(streams):
            when = photo_time(names[i]) if i < len(names) else None
            if not when:
                left.setdefault(ref, []).append(stream)
                continue
            target = by_unit.get(units_mod.unit_at(stretches, when))
            if target is None:
                if general is None:
                    general = general_section([])
                    at = next((k for k, s in enumerate(secs) if "unit" in s), len(secs))
                    secs.insert(at, general)
                target = general
            target["photo_streams"] = (target.get("photo_streams") or []) + [stream]
            placed += 1
    return placed, left


def result(walk, key):
    """The result JSON's `walk` key (Task 12 reads it)."""
    return {"key": key,
            "units": [{"unit": u["unit"], "report": u["report"]} for u in walk.get("units") or []],
            "summary": walk.get("summary"),
            "conflicts": walk.get("conflicts") or [],
            "unmatched_answers": walk.get("unmatched_answers") or []}
