"""The action agent's scope gate (spec 2026-11-05 §3.2.4; plan PR-4 Tasks 4.2-4.4).

Runs in code after `action_pack_stages.validate_draft`, before the verifier. It decides
which items a draft may change and keeps only the proposed fields whose source holds up.
Every rejection is counted under a fixed code; the counts become
`run_trace.scope_gate`.

Rule 1 -- target scope. A close or update target must be open (`OPEN_STATUSES`) whatever
the scope and whatever the field: a named target that is done or closed becomes an
"already done" note (`demoted_target_closed`), any other is dropped
(`drop_target_closed`). Under `change_scope = "named"` a target that is not a named
target's resolved item is dropped (`drop_unasked_target`); under `tidy` any open item
may be targeted (the site check stays the verifier's code check).

Rule 2 -- named target mismatch. A `target_ref` whose target resolved to a different item
is dropped (`drop_named_mismatch`: C4's look-alike). An action on an `ambiguous` or a
`none` target is demoted to a note (`demoted_ambiguous`, `demoted_target_none`), never
dropped silently. Rule 2 runs before rule 1's scope check, so a demotion is never lost
to `drop_unasked_target`.

Rule 3 -- field provenance. Every proposed field of a close, an update and a create's
optional fields (its text keeps its refs rule) must pass its declared source, or it is
removed (`field_unsourced__<field>`); a close/update left with nothing is dropped
(`drop_no_sourced_fields`).
    user             a stated value for (this target or none, field) must exist; the
                     value BECOMES the stated one (`user_value_overridden__<field>` when
                     the writer's differed, normalised);
    evidence         a ref of kind topic, chunk or document -- an item row never
                     justifies a change, except the target's own row for a deadline it
                     carries as deadline_text; web refs never set a field;
    assumed_default  a deadline only, under a delegation covering the target, on an item
                     with neither deadline nor deadline_text: the value BECOMES the
                     item's raised_on (owner rule: the raised date, never today+N), and the
                     action is marked `assumed = ["deadline"]` (`date_assumed`).
A stated status still has to be a legal change for the type (`drop_illegal_after_gate`).

Rule 4 -- deadline sanity. An `evidence` deadline carries `said_on` equal to the date of
one of its source refs (a topic's or chunk's date; the target's own item row's raised_on)
or is removed (`field_said_on_mismatch`); a deadline before that day is removed
(`field_deadline_before_source`). Code checks the anchor only; the spoken date itself is
the model's (no date parsing here).

The action's planner target is its `target_ref`; with none, the target whose resolved
item is the action's item, or -- under `named` only -- one whose ambiguous candidates
include it (so it becomes a note, not `drop_unasked_target`). Under `tidy` an unnamed
action on an ambiguous candidate is an ordinary tidy change.

Pure: no I/O, no logging. Instruction text, stated values and item text never reach the
counts.
"""
from __future__ import annotations

import datetime as dt

import action_pack_stages as st
import action_pack_view
import action_pack_targets as targets
from action_pack_verify import OPEN_STATUSES

TARGETED = ("close_item", "update_item")
SOURCED = TARGETED + ("create_item",)
EVIDENCE_KINDS = ("topic", "chunk", "document")

NOTE_SUFFIX = {
    "en": {"ambiguous": "matches more than one item: {refs}",
           "none": "named item not found",
           "closed": "already done"},
    "zh": {"ambiguous": "与多个事项匹配：{refs}",
           "none": "未找到指定的事项",
           "closed": "已完成"},
}
_NOTE_WRAP = {"en": "{text} ({suffix})", "zh": "{text}（{suffix}）"}


def _bump(d, key, n=1):
    d[key] = d.get(key, 0) + n


def _named_target(a, resolution, *, ambiguous=True):
    """The planner target id this action acts on, or None (unnamed). `ambiguous`: also
    infer it from an ambiguous target's candidates (not under `tidy`)."""
    ref = a.get("target_ref")
    if ref in resolution:
        return ref
    sid = a.get("target_stable_id")
    for tid, r in resolution.items():
        if r.kind == targets.RESOLVED and r.stable_id == sid:
            return tid
    for tid, r in resolution.items():
        if ambiguous and r.kind == targets.AMBIGUOUS and sid in r.candidates:
            return tid
    return None


