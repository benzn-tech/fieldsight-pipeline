"""Blind grader for matrix answers: one DeepSeek call per answer, no cell label.

Spec: docs/superpowers/specs/2026-10-27-claude-web-answer-bench-design.md §5.1, §5.2, §4.4
Plan: docs/superpowers/plans/2026-10-28-web-answer-prompt-matrix.md (Task 6)

    C:/Users/camil/wt/benchenv/Scripts/python.exe tools/web-answer-bench/grade.py \
        --out <matrix out dir> --calls matrix_r0_calls.jsonl --seed <int> --dry-run
    # state the printed count, then: ... --confirm-calls <count>

Exit codes: 0 done, 2 refused (arguments / env / no calls file, or the grader's
OpenRouter upper is over what is left of --openrouter-cap), 3 the OpenRouter
pool ran out mid-run (graded rows are kept; --only-ungraded finishes it).

`--openrouter-key-file` / `--openrouter-cap` as matrix.py: the grader's dollars
go to the shared ledger's `openrouter` pool (spec 2026-10-31 Part A).

The grader sees the question, the gold facts, the kind and the blinded answer --
never a run id, cell, prompt or model name. Answers go out in a seeded shuffle.

The grader cannot say `hallucinated_source`: it never sees what was retrieved,
and a model is not trusted to judge that. The §5.2 domain check runs here,
mechanically, on the raw answer, and is recorded as `source_flag` beside the
grader's label for the owner to confirm; it never downgrades the label. Draft
text (a draft cell, or a draft handed to the reader after a failed verify /
single call) searched nothing, so it is never checked against another call's
domains (`source_checked: false`).

`--only-ungraded` re-grades only runs that are ungraded or missing and merges
them into grades_r0.jsonl. A grader 429 is retried once after 2 s.

Three labels exist besides the rubric's:
  ungraded   the grader's reply was not JSON or not a known label (counted,
             never read as `wrong`)
  no_answer  the run produced nothing a reader would see; not sent to the
             grader, ranked below `wrong`
  (skipped)  a verify run with no gemini draft (`no_draft`): not the verify
             prompt's doing, so it gets no label at all
Nothing printed or written here carries question or answer text.
"""
from __future__ import annotations

import argparse
import functools
import json
import os
import random
import re
import statistics
import sys
import time
from collections import Counter
from pathlib import Path

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(os.path.dirname(HERE))
for _p in (HERE, os.path.join(REPO, "src")):
    if _p not in sys.path:
        sys.path.insert(0, _p)

import ledger as ledger_mod                               # noqa: E402
import matrix                                            # noqa: E402
import openrouter_bench                                  # noqa: E402
import prompts                                           # noqa: E402

ORDER = ["wrong", "hallucinated_source", "partial", "correctly_unsure", "correct"]
# What the grader may answer. hallucinated_source is set only from an
# owner-confirmed source_flag, never by a model.
GRADER_LABELS = ["wrong", "partial", "correctly_unsure", "correct"]
DRAFT_SOURCES = ("draft", "fallback")
RETRY_429_S = 2.0
NO_ANSWER = "no_answer"
UNGRADED = "ungraded"
_RANK = {NO_ANSWER: -1, **{label: i for i, label in enumerate(ORDER)}}
GRADER_PROMPT = "grader_v1"
GRADER_TIMEOUT_S = 30.0          # not on a reader's clock
GRADER_MAX_TOKENS = 256
# Upper input of one grader prompt (question + gold + an answer of <= 2048
# tokens + the rubric), for the dry run's OpenRouter $ upper.
GRADER_UPPER_INPUT_TOKENS = 8000

_UNVERIFIED = re.compile(r"\s*\[\s*unverified\s*\]", re.I)
_MD_LINK = re.compile(r"\[([^\]]*)\]\((https?://[^)\s]+)\)")
_URL = re.compile(r"https?://[^\s)\]>\"']+", re.I)
_FENCE = re.compile(r"^\s*```(?:json)?\s*|\s*```\s*$", re.MULTILINE)


