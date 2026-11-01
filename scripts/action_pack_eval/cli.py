"""Action-pack eval harness CLI (spec §8). Run from the repo root:

  python -m scripts.action_pack_eval.cli <command> ...

Offline (files in, files out; every write must sit under action_pack_eval_runs/):
  validate-gold      --gold G
  score              --gold G --run R --annotations A [--second A2] [--prices P]
                     [--thresholds T] --out DIR        -> DIR/report.json + DIR/report.md
                     exit 0 only when every gate passes
  make-blind-subset  --run R --annotations A (--n N | --fraction F) --seed S --out FILE.jsonl
  agreement          --first A --second A2 --out FILE.json
  compare            --baseline R1 R2 [...] [--variant V ...] --out DIR
                     (report.json files; refuses with fewer than two baseline runs)
  attach-traces      --run R --traces TRACES.jsonl --out FILE.json

Live, AFTER MERGE, TEST ONLY (token from env ACTION_PACK_EVAL_TOKEN; README):
  submit             --gold G --base-url URL [--site-map M] [--config-label L]
                     [--run-id ID] [--poll-s S] [--max-wait-s S] --out FILE.json
  fetch              --run R --base-url URL --out FILE.json
"""
from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

from scripts.action_pack_eval import annotations as ann_mod
from scripts.action_pack_eval import compare as compare_mod
from scripts.action_pack_eval import gold as gold_mod
from scripts.action_pack_eval import live
from scripts.action_pack_eval import metrics
from scripts.action_pack_eval import paths
from scripts.action_pack_eval import report as report_mod
from scripts.action_pack_eval import runs as runs_mod

TOKEN_ENV = "ACTION_PACK_EVAL_TOKEN"


def _print_warnings(warnings):
    for w in warnings:
        print(f"WARNING: {w}")


def _labels(path, run=None, gold=None):
    rows = ann_mod.load_annotations(path)
    errors = ann_mod.validate_annotations(rows, run=run, gold=gold)
    if errors:
        raise SystemExit(f"invalid labels {path}:\n  " + "\n  ".join(errors))
    return rows


def cmd_validate_gold(args):
    entries = gold_mod.load_gold(args.gold)
    errors, warnings = gold_mod.validate_gold(entries)
    _print_warnings(warnings)
    for e in errors:
        print(f"ERROR: {e}")
    print(f"{len(entries)} instructions, {len(errors)} errors, {len(warnings)} warnings")
    return 1 if errors else 0


def cmd_score(args):
    out = paths.require_run_dir(args.out)
    gold, warnings = gold_mod.load_valid_gold(args.gold)
    sha = gold_mod.gold_sha256(args.gold)
    run = runs_mod.load_run(args.run)
    warnings = list(warnings)
    if run.get("inputs_sha256") and run["inputs_sha256"] != gold_mod.inputs_sha256(gold):
        raise SystemExit("the run was submitted from different instructions than this gold "
                         "set (id/site/instruction changed); score it against the gold file "
                         "it was submitted from")
    if run.get("gold_sha256") and run["gold_sha256"] != sha:
        warnings.append("the gold file changed since submission (expected actions or other "
                        f"non-input fields: {run['gold_sha256'][:12]} vs {sha[:12]})")
    known = {e["id"] for e in gold}
    stray = sorted({str(r.get("gold_id")) for r in run["packs"]} - known)
    if stray:
        raise SystemExit(f"run packs name gold ids not in the gold set: {stray}")
    labels = _labels(args.annotations, run, gold)
    second = _labels(args.second, run, gold) if args.second else None
    rep = report_mod.build_report(
        gold, run, labels, prices=metrics.load_prices(args.prices),
        thresholds=metrics.load_thresholds(args.thresholds), second=second,
        gold_sha256=sha, warnings=warnings)
    paths.write_json(out / "report.json", rep)
    paths.write_text(out / "report.md", report_mod.render_markdown(rep))
    _print_warnings(warnings)
    print(f"overall: {rep['overall']} -> {out / 'report.md'}")
    return 0 if rep["overall"] == metrics.PASS else 1


def cmd_blind(args):
    dest = paths.require_run_dir(args.out)
    run = runs_mod.load_run(args.run)
    rows = _labels(args.annotations, run)
    items = ann_mod.make_blind_subset(run, rows, n=args.n, fraction=args.fraction,
                                      seed=args.seed)
    paths.write_jsonl(dest, items)
    print(f"{len(items)} elements -> {dest}")
    return 0


def cmd_agreement(args):
    dest = paths.require_run_dir(args.out)
    out = metrics.agreement(_labels(args.first), _labels(args.second))
    paths.write_json(dest, out)
    print(f"n {out['n']}: supported_by_refs {out['supported_by_refs']}, "
          f"is_standards_claim {out['is_standards_claim']}")
    return 0


