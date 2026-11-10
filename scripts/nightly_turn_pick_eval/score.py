"""Pre-registered scoring of a J2 run (N0 pre-registration §6 bars, §7 reported numbers).

    python -m scripts.nightly_turn_pick_eval.score --run scripts/nightly_turn_pick_eval/runs/fit_v0
    python -m scripts.nightly_turn_pick_eval.score --run .../runs/heldout            # once
    python -m scripts.nightly_turn_pick_eval.score --run .../runs/heldout --addendum 2026-11-20
    python -m scripts.nightly_turn_pick_eval.score --run .../runs/heldout --publish

Each bar is a small `score_*` function so its arithmetic can be pinned at the boundary with
hand-built fixtures; `build_summary` is the glue over the run, data and label files. Bars 1-3
are judged per rep and take the worst rep (fail > pending > insufficient > pass); reps are
never pooled.

Owner files:
- `runs/<name>/flags.owner.jsonl` (written here): bar 3(a)'s mechanical flags, ids only. The
  owner answers in `runs/<name>/done.flags.owner.jsonl` with `{"flag_id", "confirmed": bool}`.
- `runs/<name>/adjudicate.owner.jsonl` (written here): owner-sample items where an accepted
  J2 link in any rep is not correct against the agent's gold; ids and turn indexes only. The
  owner answers in `runs/<name>/done.adjudicate.owner.jsonl` with Track C gold rows.
- Track C's `labels/done.owner.jsonl`: the owner's blind answers to `labels/todo.owner.jsonl`.

A held-out run is scored once (`summary.json`, provisional while owner files are incomplete)
and re-scored at most once more with `--addendum <date>` when they are complete. A fit run may
be scored any number of times. `--publish` copies the run's newest summary to
`results/summary.json` (a fit run's only with `--fit-ok`).
"""
from __future__ import annotations

import argparse
import json
import re
import statistics
from collections import Counter, defaultdict
from datetime import datetime
from pathlib import Path

import nightly_turn_pick as ntp
from item_text_match import twin_flags

from scripts.item_support_eval import run as tc_run
from scripts.item_support_eval import score as tc_score
from scripts.item_support_eval import store
from scripts.nightly_turn_pick_eval import run as run_mod

PASS, FAIL, INSUFFICIENT, PENDING = "pass", "fail", "insufficient", "pending"
NOT_JUDGED, NOT_APPLICABLE = "not_judged", "not_applicable"
ORDER = (FAIL, PENDING, INSUFFICIENT, PASS)

LINK_ERROR_BAR = 0.05
MIN_CROSS = 30                 # bar 2's own n; NOT Track C's MIN_LINKS (pre-registration §6.2)
REP_AGREEMENT_BAR = 0.90
COVERAGE_LINE = 0.60
LEXICAL_POINT = {"floor": 0.30, "margin": 0.05, "nothing_close": 0.10}
LEXICAL_PATHS = ("quote", "window")
_DATE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
_REF_LIKE = re.compile(r"[0-9a-f]{12}:\d+:")
_FORBIDDEN_KEYS = {"item_ref", "session", "text", "item_text", "turn_text", "flag_id",
                   "raw_pick", "turns"}


# ---------------------------------------------------------------------------------------
# Verdicts and bars
# ---------------------------------------------------------------------------------------

def worst(*verdicts):
    judged = [v for v in verdicts if v in ORDER]
    return min(judged, key=ORDER.index) if judged else NOT_JUDGED


def per_rep_verdict(per_rep):
    return worst(*(r["verdict"] for r in per_rep))


def score_bar1(rows, missing_gold=0):
    """Link precision: Track C's rule (cp_upper, n >= 59, no accepted link without gold)."""
    return tc_score.score_link_precision(rows, missing_gold)


