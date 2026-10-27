"""Pre-render every (candidate, session / day) prompt into --out (spec §2).

    python rb_prerender.py --out DIR --cache DIR [--tasks X T R] [--r-template FILE]

Reads `<out>/inputs/manifest.json` (rb_collect) and candidates.json; renders
cold from each candidate's snapshot (rb_snapshot), so the live runner never
touches git. Writes `<out>/prompts/<id>/<key>.txt` and `<out>/prompts/index.json`
= {id: {key: {sha256, task, n, chars}}} (`n` = n_segments / n_transcripts,
for the answer budget; `chars` for the $ upper). Keys: X / T = session_base, R = <folder>_<date>.

  X  every X candidate x every session (set and held-out); X-0901 without
     speaker names; the others with the session's `speaker_names` rows when
     <out>/inputs/X/<session>.names.json exists (absent = raw labels, as on
     every finalize-sweep artifact).
  T  every T candidate x session, on the PINNED extraction artifact
     (<out>/inputs/T/<session>.extraction.json, hash in pins.json; written by
     `--pin-tcur`, muse X-cur rep 1). A missing or changed pin is refused.
     Transcript lines are `[HH:MM:SS] speaker: text` for every candidate
     (prod's transcript_window format is not reproduced; same for all T).
  R  R-cur x day: develop's build_daily_prompt with the served template
     (--r-template = a prompt_templates.json-shaped file; default the git copy
     only when rb_template_check said identical).

    python rb_prerender.py --out DIR --pin-tcur <out>/runs/pilot.jsonl [...]
"""
from __future__ import annotations

import argparse
import os
import sys

_HERE = os.path.dirname(os.path.abspath(__file__))
if _HERE not in sys.path:
    sys.path.insert(0, _HERE)

import rb_common  # noqa: E402
import rb_inventory  # noqa: E402
import rb_snapshot  # noqa: E402

R_CANDIDATE = "R-cur"


class PinError(RuntimeError):
    pass


def report_shape(extraction: dict) -> dict:
    """The final-pass answer in the shape a session report reads: prod's T
    input is the reviewed content org-api renders (lambda_org_api
    .render_report_shape), which spells decisions `key_decisions` (strings)
    and questions `open_questions`; develop's `_topic_content` (the #997 record
    block) reads those names, so the raw `decisions` / `questions` would drop
    out of T-cur's record silently. Everything else is kept as the model wrote it."""
    out = dict(extraction)
    topics = []
    for t in extraction.get("topics") or []:
        if not isinstance(t, dict):
            continue
        t = dict(t)
        t.setdefault("key_decisions", [d if isinstance(d, str) else (d or {}).get("decision")
                                       for d in (t.get("decisions") or [])
                                       if (d if isinstance(d, str) else (d or {}).get("decision"))])
        t.setdefault("open_questions", [q if isinstance(q, str) else (q or {}).get("question")
                                        for q in (t.get("questions") or [])
                                        if (q if isinstance(q, str) else (q or {}).get("question"))])
        topics.append(t)
    out["topics"] = topics
    return out


def _pins_path(out):
    return os.path.join(out, "inputs", "T", "pins.json")


def pin_tcur_input(runs_jsonl_paths, out) -> dict:
    """muse X-cur rep 1 per session -> the T input every T cell reuses."""
    rb_common.ensure_src_on_path()
    import llm_utils
    pins = rb_common.read_json(_pins_path(out)) if os.path.exists(_pins_path(out)) else {}
    for path in runs_jsonl_paths:
        for rec in rb_common.read_jsonl(path):
            if not (rec.get("task") == "X" and rec.get("candidate") == "X-cur"
                    and rec.get("model") == "muse" and rec.get("rep") == 1
                    and rec.get("outcome") == "ok"):
                continue
            session = rec["session"]
            if session in pins:
                continue
            with open(rec["text_path"], encoding="utf-8") as f:
                content = llm_utils.extract_json(f.read())
            if not isinstance(content, dict):
                continue
            p = os.path.join(out, "inputs", "T", session + ".extraction.json")
            rb_common.write_json(p, report_shape(content))
            pins[session] = {"sha256": rb_common.sha256_file(p), "from_key": rec["key"]}
    rb_common.write_json(_pins_path(out), pins)
    return {s: v["sha256"] for s, v in pins.items()}


def _pinned_content(out, session):
    pins = rb_common.read_json(_pins_path(out)) if os.path.exists(_pins_path(out)) else {}
    p = os.path.join(out, "inputs", "T", session + ".extraction.json")
    if session not in pins or not os.path.exists(p):
        raise PinError(f"{session}: no pinned extraction (run --pin-tcur after the pilot)")
    if rb_common.sha256_file(p) != pins[session]["sha256"]:
        raise PinError(f"{session}: pinned extraction changed since it was pinned")
    return rb_common.read_json(p)


