"""Scoring: mechanical checks, against gold, noise floor F, failure gate (spec §5.2-§5.3).

    python rb_score.py --out DIR --phase round0     -> <out>/scores_round0.json

Placeholder scan: the 09-08 replay's list was not kept, so the bench fixes its
own 14 patterns (controller decision 2026-10-28), case-insensitive:
   1 `Bullet \\d+:` or `Bullet N`          (muse's 31093b2d echo)
   2 `[insert`                            3 `{{`
   4 `<field_name>` (`<[a-z_]+>`)          5 `YYYY-MM-DD`
   6 `HH:MM`                              7 `[name]` `[date]` `[time]` `[person]`
                                            `[location]` `[company]`
   8 a field value that is exactly `TBD`  9 `lorem ipsum`
  10 `example.com`                        11 a field value exactly `string` or `...`
  12 `XX` as a whole token                13 `<placeholder>`
  14 a field value equal to a literal example value of the task's schema as
     the candidate's own prompt shows it (e.g. "What needs to be done")
Patterns 8, 11 and 14 apply to parsed field values; the rest to every parsed
string value, or to the raw text when the answer did not parse (T is text).

Against gold (actions and decisions only): gold-true = blind `true` +
adjudicated `true`; `unsure` is out of numerator and denominator. An item's
fate comes from the verdict table when the owner judged it, else from a
confident match (sim >= 0.6); a loose or unmatched item nobody judged yet is
`unadjudicated` and kept out of precision. R: recall only. T: coverage of
gold-true, and for T-cur of the pinned input's true items; sections present.
"""
from __future__ import annotations

import argparse
import os
import re
import statistics
import sys
from collections import defaultdict

_HERE = os.path.dirname(os.path.abspath(__file__))
if _HERE not in sys.path:
    sys.path.insert(0, _HERE)

import rb_common  # noqa: E402
import rb_gold  # noqa: E402
import rb_schemas  # noqa: E402

rb_common.ensure_src_on_path()
import llm_utils  # noqa: E402

_I = re.IGNORECASE
TEXT_PATTERNS = [
    re.compile(r"\bBullet\s+(?:\d+\s*:|N\b)", _I),
    re.compile(r"\[insert", _I),
    re.compile(r"\{\{"),
    re.compile(r"<[a-z_]+>", _I),
    re.compile(r"YYYY-MM-DD", _I),
    re.compile(r"\bHH:MM\b", _I),
    re.compile(r"\[(?:name|date|time|person|location|company)\]", _I),
    re.compile(r"lorem ipsum", _I),
    re.compile(r"example\.com", _I),
    re.compile(r"\bXX\b"),
    re.compile(r"<placeholder>", _I),
]
VALUE_EXACT = {"tbd", "string", "..."}
PLACEHOLDER_COUNT = len(TEXT_PATTERNS) + 3          # + TBD, string/..., schema example
assert PLACEHOLDER_COUNT == 14

_EXAMPLE_VALUE = re.compile(r'"[a-z_]+"\s*:\s*"([^"\n]{8,})"')


def schema_example_values(prompt: str | None) -> set:
    """Literal example values of the schema as the prompt shows it."""
    if not prompt:
        return set()
    return {v.strip().lower() for v in _EXAMPLE_VALUE.findall(prompt) if " " in v.strip()}


def _strings(node):
    if isinstance(node, str):
        yield node
    elif isinstance(node, dict):
        for v in node.values():
            yield from _strings(v)
    elif isinstance(node, list):
        for v in node:
            yield from _strings(v)


def placeholder_hits(parsed, text: str, examples: set = frozenset()) -> int:
    hits = 0
    if isinstance(parsed, (dict, list)):
        for s in _strings(parsed):
            v = s.strip().lower()
            if v in VALUE_EXACT or (v and v in examples):
                hits += 1
                continue
            hits += sum(1 for p in TEXT_PATTERNS if p.search(s))
        return hits
    return sum(len(p.findall(text or "")) for p in TEXT_PATTERNS)


def mechanical(record: dict, task: str, vendor: str, text: str = "",
               prompt: str | None = None) -> dict:
    parsed = None
    if task in ("X", "R"):
        parsed = llm_utils.extract_json(text) if text else None
        ok_parse = isinstance(parsed, dict)
    else:
        ok_parse = bool((text or "").strip())
    schema_keys = None
    if vendor != "anthropic" and task in ("X", "R") and ok_parse:
        req = rb_schemas.for_task(task, prompt)["required"]
        schema_keys = all(k in parsed for k in req)
    attempts = record.get("attempts") or []
    last = attempts[-1] if attempts else {}
    return {
        "parsed": ok_parse,
        "schema_keys": schema_keys,
        "finish_normal": record.get("outcome") == "ok",
        "truncated": record.get("outcome") == "truncated",
        "outcome": record.get("outcome"),
        "placeholder_hits": placeholder_hits(parsed if ok_parse else None, text,
                                             schema_example_values(prompt)),
        "latency_ms": sum(a.get("latency_ms") or 0 for a in attempts),
        "input_tokens": sum(a.get("input_tokens") or 0 for a in attempts),
        "output_tokens": sum(a.get("output_tokens") or 0 for a in attempts),
        "reasoning_tokens": sum(a.get("reasoning_tokens") or 0 for a in attempts),
        "usd": record.get("usd"),
        "provider": last.get("provider"),
        "retried": len(attempts) > 1,
    }