def score_bar2(rows, missing_gold=0):
    """Cross-language stratum: own cp_upper; n < 30 insufficient, else bound <= 0.05."""
    n = len(rows)
    wrong = sum(1 for r in rows if not r["correct"])
    ub = tc_score.cp_upper(wrong, n) if n else None
    if missing_gold or n < MIN_CROSS:
        verdict = INSUFFICIENT
    else:
        verdict = PASS if ub <= LINK_ERROR_BAR else FAIL
    return {"accepted": n, "wrong": wrong, "missing_gold": missing_gold, "cp_upper": ub,
            "verdict": verdict}


def score_bar3(flags, by_gold, pairs):
    """Twins: confirmed flags (a), by-gold hits (b) and pairs (c) must all be 0; an
    unanswered flag (a non-bool answer included) leaves the bar pending."""
    confirmed = sum(1 for f in flags if f.get("confirmed") is True)
    open_ = sum(1 for f in flags if not isinstance(f.get("confirmed"), bool))
    if confirmed or by_gold or pairs:
        verdict = FAIL
    else:
        verdict = PENDING if open_ else PASS
    return {"flags": len(flags), "confirmed": confirmed, "unanswered": open_,
            "by_gold": by_gold, "pairs": pairs, "verdict": verdict}


def flag_answers(rows):
    """{flag_id: bool}; only a JSON boolean is an answer (Track C A14)."""
    return tc_score.flag_decisions(rows)


def score_bar4(raw, reps):
    """Rep agreement over items with a prompt slot. raw: {ref: [raw pick per rep]}; null is an
    answer, a failed call agrees only with a failed call. One rep is not judged."""
    if reps < 2:
        return {"items": len(raw), "agree": None, "agreement": None, "verdict": NOT_JUDGED}
    n = len(raw)
    agree = sum(1 for picks in raw.values() if len(set(picks)) == 1)
    rate = agree / n if n else None
    verdict = INSUFFICIENT if not n else PASS if rate >= REP_AGREEMENT_BAR else FAIL
    return {"items": n, "agree": agree, "agreement": rate, "verdict": verdict}


def score_bar5(todo_refs, agent, owner):
    """Agent-owner agreement on the 45-item sample; pending until every item is answered."""
    pairs = [(agent[r], owner[r]) for r in todo_refs if r in agent and r in owner]
    missing = len(todo_refs) - len(pairs)
    if not todo_refs or missing:
        return {"n": len(todo_refs), "answered": len(pairs), "agreement": None,
                "verdict": PENDING}
    out = tc_score.score_agreement(pairs)
    return {"n": len(todo_refs), "answered": len(pairs), "agree": out["agree"],
            "agreement": out["agreement"], "verdict": out["verdict"]}


def overall_verdict(bar2, on, off, bar5):
    """(verdict, allow_cross_language). on/off: {"bar1","bar3","bar4"} verdicts with
    cross-language links counted / counted as not linked."""
    if bar2 == PASS:
        return worst(on["bar1"], on["bar3"], on["bar4"], bar5), True
    return worst(off["bar1"], off["bar3"], off["bar4"], bar5), False


# ---------------------------------------------------------------------------------------
# Glue
# ---------------------------------------------------------------------------------------

class Ctx:
    def __init__(self, sessions, gold, decisions):
        self.sessions = {s["ref"]: s for s in sessions}
        self.preps = {s["ref"]: run_mod.prepare(s) for s in sessions}
        self.text = {it["stable_id"]: it["text"]
                     for p in self.preps.values() for it in p["items"]}
        self.gold = gold
        self.decisions = decisions
        self.twins = {}
        for p in self.preps.values():
            ref_of = {it["alias"]: it["stable_id"] for it in p["request"]["items"]}
            for a, bs in ntp._twins(p["request"]["items"]).items():
                self.twins[ref_of[a]] = {ref_of[b] for b in bs}

    def correct(self, row, gold=None):
        g = (gold or self.gold).get(row["item_ref"])
        if g is None:
            return None
        s = self.sessions[row["session"]]
        return tc_score.is_link_correct(row["turn_index"], g, s["turns"],
                                        self.text[row["item_ref"]])

    def turn_text(self, row):
        return self.sessions[row["session"]]["turns"][row["turn_index"]].get("text") or ""


