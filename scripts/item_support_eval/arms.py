"""The locator measurement's extraction arms (spec 2026-11-10 section 6): A = today's final
prompt (locator off), B = locator on, each run twice (A1, A2, B1, B2), with staged, confirmed
call counts.

    python -m scripts.item_support_eval.arms count --stage fit        # prints the exact count
    python -m scripts.item_support_eval.arms run --stage fit --emit-evidence true \
        --confirm-count 120

`count` reads data/ only and makes no call. `run` refuses (non-zero exit, before the deployed
config is read and before any call) unless `--confirm-count` equals `plan_calls(stage)`: the
number of (session, arm, rep) run files that do not exist yet. An existing run file is never
re-called, errored ones included (delete an errored file to retry it; `count` then includes it).

Each call is the extractor's final-tier call, built from the extractor's own functions:
`build_extraction_prompt(..., speaker_names=None, continuity_block, locator=<arm is B>)`,
`max_tokens_for(n_segments)`, `enable_thinking=True`, `force_json=True`. Turns are the stored
session turns, which `build` made with the extractor's own `gather_session_segments` +
`assemble_session_turns` (every filter included). `EMIT_EVIDENCE` changes the instruction
block, so it is set on the imported module for the run (both arms alike) and recorded in every
run file, with the truncation constants that shape the transcript.

Run file `runs/locator/<arm>/<rep>/<ref>.json` (side set: `runs/locator/side/...`, since a side
session can also be in the main set and runs under a different prompt):
`{ref, stage, arm, rep, prompt_sha, manifest_digest, emit_evidence, transcript_text_limit,
truncation_head_share, n_segments, max_tokens, topics, shown, usage, model, latency_s, error}`.
`topics` are after `item_locator.strip`; for B, `support` is attached by
`attach_support(locator=True, shown=set(shown))` first, and the model's raw `said_at` per child
is kept in the private `_raw_said_at` (for the copied-evidence diagnostic). With EMIT_EVIDENCE
on, the extractor's `verify_evidence` runs on the topics as it does in the lambda. Side runs add the
extractor's `status_suggestions` counts. `usage` is whatever `call_llm` reports through `info`
(null on a path that reports none).

Never logs item or transcript text: stdout carries counts only.
"""
from __future__ import annotations

import argparse
import contextlib
import copy
import hashlib
import sys
import time
from pathlib import Path

import item_continuity
import item_locator
import item_support
import status_suggestions

from scripts.item_support_eval import store

STAGES = ("fit", "heldout", "side")
ARMS = ("A", "B")
REPS = (1, 2)
EXTRACT_FUNCTION = "fieldsight-test-extract-session"
CALLER = "item_locator_eval"
_ERROR_CHARS = 500


def _les():
    import lambda_extract_session
    return lambda_extract_session


def prompt_sha(locator: bool) -> str:
    """sha256 (hex) of the instruction block the arm sends, with the module's flags as they
    are NOW (EMIT_EVIDENCE changes it), so call it inside `prompt_settings`."""
    return hashlib.sha256(_les()._instructions_block(locator=bool(locator))
                          .encode("utf-8")).hexdigest()


def side_digest(data_dir=store.DATA_DIR):
    """sha256 of data/side/manifest.json: the side set's frozen identity."""
    p = Path(data_dir, store.SIDE_SUBDIR, "manifest.json")
    if not p.exists():
        raise SystemExit(f"Refusing: {p} is missing. Run `build --side` first.")
    return hashlib.sha256(p.read_bytes()).hexdigest()


def stage_sessions(stage, data_dir=store.DATA_DIR):
    if stage == "side":
        return [store.load_session(p)
                for p in sorted(Path(data_dir, store.SIDE_SUBDIR, "sessions").glob("*.json"))]
    if stage not in ("fit", "heldout"):
        raise SystemExit(f"Unknown stage {stage!r}")
    return store.load_sessions(data_dir, split=stage)


def stage_digest(stage, data_dir=store.DATA_DIR):
    return side_digest(data_dir) if stage == "side" else store.manifest_digest(data_dir)


