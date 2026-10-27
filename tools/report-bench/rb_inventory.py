"""Candidate inventory by rendered-prompt hash (spec §2, §2.3).

    python rb_inventory.py --task X|T --cache DIR [--since 2026-08-25]
        every commit touching the task's prompt sources since --since, rendered
        cold on fixtures/fixed_input.json; prints `sha date sha256[:12] subject`.
    python rb_inventory.py --pin X-0901 ... --cache DIR
        writes prompts/<id>.txt and the hashes into candidates.json.

The fixed input is synthetic, so nothing printed here is customer text; prompt
text is still never printed (hashes only).
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys

_HERE = os.path.dirname(os.path.abspath(__file__))
if _HERE not in sys.path:
    sys.path.insert(0, _HERE)

import rb_common  # noqa: E402
import rb_snapshot  # noqa: E402

FIXED = os.path.join(_HERE, "fixtures", "fixed_input.json")
CANDIDATES = os.path.join(_HERE, "candidates.json")
PROMPTS = os.path.join(_HERE, "prompts")

# The files whose text reaches each task's rendered prompt.
SOURCES = {
    "X": ["src/lambda_extract_session.py"],
    "T": ["src/report_template.py", "src/report_facts.py", "src/report_templates",
          "src/lambda_session_report.py", "src/due_display.py"],
}

# Spec §2.1 / §2.3. `names` False only for X-0901 (rendered without speaker names).
CANDIDATE_SHAS = {
    "X-0901": ("X", "763b3082", False),
    "X-0917": ("X", "83abd0d4", True),
    "X-1005": ("X", "8703e69a", True),
    "X-cur": ("X", "82036cf2", True),
    "T-0928": ("T", "e4e2ea19", True),
    "T-1001": ("T", "751481a3", True),
    "T-cur": ("T", "develop", True),
}
DEVELOP_REF = "origin/develop"


def fixed_input():
    return rb_common.read_json(FIXED)


def transcript_lines(turns):
    return "\n".join(f"[{t.get('abs_start_str', '')}] {t.get('speaker', '')}: {t.get('text', '')}"
                     for t in turns)


def inputs_for(task, fixed, names=True):
    if task == "X":
        inp = {k: fixed[k] for k in ("user_folder", "date", "session_base", "turns",
                                     "n_segments")}
        if names:
            inp["speaker_names"] = fixed["speaker_names"]
        return inp
    if task == "T":
        return {"scope": fixed["scope"], "artifact": fixed["artifact"],
                "transcript": transcript_lines(fixed["turns"])}
    raise ValueError(task)


def render_at(sha, task, cache, names=True, repo=rb_common.REPO):
    src = rb_snapshot.build(sha, cache, repo)
    return rb_snapshot.render(src, task, inputs_for(task, fixed_input(), names))


def commits(task, since="2026-08-25", ref=DEVELOP_REF, repo=rb_common.REPO):
    r = subprocess.run(["git", "-C", repo, "log", "--since", since, "--format=%H\t%ad\t%s",
                        "--date=short", ref, "--", *SOURCES[task]],
                       capture_output=True, text=True, encoding="utf-8", check=True)
    out = []
    for line in r.stdout.splitlines():
        sha, date, subject = line.split("\t", 2)
        out.append({"sha": sha, "date": date, "subject": subject})
    return out


def scan(task, since="2026-08-25", repo=rb_common.REPO, cache=None):
    rows = []
    for c in commits(task, since, repo=repo):
        try:
            c["sha256"] = rb_common.sha256_text(render_at(c["sha"], task, cache, repo=repo))
        except rb_snapshot.RenderError as exc:
            c["sha256"] = None
            c["error"] = str(exc)[:120]
        rows.append(c)
    return rows


def pin(ids, cache, repo=rb_common.REPO):
    cands = rb_common.read_json(CANDIDATES) if os.path.exists(CANDIDATES) else {}
    for cid in ids:
        task, sha, names = CANDIDATE_SHAS[cid]
        ref = DEVELOP_REF if sha == "develop" else sha
        full = rb_snapshot.resolve(ref, repo)
        prompt = render_at(full, task, cache, names, repo)
        rb_common.write_text(os.path.join(PROMPTS, cid + ".txt"), prompt)
        entry = cands.get(cid, {})
        entry.update({"task": task, "sha": sha, "resolved": full,
                      "fixed_sha256": rb_common.sha256_text(prompt), "names": names})
        cands[cid] = entry
    rb_common.write_json(CANDIDATES, cands)
    return cands


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("--task", choices=["X", "T"])
    ap.add_argument("--pin", nargs="+")
    ap.add_argument("--cache", required=True)
    ap.add_argument("--since", default="2026-08-25")
    a = ap.parse_args(argv)
    if a.pin:
        for cid, e in sorted(pin(a.pin, a.cache).items()):
            print(cid, e["resolved"][:8], e["fixed_sha256"][:12])
        return 0
    for r in scan(a.task, a.since, cache=a.cache):
        print(r["sha"][:8], r["date"], (r["sha256"] or "ERROR:" + r.get("error", ""))[:12],
              r["subject"][:80])
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