def _note(a, lang, suffix_key, items, candidates=()):
    """A demoted close/update: the rationale plus a fixed suffix, refs kept."""
    refs = ", ".join(str((items.get(s) or {}).get("ref") or "?") for s in candidates)
    suffix = NOTE_SUFFIX[lang][suffix_key].format(refs=refs)
    text = _NOTE_WRAP[lang].format(text=a.get("rationale") or "", suffix=suffix)
    return {"type": "note", "target_stable_id": None,
            "proposed": {"text": text[:st.BODY_MAX_CHARS]},
            "rationale": a.get("rationale"), "refs": list(a.get("refs") or [])}


def _scope(a, *, plan, resolution, items, named_sids, lang, counts):
    """Rules 1-2 for one close/update -> the action, a note, or None (dropped)."""
    sid = a.get("target_stable_id")
    tid = _named_target(a, resolution, ambiguous=plan.change_scope != "tidy")
    r = resolution.get(tid) if tid is not None else None
    if r is not None:
        if r.kind == targets.AMBIGUOUS:
            _bump(counts, "demoted_ambiguous")
            return _note(a, lang, "ambiguous", items, r.candidates)
        if r.kind == targets.NONE:
            _bump(counts, "demoted_target_none")
            return _note(a, lang, "none", items)
        if r.stable_id != sid:
            _bump(counts, "drop_named_mismatch")
            return None
    status = (items.get(sid) or {}).get("status") or "open"
    if status not in OPEN_STATUSES:
        if sid in named_sids:
            _bump(counts, "demoted_target_closed")
            return _note(a, lang, "closed", items)
        _bump(counts, "drop_target_closed")
        return None
    if plan.change_scope != "tidy" and sid not in named_sids:
        _bump(counts, "drop_unasked_target")
        return None
    return a


def _day_iso(value):
    """'YYYY-MM-DD' for a date, a datetime or an ISO date/timestamp string, else None."""
    if isinstance(value, dt.datetime):
        value = value.date()
    if isinstance(value, dt.date):
        return value.isoformat()
    d = st._iso_date(str(value or "")[:10])
    return d.isoformat() if d else None


def _norm(fld, value):
    """The comparison form of a field value: ISO for dates, lower-case for status and
    priority, casefold for responsible."""
    v = str(value if value is not None else "").strip()
    if fld == "deadline":
        return _day_iso(v) or v
    return v.lower() if fld in ("status", "priority") else v.casefold()


def _ref_day(entry, own_ref):
    """The date a ref shows: a topic's or chunk's `date`; the target's own item row's
    `raised_on`. None for anything else (a document, a web ref, another item)."""
    it = entry.get("item") or {}
    if entry.get("kind") in ("topic", "chunk"):
        return _day_iso(it.get("date"))
    if entry.get("kind") == "item" and entry.get("ref") == own_ref:
        return _day_iso(it.get("raised_on"))
    return None


def _deadline_sane(value, fs, *, registry, own_ref):
    """Rule 4 for an evidence deadline: None when it holds, else the removal code."""
    said_on = fs.get("said_on")
    days = {_ref_day(registry[r], own_ref) for r in fs.get("refs") or [] if r in registry}
    if said_on is None or said_on not in days:
        return "field_said_on_mismatch"
    if _norm("deadline", value) < said_on:
        return "field_deadline_before_source"
    return None


def _stated(plan, tid, fld):
    """The planner's stated value for (target, field): the target's own entry first, then
    a global one (target null). None when the user stated nothing for it."""
    stated = [e for e in getattr(plan, "stated_values", None) or [] if e["field"] == fld]
    for e in stated:
        if tid is not None and e["target"] == tid:
            return e["value"]
    for e in stated:
        if e["target"] is None:
            return e["value"]
    return None


