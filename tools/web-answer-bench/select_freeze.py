"""Pick, keep or reject a prompt per (step, model); write frozen.v1.json; build
the owner's blind spot-check sheet.

Spec: docs/superpowers/specs/2026-10-27-claude-web-answer-bench-design.md §4.4, §5.3
Plan: docs/superpowers/plans/2026-10-28-web-answer-prompt-matrix.md (Task 7)

Inputs are grade.py's cells_r0.json (per cell: p50_ms and, per item, labels,
majority, stable). The tune labels written to frozen.v1.json are never evidence:
best-of-several on 15 samples carries winner's-curse bias, so `evidential` is
always False here, whatever the caller passes.

    C:/Users/camil/wt/benchenv/Scripts/python.exe tools/web-answer-bench/select_freeze.py \
        sheet --out <matrix out dir> --seed <int>
    # writes <out>/spot_check_r0.md (question, gold, answer; no grader label)
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import random
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
if HERE not in sys.path:
    sys.path.insert(0, HERE)

import grade                                             # noqa: E402
import prompts                                           # noqa: E402

GOOD = ("correct", "correctly_unsure")
_RANK = grade._RANK                      # no_answer < wrong < ... < correct


def _load_cells(cells_json):
    if isinstance(cells_json, (str, os.PathLike)):
        with open(cells_json, encoding="utf-8") as f:
            return json.load(f)
    return cells_json


def best_round0(cells_json, step, model):
    """The prompt id of the best round-0 cell for (step, model): most items whose
    majority is good, then fewer `wrong`, then the lower p50."""
    cells = [c for c in _load_cells(cells_json).values()
             if c["step"] == step and c["model_key"] == model]
    if not cells:
        raise KeyError((step, model))

    def key(c):
        majors = [it["majority"] for it in c["items"].values()]
        p50 = c.get("p50_ms")
        return (-sum(m in GOOD for m in majors), sum(m == "wrong" for m in majors),
                p50 if p50 is not None else float("inf"), c["prompt_id"])

    return min(cells, key=key)["prompt_id"]


def keep_edit(incumbent, edit):
    """Spec §4.4: keep the edit only if no item's majority is worse and at least
    one item is better on which the incumbent was stable. An item the edit has
    no rankable label for counts as worse."""
    better = False
    for item, inc in incumbent["items"].items():
        e = edit["items"].get(item)
        r_inc = _RANK.get(inc["majority"])
        r_edit = _RANK.get(e["majority"]) if e else None
        if r_edit is None:
            return False
        if r_inc is None:
            continue
        if r_edit < r_inc:
            return False
        if r_edit > r_inc and inc.get("stable"):
            better = True
    return better


def params_for(step, model):
    """The parameters that are part of a cell (spec §4.4), as matrix.py sends them."""
    import matrix
    import openrouter_bench
    import claude_verify
    if model in matrix.CLAUDE:
        name, max_uses = matrix.CLAUDE[model]
        search = step != "draft"
        return {"vendor": "anthropic", "model": name,
                "tool": matrix.TOOL_VERSION if search else None,
                "max_uses": max_uses if search else None, "effort": "low",
                "max_tokens": claude_verify.MAX_TOKENS,
                "user_location": dict(claude_verify.USER_LOCATION) if search else None,
                "continuations": 1}
    web = step != "draft"
    payload = openrouter_bench.build_payload(model, "", web=web,
                                             max_tokens=matrix.OR_MAX_TOKENS[step])
    return {"vendor": "openrouter", "model": payload["model"],
            "reasoning": payload["reasoning"], "provider": payload.get("provider"),
            "web": web, "max_tokens": payload["max_tokens"],
            "retries": 1 if step == "verify" else 0}


def write_frozen(path, entries):
    """Write / update frozen.v1.json. One entry per (step, model); an entry
    replaces only its own (step, model), never another model's."""
    existing = []
    if os.path.exists(path):
        with open(path, encoding="utf-8") as f:
            existing = json.load(f).get("entries", [])
    by_key = {(e["step"], e["model"]): e for e in existing}
    for e in entries:
        text = e.get("text") or prompts.load(e["prompt_id"])
        by_key[(e["step"], e["model"])] = {
            "step": e["step"], "model": e["model"], "prompt_id": e["prompt_id"],
            "text": text, "sha256": hashlib.sha256(text.encode("utf-8")).hexdigest(),
            "params": e["params"], "round": e["round"],
            "tune_labels": e.get("tune_labels") or {}, "evidential": False}
    data = {"version": "frozen.v1", "entries": [by_key[k] for k in sorted(by_key)]}
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8", newline="") as f:
        json.dump(data, f, indent=2)
        f.write("\n")
    os.replace(tmp, path)
    return data


