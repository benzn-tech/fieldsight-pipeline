"""A recorded run: the packs one pass over the gold set produced (SCHEMA.md).

    {"run_id": str, "config_label": str, "gold_sha256": str?, "inputs_sha256": str?,
     "packs": [{"gold_id": "G1", "pack_id": str?,
                "pack": <GET /api/org/action-packs/{id} body, incl. run_trace and models>,
                "error": str?, "http_status": int?, "client_elapsed_s": float?,
                "conflict_retries": int?}]}

`pack` is null when the instruction never produced a pack (site unresolved, POST refused,
timeout): its expected actions then count as missed.

GET returns run_trace (counts-shaped keys) and models (action_pack_view.shape_pack);
`attach_traces` is only for packs recorded before it did.

Elements. Every stored conclusion line and action is one element, keyed
"conclusion:<index in the stored list>" or "action:<seq>". An element is SHOWN unless it is
an action the verifier labelled `unsupported` (stored for this comparison, never applicable,
spec §14 S15/S16). Conclusion lines the verifier rejected are never stored, so they cannot be
labelled; they are counted from run_trace.verify instead (metrics.score_fabrication)."""
from __future__ import annotations

import copy
import hashlib
import json

from scripts.action_pack_eval import paths


def load_run(path) -> dict:
    run = paths.read_json(path)
    if not isinstance(run, dict) or not isinstance(run.get("packs"), list):
        raise SystemExit(f"{path}: a run is an object with a 'packs' list")
    return run


def pack_ids_sha256(run):
    """Hash of the run's recorded pack ids (sorted): two reports with the same hash scored
    the SAME packs, so they cannot be two runs of a noise floor. None when no pack exists."""
    ids = sorted(str((r.get("pack") or {}).get("id") or r.get("pack_id"))
                 for r in run.get("packs") or []
                 if (r.get("pack") or {}).get("id") or r.get("pack_id"))
    if not ids:
        return None
    return hashlib.sha256(json.dumps(ids).encode("utf-8")).hexdigest()


def is_shown(element) -> bool:
    return not (element["kind"] == "action" and element.get("verifier") == "unsupported")


def pack_elements(pack) -> list:
    """The labellable elements of one recorded pack, in display order."""
    if not pack:
        return []
    out = []
    for i, c in enumerate(pack.get("conclusion") or []):
        out.append({"element": f"conclusion:{i}", "kind": "conclusion", "text": c.get("text"),
                    "refs": list(c.get("refs") or []), "confidence": c.get("confidence")})
    for a in pack.get("actions") or []:
        out.append({"element": f"action:{a.get('seq')}", "kind": "action",
                    "type": a.get("type"), "target_stable_id": a.get("target_stable_id"),
                    "target_task_no": a.get("target_task_no"),
                    "target_snapshot": a.get("target_snapshot"),
                    "proposed": a.get("proposed"), "rationale": a.get("rationale"),
                    "refs": list(a.get("refs") or []), "verifier": a.get("verifier")})
    for e in out:
        e["shown"] = is_shown(e)
    return out


def iter_elements(run):
    """(gold_id, pack, element) for every element of every recorded pack, in run order."""
    for row in run.get("packs") or []:
        pack = row.get("pack")
        for el in pack_elements(pack):
            yield row.get("gold_id"), pack, el


def attach_traces(run, traces) -> dict:
    """A copy of `run` with run_trace/models from `traces` rows ({"id", "run_trace",
    "models"}) put on the pack with that id. Packs without a row are left as they are."""
    by_id = {str(t.get("id")): t for t in traces if isinstance(t, dict)}
    out = copy.deepcopy(run)
    for row in out.get("packs") or []:
        pack = row.get("pack")
        t = by_id.get(str((pack or {}).get("id")))
        if not t:
            continue
        for k in ("run_trace", "models"):
            if t.get(k) is not None:
                pack[k] = t[k]
    return out