def _resp_ok(model_r, gold_r):
    m, g = (model_r or "").strip(), (gold_r or "").strip()
    if not g:
        return not m or m.lower() in ("null", "none")
    if g.lower() == "speaker":
        return m.lower() == "speaker"
    return bool(m) and rb_gold.similarity(m, g) >= 0.5


def assign(items: list, gold: dict, verdicts: dict, run_prefix: str) -> list:
    """Per item {fate: match|misread|fabricated|unadjudicated, gold_id}."""
    g = rb_gold.scored_gold(gold)
    out = [None] * len(items)
    used = set()
    for i in range(len(items)):
        v = verdicts.get(f"{run_prefix}|{i}")
        if v:
            out[i] = {"fate": v["verdict"], "gold_id": v.get("gold_id")}
            if v.get("gold_id"):
                used.add(v["gold_id"])
    rest = [i for i in range(len(items)) if out[i] is None]
    m = rb_gold.match([items[i] for i in rest], [x for x in g if x["id"] not in used])
    for i, mm in zip(rest, m):
        if mm["gold_id"] is not None and mm["sim"] >= rb_gold.CONFIRM_BELOW:
            out[i] = {"fate": "match", "gold_id": mm["gold_id"]}
        else:
            out[i] = {"fate": "unadjudicated", "gold_id": None}
    return out


def against_gold(items, gold, verdicts, task="X", run_prefix="", input_items=None,
                 sections=None, text=""):
    g = {x["id"]: x for x in rb_gold.scored_gold(gold)}
    fates = assign(items, gold, verdicts, run_prefix)
    res = {"fabricated": sum(1 for f in fates if f["fate"] == "fabricated"),
           "misread": sum(1 for f in fates if f["fate"] == "misread"),
           "unadjudicated": sum(1 for f in fates if f["fate"] == "unadjudicated")}
    for kind in ("action", "decision"):
        true_ids = {i for i, x in g.items() if x["kind"] == kind and x.get("label", "true") == "true"}
        kfates = [(it, f) for it, f in zip(items, fates) if it["kind"] == kind]
        hit = {f["gold_id"] for _it, f in kfates if f["fate"] == "match" and f["gold_id"] in true_ids}
        unsure = sum(1 for _it, f in kfates if f["fate"] == "match" and f["gold_id"] not in true_ids)
        tp = sum(1 for _it, f in kfates if f["fate"] == "match" and f["gold_id"] in true_ids)
        judged = len(kfates) - unsure - sum(1 for _it, f in kfates if f["fate"] == "unadjudicated")
        name = "coverage" if task == "T" else "recall"
        res[f"{kind}_{name}"] = (len(hit) / len(true_ids)) if true_ids else None
        if task == "X":
            res[f"{kind}_precision"] = (tp / judged) if judged else None
        res[f"{kind}_gold_true"] = len(true_ids)
    if task != "T":
        pairs = [(it, g[f["gold_id"]]) for it, f in zip(items, fates)
                 if f["fate"] == "match" and f["gold_id"] in g and it["kind"] == "action"
                 and g[f["gold_id"]].get("label", "true") == "true"]
        res["responsible_correct"] = (sum(1 for it, gx in pairs if _resp_ok(
            it.get("responsible"), gx.get("responsible"))) / len(pairs)) if pairs else None
        due = [(it, gx) for it, gx in pairs if (gx.get("due") or "").strip()]
        res["due_kept"] = (sum(1 for it, gx in due if (it.get("due") or "").strip()
                               and str(it.get("due")).lower() != "null") / len(due)) if due else None
    if task == "T" and input_items is not None:
        in_f = assign(input_items, gold, {}, "")
        in_true = {f["gold_id"] for f in in_f if f["fate"] == "match" and f["gold_id"] in g
                   and g[f["gold_id"]].get("label", "true") == "true"}
        got = {f["gold_id"] for f in fates if f["fate"] == "match"}
        res["input_coverage"] = (len(got & in_true) / len(in_true)) if in_true else None
    if task == "T" and sections is not None:
        # A heading line, or a line that is just the title (bold / colon allowed).
        heads = {line.strip().strip("#*_: ").strip().lower()
                 for line in (text or "").splitlines() if line.strip()}
        res["sections_present"] = all((s or "").strip().lower() in heads for s in sections if s)
    return res


def t_sections(template_id="personal-meeting", version=3) -> list:
    """The headings the model is asked to write: develop's built-in template
    after report_facts.split_plan (code-filled sections leave the plan), plus
    the catch-all. Sub-sections are not required."""
    import report_facts
    import report_template
    tmpl = report_facts.split_plan(report_template.load_template(template_id, version))[0]
    secs = list(tmpl.get("sections") or []) + [tmpl.get("catch_all") or {}]
    return [s["title"] for s in secs if (s.get("title") or "").strip()]