def _accepted(row, allow_cross):
    return row["outcome"] == "accepted" and (allow_cross or not row["cross_language"])


def rep_flags(rows, ctx, allow_cross):
    out = []
    for r in rows:
        if not _accepted(r, allow_cross):
            continue
        classes = twin_flags(ctx.text[r["item_ref"]], ctx.turn_text(r))
        if classes:
            fid = tc_score.flag_id({"item_ref": r["item_ref"], "turn": r["turn_index"]})
            out.append({"flag_id": fid, "item_ref": r["item_ref"], "turn_index": r["turn_index"],
                        "classes": classes, "confirmed": ctx.decisions.get(fid)})
    return out


def judge_rep(rows, ctx, allow_cross):
    acc = [r for r in rows if _accepted(r, allow_cross)]
    c = [ctx.correct(r) for r in acc]
    b1 = score_bar1([{"correct": x} for x in c if x is not None], sum(x is None for x in c))
    cross = [ctx.correct(r) for r in acc if r["cross_language"]]
    b2 = score_bar2([{"correct": x} for x in cross if x is not None],
                    sum(x is None for x in cross))
    by_turn = {r["item_ref"]: r["turn_index"] for r in acc}
    by_gold = pairs = 0
    for r, ok in zip(acc, c):
        twins = ctx.twins.get(r["item_ref"], set())
        if ok is False and any(r["turn_index"] in (ctx.gold.get(t) or {}).get("turns", ())
                                  for t in twins):
            by_gold += 1
        pairs += sum(1 for t in twins if t > r["item_ref"] and by_turn.get(t) == r["turn_index"])
    b3 = score_bar3(rep_flags(rows, ctx, allow_cross), by_gold, pairs)
    return b1, b2, b3


def _agg(values):
    vals = [v for v in values if v is not None]
    return {"per_rep": values, "min": min(vals) if vals else None,
            "median": statistics.median(vals) if vals else None}


def coverage(reps_rows, allow_cross):
    def rate(rs, pred):
        return sum(1 for r in rs if pred(r)) / len(rs) if rs else None

    def acc(r):
        return _accepted(r, allow_cross)

    def group(field):
        keys = sorted({r[field] for rows in reps_rows for r in rows})
        return {k: _agg([rate([r for r in rows if r[field] == k], acc) for rows in reps_rows])
                for k in keys}

    return {"line": COVERAGE_LINE,
            "overall": _agg([rate(rows, acc) for rows in reps_rows]),
            "by_kind": group("kind"), "by_lang": group("lang"),
            "mixed": _agg([rate(rows, lambda r: acc(r) and r["cross_language"])
                           for rows in reps_rows])}


def lexical_unlinked(sessions, reps_rows, ctx):
    """Items the lexical linker leaves unlinked at its most permissive point (§7), and how
    many J2 links among them per rep, split by cross-language, with correct counts."""
    unlinked = set()
    with tc_run.configured(LEXICAL_PATHS):
        for s in sessions:
            for r in tc_run.run_session(s, **LEXICAL_POINT):
                if r["status"] != "linked":
                    unlinked.add(r["item_ref"])
    per = {"linked": [], "linked_correct": [], "cross_linked": [], "cross_correct": [],
           "same_linked": [], "same_correct": []}
    for rows in reps_rows:
        acc = [r for r in rows if r["item_ref"] in unlinked and r["outcome"] == "accepted"]
        ok = [r for r in acc if ctx.correct(r) is True]
        per["linked"].append(len(acc))
        per["linked_correct"].append(len(ok))
        per["cross_linked"].append(sum(r["cross_language"] for r in acc))
        per["cross_correct"].append(sum(r["cross_language"] for r in ok))
        per["same_linked"].append(sum(not r["cross_language"] for r in acc))
        per["same_correct"].append(sum(not r["cross_language"] for r in ok))
    return {"point": {**LEXICAL_POINT, "paths": list(LEXICAL_PATHS)},
            "items_unlinked": len(unlinked), **{k: _agg(v) for k, v in per.items()}}