def _names(out, session):
    p = os.path.join(out, "inputs", "X", session + ".names.json")
    return rb_common.read_json(p) if os.path.exists(p) else None


def default_renderer(cache):
    def render(sha, task, inputs):
        return rb_snapshot.render(rb_snapshot.build(sha, cache), task, inputs)
    return render


def run(candidates, manifest, out, cache=None, tasks=("X", "T", "R"), r_templates=None,
        renderer=None):
    out = rb_common.check_out_dir(out)
    render = renderer or default_renderer(cache)
    sessions = {k: v for k, v in manifest.items() if k != "R"}
    ipath = os.path.join(out, "prompts", "index.json")
    # Merged into, never replaced: T is pre-rendered after the pilot pins its
    # input, and held-out T after the held-out pins, long after X and R.
    index = rb_common.read_json(ipath) if os.path.exists(ipath) else {}

    def put(cid, key, task, n, prompt):
        rb_common.write_text(os.path.join(out, "prompts", cid, key + ".txt"), prompt)
        index.setdefault(cid, {})[key] = {"sha256": rb_common.sha256_text(prompt),
                                          "task": task, "n": n, "chars": len(prompt),
                                          "est_tokens": rb_common.est_tokens(prompt)}

    for cid, c in sorted(candidates.items()):
        if c["task"] == "X" and "X" in tasks:
            for sid, s in sorted(sessions.items()):
                turns = rb_common.read_json(os.path.join(out, "inputs", "X", sid + ".turns.json"))
                inp = {"user_folder": s["folder"], "date": s["date"], "session_base": sid,
                       "turns": turns, "n_segments": s["n_segments"]}
                names = _names(out, sid)
                if c.get("names", True) and names:
                    inp["speaker_names"] = names
                put(cid, sid, "X", s["n_segments"], render(c["resolved"], "X", inp))
        elif c["task"] == "T" and "T" in tasks:
            for sid, s in sorted(sessions.items()):
                if s.get("role") == "heldout" and not os.path.exists(
                        os.path.join(out, "inputs", "T", sid + ".extraction.json")):
                    continue      # held-out T waits for its own muse X-cur rep 1
                content = _pinned_content(out, sid)
                turns = rb_common.read_json(os.path.join(out, "inputs", "X", sid + ".turns.json"))
                inp = {"scope": {"folder": s["folder"], "date": s["date"], "from": "00:00",
                                 "to": "23:59", "recordings": s["n_segments"]},
                       "artifact": {"folder": s["folder"], "date": s["date"],
                                    "content": dict(content, date=s["date"])},
                       "transcript": rb_inventory.transcript_lines(turns)}
                put(cid, sid, "T", s["n_segments"], render(c["resolved"], "T", inp))
    if "R" in tasks:
        if r_templates is None:
            raise ValueError("R needs the served template (--r-template)")
        develop = candidates[R_CANDIDATE]["resolved"]   # pinned, same tree as T-cur
        for key, d in sorted(manifest.get("R", {}).items()):
            r = rb_common.read_json(os.path.join(out, "inputs", "R", key + ".json"))
            put(R_CANDIDATE, key, "R", d["n_transcripts"],
                render(develop, "R", {"served_templates": r_templates, "kwargs": r["kwargs"]}))
    rb_common.write_json(ipath, index)
    return index


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", required=True)
    ap.add_argument("--cache")
    ap.add_argument("--tasks", nargs="+", default=["X", "T", "R"])
    ap.add_argument("--r-template")
    ap.add_argument("--pin-tcur", nargs="+")
    a = ap.parse_args(argv)
    out = rb_common.check_out_dir(a.out)
    if a.pin_tcur:
        for s, h in sorted(pin_tcur_input(a.pin_tcur, out).items()):
            print("pinned", s, h[:12])
        return 0
    cands = rb_common.read_json(rb_inventory.CANDIDATES)
    manifest = rb_common.read_json(os.path.join(out, "inputs", "manifest.json"))
    tmpl = rb_common.read_json(a.r_template) if a.r_template else None
    if tmpl is not None:
        h = rb_common.sha256_file(a.r_template)
        rb_common.write_json(os.path.join(out, "prompts", "r_template.json"),
                             {"path": os.path.abspath(a.r_template), "sha256": h})
        print("r-template sha256", h[:12])
    idx = run(cands, manifest, out, a.cache, a.tasks, tmpl)
    for cid, keys in sorted(idx.items()):
        print(cid, "prompts=%d" % len(keys))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
