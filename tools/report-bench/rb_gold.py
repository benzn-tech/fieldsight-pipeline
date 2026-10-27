"""Gold for actions and decisions, in two steps (spec §5.1).

1. Blind listing: `blind_page` shows one session's transcript and nothing else
   (its signature takes no runs); the owner lists items and saves
   `gold_<session>.json` (schema report-bench-gold/v1).
2. Bounded adjudication: model items are matched one-to-one to the owner's
   list; only unmatched items and loose matches (similarity in
   [MATCH_AT, CONFIRM_BELOW)) go to `adjudicate_page`, deduped across all
   cells, with no model / cell label on the page.

Similarity is content-word overlap over the shorter side, with the pipeline's
CJK-safe tokenizer (`agent_turn_filter._tokens`, as `extraction_ab._key`).
Thresholds (controller decision 2026-10-28): match at 0.4 (as
`extraction_ab._matches_any`), one-click owner confirm for 0.4 <= sim < 0.6.

    python rb_gold.py blind --out DIR --session SID [--prefill-task-admission]
    python rb_gold.py pool --out DIR --phase pilot|round0
    python rb_gold.py adjudicate --out DIR --scope SID_or_DAY
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys

_HERE = os.path.dirname(os.path.abspath(__file__))
if _HERE not in sys.path:
    sys.path.insert(0, _HERE)

import rb_common  # noqa: E402

rb_common.ensure_src_on_path()
import agent_turn_filter  # noqa: E402

PAGES = os.path.join(_HERE, "pages")
GOLD_SCHEMA = "report-bench-gold/v1"
MATCH_AT = 0.4
CONFIRM_BELOW = 0.6
DEDUP_AT = 0.8
POOL_CAP = 60
TASK_ADMISSION = os.path.join(rb_common.REPO, "tests", "fixtures",
                              "task_admission_ground_truth.json")
LABELS = ("true", "misread", "fabricated")


def _toks(text):
    return frozenset(agent_turn_filter._tokens(text or ""))


def similarity(a: str, b: str) -> float:
    ta, tb = _toks(a), _toks(b)
    if not ta or not tb:
        return 0.0
    return len(ta & tb) / min(len(ta), len(tb))


# ------------------------------------------------------------------ pages

def _page(tmpl, data, path):
    with open(os.path.join(PAGES, tmpl), encoding="utf-8") as f:
        html = f.read()
    blob = json.dumps(data, ensure_ascii=False).replace("</", "<\\/")
    rb_common.write_text(path, html.replace("__DATA__", blob))
    return path


def _turn_rows(turns):
    return [{"at": t.get("abs_start_str") or t.get("time_str") or "",
             "speaker": t.get("speaker") or t.get("device") or "", "text": t.get("text") or ""}
            for t in turns]


def blind_page(session: str, turns: list, out: str, prefill: list | None = None) -> str:
    """The transcript only. No parameter can carry model output."""
    out = rb_common.check_out_dir(out)
    return _page("blind.html.tmpl", {"session": session, "turns": _turn_rows(turns),
                                     "prefill": prefill or []},
                 os.path.join(out, "gold", f"blind_{session}.html"))


def prefill_task_admission(path=TASK_ADMISSION) -> list:
    """5 true + 3 false (kept for reference, out of gold) + 1 unsure."""
    d = rb_common.read_json(path)
    out = []
    for key, label in (("true_tasks", "true"), ("not_tasks", "false"), ("unresolved", "unsure")):
        for t in d.get(key) or []:
            out.append({"id": "ta-" + t["id"], "kind": "action", "what": t["label"],
                        "responsible": "", "due": "", "label": label})
    return out


def load_gold(path) -> dict:
    g = rb_common.read_json(path)
    if g.get("schema") != GOLD_SCHEMA:
        raise ValueError(f"{path}: not {GOLD_SCHEMA}")
    if not g.get("session"):
        raise ValueError(f"{path}: no session")
    ids = [i.get("id") for i in g.get("items") or []]
    if len(ids) != len(set(ids)) or not all(ids):
        raise ValueError(f"{path}: item ids must be present and unique")
    for i in g["items"]:
        if i.get("kind") not in ("action", "decision"):
            raise ValueError(f"{path}: item {i['id']} kind must be action|decision")
        if i.get("label", "true") not in ("true", "unsure", "false"):
            raise ValueError(f"{path}: item {i['id']} bad label")
    return g


def scored_gold(gold: dict) -> list:
    """Items that can be matched: true and unsure (false is reference only)."""
    return [i for i in gold.get("items") or [] if i.get("label", "true") in ("true", "unsure")]


# ------------------------------------------------------------------ matching

def match(items: list, gold: list) -> list:
    """One-to-one, greedy by descending similarity, same kind only. `items` are
    {kind, text}; gold {id, kind, what}. Returns, per item, {gold_id, sim}."""
    pairs = []
    for i, it in enumerate(items):
        for g in gold:
            if g.get("kind") != it.get("kind"):
                continue
            s = similarity(it.get("text"), g.get("what"))
            if s >= MATCH_AT:
                pairs.append((s, i, g["id"]))
    pairs.sort(key=lambda p: (-p[0], p[1], p[2]))
    out = [{"gold_id": None, "sim": 0.0} for _ in items]
    used_items, used_gold = set(), set()
    for s, i, gid in pairs:
        if i in used_items or gid in used_gold:
            continue
        used_items.add(i)
        used_gold.add(gid)
        out[i] = {"gold_id": gid, "sim": round(s, 4)}
    return out


# ------------------------------------------------------------------ items from answers

def items_from_parsed(task: str, parsed) -> list:
    """{kind, text, responsible, due} for every action / decision in a parsed
    X or R answer."""
    out = []
    if not isinstance(parsed, dict):
        return out
    for topic in parsed.get("topics") or []:
        if not isinstance(topic, dict):
            continue
        for a in topic.get("action_items") or []:
            if isinstance(a, dict) and (a.get("action") or "").strip():
                out.append({"kind": "action", "text": a["action"].strip(),
                            "responsible": a.get("responsible"), "due": a.get("deadline")})
        if task == "X":
            for d in topic.get("decisions") or []:
                if isinstance(d, dict) and (d.get("decision") or "").strip():
                    out.append({"kind": "decision", "text": d["decision"].strip(),
                                "responsible": d.get("decided_by"), "due": None})
        else:
            for d in topic.get("key_decisions") or []:
                if isinstance(d, str) and d.strip():
                    out.append({"kind": "decision", "text": d.strip(), "responsible": None,
                                "due": None})
    return out


_HEADING = re.compile(r"^\s{0,3}#{1,6}\s*(.+?)\s*#*\s*$")
_BULLET = re.compile(r"^\s*(?:[-*•]|\d+[.)])\s+")
_TAGS = re.compile(r"\s*\[\s*t\d+(?:\s*(?:,|and|&)\s*t\d+)*\s*\]\s*$", re.IGNORECASE)


def split_t_items(text: str) -> list:
    """Lines of a T answer's action and decision sections -> {kind, text}.
    A section is an action section when its heading names action(s) / next
    step(s) / follow-up(s); a decision section when it names decision(s).
    Table rows: the first cell. Topic tags `[t1]` stripped."""
    out, kind = [], None
    for raw in (text or "").splitlines():
        m = _HEADING.match(raw)
        if m:
            h = m.group(1).lower()
            kind = ("action" if re.search(r"action|next step|follow.?up|to.?do", h) else
                    "decision" if "decision" in h else None)
            continue
        if kind is None:
            continue
        line = raw.strip()
        if not line or line.rstrip(".").lower() == "nothing here":
            continue
        if line.startswith("|"):
            cells = [c.strip() for c in line.strip("|").split("|")]
            if not cells or set(cells[0]) <= set("-: ") or cells[0].lower() in ("item", "action"):
                continue
            line = cells[0]
        line = _TAGS.sub("", _BULLET.sub("", line)).strip()
        if line:
            out.append({"kind": kind, "text": line})
    return out


# ------------------------------------------------------------------ adjudication

def adjudication_pool(runs: list, gold: dict) -> list:
    """runs: [{cell, rep, items: [{kind, text}]}] for ONE scope (session / day).
    Returns pool rows {pid, kind, text, gold_id, gold_what, sim, members,
    n_reps, hidden}; `members` = [{cell, rep, idx}] (never shown on the page)."""
    g = scored_gold(gold)
    gwhat = {x["id"]: x.get("what") for x in g}
    rows = []
    for run in runs:
        m = match(run["items"], g)
        for idx, (it, mm) in enumerate(zip(run["items"], m)):
            low = mm["gold_id"] is not None and mm["sim"] < CONFIRM_BELOW
            if mm["gold_id"] is not None and not low:
                continue
            gid = mm["gold_id"] if low else None
            member = {"cell": run["cell"], "rep": run["rep"], "idx": idx}
            for r in rows:
                if r["kind"] == it["kind"] and r["gold_id"] == gid and \
                        similarity(r["text"], it["text"]) >= DEDUP_AT:
                    r["members"].append(member)
                    break
            else:
                rows.append({"kind": it["kind"], "text": it["text"], "gold_id": gid,
                             "gold_what": gwhat.get(gid), "sim": mm["sim"] if low else None,
                             "members": [member]})
    for r in rows:
        r["n_reps"] = len({(x["cell"], x["rep"]) for x in r["members"]})
    if len(rows) > POOL_CAP:
        rows.sort(key=lambda r: (0 if r["n_reps"] >= 2 else 1))
    for i, r in enumerate(rows):
        r["pid"] = f"p{i + 1}"
        r["hidden"] = len(rows) > POOL_CAP and r["n_reps"] < 2
    return rows


def adjudicate_page(scope_id: str, turns: list, pool: list, out: str) -> str:
    out = rb_common.check_out_dir(out)
    rows = [{"pid": r["pid"], "kind": r["kind"], "text": r["text"],
             "gold_what": r.get("gold_what"), "hidden": r.get("hidden", False)} for r in pool]
    return _page("adjudicate.html.tmpl", {"scope": scope_id, "turns": _turn_rows(turns),
                                          "rows": rows},
                 os.path.join(out, "gold", f"adjudicate_{scope_id}.html"))


def apply_adjudication(gold: dict, pool: list, verdicts: dict) -> tuple:
    """(gold with adjudicated-true items added, verdict table). The table maps
    "<scope>|<cell>|<rep>|<idx>" -> {verdict, gold_id} (scope = gold["session"]:
    the same cell / rep / index on another session is a different item); verdict is "match" for a
    confirmed loose match or an adjudicated true item, else misread /
    fabricated. Rows with no verdict are left out (still unadjudicated)."""
    new = json.loads(json.dumps(gold))
    scope = gold["session"]
    table = {}
    for r in pool:
        v = verdicts.get(r["pid"])
        if v is None:
            continue
        if v == "confirm" and r.get("gold_id"):
            entry = {"verdict": "match", "gold_id": r["gold_id"]}
        elif v == "true":
            gid = f"adj-{scope}-{r['pid']}"
            if gid not in {i["id"] for i in new["items"]}:
                new["items"].append({"id": gid, "kind": r["kind"], "what": r["text"],
                                     "responsible": "", "due": "", "label": "true",
                                     "source": "adjudicated"})
            entry = {"verdict": "match", "gold_id": gid}
        elif v in ("misread", "fabricated"):
            entry = {"verdict": v, "gold_id": None}
        else:
            raise ValueError(f"{r['pid']}: verdict {v!r}")
        for m in r["members"]:
            table[f"{scope}|{m['cell']}|{m['rep']}|{m['idx']}"] = entry
    return new, table


def gold_for_day(out: str, day_key: str, manifest: dict) -> dict:
    """R's gold: the union of the gold of that day's labelled sessions (spec
    §5.2: R recall is on those items only)."""
    d = manifest["R"][day_key]
    items = []
    for sid, s in manifest.items():
        if sid == "R" or s.get("folder") != d["folder"] or s.get("date") != d["date"]:
            continue
        p = os.path.join(out, "gold", f"gold_{sid}.json")
        if os.path.exists(p):
            items += [dict(i, id=f"{sid}:{i['id']}") for i in load_gold(p)["items"]]
    return {"schema": GOLD_SCHEMA, "session": day_key, "items": items}


# ------------------------------------------------------------------ CLI

def _runs_for_scope(out, phase, scope):
    """Pool input from the run records of one phase for one scope."""
    rb_common.ensure_src_on_path()
    import llm_utils
    runs = []
    for rec in rb_common.read_jsonl(os.path.join(out, "runs", phase + ".jsonl")):
        if rec.get("session") != scope or not rec.get("text_path"):
            continue
        with open(rec["text_path"], encoding="utf-8") as f:
            text = f.read()
        items = (split_t_items(text) if rec["task"] == "T"
                 else items_from_parsed(rec["task"], llm_utils.extract_json(text)))
        runs.append({"cell": f"{rec['task']}|{rec['candidate']}|{rec['model']}",
                     "rep": rec["rep"], "items": items})
    return runs


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("cmd", choices=["blind", "pool", "adjudicate", "apply"])
    ap.add_argument("--out", required=True)
    ap.add_argument("--session")
    ap.add_argument("--scope")
    ap.add_argument("--phase", default="round0")
    ap.add_argument("--prefill-task-admission", action="store_true")
    ap.add_argument("--verdicts")
    a = ap.parse_args(argv)
    out = rb_common.check_out_dir(a.out)
    gold_path = lambda s: os.path.join(out, "gold", f"gold_{s}.json")  # noqa: E731
    if a.cmd == "blind":
        turns = rb_common.read_json(os.path.join(out, "inputs", "X", a.session + ".turns.json"))
        pre = prefill_task_admission() if a.prefill_task_admission else None
        print(blind_page(a.session, turns, out, pre))
        return 0
    scope = a.scope or a.session
    manifest = rb_common.read_json(os.path.join(out, "inputs", "manifest.json"))
    if scope in manifest.get("R", {}) and not os.path.exists(gold_path(scope)):
        rb_common.write_json(gold_path(scope), gold_for_day(out, scope, manifest))
    gold = load_gold(gold_path(scope))
    pool = adjudication_pool(_runs_for_scope(out, a.phase, scope), gold)
    pool_path = os.path.join(out, "gold", f"pool_{a.phase}_{scope}.json")
    if a.cmd == "pool":
        rb_common.write_json(pool_path, pool)
        print(scope, "pool", len(pool), "shown", sum(1 for r in pool if not r["hidden"]))
        return 0
    if a.cmd == "adjudicate":
        turns = rb_common.read_json(os.path.join(out, "inputs", "X", scope + ".turns.json")) \
            if os.path.exists(os.path.join(out, "inputs", "X", scope + ".turns.json")) else \
            rb_common.read_json(os.path.join(out, "inputs", "R", scope + ".json"))["kwargs"][
                "transcripts_with_photos"]
        rb_common.write_json(pool_path, pool)
        print(adjudicate_page(scope, turns, pool, out))
        return 0
    verdicts = rb_common.read_json(a.verdicts)["verdicts"]
    new, table = apply_adjudication(gold, rb_common.read_json(pool_path), verdicts)
    rb_common.write_json(gold_path(scope), new)
    rb_common.write_json(os.path.join(out, "gold", f"verdicts_{a.phase}_{scope}.json"), table)
    print(scope, "gold items", len(new["items"]), "verdicts", len(table))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