def adjudication_rows(todo_refs, reps_rows, ctx, agent):
    accepted = defaultdict(list)
    for rows in reps_rows:
        for r in rows:
            if r["item_ref"] in todo_refs and r["outcome"] == "accepted":
                accepted[r["item_ref"]].append(r)
    out = []
    for ref in sorted(accepted):
        rs = accepted[ref]
        if any(ctx.correct(r, agent) is not True for r in rs):
            g = agent.get(ref) or {}
            out.append({"item_ref": ref, "session": rs[0]["session"],
                        "gold_label": g.get("label"), "gold_turns": g.get("turns"),
                        "j2_turns": sorted({r["turn_index"] for r in rs})})
    return out


def assert_counts_only(obj, path="summary"):
    if isinstance(obj, dict):
        for k, v in obj.items():
            if k in _FORBIDDEN_KEYS:
                raise RuntimeError(f"{path}.{k} must not be in a summary")
            assert_counts_only(v, f"{path}.{k}")
    elif isinstance(obj, list):
        for i, v in enumerate(obj):
            assert_counts_only(v, f"{path}[{i}]")
    elif isinstance(obj, str) and _REF_LIKE.search(obj):
        raise RuntimeError(f"{path} holds an item ref")


def load_run(run_dir, data_dir, labels_dir):
    meta = store.read_json(Path(run_dir, "meta.json"))
    if meta.get("split") == "heldout" and meta.get("reps") != run_mod.HELDOUT_REPS:
        run_mod.refuse(f"a held-out run is scored only with {run_mod.HELDOUT_REPS} reps; "
                       f"this one has {meta.get('reps')}")
    if not meta.get("complete"):
        run_mod.refuse(f"{run_dir} is not complete; resume it first")
    digest = store.manifest_digest(data_dir)
    if not digest == meta.get("manifest_digest") == run_mod.EXPECTED_MANIFEST:
        run_mod.refuse("manifest digest differs between data/, the run and the pre-registration")
    current = run_mod.file_hashes(data_dir, labels_dir)
    for rel, h in current.items():
        if rel != run_mod.WORDING_FREE and meta["hashes"].get(rel) != h:
            run_mod.refuse(f"{rel} changed since the run")
    reps_rows = [store.read_jsonl(Path(run_dir, f"rep{k}.jsonl"))
                 for k in range(1, meta["reps"] + 1)]
    return meta, reps_rows


