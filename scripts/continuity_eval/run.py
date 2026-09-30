"""Runs the item-continuity measurement's baseline / with-block arms (spec §7 "Runs").

Builds each shape's chain of extraction passes over a growing prefix of a session's
transcript segments, using the REAL `lambda_extract_session.build_extraction_prompt`
to build the prompt and the REAL `gather_session_segments` / `assemble_session_turns`
to gather turns -- this harness never re-types the prompt. Continuity itself (prior
selection, the rendered block, and the accept/reject guards) goes through the real
`item_continuity` module, never re-derived here.

Never writes to S3 or any database. Reads use the `fieldsight-deployer` AWS profile.
`--dry-run` lists what the harness would run and makes no LLM call.

    python -m scripts.continuity_eval.run --env test --sessions 15 \
        --out continuity_eval_runs/<run_id>
"""
from __future__ import annotations

import argparse
import json
import os
import time
from pathlib import Path

import item_continuity
import lambda_extract_session as les
import llm_utils

from scripts.continuity_eval import sessions as sessions_mod
from scripts.jev_eval.baseline import load_deployed_llm_env

# Spec/continuity-plan-facts.md §9: StageConfig prefixes give the deployed function
# names extract_session runs under. Only "test" is used by Task 10/11 -- the prod
# function name is recorded here for Task 12 (a prod-config run), not called by this
# task.
EXTRACT_SESSION_FUNCTION = {
    "test": "fieldsight-test-extract-session",
    "prod": "fieldsight-prod-extract-session",
}

ARMS = ("baseline", "with_block")
REPS = (1, 2, 3)
DEFAULT_SHAPE_NAMES = ("a", "b")


def void(run):
    """Spec §7 "Void condition": a with-block step whose prompt lacks the
    `<<<PRIOR_ITEMS>>>` block, or whose `prior_count` is 0, is void and excluded.

    A chain's first pass always has `prior_count` 0 -- nothing is published yet for
    it to offer -- so it is void under this exact same rule whenever it runs under
    the with-block arm; there is no separate "first step of a chain" case to encode
    here, the shared rule already covers it."""
    return run.get("arm") == "with_block" and (
        run.get("prior_count", 0) == 0 or not run.get("prompt_has_block", False))


def _session_key(session):
    return f"{session['user_folder']}__{session['date']}__{session['session_base']}"


def _count_by_kind(topics):
    counts = {k: 0 for k in item_continuity.KINDS}
    for topic in topics if isinstance(topics, list) else []:
        if not isinstance(topic, dict):
            continue
        for list_name in item_continuity.KINDS:
            counts[list_name] += len(topic.get(list_name) or [])
    return counts


def _run_one_step(user_folder, date, session_base, turns, n_segments, *,
                   arm, is_first, is_final, prior_topics):
    """One extraction pass. Returns the step record (spec §7's per-step shape, plus
    bookkeeping fields) and the resolved topics to feed as next step's prior."""
    if arm == "with_block" and not is_first and prior_topics:
        prior_list = item_continuity.prior_items({"topics": prior_topics})
    else:
        prior_list = []
    continuity_block = item_continuity.render_block(prior_list) if prior_list else ""

    prompt, _stats = les.build_extraction_prompt(
        user_folder, date, session_base, turns, n_segments,
        continuity_block=continuity_block)
    prompt_has_block = "<<<PRIOR_ITEMS>>>" in prompt
    prior_count = len(prior_list)
    # The exact transcript text this step's prompt was built from -- `build_extraction_prompt`
    # doesn't return it, so it's rendered here the same way (`les.render_transcript`, the
    # function it calls internally). Kept on the step record (gitignored run dir only) so
    # Task 11's gold labelling can show the annotator real evidence for the "supported by the
    # transcript" judgement, instead of asking them to judge it from nothing.
    transcript_text, _transcript_stats = les.render_transcript(turns)

    started = time.time()
    raw, error = llm_utils.call_llm(
        prompt, max_tokens=les.max_tokens_for(n_segments), force_json=True,
        enable_thinking=is_final, caller="continuity_eval")
    latency_ms = int((time.time() - started) * 1000)

    topics = []
    claims = []
    if raw is not None and error is None:
        parsed = llm_utils.extract_json(raw)
        if isinstance(parsed, dict):
            candidate_topics = parsed.get("topics", [])
            if isinstance(candidate_topics, list) and all(isinstance(t, dict) for t in candidate_topics):
                topics = candidate_topics
                item_continuity.normalise_children(topics)
                if arm == "with_block" and prior_list:
                    claims = item_continuity.resolve(topics, prior_list)
                else:
                    claims = item_continuity.assign_fresh_ids(topics) or []
            else:
                error = "malformed 'topics' in LLM response"
        else:
            error = error or "could not parse JSON from LLM response"

    run_record = {"arm": arm, "prior_count": prior_count, "prompt_has_block": prompt_has_block}
    record = {
        "prompt_has_block": prompt_has_block,
        "prior_count": prior_count,
        "extraction": {"topics": topics},
        "claims": claims,
        "void": void(run_record),
        "error": error,
        "latency_ms": latency_ms,
        "n_segments": n_segments,
        "transcript_text": transcript_text,
    }
    next_prior = topics if topics else prior_topics
    return record, next_prior


