"""Gold labelling files for the item_support measurement (pre-registration §3). Local only:
everything here holds item and turn text and is written under the gitignored labels/.

    python -m scripts.item_support_eval.label export          # todo.agent.jsonl + todo.owner.jsonl
    python -m scripts.item_support_eval.label adjudicate --run runs/heldout_a
    python -m scripts.item_support_eval.label flags --run runs/heldout_a

Files the labellers write back (one JSON object per line):
  labels/done.agent.jsonl            {"item_ref", "label": "turn"|"several"|"none", "turns": [i...]}
  labels/done.owner.jsonl            same shape, the owner's blind answers on the sample
  labels/done.adjudicate.owner.jsonl same shape, the owner's call on a disagreement
  labels/done.flags.owner.jsonl      {"flag_id", "confirmed": true|false}

The blind view (todo.agent / todo.owner) shows the item and the topic's turns only: no score,
no status, no candidate path, nothing a heuristic produced.
"""
from __future__ import annotations

import argparse
import random
from datetime import datetime, timedelta
from pathlib import Path

import item_support

from scripts.item_support_eval import score as score_mod
from scripts.item_support_eval import store

OWNER_SAMPLE_SIZE = 45
VIEW_PAD = timedelta(minutes=2)
LABELS = ("turn", "several", "none")

BLIND_LABEL_INSTRUCTION = (
    "You are shown one item written from a site recording (an action, finding, decision or "
    "question) and some transcript turns from the same recording, each with an index, a speaker "
    "label and a clock time. Decide which turn the item's words came from. Answer "
    "{\"label\": \"turn\", \"turns\": [i]} for exactly one turn; {\"label\": \"several\", "
    "\"turns\": [i, j, ...]} when more than one turn each says it; or {\"label\": \"none\", "
    "\"turns\": []} when no shown turn says it. A turn that is about the same thing but names "
    "a different unit, level, block, room or direction does not say it."
)


def _hhmm_shift(hhmm_h, hhmm_m, delta):
    t = datetime(2000, 1, 2, hhmm_h, hhmm_m) + delta
    return f"{t.hour:02d}:{t.minute:02d}"


def view_indices(topic, turns, session_date, pad=VIEW_PAD):
    """Whole-session indices the labeller sees: turns cited by a verified quote, plus turns in
    the topic's time_range widened by `pad` on each side (same day rule as item_support)."""
    idx = set(item_support._evidence_turns(topic.get("evidence"), turns))
    m = item_support._TIME_RANGE.search(str(topic.get("time_range") or ""))
    if m:
        h1, m1, h2, m2 = (int(g) for g in m.groups())
        if h1 < 24 and h2 < 24 and m1 < 60 and m2 < 60:
            widened = f"{_hhmm_shift(h1, m1, -pad)} - {_hhmm_shift(h2, m2, pad)}"
            idx |= set(item_support._window_turns(widened, session_date, turns))
    return sorted(idx)


def _turn_view(i, t):
    at = t.get("abs_start")
    return {"i": i, "speaker": t.get("speaker"),
            "time": at.strftime("%H:%M:%S") if isinstance(at, datetime) else None,
            "text": t.get("text") or ""}


def blind_rows(sessions):
    rows = []
    for s in sessions:
        for ti, list_name, ci, child, topic in store.iter_items(
                [dict(t) if isinstance(t, dict) else t for t in s["topics"]]):
            rows.append({"item_ref": store.item_ref(s["ref"], ti, list_name, ci),
                         "split": s["split"], "kind": list_name,
                         "item_text": store.item_text(list_name, child),
                         "turns": [_turn_view(i, s["turns"][i])
                                   for i in view_indices(topic, s["turns"], s["date"])]})
    return sorted(rows, key=lambda r: r["item_ref"])


def owner_sample(rows, n=OWNER_SAMPLE_SIZE, seed=store.OWNER_SEED):
    held = sorted(r["item_ref"] for r in rows if r["split"] == "heldout")
    chosen = set(random.Random(seed).sample(held, min(n, len(held))))
    return [r for r in rows if r["item_ref"] in chosen]