def _cell_text(text):
    return " ".join((text or "").replace("|", "\\|").split())


def spot_check_sheet(grades, n=10, seed=0):
    """Markdown for the owner: n answers drawn across cells (seeded), each with
    its opaque run id, question, gold and blinded answer, and an empty label
    column. No grader label, model, prompt or cell is printed."""
    rows = sorted(grades, key=lambda r: r["run_id"])
    random.Random(seed).shuffle(rows)
    by_cell = {}
    for r in rows:
        by_cell.setdefault(r.get("cell"), []).append(r)
    picked = []
    while len(picked) < n and any(by_cell.values()):
        for cell in list(by_cell):
            if by_cell[cell] and len(picked) < n:
                picked.append(by_cell[cell].pop(0))
    random.Random(seed + 1).shuffle(picked)
    lines = ["# Spot check, round 0 (blind)", "",
             "Label each answer against the gold facts: correct, partial, wrong, "
             "correctly_unsure (no-answer items), hallucinated_source. Rubric: spec §5.1.",
             "", "| ref | question | gold | answer | your label |",
             "|---|---|---|---|---|"]
    for r in picked:
        gold = r.get("gold")
        gold = "; ".join(gold) if isinstance(gold, (list, tuple)) else gold
        lines.append(f"| {r['run_id']} | {_cell_text(r['question'])} | {_cell_text(gold)} "
                     f"| {_cell_text(grade.blind(r['answer']))} |  |")
    return "\n".join(lines) + "\n"


def _sheet_main(args):
    calls = {}
    with open(os.path.join(args.out, "matrix_r0_calls.jsonl"), encoding="utf-8") as f:
        for line in f:
            if line.strip():
                r = json.loads(line)
                calls[r["run_id"]] = r
    gold = grade.load_gold(args.set)
    rows = []
    with open(os.path.join(args.out, "grades_r0.jsonl"), encoding="utf-8") as f:
        for line in f:
            g = json.loads(line)
            r = calls.get(g["run_id"])
            if not r or not (r.get("answer") or "").strip():
                continue
            item = gold[r["item"]]
            rows.append({"run_id": r["run_id"], "cell": r["cell"],
                         "question": item["question"],
                         "gold": list(item.get("expected") or []) +
                         list(item.get("variants") or []),
                         "answer": r["answer"]})
    md = spot_check_sheet(rows, n=args.n, seed=args.seed)
    with open(os.path.join(args.out, "spot_check_r0.md"), "w", encoding="utf-8",
              newline="") as f:
        f.write(md)
    print(f"spot check: {min(args.n, len(rows))} answers -> spot_check_r0.md")
    return 0


def main(argv=None):
    ap = argparse.ArgumentParser(prog="select_freeze")
    sub = ap.add_subparsers(dest="cmd", required=True)
    sh = sub.add_parser("sheet")
    sh.add_argument("--out", required=True)
    sh.add_argument("--seed", type=int, required=True)
    sh.add_argument("--n", type=int, default=10)
    sh.add_argument("--set", default=os.path.join(HERE, "set.v1.jsonl"))
    args = ap.parse_args(sys.argv[1:] if argv is None else argv)
    return _sheet_main(args)


if __name__ == "__main__":
    sys.exit(main())