def noise_floor(values_by_session: dict, gold_true_by_session: dict, metric: str) -> float:
    rng = 0.0
    for vals in values_by_session.values():
        vals = [v for v in vals if v is not None]
        if vals:
            rng = max(rng, max(vals) - min(vals))
    floor = 0.0
    if "recall" in metric or "coverage" in metric:
        counts = [n for n in gold_true_by_session.values() if n]
        floor = max((1.0 / n for n in counts), default=0.0)
    return max(rng, floor)


def rep_failed(rep: dict) -> bool:
    return (not rep.get("parsed") or rep.get("truncated") or
            (rep.get("placeholder_hits") or 0) > 0 or (rep.get("fabricated") or 0) >= 1)


def session_failed(reps: list) -> bool:
    return sum(1 for r in reps if rep_failed(r)) >= 2


# ------------------------------------------------------------------ CLI

def _verdict_tables(out, phase):
    d = os.path.join(out, "gold")
    tables = {}
    if os.path.isdir(d):
        for name in os.listdir(d):
            # A phase's own tables only: verdict keys are cell|rep|idx, and rep 1
            # of the pilot is a different run from rep 1 of round 0.
            if name.startswith(f"verdicts_{phase}_") and name.endswith(".json"):
                tables.update(rb_common.read_json(os.path.join(d, name)))
    return tables


def score_phase(out, phase):
    import rb_requests
    import rb_run
    # The last record per key, and only runs the model finished (an infra
    # failure is rerun, not scored).
    recs = [r for r in rb_run.latest(rb_common.read_jsonl(
        os.path.join(out, "runs", phase + ".jsonl"))).values() if rb_run.is_done(r)]
    verdicts = _verdict_tables(out, phase)
    golds = {}
    manifest = rb_common.read_json(os.path.join(out, "inputs", "manifest.json"))
    cells = defaultdict(lambda: defaultdict(list))
    sections = t_sections() if any(r["task"] == "T" for r in recs) else None
    for r in recs:
        task, sess = r["task"], r["session"]
        if sess not in golds:
            gp = os.path.join(out, "gold", f"gold_{sess}.json")
            golds[sess] = (rb_gold.current_day_gold(out, sess, manifest)
                           if sess in manifest.get("R", {}) else
                           rb_gold.load_gold(gp) if os.path.exists(gp) else None)
        with open(r["text_path"], encoding="utf-8") as f:
            text = f.read()
        prompt_path = os.path.join(out, r["prompt_path"])
        prompt = open(prompt_path, encoding="utf-8").read() if os.path.exists(prompt_path) else None
        vendor = rb_requests.MODELS[r["model"]]["vendor"]
        m = mechanical(r, task, vendor, text, prompt)
        gold = golds[sess]
        if gold is not None:
            items = (rb_gold.split_t_items(text) if task == "T" else
                     rb_gold.items_from_parsed(task, llm_utils.extract_json(text)))
            input_items = None
            if task == "T" and r["candidate"] == "T-cur":
                pin = os.path.join(out, "inputs", "T", sess + ".extraction.json")
                if os.path.exists(pin):
                    input_items = rb_gold.items_from_parsed("X", rb_common.read_json(pin))
            m.update(against_gold(items, gold, verdicts, task,
                                  f"{sess}|{task}|{r['candidate']}|{r['model']}|{r['rep']}",
                                  input_items=input_items, text=text,
                                  sections=sections if task == "T" else None))
        cells[f"{task}|{r['candidate']}|{r['model']}"][sess].append(m)
    result = {}
    for cell, by_sess in sorted(cells.items()):
        metrics = sorted({k for reps in by_sess.values() for rep in reps for k, v in rep.items()
                          if isinstance(v, (int, float)) and not isinstance(v, bool)})
        # F's one-gold-item floor uses that metric's own kind of gold.
        gold_true = {kind: {s: (reps[0].get(f"{kind}_gold_true") or 0)
                            for s, reps in by_sess.items()}
                     for kind in ("action", "decision")}
        result[cell] = {
            "sessions": {s: {"reps": reps, "failed": session_failed(reps)}
                         for s, reps in sorted(by_sess.items())},
            "failed_sessions": sum(1 for reps in by_sess.values() if session_failed(reps)),
            "mean": {k: statistics.mean(v for reps in by_sess.values() for rep in reps
                                        if isinstance(v := rep.get(k), (int, float))
                                        and not isinstance(v, bool))
                     for k in metrics},
            "noise_floor": {k: noise_floor({s: [rep.get(k) for rep in reps]
                                            for s, reps in by_sess.items()},
                                           gold_true["decision" if k.startswith("decision")
                                                     else "action"], k)
                            for k in metrics},
        }
    return result


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", required=True)
    ap.add_argument("--phase", required=True)
    a = ap.parse_args(argv)
    out = rb_common.check_out_dir(a.out)
    res = score_phase(out, a.phase)
    rb_common.write_json(os.path.join(out, f"scores_{a.phase}.json"), res)
    for cell, c in res.items():
        print(cell, "failed_sessions", c["failed_sessions"],
              "action_recall", round(c["mean"].get("action_recall", float("nan")), 3))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