def _host(url):
    m = re.match(r"^[a-z][a-z0-9+.-]*://([^/?#]+)", url, re.I)
    host = m.group(1).split("@")[-1].split(":")[0].lower() if m else ""
    return host[4:] if host.startswith("www.") else host


def _strip_url(url):
    return url.rstrip(".,;:!?")


def _url_to_host(m):
    url = _strip_url(m.group(0))
    return _host(url) + m.group(0)[len(url):]          # keep trailing punctuation


def blind(text):
    """The grader's copy: no [unverified] markers, links reduced to their text,
    bare URLs to their host."""
    t = _UNVERIFIED.sub("", text or "")
    t = _MD_LINK.sub(lambda m: m.group(1), t)
    t = _URL.sub(_url_to_host, t)
    return re.sub(r"[ \t]{2,}", " ", t).strip()


def source_flag(answer, domains, urls=()):
    """Hosts of http(s) literals in the answer that are in no retrieved domain
    (spec §5.2). A literal that is exactly a result URL counts as retrieved."""
    allowed = {(d or "").lower().removeprefix("www.") for d in domains or () if d}
    seen = set(urls or ())
    flagged = []
    for m in _URL.finditer(answer or ""):
        url = _strip_url(m.group(0))
        host = _host(url)
        if not host or url in seen:
            continue
        if host in allowed or any(host.endswith("." + d) for d in allowed):
            continue
        if host not in flagged:
            flagged.append(host)
    return flagged


def _parse_label(text):
    try:
        parsed = json.loads(_FENCE.sub("", text or "").strip())
    except Exception:                                     # noqa: BLE001 - any shape but ours
        return None
    label = parsed.get("label") if isinstance(parsed, dict) else None
    return label if label in GRADER_LABELS else None


def _lines(values):
    return "\n".join(f"- {v}" for v in values) if values else "(none)"


def grade_one(question, gold, answer, domains, *, urls=(), call=openrouter_bench.call,
              check_sources=True, sleep=time.sleep):
    flagged = source_flag(answer, domains, urls) if check_sources else []
    prompt = prompts.load(GRADER_PROMPT).format(
        question=question, kind=gold.get("kind") or "standard",
        expected=_lines(gold.get("expected") or []),
        variants=_lines(gold.get("variants") or []), answer=blind(answer))
    cost = 0.0
    for attempt in (1, 2):
        r = call("grader", prompt, web=False, timeout=GRADER_TIMEOUT_S,
                 max_tokens=GRADER_MAX_TOKENS)
        if isinstance(r.get("openrouter_cost_usd"), (int, float)):
            cost += r["openrouter_cost_usd"]
        if r.get("http_status") != 429 or attempt == 2:
            break
        sleep(RETRY_429_S)
    label = _parse_label(r.get("text")) if r.get("outcome") == "ok" else None
    return {"label": label or UNGRADED, "raw_ok": label is not None,
            "source_checked": bool(check_sources), "source_flag": bool(flagged),
            "flagged_hosts": flagged, "grader_outcome": r.get("outcome"),
            "grader_calls": attempt, "grader_cost_usd": round(cost, 8)}


def majority(labels):
    """(label, stable) over one cell's reps of one item. `ungraded` is left out;
    no majority -> the worst label. Stable = three reps, all graded, all equal."""
    graded = [x for x in labels if x != UNGRADED]
    if not graded:
        return UNGRADED, False
    label, n = Counter(graded).most_common(1)[0]
    if n * 2 <= len(graded):
        label = min(graded, key=lambda x: _RANK[x])
    stable = len(labels) == 3 and len(graded) == 3 and len(set(graded)) == 1
    return label, stable


def load_gold(path):
    with open(path, encoding="utf-8") as f:
        rows = [json.loads(line) for line in f if line.strip()]
    return {r["id"]: r for r in rows if r.get("split") == "tune"}


def _read_calls(path):
    with open(path, encoding="utf-8") as f:
        return [json.loads(line) for line in f if line.strip()]


def split(rows):
    """(to grade, no answer, skipped)."""
    skipped = [r for r in rows if r.get("outcome") == "no_draft"]
    live = [r for r in rows if r.get("outcome") != "no_draft"]
    return ([r for r in live if (r.get("answer") or "").strip()],
            [r for r in live if not (r.get("answer") or "").strip()], skipped)


