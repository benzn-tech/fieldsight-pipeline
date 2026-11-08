"""Run the real `item_support.attach_support` over the built session set (pre-registration §4-5).

    # fit half, every grid point -> runs/grid/rows.jsonl
    python -m scripts.item_support_eval.run grid

    # one point (held-out half), twice for bar 3
    python -m scripts.item_support_eval.run point --floor 0.6 --margin 0.1 \
        --nothing-close 0.2 --paths quote,window --split heldout --out runs/heldout_a

The decision code is never re-derived here: each point is one `attach_support` call on a deep
copy of the session's topics, with `item_support.PATHS` set for the run. Rows carry refs, ids,
statuses and scores only -- no item or turn text. Output is deterministic (sorted rows, sorted
keys, no timestamps, LF line ends), so two runs can be compared byte for byte.

`item_text_match.overlap` is memoised for the process while a run is in progress (the grid
scores the same item/turn pairs 208 times); it is pure, so the answers are unchanged.
"""
from __future__ import annotations

import argparse
import contextlib
import copy
import functools
import hashlib
from pathlib import Path

import claim_type
import item_support
from turn_name_overlay import _stem

from scripts.item_support_eval import store

FLOORS = tuple(round(0.30 + 0.05 * i, 2) for i in range(13))
MARGINS = (0.05, 0.10, 0.15, 0.20)
NOTHING_CLOSES = (0.10, 0.15, 0.20, 0.25)


def grid():
    return [(f, m, n) for f in FLOORS for m in MARGINS for n in NOTHING_CLOSES]


@contextlib.contextmanager
def configured(paths):
    """item_support.PATHS = paths and a memoised overlap, restored on exit."""
    old_paths, old_overlap = item_support.PATHS, item_support.overlap
    item_support.PATHS = tuple(paths)
    item_support.overlap = functools.lru_cache(maxsize=None)(old_overlap)
    try:
        yield
    finally:
        item_support.PATHS, item_support.overlap = old_paths, old_overlap


def linked_turn_index(support, turns):
    """Whole-session index of the turn a `linked` support names, or None."""
    if not isinstance(support, dict) or support.get("status") != "linked":
        return None
    for i, t in enumerate(turns):
        if _stem(t.get("source_filename")) == support.get("source_stem") \
                and t.get("start_sec") == support.get("start_sec"):
            return i
    return None


def run_session(session, floor, margin, nothing_close):
    """One row per item, in iter_items order. Caller sets PATHS via `configured`."""
    work = {"topics": copy.deepcopy(session["topics"])}
    item_support.attach_support(work, session["turns"], session["date"],
                                floor=floor, margin=margin, nothing_close=nothing_close)
    rows = []
    for ti, list_name, ci, child, topic in store.iter_items(work["topics"]):
        s = child.get("support") or {}
        ctype, _basis = claim_type.derive(s, None, None)
        rows.append({
            "item_ref": store.item_ref(session["ref"], ti, list_name, ci),
            "session": session["ref"], "split": session["split"], "kind": list_name,
            "lang": store.language(store.item_text(list_name, child)),
            "evidence_status": topic.get("evidence_status") or "none",
            "status": s.get("status"), "path": s.get("candidate_path"),
            "guard": s.get("guard"), "score": s.get("score", s.get("best")),
            "turn": linked_turn_index(s, session["turns"]), "claim_type": ctype,
        })
    return rows


def run_point(sessions, floor, margin, nothing_close, paths):
    rows = []
    with configured(paths):
        for s in sessions:
            rows.extend(run_session(s, floor, margin, nothing_close))
    return sorted(rows, key=lambda r: r["item_ref"])


def write_point_run(out_dir, rows, meta):
    out_dir = store.require_eval_dir(out_dir)
    store.write_jsonl(Path(out_dir, "rows.jsonl"), rows)
    store.write_json(Path(out_dir, "meta.json"), meta)


def run_digest(run_dir):
    """sha256 over rows.jsonl and meta.json bytes (bar 3)."""
    h = hashlib.sha256()
    for name in ("meta.json", "rows.jsonl"):
        h.update(Path(run_dir, name).read_bytes())
    return h.hexdigest()


def check_fit_manifest(data_dir, choice_path=None):
    """Refuse a point run when a fit exists and was made on a different manifest."""
    choice_path = Path(choice_path or store.RUNS_DIR / "fit" / "choice.json")
    if choice_path.exists():
        store.require_same_manifest(store.read_json(choice_path).get("manifest_digest"),
                                    data_dir, "fit")


def run_grid(sessions, paths=item_support.PATHS):
    out = []
    with configured(paths):
        for f, m, n in grid():
            for s in sessions:
                for r in run_session(s, f, m, n):
                    out.append({"point": [f, m, n], **r})
    return sorted(out, key=lambda r: (r["point"], r["item_ref"]))


def main(argv=None):
    p = argparse.ArgumentParser(description=__doc__)
    sub = p.add_subparsers(dest="cmd", required=True)
    g = sub.add_parser("grid")
    g.add_argument("--data", default=str(store.DATA_DIR))
    g.add_argument("--out", default=str(store.RUNS_DIR / "grid"))
    g.add_argument("--split", default="fit")
    pt = sub.add_parser("point")
    pt.add_argument("--data", default=str(store.DATA_DIR))
    pt.add_argument("--floor", type=float, required=True)
    pt.add_argument("--margin", type=float, required=True)
    pt.add_argument("--nothing-close", type=float, required=True)
    pt.add_argument("--paths", default="quote,window")
    pt.add_argument("--split", default="heldout")
    pt.add_argument("--out", required=True)
    a = p.parse_args(argv)

    sessions = store.load_sessions(a.data, split=a.split)
    if a.cmd == "grid":
        out = store.require_eval_dir(a.out)
        rows = run_grid(sessions)
        store.write_jsonl(Path(out, "rows.jsonl"), rows)
        store.write_json(Path(out, "meta.json"),
                         {"split": a.split, "paths": list(item_support.PATHS),
                          "points": len(grid()), "sessions": len(sessions),
                          "manifest_digest": store.manifest_digest(a.data)})
        print(store.dumps({"rows": len(rows), "sessions": len(sessions)}))
        return rows
    paths = tuple(x for x in a.paths.split(",") if x)
    check_fit_manifest(a.data)
    rows = run_point(sessions, a.floor, a.margin, a.nothing_close, paths)
    write_point_run(a.out, rows, {"split": a.split, "paths": list(paths), "floor": a.floor,
                                  "margin": a.margin, "nothing_close": a.nothing_close,
                                  "sessions": len(sessions), "items": len(rows),
                                  "manifest_digest": store.manifest_digest(a.data)})
    print(store.dumps({"items": len(rows), "digest": run_digest(store.require_eval_dir(a.out))}))
    return rows


if __name__ == "__main__":
    main()
