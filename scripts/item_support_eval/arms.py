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
extractor's `status_suggestions` counts; main-stage runs record `status_suggestions_stripped`
(stray suggestions removed, for the report). `usage` is whatever `call_llm` reports through `info`
(null on a path that reports none).

Never logs item or transcript text: stdout carries counts only.

Re-test (design 2026-11-12, C3/C4):

    python -m scripts.item_support_eval.arms count --stage fit --wording w1 --arms B1
    python -m scripts.item_support_eval.arms run --stage fit --wording w1 --arms B1 \
        --emit-evidence true --confirm-count 30

`--wording w1|w2` puts the runs under `runs/locator_v2/<wording>/` (same layout inside);
without it everything is the first test's `runs/locator/`, unchanged. `--arms` (a comma list
of A1, A2, B1, B2; default all four) restricts the jobs, and `count` and the confirm gate count
the same subset. A re-test `run` also reads the deployed function's configuration before
the extractor is imported and:
  - refuses when TRANSCRIPT_TEXT_LIMIT, any EVIDENCE_* key or STATUS_EVIDENCE_FLOOR_TOKENS
    differs from this process's environment (set-vs-unset counts as a difference);
  - refuses when the function has no LLM_REASONING_EFFORT, unless --allow-default-effort;
  - after `load_deployed_llm_env`, mirrors MIRRORED_LLM_KEYS (a key set but empty stays set
    and empty; a key missing on the function is removed here) and reloads `llm_utils`;
  - refuses when any B run already in the namespace records another B prompt sha;
  - records in every run file `wording`, `reasoning_effort`, `fallback_models`,
    `primary_model` (what `llm_utils` will call, read back after the reload) and
    `deployed_env`, the copied key/value map without API keys (null = unset on the function).
