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

The action's planner target is its `target_ref`; with none, the target whose resolved
item (or one of whose ambiguous candidates) is the action's item.

Pure: no I/O, no logging. Instruction text, stated values and item text never reach the
counts.
"""
from __future__ import annotations

import action_pack_stages as st
import action_pack_targets as targets
from action_pack_verify import OPEN_STATUSES

TARGETED = ("close_item", "update_item")

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


def _named_target(a, resolution):
    """The planner target id this action acts on, or None (unnamed)."""
    ref = a.get("target_ref")
    if ref in resolution:
        return ref
    sid = a.get("target_stable_id")
    for tid, r in resolution.items():
        if r.kind == targets.RESOLVED and r.stable_id == sid:
            return tid
    for tid, r in resolution.items():
        if r.kind == targets.AMBIGUOUS and sid in r.candidates:
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
    tid = _named_target(a, resolution)
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
        if a is not None:
            out.append(a)
    return dict(draft, actions=out), counts