def build_summary(run_dir, data_dir, labels_dir, scoring):
    """(summary, exports) for one run. Exports: {"flags.owner.jsonl": [..],
    "adjudicate.owner.jsonl": [..]}."""
    meta, reps_rows = load_run(run_dir, data_dir, labels_dir)
    split = meta["split"]
    store.require_labels_on_manifest(labels_dir, data_dir)
    agent = tc_score.gold_by_ref(store.read_jsonl(Path(labels_dir, "done.agent.jsonl")))
    owner = tc_score.gold_by_ref(store.read_jsonl(Path(labels_dir, "done.owner.jsonl")))
    adjudicated = tc_score.gold_by_ref(
        store.read_jsonl(Path(run_dir, "done.adjudicate.owner.jsonl")))
    gold = tc_score.merge_gold(agent, owner, adjudicated)
    decisions = flag_answers(store.read_jsonl(Path(run_dir, "done.flags.owner.jsonl")))
    sessions = store.load_sessions(data_dir, split=split)
    ctx = Ctx(sessions, gold, decisions)
    split_refs = set(ctx.text)
    for rows in reps_rows:
        if {r["item_ref"] for r in rows} != split_refs or len(rows) != len(split_refs):
            run_mod.refuse("a rep file does not hold exactly one row per item of the split")

    judged = {True: [judge_rep(rows, ctx, True) for rows in reps_rows],
              False: [judge_rep(rows, ctx, False) for rows in reps_rows]}
    slot = sorted(r["item_ref"] for r in reps_rows[0] if not r["outcome"].startswith("skipped_"))
    by_ref = [{r["item_ref"]: r for r in rows} for rows in reps_rows]
    b4 = score_bar4({ref: [m[ref]["raw_pick"] for m in by_ref] for ref in slot}, meta["reps"])

    todo_refs = [t["item_ref"] for t in store.read_jsonl(Path(labels_dir, "todo.owner.jsonl"))]
    if split == "heldout":
        b5 = score_bar5(todo_refs, agent, owner)
    else:
        b5 = {"verdict": NOT_APPLICABLE}

    def bars(allow):
        per = judged[allow]
        out = {"bar1": {"per_rep": [x[0] for x in per]},
               "bar3": {"per_rep": [x[2] for x in per]},
               "bar4": b4}
        for k in ("bar1", "bar3"):
            out[k]["verdict"] = per_rep_verdict(out[k]["per_rep"])
        return out

    on, off = bars(True), bars(False)
    b2 = {"per_rep": [x[1] for x in judged[True]]}
    b2["verdict"] = per_rep_verdict(b2["per_rep"])
    verdict, allow = overall_verdict(b2["verdict"], {k: v["verdict"] for k, v in on.items()},
                                     {k: v["verdict"] for k, v in off.items()}, b5["verdict"])

    flags = {}   # every rep's flags over all accepted links, cross-language included
    for rows in reps_rows:
        for f in rep_flags(rows, ctx, True):
            flags[f["flag_id"]] = {k: f[k] for k in ("flag_id", "item_ref", "turn_index",
                                                     "classes")}
    sample = set(todo_refs) & split_refs
    adj = adjudication_rows(sample, reps_rows, ctx, agent)
    owner_files = {
        "owner_blind": {"answered": sum(1 for r in todo_refs if r in owner),
                        "of": len(todo_refs), "required": split == "heldout"},
        "adjudication": {"answered": sum(1 for r in adj if r["item_ref"] in adjudicated),
                         "of": len(adj), "required": True},
        "flags": {"answered": sum(1 for f in flags if f in decisions), "of": len(flags),
                  "required": True},
    }
    provisional = any(v["required"] and (v["answered"] < v["of"] or
                                         (k == "owner_blind" and not v["of"]))
                      for k, v in owner_files.items())

    called = store.read_jsonl(Path(run_dir, "sessions.jsonl"))
    summary = {
        "split": split, "scoring": scoring, "provisional": provisional,
        "prompt_hash": meta["prompt_hash"], "manifest_digest": meta["manifest_digest"],
        "model": meta["model"], "reps": meta["reps"],
        "verdict": verdict, "allow_cross_language": allow,
        "owner_files": owner_files,
        "bars": {"bar1": on["bar1"], "bar2": b2, "bar3": on["bar3"], "bar4": b4, "bar5": b5},
        "bars_cross_off": None if b2["verdict"] == PASS else off,
        "reported": {
            "coverage": coverage(reps_rows, True),
            "coverage_cross_off": None if b2["verdict"] == PASS else coverage(reps_rows, False),
            "outcomes": [dict(sorted(Counter(
                r["outcome"] + (f":{r['guard']}" if r["guard"] else "") for r in rows).items()))
                for rows in reps_rows],
            "unknown_script_picks": [sum(1 for r in rows if r["script_unknown"])
                                     for rows in reps_rows],
            "failed_calls": {"total": meta["failed_calls"], "calls": meta["calls"],
                             "by_reason": meta.get("failures", {}),
                             "per_rep": [sum(1 for e in called if e["rep"] == k and
                                             e["status"] == "failed")
                                         for k in range(1, meta["reps"] + 1)]},
            "unknown_aliases": meta.get("unknown_aliases", 0),
            "lexical_unlinked": lexical_unlinked(sessions, reps_rows, ctx),
            "tokens": meta["usage"],
        },
    }
    assert_counts_only(summary)
    exports = {"flags.owner.jsonl": [flags[k] for k in sorted(flags)],
               "adjudicate.owner.jsonl": adj}
    return summary, exports


