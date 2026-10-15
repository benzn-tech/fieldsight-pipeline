"""Validate a read checklist form and flag what a person must look at (spec 2026-10-16 s3).

Validation lives in code, never in the prompt: the model's output is data. What can be
fixed safely is fixed (a list label, stray whitespace, an empty row); what cannot -- two
sections that may be one table split by a page break -- is FLAGGED for the review screen,
and nothing becomes a template until a person presses Save.
"""
import difflib
import json
import re

import checklist
import report_template

PURPOSE = "Record the inspector's answer to each item from the recording."
DEFAULT_TITLE = "Checklist"
NEAR = 0.85
# "1." "12)" "1.2." "1.2)" "a)" "b." "ii)" -- followed by whitespace. A bare "1.5" is NOT a
# label: "1.5 m high barrier" is a measurement, and stripping it would silently change the
# customer's wording. "2 workers" is not a label either.
LABEL_RE = re.compile(
    r"^\s*(?:\d{1,3}(?:\.\d{1,3})*[.)]|[a-z][.)]|[ivx]{1,5}[.)])\s+",
    re.IGNORECASE)
# A single capital letter + "." heads a section ("A. Documents & Approvals") but in an item
# it may be an initial ("A. Smith signs off"): items keep it and are flagged for the person.
_ITEM_INITIAL_RE = re.compile(r"^[A-Z]\.\s+")
# What the save body adds beyond the sections themselves (catch_all, keys, purposes are
# counted per section below); generous so the flag errs towards warning.
_BODY_OVERHEAD = 600


def _norm(value):
    return " ".join(value.split()) if isinstance(value, str) else ""


def _strip_label(text):
    return LABEL_RE.sub("", text, count=1)


def _ratio(a, b):
    return difflib.SequenceMatcher(None, a.lower(), b.lower()).ratio()


def _item(raw_text):
    text = _norm(raw_text)
    if not _ITEM_INITIAL_RE.match(text):
        text = _strip_label(text)
    if not text:
        return None
    flags = []
    if LABEL_RE.match(text):
        flags.append("label_kept")
    if len(text) > checklist.MAX_ITEM_CHARS:
        text = text[:checklist.MAX_ITEM_CHARS].rstrip()
        flags.append("truncated")
    return {"text": text, "flags": flags}


def _title(raw_title):
    title = _strip_label(_norm(raw_title))
    flags = []
    if LABEL_RE.match(title):
        flags.append("label_kept")
    if not title:
        title = DEFAULT_TITLE
    if len(title) > report_template.MAX_TITLE_CHARS:
        title = title[:report_template.MAX_TITLE_CHARS].rstrip()
        flags.append("truncated")
    return title, flags


def _flag_similar(sections):
    for i in range(len(sections)):
        for j in range(i + 1, len(sections)):
            a, b = sections[i], sections[j]
            if "split_for_limit" in a["flags"] and "split_for_limit" in b["flags"]:
                continue  # our own split of one long section
            if _ratio(a["title"], b["title"]) < NEAR:
                continue
            flag = "split_section" if j == i + 1 else "near_duplicate_section"
            for s in (a, b):
                if flag not in s["flags"]:
                    s["flags"].append(flag)


def _body_chars(sections):
    body = [{"key": "section-%d" % n, "title": s["title"], "kind": "checklist",
             "purpose": PURPOSE, "items": [i["text"] for i in s["items"]]}
            for n, s in enumerate(sections)]
    return len(json.dumps(body, ensure_ascii=False)) + _BODY_OVERHEAD


def clean(raw):
    sections = []
    for raw_section in raw or []:
        if not isinstance(raw_section, dict):
            continue
        title, section_flags = _title(raw_section.get("title"))
        items = [it for it in (_item(x) for x in (raw_section.get("items") or [])) if it]
        if not items:
            continue
        if len(items) <= checklist.MAX_ITEMS:
            sections.append({"title": title, "items": items, "flags": section_flags})
            continue
        for n, start in enumerate(range(0, len(items), checklist.MAX_ITEMS), 1):
            suffix = " (%d)" % n
            base = title[:report_template.MAX_TITLE_CHARS - len(suffix)].rstrip()
            sections.append({"title": base + suffix,
                             "items": items[start:start + checklist.MAX_ITEMS],
                             "flags": section_flags + ["split_for_limit"]})
    _flag_similar(sections)
    flags = []
    if len(sections) > report_template.MAX_SECTIONS:
        flags.append("too_many_sections")
    if _body_chars(sections) > report_template.MAX_BODY_CHARS:
        flags.append("too_long")
    return {"sections": sections,
            "counts": {"sections": len(sections),
                       "items": sum(len(s["items"]) for s in sections)},
            "flags": flags}