def jobs(sessions):
    """(session, arm, rep) in run order: per session A1, B1, A2, B2 (interleaved, so drift
    over the hours a run takes falls on both arms alike)."""
    return [(s, arm, rep) for s in sessions for rep in REPS for arm in ARMS]


def plan_calls(stage, *, data_dir=store.DATA_DIR, runs_dir=store.RUNS_DIR) -> int:
    """The exact number of model calls `run --stage <stage>` would make now."""
    side = stage == "side"
    return sum(1 for s, arm, rep in jobs(stage_sessions(stage, data_dir))
               if not store.locator_run_path(arm, rep, s["ref"], side=side, runs_dir=runs_dir).exists())


def n_segments(session):
    """What the extractor passes: len(source_filenames). Recorded at build for the side set;
    for the main set, the distinct source files among the stored turns (which undercounts a
    segment that normalised to no timed turn)."""
    if session.get("n_segments"):
        return int(session["n_segments"])
    return len({t.get("source_filename") for t in session.get("turns") or []
                if t.get("source_filename")})


@contextlib.contextmanager
def prompt_settings(emit_evidence):
    les = _les()
    old = les.EMIT_EVIDENCE
    les.EMIT_EVIDENCE = bool(emit_evidence)
    try:
        yield {"emit_evidence": les.EMIT_EVIDENCE,
               "transcript_text_limit": les.TRANSCRIPT_TEXT_LIMIT,
               "truncation_head_share": les.TRUNCATION_HEAD_SHARE,
               # Read from os.environ at import, not copied by load_deployed_llm_env: they
               # decide verify_evidence and the side set's quote check (dropped_by.weak).
               # Export the deployed values before the run; recorded so a mismatch shows.
               "evidence_window_sec": les.EVIDENCE_WINDOW_SEC,
               "evidence_floor_tokens": les.EVIDENCE_FLOOR_TOKENS,
               "evidence_fuzzy": les.EVIDENCE_FUZZY,
               "status_evidence_floor_tokens": les.STATUS_EVIDENCE_FLOOR_TOKENS}
    finally:
        les.EMIT_EVIDENCE = old


def _prior(session):
    return [item_continuity.PriorItem(**p) for p in session.get("prior") or []]


def continuity_block(session, side):
    """Main stages: "" (spec section 6). Side set: the block the deployed final builds with
    status suggestions on, over the prior stored at build."""
    if not side:
        return ""
    return item_continuity.render_block(_prior(session), status_suggestions.INSTRUCTION)


def _raw_said_at(topics):
    return [{"item": f"{ti}:{ln}:{ci}", "said_at": child.get(item_locator.LOCATOR_FIELD)}
            for ti, ln, ci, child, _t in store.iter_items(topics)
            if item_locator.LOCATOR_FIELD in child]


def _suggestions(topics, session):
    """The extractor's own steps for status suggestions: resolve (which decides carried
    identity) then filter_suggestions with the extractor's quote check. No prior: stripped,
    as the extractor does."""
    les = _les()
    prior = _prior(session)
    if not prior:
        status_suggestions.strip_all(topics)
        return None
    try:
        item_continuity.resolve(topics, prior)
    except Exception as e:
        status_suggestions.strip_all(topics)
        return {"error": True, "continuity_error": type(e).__name__}
    try:
        return status_suggestions.filter_suggestions(
            topics, prior, lambda quote, at: les._check_status_quote(
                quote, at, session["turns"], session["date"], session["folder"]))
    except Exception as e:
        status_suggestions.strip_all(topics)
        return {"error": True, "filter_error": type(e).__name__}