def publish(src):
    data = store.read_json(src)
    assert_counts_only(data)
    target = Path(run_mod.RESULTS_DIR).resolve() / "summary.json"
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(json.dumps(data, indent=2, sort_keys=True) + "\n",
                      encoding="utf-8", newline="\n")
    return data


def main(argv=None):
    p = argparse.ArgumentParser(description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--run", required=True)
    p.add_argument("--addendum", default=None, help="YYYY-MM-DD")
    p.add_argument("--publish", action="store_true")
    p.add_argument("--fit-ok", action="store_true",
                   help="allow --publish of a fit run's summary")
    p.add_argument("--data", default=None, help="default: the run's recorded data dir")
    p.add_argument("--labels", default=None, help="default: the run's recorded labels dir")
    a = p.parse_args(argv)
    run_dir = run_mod.require_runs_dir(a.run)
    meta = store.read_json(Path(run_dir, "meta.json"))
    data_dir, labels_dir = a.data or meta["data_dir"], a.labels or meta["labels_dir"]
    if a.addendum:
        try:
            ok = bool(_DATE.match(a.addendum)) and bool(datetime.strptime(a.addendum, "%Y-%m-%d"))
        except ValueError:
            ok = False
        if not ok:
            run_mod.refuse(f"--addendum {a.addendum!r} is not YYYY-MM-DD")
    summary_path = Path(run_dir, "summary.json")
    addenda = sorted(Path(run_dir).glob("summary.addendum-*.json"))

    if meta["split"] == "fit":
        if a.addendum:
            run_mod.refuse("--addendum is for the held-out run only")
        if a.publish and not a.fit_ok:
            run_mod.refuse("publishing a fit run's summary needs --fit-ok")
        out_path, scoring = summary_path, "fit"
    elif a.addendum:
        if not summary_path.exists():
            run_mod.refuse("score the held-out run once before its addendum")
        if addenda:
            run_mod.refuse(f"an addendum scoring already exists ({addenda[0].name}); "
                           f"only one is authorised")
        out_path, scoring = Path(run_dir, f"summary.addendum-{a.addendum}.json"), \
            f"addendum-{a.addendum}"
    elif summary_path.exists():
        if a.publish:
            data = publish(addenda[-1] if addenda else summary_path)
            print(store.dumps({"published": True, "verdict": data["verdict"]}))
            return data
        run_mod.refuse(f"{summary_path} exists; a held-out run is scored once "
                       f"(one --addendum <date> re-scoring is authorised)")
    else:
        out_path, scoring = summary_path, "first"

    summary, exports = build_summary(run_dir, data_dir, labels_dir, scoring)
    if a.addendum and summary["provisional"]:
        run_mod.refuse("owner files are incomplete; the one addendum scoring is the final "
                       "verdict and needs all of them")
    for name, rows in exports.items():
        run_mod.write_jsonl(Path(run_dir, name), rows)
    run_mod.write_json(out_path, summary)
    if a.publish:
        publish(out_path)
    print(store.dumps({"split": summary["split"], "scoring": scoring,
                       "verdict": summary["verdict"], "provisional": summary["provisional"],
                       "allow_cross_language": summary["allow_cross_language"]}))
    return summary


if __name__ == "__main__":
    main()
