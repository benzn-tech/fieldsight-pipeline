"""Select, the per-model tuning rule, the keep rule and frozen.v1.json (spec §6).

Pattern: tools/web-answer-bench/select_freeze.py (evidential always False:
best-of-several on 4 sessions carries winner's-curse bias). Named rb_select so
it never collides with the web bench's `select_freeze` in sys.modules.

    python rb_select.py --out DIR underperforming      -> <out>/underperforming.json
    python rb_select.py --out DIR edits                -> edit candidates into the index
    python rb_select.py --out DIR freeze [--keep FILE] -> tools/report-bench/frozen.v1.json

Best round-0 cell per (task, model) (plan decision; the spec says "best"):
fewest failed sessions, then highest mean action recall (T: coverage), then
highest mean action precision.

Underperforming (per model, its best cell): (a) fails >= 2 of the sessions;
(b) action recall below the cross-model median of best cells by more than the
model's own F on >= 2 sessions. `misread` is reported, never a trigger.

Edits (rounds 1-2): `edits/<task>/<model>/e<1|2>.json` =
{"base": "<candidate id>", "why": "...", "replace": [[old, new], ...]}, applied
to the base candidate's pre-rendered prompt for every session (an `old` that is
not found exactly once is an error). The edit's candidate id is
`<task>-<model>-e<n>`.
"""
from __future__ import annotations

import argparse
import json
import os
import statistics
import sys

_HERE = os.path.dirname(os.path.abspath(__file__))
if _HERE not in sys.path:
    sys.path.insert(0, _HERE)

import rb_common  # noqa: E402
import rb_score  # noqa: E402

EDITS = os.path.join(_HERE, "edits")
FROZEN = os.path.join(_HERE, "frozen.v1.json")
EPS = 1e-9


def _recall_key(task):
    return "action_coverage" if task == "T" else "action_recall"


def _cells(scores, task, model):
    return [c for c in scores if c.split("|")[0] == task and c.split("|")[2] == model]


def _mean(c, key):
    v = c.get("mean", {}).get(key)
    return v if isinstance(v, (int, float)) else float("-inf")


def best_round0(scores, task, model):
    rk = _recall_key(task)
    cells = _cells(scores, task, model)
    if not cells:
        return None
    return min(cells, key=lambda c: (scores[c]["failed_sessions"], -_mean(scores[c], rk),
                                     -_mean(scores[c], "action_precision"), c))


def per_session(cell, key):
    out = {}
    for s, d in cell["sessions"].items():
        vals = [r.get(key) for r in d["reps"] if isinstance(r.get(key), (int, float))]
        if vals:
            out[s] = statistics.mean(vals)
    return out


def underperforming(scores, task):
    rk = _recall_key(task)
    models = sorted({c.split("|")[2] for c in scores if c.split("|")[0] == task})
    best = {m: best_round0(scores, task, m) for m in models}
    recall = {m: per_session(scores[best[m]], rk) for m in models}
    sessions = sorted({s for r in recall.values() for s in r})
    median = {s: statistics.median([recall[m][s] for m in models if s in recall[m]])
              for s in sessions}
    out = {}
    for m in models:
        cell = scores[best[m]]
        trig = []
        if cell["failed_sessions"] >= 2:
            trig.append("a")
        f = cell["noise_floor"].get(rk, 0.0)
        below = sum(1 for s in sessions if s in recall[m] and median[s] - recall[m][s] > f + EPS)
        if below >= 2:
            trig.append("b")
        out[m] = {"best_cell": best[m], "triggers": trig,
                  "misread_mean": cell["mean"].get("misread")}
    return out


def keep_edit(incumbent, edit, metric):
    """Incumbent's F: no session worse by > F, >= 1 better by > F, no newly
    failed session."""
    f = incumbent["noise_floor"].get(metric, 0.0)
    a, b = per_session(incumbent, metric), per_session(edit, metric)
    better = False
    for s in a:
        if s not in b:
            return False
        if b[s] < a[s] - f - EPS:
            return False
        if b[s] > a[s] + f + EPS:
            better = True
    for s, d in edit["sessions"].items():
        if d["failed"] and not incumbent["sessions"].get(s, {}).get("failed", False):
            return False
    return better


def load_edits(edits_dir=EDITS):
    out = {}
    if not os.path.isdir(edits_dir):
        return out
    for task in sorted(os.listdir(edits_dir)):
        for model in sorted(os.listdir(os.path.join(edits_dir, task))):
            d = os.path.join(edits_dir, task, model)
            for name in sorted(os.listdir(d)):
                if name.startswith("e") and name.endswith(".json"):
                    n = name[1:-5]
                    out[f"{task}-{model}-e{n}"] = dict(rb_common.read_json(os.path.join(d, name)),
                                                       task=task, model=model)
    return out


def apply_edit(prompt, spec):
    for old, new in spec["replace"]:
        if prompt.count(old) != 1:
            raise ValueError(f"edit text must occur exactly once (found {prompt.count(old)})")
        prompt = prompt.replace(old, new)
    return prompt


