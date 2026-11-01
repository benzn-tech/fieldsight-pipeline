"""Human labels and the blind second-annotator subset (spec §8.1; SCHEMA.md).

One label row per (pack, shown or stored element, annotator), JSONL:

    {"pack_id": "<uuid>", "element": "conclusion:0" | "action:<seq>", "annotator": "ann1",
     "supported_by_refs": "yes" | "no" | "unclear",
     "is_standards_claim": true | false,
     "matches_expected": "e2",            # optional: the gold expected-action id this action
                                          # fulfils (how a create/draft/note is matched)
     "expected_uncertain_id": "u1"}       # optional: the gold expected_uncertain claim this
                                          # conclusion line states

`make_blind_subset` samples, with a seeded RNG, elements the first annotator labelled and
writes them WITHOUT the first annotator's labels and WITHOUT the verifier's labels
(verifier, confidence) or anything derived from them (state, applicable), so the second
annotator's agreement is a ceiling, not an echo.
"""
from __future__ import annotations

import math
import random

from scripts.action_pack_eval import paths
from scripts.action_pack_eval import runs as runs_mod

SUPPORT_VALUES = ("yes", "no", "unclear")
_SOURCE_FIELDS = ("ref", "kind", "provenance", "title", "date", "who", "excerpt", "url",
                  "page_from", "page_to", "removed")


def load_annotations(path) -> list:
    return paths.read_records(path, key="labels")


def key(row):
    return (str(row.get("pack_id")), row.get("element"))


def label_index(rows) -> dict:
    """{(pack_id, element): row}; the last row wins (validate_annotations reports dups)."""
    return {key(r): r for r in rows}


def validate_annotations(rows, *, run=None, gold=None) -> list:
    errors, seen = [], set()
    elements, gold_of_pack = {}, {}
    if run is not None:
        for gid, pack, el in runs_mod.iter_elements(run):
            elements[(str(pack["id"]), el["element"])] = el
            gold_of_pack[str(pack["id"])] = gid
    expected = {}
    if gold is not None:
        for e in gold:
            expected[e["id"]] = ({x["id"] for x in e.get("expected_actions") or []},
                                 {u["id"] for u in e.get("expected_uncertain") or []})
    for n, r in enumerate(rows):
        where = f"label #{n} ({r.get('pack_id')} {r.get('element')})"
        if not isinstance(r, dict) or not r.get("pack_id") or not r.get("element"):
            errors.append(f"label #{n}: pack_id and element are required")
            continue
        if not r.get("annotator"):
            errors.append(f"{where}: annotator is required")
        if r.get("supported_by_refs") not in SUPPORT_VALUES:
            errors.append(f"{where}: supported_by_refs must be one of {list(SUPPORT_VALUES)}")
        if not isinstance(r.get("is_standards_claim"), bool):
            errors.append(f"{where}: is_standards_claim must be true or false")
        k = (str(r["pack_id"]), r["element"], r.get("annotator"))
        if k in seen:
            errors.append(f"{where}: duplicate label for this annotator")
        seen.add(k)
        if run is None:
            continue
        el = elements.get(key(r))
        if el is None:
            errors.append(f"{where}: no such element in the run")
            continue
        gid = gold_of_pack.get(str(r["pack_id"]))
        exp_actions, exp_unc = expected.get(gid, (None, None))
        m = r.get("matches_expected")
        if m is not None:
            if el["kind"] != "action":
                errors.append(f"{where}: matches_expected is for actions only")
            elif exp_actions is not None and m not in exp_actions:
                errors.append(f"{where}: matches_expected {m} is not an expected action "
                              f"of gold {gid}")
        u = r.get("expected_uncertain_id")
        if u is not None and exp_unc is not None and u not in exp_unc:
            errors.append(f"{where}: expected_uncertain_id {u} is not in gold {gid}")
    return errors


def _blind_item(pack, el):
    sources = {s.get("ref"): s for s in pack.get("sources") or []}
    item = {"pack_id": str(pack["id"]), "element": el["element"], "kind": el["kind"],
            "instruction": pack.get("instruction")}
    if el["kind"] == "conclusion":
        item["text"] = el["text"]
    else:
        item.update({k: el[k] for k in ("type", "target_stable_id", "target_task_no",
                                         "proposed", "rationale")})
        if isinstance(el.get("target_snapshot"), dict):
            item["target_snapshot"] = dict(el["target_snapshot"])   # the item as the run read it
    item["refs"] = list(el["refs"])
    item["sources"] = [{k: sources[r].get(k) for k in _SOURCE_FIELDS if k in sources[r]}
                       for r in el["refs"] if r in sources]
    item.update({"annotator": None, "supported_by_refs": None, "is_standards_claim": None})
    return item


def make_blind_subset(run, first_rows, *, n=None, fraction=None, seed) -> list:
    """A deterministic sample (seeded) of the elements `first_rows` labelled, as blank
    label rows carrying only what a person needs to judge them."""
    labelled = {key(r) for r in first_rows}
    population = [(pack, el) for _, pack, el in runs_mod.iter_elements(run)
                  if (str(pack["id"]), el["element"]) in labelled]
    if n is None and fraction is None:
        raise ValueError("give n or fraction")
    k = n if n is not None else math.ceil(fraction * len(population))
    k = max(0, min(int(k), len(population)))
    chosen = random.Random(seed).sample(population, k)
    return [_blind_item(pack, el) for pack, el in chosen]
