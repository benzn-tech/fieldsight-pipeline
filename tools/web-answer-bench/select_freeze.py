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


def cell_of(cells_json, step, prompt_id, model):
    for c in _load_cells(cells_json).values():
        if (c["step"], c["prompt_id"], c["model_key"]) == (step, prompt_id, model):
            return c
    raise KeyError((step, prompt_id, model))


def keep_round1(incumbent, edit):
    """Spec rev 4.3 §4.5: keep_edit, and the edit is stable on every non-unsure
    item where the incumbent's majority is good (the edit targets unsure items,
    so hedging that spills onto standard / current items must not pass)."""
    if not keep_edit(incumbent, edit):
        return False
    for item, inc in incumbent["items"].items():
        if inc.get("kind") == "unsure" or inc["majority"] not in GOOD:
            continue
        e = edit["items"].get(item) or {}
        if not e.get("stable") or e.get("majority") not in GOOD:
            return False
    return True


def round1_decisions(cells_r0, cells_r1):
    """[(step, model, incumbent id, edit id, kept)] for every round-1 cell."""
    out = []
    for c in sorted(_load_cells(cells_r1).values(),
                    key=lambda c: (c["step"], c["model_key"])):
        base = prompts.EDITS[c["prompt_id"]]
        inc = cell_of(cells_r0, c["step"], base, c["model_key"])
        out.append((c["step"], c["model_key"], base, c["prompt_id"], keep_round1(inc, c)))
    return out


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


def _has_digit(text):
    return any(ch.isdigit() for ch in grade.blind(text or ""))


def stratified_draw(rows, n=10, seed=0, exclude=(), min_unsure=4):
    """Spec rev 4.3 §4.5 grader check v2: a fresh draw (refs in `exclude` left
    out) with >= min_unsure answers on unsure items, spread over three strata:
    figure-as-answer (v1 label wrong), a figure given (digit in the answer),
    bare refusal (no digit). The rest is drawn across cells as spot_check_sheet."""
    rng = random.Random(seed)
    pool = sorted((r for r in rows if r["run_id"] not in set(exclude)),
                  key=lambda r: r["run_id"])
    rng.shuffle(pool)
    unsure = [r for r in pool if r.get("kind") == "unsure"]
    strata = [[r for r in unsure if r.get("v1_label") == "wrong"],
              [r for r in unsure if r.get("v1_label") != "wrong" and _has_digit(r["answer"])],
              [r for r in unsure if r.get("v1_label") != "wrong"
               and not _has_digit(r["answer"])]]
    picked = []
    while len(picked) < min(min_unsure, len(unsure)) and any(strata):
        for st in strata:
            if st and len(picked) < min_unsure:
                picked.append(st.pop(0))
    taken = {r["run_id"] for r in picked}
    by_cell = {}
    for r in pool:
        if r["run_id"] not in taken and r.get("kind") != "unsure":
            by_cell.setdefault(r.get("cell"), []).append(r)
    while len(picked) < n and any(by_cell.values()):
        for cell in sorted(by_cell, key=str):
            if by_cell[cell] and len(picked) < n:
                picked.append(by_cell[cell].pop(0))
    rng.shuffle(picked)
    return picked


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
    kp = sub.add_parser("keep", help="round-1 keep / reject per (step, model)")
    kp.add_argument("--out", required=True)
    args = ap.parse_args(sys.argv[1:] if argv is None else argv)
    if args.cmd == "keep":
        for step, model, base, edit, kept in round1_decisions(
                os.path.join(args.out, "cells_r0.json"),
                os.path.join(args.out, "cells_r1.json")):
            print(f"{step}/{model}: {base} -> {edit}: {'KEEP' if kept else 'reject'}")
        return 0
    return _sheet_main(args)


if __name__ == "__main__":
    sys.exit(main())