def _delegated(plan, tid, fld):
    return any(e["field"] == fld and (e["target"] is None or e["target"] == tid)
               for e in getattr(plan, "delegations", None) or [])


def _field(fld, value, fs, *, tid, plan, item, registry, counts):
    """(value to keep, None) or (None, removal code) for one proposed field."""
    src = fs.get("source") if isinstance(fs, dict) else None
    unsourced = f"field_unsourced__{fld}"
    if src == "user":
        stated = _stated(plan, tid, fld)
        if stated is None:
            return None, unsourced
        if _norm(fld, value) != _norm(fld, stated):
            _bump(counts, f"user_value_overridden__{fld}")
        return stated, None
    if src == "evidence":
        refs = [r for r in fs.get("refs") or [] if r in registry]
        own = (item or {}).get("ref")
        ok = any(registry[r].get("kind") in EVIDENCE_KINDS for r in refs) or (
            fld == "deadline" and item is not None and bool(item.get("deadline_text"))
            and own in refs)
        if not ok:
            return None, unsourced
        if fld == "deadline":
            code = _deadline_sane(value, fs, registry=registry, own_ref=own)
            if code is not None:
                return None, code
        return value, None
    if src == "assumed_default":
        raised = _day_iso((item or {}).get("raised_on"))
        if (fld != "deadline" or item is None or raised is None
                or item.get("deadline") or item.get("deadline_text")
                or not _delegated(plan, tid, fld)):
            return None, unsourced
        return raised, None
    return None, unsourced


def _provenance(a, *, tid, plan, items, registry, counts):
    """Rule 3 for one close/update/create -> the action with only its sourced fields, or
    None (dropped)."""
    atype, sid = a.get("type"), a.get("target_stable_id")
    item = items.get(sid) if atype in TARGETED else None
    sources = a.get("field_sources") or {}
    proposed, assumed = dict(a.get("proposed") or {}), []
    for fld in [f for f in proposed if f != "text"]:
        value, code = _field(fld, proposed[fld], sources.get(fld), tid=tid, plan=plan,
                             item=item, registry=registry, counts=counts)
        if code is not None:
            proposed.pop(fld)
            _bump(counts, code)
            continue
        proposed[fld] = value
        if (sources.get(fld) or {}).get("source") == "assumed_default":
            assumed.append(fld)
            _bump(counts, "date_assumed")
    if atype in TARGETED:
        if not proposed:
            _bump(counts, "drop_no_sourced_fields")
            return None
        status = proposed.get("status")
        legal = st.legal_change(atype, proposed, (item or {}).get("status"))
        if legal is None or (atype == "close_item"
                             and status not in action_pack_view.CLOSE_STATUSES):
            _bump(counts, "drop_illegal_after_gate")
            return None
        proposed = legal
    out = dict(a, proposed=proposed)
    if assumed:
        out["assumed"] = assumed
    return out


def gate(draft: dict, *, plan, resolution: dict, items: dict, registry: dict):
    """-> (the gated draft, counts). `resolution` is {target id: Resolution} (round 1),
    `items` the run's items by stable_id, `registry` its refs. The conclusion and the
    actions that change no item pass untouched."""
    counts = {}
    lang = st._lang_key(getattr(plan, "language", None))
    named_sids = {r.stable_id for r in resolution.values() if r.kind == targets.RESOLVED}
    out = []
    for a in draft.get("actions") or []:
        if a.get("type") in TARGETED:
            a = _scope(a, plan=plan, resolution=resolution, items=items,
                       named_sids=named_sids, lang=lang, counts=counts)
        if a is not None and a.get("type") in SOURCED:
            tid = (_named_target(a, resolution, ambiguous=False)
                   if a["type"] in TARGETED else None)
            a = _provenance(a, tid=tid, plan=plan, items=items, registry=registry,
                            counts=counts)
        if a is not None:
            out.append(a)
    return dict(draft, actions=out), counts