def _read_grades(out):
    path = os.path.join(out, "grades_r0.jsonl")
    if not os.path.exists(path):
        return []
    with open(path, encoding="utf-8") as f:
        return [json.loads(line) for line in f if line.strip()]


def pending(todo, grades):
    """--only-ungraded: the runs with no label yet, or an ungraded one."""
    labelled = {g["run_id"] for g in grades if g.get("label") != UNGRADED}
    return [r for r in todo if r["run_id"] not in labelled]


def shuffled(rows, seed):
    rows = sorted(rows, key=lambda r: r["run_id"])
    random.Random(seed).shuffle(rows)
    return rows


def build_cells(rows, labels, gold):
    """Per cell, per item: the labels by rep, the majority, `stable`; plus the
    cell's p50 latency (the tie-break of select_freeze.best_round0)."""
    cells = {}
    for r in sorted(rows, key=lambda r: (r["cell"], r["item"], r["rep"])):
        c = cells.setdefault(r["cell"], {"step": r["step"], "prompt_id": r["prompt_id"],
                                         "model_key": r["model_key"], "_ms": [],
                                         "items": {}})
        if r.get("calls"):
            c["_ms"].append(r["latency_ms"])
        if r["run_id"] not in labels:
            continue
        it = c["items"].setdefault(r["item"], {"labels": []})
        it["labels"].append(labels[r["run_id"]])
    for c in cells.values():
        ms = c.pop("_ms")
        c["p50_ms"] = int(statistics.median(ms)) if ms else None
        for item, it in c["items"].items():
            it["majority"], it["stable"] = majority(it["labels"])
            it["kind"] = (gold.get(item) or {}).get("kind")
    return cells


def _refuse(msg):
    print("grade: " + msg, file=sys.stderr)
    raise SystemExit(2)


def validate(argv, env, repo_root=REPO):
    ap = argparse.ArgumentParser(prog="grade")
    ap.add_argument("--out", required=True)
    ap.add_argument("--calls", required=True)
    ap.add_argument("--seed", type=int, required=True)
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--confirm-calls", type=int)
    ap.add_argument("--only-ungraded", action="store_true")
    ap.add_argument("--set", default=os.path.join(HERE, "set.v1.jsonl"))
    ap.add_argument("--openrouter-key-file")
    ap.add_argument("--openrouter-cap", type=float, default=ledger_mod.OPENROUTER_CAP_USD)
    args = ap.parse_args(argv)
    out = Path(args.out).resolve()
    if out == Path(repo_root).resolve() or Path(repo_root).resolve() in out.parents:
        _refuse("--out must be outside the repository")
    if args.openrouter_cap < 0:
        _refuse("--openrouter-cap must be >= 0")
    if args.openrouter_key_file and not os.path.isfile(args.openrouter_key_file):
        _refuse("--openrouter-key-file does not exist")
    ledger_mod.refuse_if_locked(out, _refuse)
    calls = Path(args.calls)
    calls = calls if calls.is_absolute() else out / calls
    if not calls.exists():
        _refuse(f"no calls file {calls.name} in --out (run matrix.py first)")
    args.out, args.calls = str(out), str(calls)
    stale = matrix.stale_rows(_read_calls(args.calls))
    if stale:
        _refuse(f"{calls.name} holds {len(stale)} rows of cells not in round 0; "
                "grade a fresh matrix --out")
    if not args.dry_run:
        if not args.openrouter_key_file and not env.get("CORROBORATION_API_KEY"):
            _refuse("missing environment variable(s): CORROBORATION_API_KEY")
        todo = split(_read_calls(args.calls))[0]
        if args.only_ungraded:
            todo = pending(todo, _read_grades(args.out))
        n = len(todo)
        if args.confirm_calls != n:
            _refuse(f"--confirm-calls must equal the answers to grade {n} "
                    "(run --dry-run first)")
    return args


def grader_upper():
    return ledger_mod.or_upper(openrouter_bench.PROFILES["grader"]["model"],
                               GRADER_UPPER_INPUT_TOKENS, GRADER_MAX_TOKENS)


