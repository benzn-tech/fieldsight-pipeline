"""Offline A/B for extraction prompt changes. Reads S3, writes nothing.

A prompt change is the one class of change here that no unit test can pin: the code path is
identical, so every test passes either way. This is the instrument that replaces them.

Two rules it exists to enforce, both learned the hard way:

**It imports the real path rather than re-deriving it.** Turn assembly (chunk-overlap dedup,
audio-event tags before device announcements -- that order is load-bearing -- and now the agent
playback filter), prompt construction, truncation, `max_tokens`, `force_json`, thinking mode:
every one of those shapes what the model sees. A harness that rebuilds any of them measures a
request production never sends, and its verdict is about a prompt nobody runs.

**It never writes.** `extract_session` publishes to the real `extraction_key`, which fires
item-writer's delete-then-insert and rewrites a live customer's Aurora topics. Evaluating a
prompt must not mutate the thing being evaluated.

Non-determinism is the reason for `--runs`. `_call_qwen` sends no temperature and no seed, so
three identical prompts give three different answers; one run per arm measures sampling, not the
change. Both thinking modes matter too -- prod runs non-thinking for the live pass and thinking
for the final one, so pinning one leaves the other unmeasured.

Usage:
    python scripts/extraction_ab.py --user Ben_UCPK2 --date 2026-08-12 \
        --session sid2c3084aa7b1146389d0fc008a2c94a51 --runs 3 --thinking both \
        --variant baseline --variant no_invent

    python scripts/extraction_ab.py --user Ben_UCPK2 --date 2026-08-12 --list
"""
import argparse
import json
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "src"))

import boto3                                    # noqa: E402
import lambda_extract_session as ex             # noqa: E402
import llm_utils                                # noqa: E402

BUCKET = os.environ.get("S3_BUCKET", "fieldsight-data-509194952652")


# --------------------------------------------------------------------------
# Variants. A variant is a function prompt -> prompt, applied AFTER the real
# builder, so the baseline arm is byte-identical to what production sends.
# --------------------------------------------------------------------------

NO_INVENT_LINE = (
    "   - If no line in the transcript states an observation, do NOT invent one -- "
    "findings may be an empty array.\n")


def _no_invent(prompt):
    """Insert the escape hatch into the findings instruction (spec v4).

    Anchored on the severity bullet because that is the last line of the findings block; if the
    anchor ever stops matching, the variant must FAIL rather than silently return the baseline
    and report "no difference".
    """
    anchor = "   - entity: the party RESPONSIBLE"
    if anchor not in prompt:
        raise SystemExit("no_invent: anchor not found -- the findings instruction moved. "
                         "Fix the anchor rather than shipping a silent no-op arm.")
    return prompt.replace(anchor, NO_INVENT_LINE + anchor, 1)


VARIANTS = {"baseline": lambda p: p, "no_invent": _no_invent}


# --------------------------------------------------------------------------

def build_prompt(bucket, user_folder, date, session_base):
    """Exactly what extract_session would send, minus the model call."""
    keys = ex.gather_session_segments(bucket, user_folder, date, session_base)
    if not keys:
        raise SystemExit(f"no transcript segments for {user_folder}/{date}/{session_base}")
    turns, source_filenames, _stats = ex.assemble_session_turns(bucket, keys)
    if not turns:
        raise SystemExit("no turns after assembly (all filtered or unparseable)")
    prompt, tstats = ex.build_extraction_prompt(
        user_folder, date, session_base, turns, len(source_filenames))
    return prompt, len(source_filenames), turns, tstats


def summarise(parsed):
    """The few numbers worth comparing. Counts alone are not the verdict -- the observations are
    printed too, because a wording change leaves every count identical."""
    topics = (parsed or {}).get("topics") or []
    findings, actions, questions = [], 0, 0
    for t in topics:
        for f in t.get("findings") or []:
            findings.append((f.get("observation") or "").strip())
        actions += len(t.get("action_items") or [])
        questions += len(t.get("questions") or [])
    return {"topics": len(topics), "findings": len(findings),
            "actions": actions, "questions": questions, "observations": findings}