def run_one(session, arm, rep, *, call, stage, digest, settings):
    les = _les()
    side = stage == "side"
    locator = arm == "B"
    n_seg = n_segments(session)
    prompt, stats = les.build_extraction_prompt(
        session["folder"], session["date"], session["base"], session["turns"], n_seg,
        speaker_names=None, continuity_block=continuity_block(session, side), locator=locator)
    shown = sorted(stats.get("shown") or ())
    rec = {"ref": session["ref"], "stage": stage, "arm": arm, "rep": rep,
           "prompt_sha": prompt_sha(locator), "manifest_digest": digest, **settings,
           "n_segments": n_seg, "max_tokens": les.max_tokens_for(n_seg),
           "enable_thinking": True, "shown": shown, "topics": None, "usage": None,
           "model": None, "latency_s": None, "error": None}
    info = {}
    t0 = time.monotonic()
    raw, error = call(prompt, max_tokens=rec["max_tokens"], force_json=True,
                      enable_thinking=True, caller=CALLER, info=info)
    rec["latency_s"] = round(time.monotonic() - t0, 3)
    rec["usage"] = info.get("usage")
    rec["model"] = info.get("model")
    if raw is None:
        rec["error"] = f"call_failed: {str(error)[:_ERROR_CHARS]}"
        return rec
    import llm_utils
    parsed = llm_utils.extract_json(raw)
    if parsed is None:
        rec["error"] = "unparseable"
        return rec
    topics = parsed.get("topics", []) if isinstance(parsed, dict) else None
    if not isinstance(topics, list) or not all(isinstance(t, dict) for t in topics):
        rec["error"] = "malformed_topics"
        return rec
    # Read before anything else touches the children (iter_items normalises plain-string
    # decisions/questions in place, exactly as attach_support would).
    raw_said_at = _raw_said_at(topics) if locator else None
    if side:
        item_continuity.normalise_children(topics)   # the extractor does this before resolve
        rec["status_suggestions"] = _suggestions(topics, session)
    else:
        status_suggestions.strip_all(topics)
    if settings["emit_evidence"]:
        # The extractor's next step, so the stored evidence carries the deployed statuses.
        try:
            les.verify_evidence({"topics": topics}, session["turns"], session["date"],
                                user_folder=session["folder"], date=session["date"])
        except Exception as e:
            rec["evidence_error"] = type(e).__name__
    if locator:
        rec["_raw_said_at"] = raw_said_at
        work = {"topics": topics}
        item_support.attach_support(
            work, session["turns"], session["date"], floor=item_support.FLOOR,
            margin=item_support.MARGIN, nothing_close=item_support.NOTHING_CLOSE,
            shown=set(shown), locator=True)
        topics = work["topics"]
    rec["said_at_stripped"] = item_locator.strip(topics)
    rec["topics"] = topics
    return rec


def run_arm(sessions, arm, rep, *, dry_run, call=None, stage="fit",
            runs_dir=store.RUNS_DIR, manifest_digest=None, emit_evidence=False):
    """One arm/rep over `sessions`. dry_run builds each prompt and returns its sha and size,
    with no call and no write. Otherwise each missing run file is made (one call each); an
    existing one is skipped; an error is recorded in its run file and the run goes on.
    Returns one summary row per session (no text)."""
    les = _les()
    side = stage == "side"
    out = []
    with prompt_settings(emit_evidence) as settings:
        for s in sessions:
            path = store.locator_run_path(arm, rep, s["ref"], side=side, runs_dir=runs_dir)
            if dry_run:
                prompt, _stats = les.build_extraction_prompt(
                    s["folder"], s["date"], s["base"], s["turns"], n_segments(s),
                    speaker_names=None, continuity_block=continuity_block(s, side),
                    locator=arm == "B")
                out.append({"ref": s["ref"], "arm": arm, "rep": rep,
                            "prompt_sha": prompt_sha(arm == "B"), "prompt_chars": len(prompt)})
                continue
            if path.exists():
                out.append({"ref": s["ref"], "arm": arm, "rep": rep, "skipped": True})
                continue
            try:
                rec = run_one(s, arm, rep, call=call, stage=stage, digest=manifest_digest,
                              settings=settings)
            except Exception as e:
                # Type name only: a message could carry item or transcript text.
                rec = {"ref": s["ref"], "stage": stage, "arm": arm, "rep": rep,
                       "prompt_sha": prompt_sha(arm == "B"), "manifest_digest": manifest_digest,
                       **settings, "topics": None, "error": f"exception: {type(e).__name__}"}
            store.write_json(path, rec)
            out.append({"ref": s["ref"], "arm": arm, "rep": rep, "error": bool(rec["error"])})
    return out


