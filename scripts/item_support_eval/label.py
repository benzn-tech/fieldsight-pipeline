"""Gold labelling files for the item_support measurement (pre-registration §3). Local only:
everything here holds item and turn text and is written under the gitignored labels/.

    python -m scripts.item_support_eval.label export          # todo.agent.jsonl + todo.owner.jsonl
    python -m scripts.item_support_eval.label adjudicate --run runs/heldout_a
    python -m scripts.item_support_eval.label flags --run runs/heldout_a

Locator measurement (spec 2026-11-10 section 6), a separate label set on B1 run items:
    python -m scripts.item_support_eval.label locator-export   # todo.locator.agent + flags
    python -m scripts.item_support_eval.label locator-owner    # todo.owner.locator (40 held-out)
  answers: done.locator.agent.jsonl, done.owner.locator.jsonl (same shape as above),
           done.flags.owner.locator.jsonl {"flag_id", "confirmed": true|false}

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
    """HH:MM shifted by delta, clamped to 00:00..23:59 (never wraps to the other end of the
    day, which would turn a widened range into one that crosses midnight)."""
    base = datetime(2000, 1, 2, hhmm_h, hhmm_m)
    t = min(max(base + delta, datetime(2000, 1, 2, 0, 0)), datetime(2000, 1, 2, 23, 59))
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
    digest = store.manifest_digest(data_dir)
    for todo, dones in store.LABEL_FILES.items():
        # Re-exporting over answers made on another manifest would launder them onto this one.
        old = {r.get("manifest_digest") for r in store.read_jsonl(Path(labels_dir, todo))}
        if old - {digest} and any(store.read_jsonl(Path(labels_dir, x)) for x in dones):
            raise SystemExit(f"Refusing: {todo} has answers made on another manifest.")
    rows = [{**r, "manifest_digest": digest} for r in blind_rows(store.load_sessions(data_dir))]
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


# ---------------------------------------------------------------------------------------
# Locator labels (item quote locator spec 2026-11-10 section 6): a separate label set
# ---------------------------------------------------------------------------------------

LOCATOR_AGENT_TODO = "todo.locator.agent.jsonl"
LOCATOR_OWNER_TODO = "todo.owner.locator.jsonl"
LOCATOR_FLAGS = "flags.owner.locator.jsonl"
# 40 held-out B1 items: 20 linked, 12 no_locator, 8 refusals; a short stratum fills from linked.
LOCATOR_OWNER_STRATA = (("linked", 20), ("no_locator", 12), ("refusal", 8))
# Whitelist: a blind row never carries said_at, support, status or anything about the arm.
LOCATOR_BLIND_KEYS = ("item_ref", "split", "kind", "item_text", "turns", "run_sha")


def _locator_stratum(status):
    return status if status in ("linked", "no_locator") else "refusal"


def owner_sample_locator(rows, seed=store.LOCATOR_OWNER_SEED, strata=LOCATOR_OWNER_STRATA):
    """The owner's stratified sample from held-out rows ({item_ref, split, status}). Each
    stratum is drawn with its own seeded generator, so one stratum's population never
    reshuffles another; shortfalls in no_locator / refusal are taken from linked."""
    held = [r for r in rows if r["split"] == "heldout"]
    pools = {name: sorted(r["item_ref"] for r in held
                          if _locator_stratum(r["status"]) == name) for name, _n in strata}
    want = dict(strata)
    want["linked"] += sum(max(0, n - len(pools[name])) for name, n in strata
                          if name != "linked")
    chosen = set()
    for name, _n in strata:
        pool = pools[name]
        chosen |= set(random.Random(f"{seed}:{name}").sample(pool, min(want[name], len(pool))))
    return sorted((r for r in held if r["item_ref"] in chosen), key=lambda r: r["item_ref"])


def _locator_rows(sessions, runs, runs_dir):
    """One row per B1 item (non-errored B1 run): the blind fields plus a transient status."""
    rows = []
    for s in sessions:
        rec = (runs.get(s["ref"]) or {}).get("B1")
        if not isinstance(rec, dict) or rec.get("error") or not isinstance(rec.get("topics"),
                                                                           list):
            continue
        sha = score_mod.file_sha(store.locator_run_path("B", 1, s["ref"], runs_dir=runs_dir))
        for r in score_mod.run_items(rec):
            rows.append({"item_ref": r["item_ref"], "split": s["split"], "kind": r["kind"],
                         "item_text": r["text"],
                         "turns": [_turn_view(i, s["turns"][i])
                                   for i in view_indices(r["topic"], s["turns"], s["date"])],
                         "run_sha": sha, "status": score_mod.item_status(r)})
    return sorted(rows, key=lambda r: r["item_ref"])


def _blind(row, digest):
    return {**{k: row[k] for k in LOCATOR_BLIND_KEYS}, "manifest_digest": digest}


def _refuse_other_manifest(labels_dir, digest, todo):
    old = {r.get("manifest_digest") for r in store.read_jsonl(Path(labels_dir, todo))}
    if old - {digest} and any(store.read_jsonl(Path(labels_dir, x))
                              for x in store.LOCATOR_LABEL_FILES[todo]):
        raise SystemExit(f"Refusing: {todo} has answers made on another manifest.")


def export_locator(runs_dir=store.RUNS_DIR, data_dir=store.DATA_DIR,
                   labels_dir=store.LABELS_DIR):
    """Blind rows for every B1 item (todo.locator.agent.jsonl) and the twin-flag candidates
    for the owner (flags.owner.locator.jsonl; answers go to done.flags.owner.locator.jsonl as
    {"flag_id", "confirmed": true|false}). Re-run after the held-out half: answers are keyed by
    item_ref and stay valid while the B1 run file is unchanged (score checks its sha)."""
    digest = store.manifest_digest(data_dir)
    _refuse_other_manifest(labels_dir, digest, LOCATOR_AGENT_TODO)
    sessions = store.load_sessions(data_dir)
    runs = score_mod.load_locator_runs(sessions, runs_dir)
    rows = _locator_rows(sessions, runs, runs_dir)
    flags_ = score_mod.locator_flags(sessions, runs)
    store.write_jsonl(Path(labels_dir, LOCATOR_AGENT_TODO), [_blind(r, digest) for r in rows])
    store.write_jsonl(Path(labels_dir, LOCATOR_FLAGS), flags_)
    return {"agent": len(rows), "flags": len(flags_),
            "empty_view": sum(1 for r in rows if not r["turns"])}


def export_locator_owner(runs_dir=store.RUNS_DIR, data_dir=store.DATA_DIR,
                         labels_dir=store.LABELS_DIR, seed=store.LOCATOR_OWNER_SEED):
    """The owner's blind sample (todo.owner.locator.jsonl) from held-out B1 items of the
    sessions every bar reads (`score.session_status` == eligible). Give it to the owner
    before the flag file, which shows linked turns."""
    digest = store.manifest_digest(data_dir)
    _refuse_other_manifest(labels_dir, digest, LOCATOR_OWNER_TODO)
    sessions = store.load_sessions(data_dir)
    runs = score_mod.load_locator_runs(sessions, runs_dir)
    primary = score_mod.primary_model(runs)
    elig = [s for s in sessions
            if score_mod.session_status(runs.get(s["ref"]) or {}, primary) == "eligible"]
    sample = owner_sample_locator(_locator_rows(elig, runs, runs_dir), seed)
    store.write_jsonl(Path(labels_dir, LOCATOR_OWNER_TODO), [_blind(r, digest) for r in sample])
    strata = {name: sum(1 for r in sample if _locator_stratum(r["status"]) == name)
              for name, _n in LOCATOR_OWNER_STRATA}
    return {"owner": len(sample), "strata": strata}


def main(argv=None):
    p = argparse.ArgumentParser(description=__doc__)
    sub = p.add_subparsers(dest="cmd", required=True)
    sub.add_parser("export")
    for name in ("adjudicate", "flags"):
        sp = sub.add_parser(name)
        sp.add_argument("--run", required=True)
    sub.add_parser("instruction")
    for name in ("locator-export", "locator-owner"):
        sp = sub.add_parser(name)
        sp.add_argument("--data", default=str(store.DATA_DIR))
        sp.add_argument("--runs", default=str(store.RUNS_DIR))
        sp.add_argument("--labels", default=str(store.LABELS_DIR))
    a = p.parse_args(argv)
    if a.cmd == "instruction":
        print(BLIND_LABEL_INSTRUCTION)
        return None
    if a.cmd in ("locator-export", "locator-owner"):
        fn = export_locator if a.cmd == "locator-export" else export_locator_owner
        out = fn(a.runs, data_dir=a.data, labels_dir=a.labels)
        print(store.dumps(out))
        return out
    out = export() if a.cmd == "export" else \
        adjudicate(a.run) if a.cmd == "adjudicate" else flags(a.run)
    print(store.dumps(out))
    return out


if __name__ == "__main__":
    main()