def run_once(prompt, n_segments, thinking):
    max_tokens = ex.max_tokens_for(n_segments)
    raw, err = llm_utils.call_llm(prompt, max_tokens=max_tokens,
                                  force_json=True, enable_thinking=thinking)
    if raw is None:
        return {"error": err}
    parsed = llm_utils.extract_json(raw)
    if parsed is None:
        return {"error": "unparseable",
                "truncated": ex.looks_truncated(raw), "chars": len(raw)}
    out = summarise(parsed)
    out["chars"] = len(raw)
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--user", required=True, help="user_folder, as in transcripts/{user}/")
    ap.add_argument("--date", required=True)
    ap.add_argument("--session", help="session_base; omit with --list")
    ap.add_argument("--bucket", default=BUCKET)
    ap.add_argument("--runs", type=int, default=3,
                    help="runs per arm; 1 measures sampling, not the change")
    ap.add_argument("--thinking", choices=["off", "on", "both"], default="both",
                    help="prod runs off for the live pass and on for the final one")
    ap.add_argument("--variant", action="append", default=None,
                    choices=sorted(VARIANTS), help="repeatable; default baseline+no_invent")
    ap.add_argument("--list", action="store_true", help="list this day's session bases and exit")
    ap.add_argument("--json", action="store_true", help="emit raw JSON instead of a table")
    ap.add_argument("--out", help="append each result as one JSON line here, as it completes. "
                                  "A thinking-on matrix runs for over an hour; without this, a "
                                  "run that dies at 80%% has measured nothing.")
    args = ap.parse_args()

    if args.list:
        s3 = boto3.client("s3")
        prefix = f"transcripts/{args.user}/{args.date}/"
        bases = {}
        for page in s3.get_paginator("list_objects_v2").paginate(
                Bucket=args.bucket, Prefix=prefix):
            for obj in page.get("Contents", []):
                parsed = ex.session_base_from_key(obj["Key"])
                if parsed is not None:
                    bases[parsed[2]] = bases.get(parsed[2], 0) + 1
        for b, n in sorted(bases.items()):
            print(f"{b}  segments={n}")
        return

    if not args.session:
        ap.error("--session is required (or use --list)")

    variants = args.variant or ["baseline", "no_invent"]
    modes = {"off": [False], "on": [True], "both": [False, True]}[args.thinking]

    base_prompt, n_segments, turns, tstats = build_prompt(
        args.bucket, args.user, args.date, args.session)
    print(f"# {args.user}/{args.date}/{args.session}", file=sys.stderr)
    print(f"# turns={len(turns)} segments={n_segments} prompt_chars={len(base_prompt)} "
          f"truncated={tstats.get('truncated')} max_tokens={ex.max_tokens_for(n_segments)}",
          file=sys.stderr)

    results = []
    for variant in variants:
        prompt = VARIANTS[variant](base_prompt)
        for thinking in modes:
            for i in range(args.runs):
                r = run_once(prompt, n_segments, thinking)
                r.update(variant=variant, thinking=thinking, run=i + 1,
                         prompt_chars=len(prompt), session=args.session,
                         user=args.user, date=args.date)
                results.append(r)
                if args.out:
                    with open(args.out, "a", encoding="utf-8") as fh:
                        fh.write(json.dumps(r, ensure_ascii=False) + "\n")
                        fh.flush()
                if not args.json:
                    if "error" in r:
                        print(f"{variant:10} thinking={str(thinking):5} run{i+1}  "
                              f"ERROR {r['error']}")
                    else:
                        print(f"{variant:10} thinking={str(thinking):5} run{i+1}  "
                              f"topics={r['topics']:<3} findings={r['findings']:<3} "
                              f"actions={r['actions']:<3} questions={r['questions']:<3} "
                              f"chars={r['chars']}")

    if args.json:
        print(json.dumps(results, ensure_ascii=False, indent=2))
        return

    # Observations printed in full: the scaffolding case showed a count falling while nothing
    # was lost -- the item had moved to action_items. Only reading them tells the two apart.
    print("\n--- observations ---")
    for r in results:
        if "observations" in r:
            print(f"[{r['variant']} thinking={r['thinking']} run{r['run']}]")
            for o in r["observations"]:
                print(f"    - {o[:110]}")


if __name__ == "__main__":
    main()