def run_stage(stage, *, call, data_dir=store.DATA_DIR, runs_dir=store.RUNS_DIR,
              emit_evidence, digest=None):
    sessions = stage_sessions(stage, data_dir)
    digest = digest or stage_digest(stage, data_dir)
    rows = []
    for s, arm, rep in jobs(sessions):
        rows.extend(run_arm([s], arm, rep, dry_run=False, call=call, stage=stage,
                            runs_dir=runs_dir, manifest_digest=digest,
                            emit_evidence=emit_evidence))
    return rows


def _default_load_env(function_name, *, profile, region):
    from scripts.jev_eval.baseline import load_deployed_llm_env
    return load_deployed_llm_env(function_name, profile=profile, region=region)


def _default_call(*a, **kw):
    import llm_utils      # attribute access at call time: load_deployed_llm_env reloads it
    return llm_utils.call_llm(*a, **kw)


def main(argv=None, *, call=None, load_env=None):
    p = argparse.ArgumentParser(description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = p.add_subparsers(dest="cmd", required=True)
    for name in ("count", "run"):
        sp = sub.add_parser(name)
        sp.add_argument("--stage", required=True, choices=STAGES)
        sp.add_argument("--data", default=str(store.DATA_DIR))
        sp.add_argument("--runs", default=str(store.RUNS_DIR))
    r = sub.choices["run"]
    r.add_argument("--confirm-count", type=int, default=None)
    r.add_argument("--emit-evidence", choices=("true", "false"), default=None,
                   help="Required: the deployed TEST extract function's EMIT_EVIDENCE.")
    r.add_argument("--dry-run", action="store_true",
                   help="Build every prompt, print shas and sizes; no call, no write.")
    r.add_argument("--function", default=EXTRACT_FUNCTION)
    r.add_argument("--profile", default="fieldsight-deployer")
    r.add_argument("--region", default="ap-southeast-2")
    a = p.parse_args(argv)

    total = len(stage_sessions(a.stage, a.data)) * len(ARMS) * len(REPS)
    calls = plan_calls(a.stage, data_dir=a.data, runs_dir=a.runs)
    if a.cmd == "count":
        out = {"stage": a.stage, "calls": calls, "total": total,
               "skipped_existing": total - calls}
        print(store.dumps(out))
        return out

    if a.emit_evidence is None:
        print("Refusing: --emit-evidence true|false is required (read it from the deployed "
              "extract function's config).", file=sys.stderr)
        raise SystemExit(2)
    emit = a.emit_evidence == "true"
    if a.dry_run:
        rows = []
        for s, arm, rep in jobs(stage_sessions(a.stage, a.data)):
            rows.extend(run_arm([s], arm, rep, dry_run=True, stage=a.stage,
                                runs_dir=a.runs, emit_evidence=emit))
        out = {"stage": a.stage, "dry_run": True, "prompts": len(rows),
               "prompt_sha": {arm: sorted({x["prompt_sha"] for x in rows if x["arm"] == arm})
                              for arm in ARMS}}
        print(store.dumps(out))
        return out
    if a.confirm_count != calls:
        print(f"Refusing: this run makes {calls} call(s); pass --confirm-count {calls} "
              f"(got {a.confirm_count}).", file=sys.stderr)
        raise SystemExit(2)
    store.require_eval_dir(a.runs)
    digest = stage_digest(a.stage, a.data)   # a missing manifest refuses before the config read
    copied = (load_env or _default_load_env)(a.function, profile=a.profile, region=a.region)
    rows = run_stage(a.stage, call=call or _default_call, data_dir=a.data, runs_dir=a.runs,
                     emit_evidence=emit, digest=digest)
    out = {"stage": a.stage, "calls": sum(1 for x in rows if not x.get("skipped")),
           "errors": sum(1 for x in rows if x.get("error")),
           "skipped_existing": sum(1 for x in rows if x.get("skipped")),
           "env_keys": sorted(copied or [])}
    print(store.dumps(out))
    return out


if __name__ == "__main__":
    main()