def run_chain(bucket, session, shape_name, shape_steps, arm, rep, out_dir):
    """Runs one (session, shape, arm, rep) chain end to end, writing each step's
    record to `runs/<shape>/<arm>/<rep>/<session>/<step>.json` under `out_dir`.
    Returns the list of step records, in step order."""
    user_folder = session["user_folder"]
    date = session["date"]
    session_base = session["session_base"]

    keys = les.gather_session_segments(bucket, user_folder, date, session_base)
    fracs = sessions_mod.fraction_sequence(shape_steps)

    session_dir = Path(out_dir) / "runs" / shape_name / arm / str(rep) / _session_key(session)
    session_dir.mkdir(parents=True, exist_ok=True)

    records = []
    prior_topics = None
    for idx, frac in enumerate(fracs):
        prefix_keys = sessions_mod.prefix_segments(keys, frac)
        turns, source_filenames, _announce = les.assemble_session_turns(bucket, prefix_keys)
        n_segments = len(source_filenames)
        record, prior_topics = _run_one_step(
            user_folder, date, session_base, turns, n_segments,
            arm=arm, is_first=(idx == 0), is_final=(idx == len(fracs) - 1),
            prior_topics=prior_topics)
        record["frac"] = frac
        (session_dir / f"{idx}.json").write_text(
            json.dumps(record, ensure_ascii=False, indent=2), encoding="utf-8")
        records.append(record)
    return records


def planned_runs(sessions, shapes_dict, arms=ARMS, reps=REPS):
    """Every (session, shape, arm, rep) combination the harness would run -- pure, so
    `--dry-run` and the real run can never disagree about scope."""
    plan = []
    for session in sessions:
        for shape_name, steps in shapes_dict.items():
            for arm in arms:
                for rep in reps:
                    plan.append({
                        "session": _session_key(session),
                        "shape": shape_name,
                        "arm": arm,
                        "rep": rep,
                        "n_steps": len(sessions_mod.fraction_sequence(steps)),
                    })
    return plan


def build_counts(sessions):
    """counts.json: per-session, per-kind item counts from PROD's published
    extractions -- read-only, regardless of which `--env` the run's own sessions came
    from, per spec §6.6 (a fixed reference distribution, not part of the arm
    comparison)."""
    prod_bucket = sessions_mod.BUCKETS["prod"]
    out = []
    for session in sessions:
        out_key = les.extraction_key(session["user_folder"], session["date"], session["session_base"])
        extraction = les.read_existing_extraction(prod_bucket, out_key)
        counts = _count_by_kind(extraction.get("topics")) if isinstance(extraction, dict) else \
            {k: None for k in item_continuity.KINDS}
        out.append({"session": _session_key(session), **counts})
    return out


def _select_shapes(names):
    all_shapes = sessions_mod.shapes()
    return {name: all_shapes[name] for name in names if name in all_shapes}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--env", choices=("test", "prod"), required=True)
    parser.add_argument("--sessions", type=int, default=15,
                         help="How many candidate sessions to take (spec: at least 15).")
    parser.add_argument("--out", required=True, help="Output directory (gitignored).")
    parser.add_argument("--shapes", default=",".join(DEFAULT_SHAPE_NAMES),
                         help="Comma-separated shape names to run (a, b, c).")
    parser.add_argument("--dry-run", action="store_true",
                         help="List what would run; makes no LLM call.")
    parser.add_argument("--profile", default=sessions_mod.DEFAULT_PROFILE)
    parser.add_argument("--region", default=sessions_mod.DEFAULT_REGION)
    args = parser.parse_args(argv)

    # sessions_mod.s3_client() takes an explicit profile/region, but
    # gather_session_segments / assemble_session_turns / read_existing_extraction
    # read through lambda_extract_session's own lazily-built client (`les.s3()`),
    # which has no profile parameter -- it only ever sees the process's ambient AWS
    # credential chain. setdefault so an operator who already exported AWS_PROFILE
    # is never overridden.
    os.environ.setdefault("AWS_PROFILE", args.profile)
    os.environ.setdefault("AWS_DEFAULT_REGION", args.region)

    out_dir = Path(args.out)
    out_dir.mkdir(parents=True, exist_ok=True)

    shapes_dict = _select_shapes(args.shapes.split(","))

    client = sessions_mod.s3_client(profile=args.profile, region=args.region)
    candidates = sessions_mod.list_candidate_sessions(args.env, client=client)
    chosen = candidates[: args.sessions]

    (out_dir / "sessions.json").write_text(
        json.dumps(chosen, ensure_ascii=False, indent=2), encoding="utf-8")

    plan = planned_runs(chosen, shapes_dict)
    if args.dry_run:
        for entry in plan:
            print(json.dumps(entry))
        return plan

    # Same mechanism as scripts/jev_eval/baseline.py: copies the DEPLOYED extract
    # session function's LLM env vars into this process and reloads llm_utils, so
    # every call_llm below runs under the actual deployed model config, not this
    # process's own default.
    load_deployed_llm_env(
        EXTRACT_SESSION_FUNCTION[args.env], profile=args.profile, region=args.region)

    bucket = sessions_mod.BUCKETS[args.env]
    all_records = []
    for session in chosen:
        for shape_name, steps in shapes_dict.items():
            for arm in ARMS:
                for rep in REPS:
                    all_records.extend(
                        run_chain(bucket, session, shape_name, steps, arm, rep, out_dir))

    counts = build_counts(chosen)
    (out_dir / "counts.json").write_text(
        json.dumps(counts, ensure_ascii=False, indent=2), encoding="utf-8")

    return all_records


if __name__ == "__main__":
    main()