def add_edit_candidates(out, edits=None):
    """Edit prompts for every key of the base candidate, into the index."""
    edits = load_edits() if edits is None else edits
    ipath = os.path.join(out, "prompts", "index.json")
    index = rb_common.read_json(ipath)
    for cid, spec in edits.items():
        for key, e in index[spec["base"]].items():
            with open(os.path.join(out, "prompts", spec["base"], key + ".txt"),
                      encoding="utf-8", newline="") as f:
                p = apply_edit(f.read(), spec)
            rb_common.write_text(os.path.join(out, "prompts", cid, key + ".txt"), p)
            index.setdefault(cid, {})[key] = dict(e, sha256=rb_common.sha256_text(p),
                                                  chars=len(p))
    rb_common.write_json(ipath, index)
    return index


def cells_for_phase(phase, out, frozen_path=FROZEN, edits=None):
    if phase == "rounds12":
        under = rb_common.read_json(os.path.join(out, "underperforming.json"))
        edits = load_edits() if edits is None else edits
        return {(s["task"], cid, s["model"]) for cid, s in edits.items()
                if under.get(s["task"], {}).get(s["model"], {}).get("triggers")}
    if phase == "heldout":
        fr = rb_common.read_json(frozen_path)
        cells = {(e["task"], e["candidate"], e["model"]) for e in fr["entries"]}
        if any(t == "T" for t, _c, _m in cells):
            # T's input on a held-out session is that session's muse X-cur rep 1.
            cells.add(("X", "X-cur", "muse"))
        return cells
    raise ValueError(phase)


def frozen_entry(task, model, candidate, metrics, round_, candidates, edits=None):
    import rb_requests
    req = rb_requests.request_for(model, task, "<prompt>", rb_requests.budget(task, 0))
    entry = {"task": task, "model": model, "candidate": candidate, "round": round_,
             "params": rb_requests.params_of(req),
             "budget_rule": ("T 8000 (retry 12000 at effort low)" if task == "T" else
                             "min(8192 + 700 n, 32000)") + " + headroom per vendor",
             "metrics": metrics, "evidential": False, "heldout": None}
    if candidate in candidates:
        # R-cur has no fixed-input hash: its prompt is develop's code + the
        # served template, whose sha256 is in <out>/prompts/r_template.json.
        entry["prompt_sha256"] = candidates[candidate].get("fixed_sha256")
        entry["resolved"] = candidates[candidate].get("resolved")
    else:
        spec = (edits or load_edits())[candidate]
        with open(os.path.join(_HERE, "prompts", spec["base"] + ".txt"), encoding="utf-8",
                  newline="") as f:
            text = apply_edit(f.read(), spec)
        entry.update(prompt_text=text, prompt_sha256=rb_common.sha256_text(text),
                     prompt_edit=spec)
    return entry


def write_frozen(entries, path=FROZEN):
    for e in entries:
        e["evidential"] = False
    rb_common.write_json(path, {"schema": "report-bench-frozen/v1",
                                "note": "selection numbers are not evidence (winner's curse); "
                                        "held-out numbers alongside",
                                "entries": entries})
    return path


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("cmd", choices=["underperforming", "edits", "freeze"])
    ap.add_argument("--out", required=True)
    ap.add_argument("--keep", help="JSON {task|model: candidate id} chosen after rounds 1-2")
    a = ap.parse_args(argv)
    out = rb_common.check_out_dir(a.out)
    if a.cmd == "edits":
        idx = add_edit_candidates(out)
        print("index candidates", len(idx))
        return 0
    scores = rb_common.read_json(os.path.join(out, "scores_round0.json"))
    if a.cmd == "underperforming":
        res = {t: underperforming(scores, t) for t in ("X", "T", "R")
               if any(c.startswith(t + "|") for c in scores)}
        rb_common.write_json(os.path.join(out, "underperforming.json"), res)
        for t, ms in res.items():
            for m, d in ms.items():
                print(t, m, d["best_cell"], "triggers", ",".join(d["triggers"]) or "-")
        return 0
    s12 = os.path.join(out, "scores_rounds12.json")
    scores12 = rb_common.read_json(s12) if os.path.exists(s12) else {}
    keep = rb_common.read_json(a.keep) if a.keep else {}
    cands = rb_common.read_json(os.path.join(_HERE, "candidates.json"))
    entries = []
    for t in ("X", "T", "R"):
        for m in sorted({c.split("|")[2] for c in scores if c.startswith(t + "|")}):
            cid = keep.get(f"{t}|{m}")
            if cid:
                cell = scores12[f"{t}|{cid}|{m}"]
                rnd = 1 if cid.endswith("e1") else 2
            else:
                best = best_round0(scores, t, m)
                cid, cell, rnd = best.split("|")[1], scores[best], 0
            metrics = {"mean": cell["mean"], "noise_floor": cell["noise_floor"],
                       "failed_sessions": cell["failed_sessions"]}
            entries.append(frozen_entry(t, m, cid, metrics, rnd, cands))
    print(write_frozen(entries))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