"""
from __future__ import annotations

import argparse
import contextlib
import copy
import hashlib
import importlib
import json
import os
import subprocess
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
ARM_KEYS = ("A1", "A2", "B1", "B2")

# Copied from the deployed function after `load_deployed_llm_env` (design 2026-11-12 C3):
# the first test ran at the provider's default effort because none of these was copied.
MIRRORED_LLM_KEYS = ("LLM_REASONING_EFFORT", "LLM_FALLBACK_MODELS",
                     "LLM_REASONING_HEADROOM", "LLM_ANSWER_TOKEN_CEILING", "LLM_HTTP_TIMEOUT",
                     "LLM_CHAIN_BUDGET_SECONDS", "QWEN_ENABLE_THINKING")
# Read by lambda_extract_session at import: env key -> (module constant, parser, default).
EXTRACTOR_ENV = {
    "TRANSCRIPT_TEXT_LIMIT": ("TRANSCRIPT_TEXT_LIMIT", int, "300000"),
    "EVIDENCE_WINDOW_SEC": ("EVIDENCE_WINDOW_SEC", float, "60"),
    "EVIDENCE_FLOOR_TOKENS": ("EVIDENCE_FLOOR_TOKENS", int, "5"),
    "EVIDENCE_FUZZY_THRESHOLD": ("EVIDENCE_FUZZY", float, "0.80"),
    "STATUS_EVIDENCE_FLOOR_TOKENS": ("STATUS_EVIDENCE_FLOOR_TOKENS", int, "5"),
}
_EXTRACTOR_PREFIX = "EVIDENCE_"
EFFORT_KEY = "LLM_REASONING_EFFORT"


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


def parse_arms(value):
    """`--arms` (comma list of A1, A2, B1, B2) -> the keys in ARM_KEYS order; None -> all."""
    if value is None:
        return ARM_KEYS
    keys = {x.strip() for x in str(value).split(",") if x.strip()}
    bad = sorted(keys - set(ARM_KEYS))
    if bad or not keys:
        raise SystemExit(f"Refusing: --arms takes a comma list of {', '.join(ARM_KEYS)} "
                         f"(got {value!r}).")
    return tuple(k for k in ARM_KEYS if k in keys)


def jobs(sessions, arms=ARM_KEYS):
    """(session, arm, rep) in run order: per session A1, B1, A2, B2 (interleaved, so drift
    over the hours a run takes falls on both arms alike), restricted to `arms`."""
    return [(s, arm, rep) for s in sessions for rep in REPS for arm in ARMS
            if f"{arm}{rep}" in arms]


def plan_calls(stage, *, data_dir=store.DATA_DIR, runs_dir=store.RUNS_DIR, arms=ARM_KEYS,
               wording=None) -> int:
    """The exact number of model calls `run --stage <stage>` would make now."""
    side = stage == "side"
    return sum(1 for s, arm, rep in jobs(stage_sessions(stage, data_dir), arms)
               if not store.locator_run_path(arm, rep, s["ref"], side=side, runs_dir=runs_dir,
                                             wording=wording).exists())


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
    """The model's raw `said_at` per child, before anything strips it. A child that also
    carries a status_suggestion records its raw `at` as `status_at` (a main stage strips the
    suggestion before the run file is written; the re-test reports how often the two match)."""
    out = []
    for ti, ln, ci, child, _t in store.iter_items(topics):
        if item_locator.LOCATOR_FIELD not in child:
            continue
        row = {"item": f"{ti}:{ln}:{ci}", "said_at": child.get(item_locator.LOCATOR_FIELD)}
        sugg = child.get("status_suggestion")
        if isinstance(sugg, dict) and "at" in sugg:
            row["status_at"] = sugg.get("at")
        out.append(row)
    return out


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


def run_one(session, arm, rep, *, call, stage, digest, settings, extra=None):
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
           **(extra or {}),
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
        # Counted for the report: a stray suggestion on a final with no status instruction.
        rec["status_suggestions_stripped"] = status_suggestions.strip_all(topics)
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
            runs_dir=store.RUNS_DIR, manifest_digest=None, emit_evidence=False, wording=None,
            extra=None):
    """One arm/rep over `sessions`. dry_run builds each prompt and returns its sha and size,
    with no call and no write. Otherwise each missing run file is made (one call each); an
    existing one is skipped; an error is recorded in its run file and the run goes on.
    `extra` (the re-test's deployed-config record) is merged into every run file.
    Returns one summary row per session (no text)."""
    les = _les()
    side = stage == "side"
    out = []
    with prompt_settings(emit_evidence) as settings:
        for s in sessions:
            path = store.locator_run_path(arm, rep, s["ref"], side=side, runs_dir=runs_dir,
                                          wording=wording)
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
                              settings=settings, extra=extra)
            except Exception as e:
                # Type name only: a message could carry item or transcript text.
                rec = {"ref": s["ref"], "stage": stage, "arm": arm, "rep": rep,
                       "prompt_sha": prompt_sha(arm == "B"), "manifest_digest": manifest_digest,
                       **settings, **(extra or {}), "topics": None,
                       "error": f"exception: {type(e).__name__}"}
            store.write_json(path, rec)
            out.append({"ref": s["ref"], "arm": arm, "rep": rep, "error": bool(rec["error"])})
    return out


def run_stage(stage, *, call, data_dir=store.DATA_DIR, runs_dir=store.RUNS_DIR,
              emit_evidence, digest=None, arms=ARM_KEYS, wording=None, extra=None):
    sessions = stage_sessions(stage, data_dir)
    digest = digest or stage_digest(stage, data_dir)
    rows = []
    for s, arm, rep in jobs(sessions, arms):
        rows.extend(run_arm([s], arm, rep, dry_run=False, call=call, stage=stage,
                            runs_dir=runs_dir, manifest_digest=digest,
                            emit_evidence=emit_evidence, wording=wording, extra=extra))
    return rows


# ---------------------------------------------------------------------------------------
# The deployed configuration (re-test design 2026-11-12, C3)
# ---------------------------------------------------------------------------------------

def _is_secret(key):
    k = key.upper()
    return "API_KEY" in k or k.endswith("_KEY") or "SECRET" in k or "PASSWORD" in k


def extractor_env_mismatches(deployed, environ=None, module=None):
    """[{"key", "deployed", "local"}] for every extractor setting that differs.

    Compared as strings against `environ` (None = unset on either side; both unset agree):
    the EXTRACTOR_ENV keys and every EVIDENCE_* key either side sets. When `module` (an
    already imported lambda_extract_session) is given, its import-time constants are also
    compared with the deployed value parsed the extractor's way (`local` is then the
    constant), since an imported module no longer reads the environment."""
    environ = os.environ if environ is None else environ
    keys = set(EXTRACTOR_ENV) | {k for k in set(deployed) | set(environ)
                                 if k.startswith(_EXTRACTOR_PREFIX)}
    out = []
    for key in sorted(keys):
        if deployed.get(key) != environ.get(key):
            out.append({"key": key, "deployed": deployed.get(key), "local": environ.get(key)})
            continue
        if module is not None and key in EXTRACTOR_ENV:
            attr, parse, default = EXTRACTOR_ENV[key]
            try:
                want = parse(deployed.get(key, default))
            except (TypeError, ValueError):
                want = None
            if want is None or getattr(module, attr, None) != want:
                out.append({"key": key, "deployed": deployed.get(key),
                            "local": repr(getattr(module, attr, None))})
    return out


def mirror_deployed_keys(deployed, environ=None):
    """Copy MIRRORED_LLM_KEYS from the deployed Variables into `environ`: present (even
    empty) -> set to that string; missing -> removed. Returns {key: value or None}. The
    caller reloads llm_utils (it reads most of these at import)."""
    environ = os.environ if environ is None else environ
    copied = {}
    for key in MIRRORED_LLM_KEYS:
        if key in deployed and deployed[key] is not None:
            environ[key] = str(deployed[key])
            copied[key] = str(deployed[key])
        else:
            environ.pop(key, None)
            copied[key] = None
    return copied


def deployed_record(deployed, copied_names, mirrored, wording):
    """What every re-test run file records about the configuration calls run under. The
    effort, fallbacks and primary model are read back from the reloaded `llm_utils`, never
    from os.environ, so they are the values calls use. No API key value is ever kept."""
    import llm_utils
    env = {k: deployed.get(k) for k in copied_names or () if not _is_secret(k)}
    env.update({k: v for k, v in mirrored.items() if not _is_secret(k)})
    return {"wording": wording,
            "reasoning_effort": llm_utils.LLM_REASONING_EFFORT or None,
            "fallback_models": list(llm_utils.fallback_models()),
            "primary_model": llm_utils.active_model(enable_thinking=True),
            "deployed_env": dict(sorted(env.items()))}


class _Replay:
    """A `subprocess.run` stand-in that returns one already-read configuration, so
    `load_deployed_llm_env` copies from the same snapshot this module compared."""

    def __init__(self, config):
        self.stdout = json.dumps(config).encode("utf-8")

    def __call__(self, *a, **kw):
        return subprocess.CompletedProcess(a[0] if a else [], 0, stdout=self.stdout,
                                           stderr=b"")


def _default_read_env(function_name, *, profile, region, run=subprocess.run):
    """The deployed function's Environment.Variables via the aws CLI (read only). Never
    called by tests."""
    env = dict(os.environ, PYTHONUTF8="1", PYTHONIOENCODING="utf-8",
               AWS_CLI_FILE_ENCODING="UTF-8")
    result = run(["aws", "lambda", "get-function-configuration", "--function-name",
                  function_name, "--profile", profile, "--region", region, "--output", "json"],
                 capture_output=True, env=env)
    if result.returncode != 0:
        err = result.stderr.decode("utf-8", errors="replace") \
            if isinstance(result.stderr, bytes) else str(result.stderr)
        raise SystemExit(f"Refusing: could not read {function_name}'s configuration: "
                         f"{err.strip()[:2000]}")
    out = result.stdout.decode("utf-8") if isinstance(result.stdout, bytes) else result.stdout
    return dict((json.loads(out).get("Environment") or {}).get("Variables") or {})


def _default_load_env(function_name, *, profile, region, deployed=None):
    from scripts.jev_eval.baseline import load_deployed_llm_env
    if deployed is None:
        return load_deployed_llm_env(function_name, profile=profile, region=region)
    return load_deployed_llm_env(function_name, profile=profile, region=region,
                                 run=_Replay({"Environment": {"Variables": deployed}}))


def require_b_prompt_unchanged(runs_dir, wording, emit_evidence):
    """Re-test: refuse when any B run already in the wording's namespace (main or side)
    records another prompt sha than the B prompt this run would send. A changed wording is
    a new namespace (w2), never a mix inside one."""
    with prompt_settings(emit_evidence):
        current = prompt_sha(True)
    base = store.locator_runs_base(runs_dir, wording)
    for p in sorted(base.glob("**/B/*/*.json")):
        recorded = store.read_json(p).get("prompt_sha")
        if recorded != current:
            _refuse(f"a B run under locator_v2/{wording}/ ({p.relative_to(base).as_posix()}) "
                    f"records prompt_sha {recorded}; this run would send {current}. Use "
                    f"another wording namespace.")


def _refuse(msg):
    print(f"Refusing: {msg}", file=sys.stderr)
    raise SystemExit(2)


def prepare_deployed_config(a, *, read_env, load_env):
    """The re-test's config steps, in order, before the extractor is imported here. Returns
    the record every run file carries. Refusals exit non-zero before any model call."""
    deployed = read_env(a.function, profile=a.profile, region=a.region)
    mism = extractor_env_mismatches(deployed, module=sys.modules.get("lambda_extract_session"))
    if mism:
        lines = "; ".join(f"{m['key']}: deployed={m['deployed']!r} local={m['local']!r}"
                          for m in mism)
        _refuse(f"the extractor settings differ from {a.function}'s ({lines}). Export the "
                f"deployed values (unset what it does not set) and run again.")
    if not str(deployed.get(EFFORT_KEY) or "").strip() and not a.allow_default_effort:
        _refuse(f"{a.function} has no {EFFORT_KEY}; the run would use the provider's default "
                f"effort. Pass --allow-default-effort to run anyway (it is recorded).")
    copied = load_env(a.function, profile=a.profile, region=a.region, deployed=deployed)
    mirrored = mirror_deployed_keys(deployed)
    import llm_utils
    importlib.reload(llm_utils)
    return deployed_record(deployed, copied, mirrored, a.wording)


def _default_call(*a, **kw):
    import llm_utils      # attribute access at call time: load_deployed_llm_env reloads it
    return llm_utils.call_llm(*a, **kw)


def main(argv=None, *, call=None, load_env=None, read_env=None):
    p = argparse.ArgumentParser(description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = p.add_subparsers(dest="cmd", required=True)
    for name in ("count", "run"):
        sp = sub.add_parser(name)
        sp.add_argument("--stage", required=True, choices=STAGES)
        sp.add_argument("--data", default=str(store.DATA_DIR))
        sp.add_argument("--runs", default=str(store.RUNS_DIR))
        sp.add_argument("--wording", default=None, choices=store.WORDINGS,
                        help="Re-test namespace runs/locator_v2/<wording>/ (design "
                             "2026-11-12). Omitted: the first test's runs/locator/.")
        sp.add_argument("--arms", default=None,
                        help="Comma list of A1,A2,B1,B2 (default: all four).")
    r = sub.choices["run"]
    r.add_argument("--allow-default-effort", action="store_true",
                   help="Re-test only: run even when the deployed function sets no "
                        "LLM_REASONING_EFFORT.")
    r.add_argument("--confirm-count", type=int, default=None)
    r.add_argument("--emit-evidence", choices=("true", "false"), default=None,
                   help="Required: the deployed TEST extract function's EMIT_EVIDENCE.")
    r.add_argument("--dry-run", action="store_true",
                   help="Build every prompt, print shas and sizes; no call, no write.")
    r.add_argument("--function", default=EXTRACT_FUNCTION)
    r.add_argument("--profile", default="fieldsight-deployer")
    r.add_argument("--region", default="ap-southeast-2")
    a = p.parse_args(argv)

    sel = parse_arms(a.arms)
    total = len(stage_sessions(a.stage, a.data)) * len(sel)
    calls = plan_calls(a.stage, data_dir=a.data, runs_dir=a.runs, arms=sel, wording=a.wording)
    if a.cmd == "count":
        out = {"stage": a.stage, "calls": calls, "total": total,
               "skipped_existing": total - calls}
        if a.wording is not None or a.arms is not None:
            out.update({"wording": a.wording, "arms": list(sel)})
        print(store.dumps(out))
        return out

    if a.emit_evidence is None:
        print("Refusing: --emit-evidence true|false is required (read it from the deployed "
              "extract function's config).", file=sys.stderr)
        raise SystemExit(2)
    emit = a.emit_evidence == "true"
    if a.dry_run:
        rows = []
        for s, arm, rep in jobs(stage_sessions(a.stage, a.data), sel):
            rows.extend(run_arm([s], arm, rep, dry_run=True, stage=a.stage,
                                runs_dir=a.runs, emit_evidence=emit, wording=a.wording))
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
    if a.wording is None:
        # The first test's path, unchanged.
        copied = (load_env or _default_load_env)(a.function, profile=a.profile,
                                                 region=a.region)
        extra = None
    else:
        extra = prepare_deployed_config(a, read_env=read_env or _default_read_env,
                                        load_env=load_env or _default_load_env)
        copied = list(extra["deployed_env"])
        require_b_prompt_unchanged(a.runs, a.wording, emit)
    rows = run_stage(a.stage, call=call or _default_call, data_dir=a.data, runs_dir=a.runs,
                     emit_evidence=emit, digest=digest, arms=sel, wording=a.wording,
                     extra=extra)
    out = {"stage": a.stage, "calls": sum(1 for x in rows if not x.get("skipped")),
           "errors": sum(1 for x in rows if x.get("error")),
           "skipped_existing": sum(1 for x in rows if x.get("skipped")),
           "env_keys": sorted(copied or [])}
    if extra is not None:
        out.update({"wording": a.wording, "arms": list(sel),
                    "reasoning_effort": extra["reasoning_effort"]})
    print(store.dumps(out))
    return out


if __name__ == "__main__":
    main()