def cmd_compare(args):
    out = paths.require_run_dir(args.out)
    try:
        cmp = compare_mod.compare([paths.read_json(p) for p in args.baseline],
                                  [paths.read_json(p) for p in args.variant or []])
    except compare_mod.CompareRefused as exc:
        raise SystemExit(f"compare refused: {exc}") from None
    paths.write_json(out / "compare.json", cmp)
    paths.write_text(out / "compare.md", compare_mod.render_markdown(cmp))
    print(f"-> {out / 'compare.md'}")
    return 0


def cmd_attach_traces(args):
    dest = paths.require_run_dir(args.out)
    run = runs_mod.attach_traces(runs_mod.load_run(args.run),
                                 paths.read_records(args.traces, key="traces"))
    paths.write_json(dest, run)
    return 0


def _token():
    tok = os.environ.get(TOKEN_ENV, "").strip()
    if not tok:
        raise SystemExit(f"set {TOKEN_ENV} to a TEST platform_admin id token")
    return tok


def cmd_submit(args):
    dest = paths.require_run_dir(args.out)
    token = _token()
    gold, warnings = gold_mod.load_valid_gold(args.gold)
    _print_warnings(warnings)
    site_map = paths.read_json(args.site_map) if args.site_map else {}
    run = live.submit(gold, base_url=args.base_url, token=token,
                      client=live.urllib_client(timeout=30), site_map=site_map,
                      sleep=live.time.sleep, clock=live.time.monotonic, poll_s=args.poll_s,
                      max_wait_s=args.max_wait_s, config_label=args.config_label,
                      run_id=args.run_id or Path(dest).stem,
                      gold_sha256=gold_mod.gold_sha256(args.gold),
                      inputs_sha256=gold_mod.inputs_sha256(gold))
    paths.write_json(dest, run)
    errors = [r for r in run["packs"] if r.get("error")]
    print(f"{len(run['packs'])} instructions, {len(errors)} without a finished pack -> {dest}")
    return 0


def cmd_fetch(args):
    dest = paths.require_run_dir(args.out)
    token = _token()
    run = live.fetch(runs_mod.load_run(args.run), base_url=args.base_url, token=token,
                     client=live.urllib_client(timeout=30))
    paths.write_json(dest, run)
    return 0


def build_parser():
    p = argparse.ArgumentParser(prog="python -m scripts.action_pack_eval.cli",
                                description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = p.add_subparsers(dest="command", required=True)

    s = sub.add_parser("validate-gold")
    s.add_argument("--gold", required=True)
    s.set_defaults(fn=cmd_validate_gold)

    s = sub.add_parser("score")
    s.add_argument("--gold", required=True)
    s.add_argument("--run", required=True)
    s.add_argument("--annotations", required=True)
    s.add_argument("--second", help="second annotator's labels (the blind subset)")
    s.add_argument("--prices", help="price table JSON; without it cost is tokens only")
    s.add_argument("--thresholds", help="JSON overriding metrics.DEFAULT_THRESHOLDS keys")
    s.add_argument("--out", required=True)
    s.set_defaults(fn=cmd_score)

    s = sub.add_parser("make-blind-subset")
    s.add_argument("--run", required=True)
    s.add_argument("--annotations", required=True, help="the FIRST annotator's labels")
    g = s.add_mutually_exclusive_group(required=True)
    g.add_argument("--n", type=int)
    g.add_argument("--fraction", type=float)
    s.add_argument("--seed", type=int, required=True)
    s.add_argument("--out", required=True)
    s.set_defaults(fn=cmd_blind)

    s = sub.add_parser("agreement")
    s.add_argument("--first", required=True)
    s.add_argument("--second", required=True)
    s.add_argument("--out", required=True)
    s.set_defaults(fn=cmd_agreement)

    s = sub.add_parser("compare")
    s.add_argument("--baseline", nargs="+", required=True)
    s.add_argument("--variant", nargs="*")
    s.add_argument("--out", required=True)
    s.set_defaults(fn=cmd_compare)

    s = sub.add_parser("attach-traces")
    s.add_argument("--run", required=True)
    s.add_argument("--traces", required=True)
    s.add_argument("--out", required=True)
    s.set_defaults(fn=cmd_attach_traces)

    s = sub.add_parser("submit", help="LIVE, after merge, TEST only")
    s.add_argument("--gold", required=True)
    s.add_argument("--base-url", required=True, help="TEST org-api base, ending /api/org")
    s.add_argument("--site-map", help="JSON {site name: TEST site uuid}")
    s.add_argument("--config-label", default="baseline")
    s.add_argument("--run-id")
    s.add_argument("--poll-s", type=float, default=15)
    s.add_argument("--max-wait-s", type=float, default=900)
    s.add_argument("--out", required=True)
    s.set_defaults(fn=cmd_submit)

    s = sub.add_parser("fetch", help="LIVE, after merge, TEST only")
    s.add_argument("--run", required=True)
    s.add_argument("--base-url", required=True)
    s.add_argument("--out", required=True)
    s.set_defaults(fn=cmd_fetch)
    return p


def main(argv=None):
    args = build_parser().parse_args(argv)
    return args.fn(args)


if __name__ == "__main__":
    sys.exit(main())