def run(args, *, call=openrouter_bench.call, stdout=sys.stdout, sleep=time.sleep):
    rows = _read_calls(args.calls)
    ledger_path = os.path.join(args.out, "anthropic_ledger.json")
    or_cap = getattr(args, "openrouter_cap", ledger_mod.OPENROUTER_CAP_USD)
    todo, empty, skipped = split(rows)
    previous = _read_grades(args.out) if args.only_ungraded else []
    if args.only_ungraded:
        todo = pending(todo, previous)
    info = {"to_grade": len(todo), "no_answer": len(empty), "skipped": len(skipped)}
    print(f"grade: {len(todo)} grader calls (1 per answer, no retry), {len(empty)} runs "
          f"without an answer (no_answer), {len(skipped)} skipped (no_draft), "
          f"seed {args.seed}", file=stdout)
    upper = len(todo) * grader_upper()
    room = ledger_mod.openrouter_room(ledger_path, or_cap)
    print(f"grade: openrouter upper ${upper:.2f}, pool "
          f"${ledger_mod.openrouter_spent(ledger_path):.2f} of ${or_cap:g}, "
          f"room ${room:.2f}", file=stdout)
    if upper > room + 1e-9:
        print(f"grade: refused, OpenRouter upper ${upper:.2f} > room ${room:.2f} "
              "(raise --openrouter-cap or split the run)", file=stdout)
        return 2, dict(info, refused="openrouter_cap")
    if args.dry_run:
        return 0, dict(info, dry_run=True)

    gold = load_gold(args.set)
    redo = {r["run_id"] for r in todo}
    out_rows = [g for g in previous if g["run_id"] not in redo]
    have = {g["run_id"] for g in out_rows}
    for r in empty:
        if r["run_id"] not in have:
            out_rows.append({"run_id": r["run_id"], "label": NO_ANSWER, "raw_ok": None,
                             "source_checked": False, "source_flag": False,
                             "flagged_hosts": []})
    cap_hit = False
    for n, r in enumerate(shuffled(todo, args.seed)):
        if ledger_mod.openrouter_room(ledger_path, or_cap) <= 1e-9:
            cap_hit = True
            print(f"grade: OpenRouter pool spent (${or_cap:g}), {len(todo) - n} left "
                  "ungraded", file=stdout)
            break
        g = gold[r["item"]]
        draft_text = r.get("step") == "draft" or r.get("answer_source") in DRAFT_SOURCES
        res = grade_one(g["question"], g, r["answer"], r.get("result_domains") or [],
                        urls=r.get("result_urls") or [], call=call,
                        check_sources=not draft_text, sleep=sleep)
        ledger_mod.add_openrouter_spend(ledger_path, res["grader_cost_usd"])
        out_rows.append(dict(res, run_id=r["run_id"], order=n))
    tmp = os.path.join(args.out, "grades_r0.jsonl.tmp")
    with open(tmp, "w", encoding="utf-8", newline="") as f:
        for g in out_rows:
            f.write(json.dumps(g) + "\n")
    os.replace(tmp, os.path.join(args.out, "grades_r0.jsonl"))
    labels = {g["run_id"]: g["label"] for g in out_rows}
    cells = build_cells(rows, labels, gold)
    with open(os.path.join(args.out, "cells_r0.json"), "w", encoding="utf-8",
              newline="") as f:
        json.dump(cells, f, indent=2)
    counts = Counter(labels.values())
    print("grade: labels " + json.dumps(dict(sorted(counts.items()))), file=stdout)
    return (3 if cap_hit else 0), dict(info, labels=dict(counts), openrouter_cap_hit=cap_hit)


def main(argv=None):
    args = validate(sys.argv[1:] if argv is None else argv, os.environ)
    try:
        key, _ = ledger_mod.load_or_key(args.openrouter_key_file, "CORROBORATION_API_KEY")
    except ValueError as exc:
        _refuse(str(exc))
    code, _ = run(args, call=functools.partial(openrouter_bench.call, key=key or None))
    return code


if __name__ == "__main__":
    sys.exit(main())