def export(data_dir=store.DATA_DIR, labels_dir=store.LABELS_DIR):
    rows = blind_rows(store.load_sessions(data_dir))
    sample = owner_sample(rows)
    store.write_jsonl(Path(labels_dir, "todo.agent.jsonl"), rows)
    store.write_jsonl(Path(labels_dir, "todo.owner.jsonl"), sample)
    return {"agent": len(rows), "owner": len(sample),
            "empty_view": sum(1 for r in rows if not r["turns"])}


def adjudication_rows(todo_owner, agent_gold, run_rows, sessions_by_ref):
    """Sample items where the agent's answer and the heuristic disagree: the heuristic linked
    a turn the agent's answer does not count as correct, or did not link an item the agent says
    a turn supports. Shows both answers -- this file is for the owner, not blind."""
    by_ref = {r["item_ref"]: r for r in run_rows}
    out = []
    for row in todo_owner:
        ref = row["item_ref"]
        agent, run = agent_gold.get(ref), by_ref.get(ref)
        if agent is None or run is None:
            continue
        s = sessions_by_ref[run["session"]]
        if run["status"] == "linked":
            agree = score_mod.is_link_correct(run["turn"], agent, s["turns"], row["item_text"])
        else:
            agree = not score_mod.gold_supported(agent)
        if not agree:
            out.append({**row, "agent_answer": agent,
                        "heuristic": {"status": run["status"], "turn": run["turn"]}})
    return out


def adjudicate(run_dir, data_dir=store.DATA_DIR, labels_dir=store.LABELS_DIR):
    sessions = {s["ref"]: s for s in store.load_sessions(data_dir)}
    todo = store.read_jsonl(Path(labels_dir, "todo.owner.jsonl"))
    agent = score_mod.gold_by_ref(store.read_jsonl(Path(labels_dir, "done.agent.jsonl")))
    rows = adjudication_rows(todo, agent, store.read_jsonl(Path(run_dir, "rows.jsonl")),
                             sessions)
    store.write_jsonl(Path(labels_dir, "adjudicate.owner.jsonl"), rows)
    return {"disagreements": len(rows)}


def flag_rows(run_rows, sessions_by_ref):
    """Every accepted link the bar-2 check flags, with the texts the owner needs."""
    out = []
    for r in run_rows:
        if r["status"] != "linked" or r["turn"] is None:
            continue
        s = sessions_by_ref[r["session"]]
        text = store.item_text_at(s, r["item_ref"])
        turn_text = s["turns"][r["turn"]].get("text") or ""
        classes = score_mod.twin_flags(text, turn_text)
        if classes:
            out.append({"flag_id": score_mod.flag_id(r), "item_ref": r["item_ref"],
                        "path": r["path"], "classes": classes, "item_text": text,
                        "turn_text": turn_text})
    return out


def flags(run_dir, data_dir=store.DATA_DIR, labels_dir=store.LABELS_DIR):
    sessions = {s["ref"]: s for s in store.load_sessions(data_dir)}
    rows = flag_rows(store.read_jsonl(Path(run_dir, "rows.jsonl")), sessions)
    store.write_jsonl(Path(labels_dir, "flags.owner.jsonl"), rows)
    return {"flags": len(rows)}


def main(argv=None):
    p = argparse.ArgumentParser(description=__doc__)
    sub = p.add_subparsers(dest="cmd", required=True)
    sub.add_parser("export")
    for name in ("adjudicate", "flags"):
        sp = sub.add_parser(name)
        sp.add_argument("--run", required=True)
    sub.add_parser("instruction")
    a = p.parse_args(argv)
    if a.cmd == "instruction":
        print(BLIND_LABEL_INSTRUCTION)
        return None
    out = export() if a.cmd == "export" else \
        adjudicate(a.run) if a.cmd == "adjudicate" else flags(a.run)
    print(store.dumps(out))
    return out


if __name__ == "__main__":
    main()
