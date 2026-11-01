"""The gold set: owner-supplied instructions with their expected actions (spec §8.1).

Format (SCHEMA.md): a JSON object {"instructions": [...]}, a JSON list, or JSONL, one entry
per instruction:

    {"id": "G1", "site": "<site uuid on TEST, or its name>", "instruction": "...",
     "scenario": "pour_review|tidy_open_items|standard_in_library|standard_not_in_library|other",
     "expected_actions": [
        {"id": "e1", "type": "close_item|update_item", "target_stable_id": "<uuid>"}
            # or "target_task_no": 12 (stable_id is preferred: task_no is only shown while
            # the reader can see the item)
        {"id": "e2", "type": "create_item|draft_rfi|draft_email|note",
         "match_hint": "what the annotator matches a proposed action against"},
        ... "fields": {...} optional on any (documentation for the annotator, not scored)
     ],
     "must_not_touch": ["<stable uuid>", 12, ...],   # close/update on these is wrong-target
     "expected_uncertain": [{"id": "u1", "standard": "NZS 3109"}]}

`validate_gold` returns (errors, warnings). Fewer than 20 instructions, or fewer per scenario
than spec §8.1's composition, is a WARNING (the example set is three), never an error.
"""
from __future__ import annotations

import hashlib
import json
from pathlib import Path

from scripts.action_pack_eval import paths

SCENARIOS = ("pour_review", "tidy_open_items", "standard_in_library",
             "standard_not_in_library", "other")
TARGETED = ("close_item", "update_item")
UNTARGETED = ("create_item", "draft_rfi", "draft_email", "note")
ACTION_TYPES = TARGETED + UNTARGETED
MIN_INSTRUCTIONS = 20
# Spec §8.1: the 10-10 pour, "tidy open items" for 3 sites, 5 in-library, 5 not.
SPEC_COMPOSITION = {"pour_review": 1, "tidy_open_items": 3, "standard_in_library": 5,
                    "standard_not_in_library": 5}
INSTRUCTION_MAX_CHARS = 2000        # action_pack_view.INSTRUCTION_MAX_CHARS


def load_gold(path) -> list:
    return paths.read_records(path, key="instructions")


def gold_sha256(path) -> str:
    """Hash of the gold file's canonical JSON, so reports can prove they share a gold set."""
    canon = json.dumps(load_gold(path), sort_keys=True, ensure_ascii=False)
    return hashlib.sha256(canon.encode("utf-8")).hexdigest()


def inputs_sha256(entries) -> str:
    """Hash of what `submit` sends for each instruction (id, site, instruction text) and
    nothing else: two runs with the same inputs hash ran the same instructions, whatever
    edits were made to expected actions or labels-side fields afterwards."""
    canon = json.dumps([[e.get("id"), e.get("site"), e.get("instruction")] for e in entries],
                       ensure_ascii=False)
    return hashlib.sha256(canon.encode("utf-8")).hexdigest()


def _nonempty_str(v):
    return isinstance(v, str) and bool(v.strip())


def _is_task_no(v):
    return isinstance(v, int) and not isinstance(v, bool) and v > 0


def validate_gold(entries):
    errors, warnings = [], []
    if not isinstance(entries, list):
        return ["gold set is not a list of instructions"], []
    seen = set()
    for n, e in enumerate(entries):
        where = f"instruction #{n}"
        if not isinstance(e, dict):
            errors.append(f"{where}: not an object")
            continue
        gid = e.get("id")
        if not _nonempty_str(gid):
            errors.append(f"{where}: id is required")
        else:
            where = f"instruction {gid}"
            if gid in seen:
                errors.append(f"{where}: duplicate id")
            seen.add(gid)
        if not _nonempty_str(e.get("site")):
            errors.append(f"{where}: site is required (TEST site uuid or name)")
        text = e.get("instruction")
        if not _nonempty_str(text):
            errors.append(f"{where}: instruction text is required")
        elif len(text.strip()) > INSTRUCTION_MAX_CHARS:
            errors.append(f"{where}: instruction longer than {INSTRUCTION_MAX_CHARS} chars")
        if e.get("scenario") not in SCENARIOS:
            errors.append(f"{where}: scenario must be one of {list(SCENARIOS)}")
        errors += _validate_expected(where, e.get("expected_actions", []))
        mnt = e.get("must_not_touch", [])
        if not isinstance(mnt, list) or not all(_nonempty_str(x) or _is_task_no(x)
                                                for x in mnt):
            errors.append(f"{where}: must_not_touch must list stable ids or task numbers")
        unc = e.get("expected_uncertain", [])
        if not isinstance(unc, list) or not all(
                isinstance(u, dict) and _nonempty_str(u.get("id"))
                and _nonempty_str(u.get("standard")) for u in unc):
            errors.append(f"{where}: expected_uncertain entries need id and standard")
        elif len({u["id"] for u in unc}) != len(unc):
            errors.append(f"{where}: duplicate expected_uncertain id")
    if len(entries) < MIN_INSTRUCTIONS:
        warnings.append(f"gold set has {len(entries)} instructions; spec s8.1 asks for at "
                        f"least {MIN_INSTRUCTIONS}")
    counts = {s: sum(1 for e in entries if isinstance(e, dict) and e.get("scenario") == s)
              for s in SPEC_COMPOSITION}
    for s, want in SPEC_COMPOSITION.items():
        if counts[s] < want:
            warnings.append(f"scenario {s}: {counts[s]} instructions; spec s8.1 asks for "
                            f"{want}")
    return errors, warnings


def _validate_expected(where, expected):
    if not isinstance(expected, list):
        return [f"{where}: expected_actions must be a list"]
    errors, ids = [], set()
    for x in expected:
        if not isinstance(x, dict) or not _nonempty_str(x.get("id")):
            errors.append(f"{where}: every expected action needs an id")
            continue
        w = f"{where} expected {x['id']}"
        if x["id"] in ids:
            errors.append(f"{where}: duplicate expected action id {x['id']}")
        ids.add(x["id"])
        if x.get("type") not in ACTION_TYPES:
            errors.append(f"{w}: type must be one of {list(ACTION_TYPES)}")
        elif x["type"] in TARGETED:
            if not (_nonempty_str(x.get("target_stable_id"))
                    or _is_task_no(x.get("target_task_no"))):
                errors.append(f"{w}: a close/update needs target_stable_id or target_task_no")
        elif not _nonempty_str(x.get("match_hint")):
            errors.append(f"{w}: a {x['type']} needs a match_hint for the annotator")
        if "fields" in x and not isinstance(x["fields"], dict):
            errors.append(f"{w}: fields must be an object")
    return errors


def by_id(entries) -> dict:
    return {e["id"]: e for e in entries}


def load_valid_gold(path):
    """(entries, warnings); SystemExit listing every error when the gold set is invalid."""
    entries = load_gold(path)
    errors, warnings = validate_gold(entries)
    if errors:
        raise SystemExit("invalid gold set " + str(Path(path)) + ":\n  " + "\n  ".join(errors))
    return entries, warnings
