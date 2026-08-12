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
import agent_turn_filter                        # noqa: E402
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
    """Insert the escape hatch above the entity bullet in the findings instruction (spec v4).

    Two guards, and the second one exists because the first was not enough.

    The anchor must still match, or the arm would silently be a copy of the baseline and report
    "no difference" -- the most expensive possible wrong answer from this tool.

    The line must ALSO not already be present. Once the change is merged, the baseline prompt
    contains it, and this function would happily add a SECOND copy: the comparison silently
    becomes line-once versus line-twice instead of without versus with. That is the same
    silent-no-op failure the first guard is written to prevent, arriving through the other door,
    and it appears precisely when someone re-runs the harness to confirm a shipped change.
    """
    anchor = "   - entity: the party RESPONSIBLE"
    if anchor not in prompt:
        raise SystemExit("no_invent: anchor not found -- the findings instruction moved. "
                         "Fix the anchor rather than shipping a silent no-op arm.")
    # Matched on a short fragment, NOT on NO_INVENT_LINE itself. The shipped line is wrapped
    # across two source lines, so comparing the full string silently found nothing and the guard
    # did not fire -- the very failure it was written to catch, arriving through a third door.
    if "do NOT invent one" in prompt:
        raise SystemExit(
            "no_invent: the line is ALREADY in the prompt -- this change is merged. Comparing "
            "now would measure line-once vs line-twice. To re-measure against the pre-change "
            "prompt, check out the parent commit of the change and run there.")
    return prompt.replace(anchor, NO_INVENT_LINE + anchor, 1)


DROP_BLOCK = '''      "decisions": [
        {
          "decision": "What was decided",
          "rationale": "Why this decision was made",
          "decided_by": "Who decided, or null if not stated"
        }
      ],
      "questions": [
        {"question": "An open/unresolved question raised in the session"}
      ]
'''


def _no_qd(prompt):
    """Remove `decisions` and `questions` from the requested schema.

    Both are write-only today: item_writer persists neither and chunking embeds neither (the
    `key_decisions` that chunking and ask_agent read lives on the REPORT artifact, a different
    shape with a different key). So they are output tokens bought on every extraction and thrown
    away, on a path with a live truncation history.

    Deleting them is not obviously safe, which is why this is a measured arm and not a patch.
    `questions` is where the model puts what it is unsure about -- it filled that field correctly
    in 3/3 pilot runs. Removing the relief valve may push the same uncertain content into
    `findings` as assertions, which is precisely the defect `no_invent` exists to fix. If the
    findings count rises here, dropping them is the wrong move regardless of the token saving.
    """
    if DROP_BLOCK not in prompt:
        raise SystemExit("no_qd: schema block not found -- it moved. Fix the anchor rather than "
                         "shipping an arm that silently measures the baseline twice.")
    # The preceding block's closing "]," must become "]" or the JSON example is malformed.
    return prompt.replace("      ],\n" + DROP_BLOCK, "      ]\n", 1)


VARIANTS = {"baseline": lambda p: p, "no_invent": _no_invent, "no_qd": _no_qd,
            "both": lambda p: _no_qd(_no_invent(p))}


# --------------------------------------------------------------------------

def build_prompt_from_turns(user_folder, date, session_base, turns, n_segments):
    """Same prompt builder, turns supplied locally instead of read from S3.

    This exists for the both-languages criterion. The bucket holds exactly one day with any CJK at
    all, and that session turns out to be English speech containing a few Chinese names -- 51 CJK
    characters total -- so it tests nothing about a Chinese transcript.

    The obvious alternative, uploading a Chinese transcript to S3, is not available: `transcripts/`
    is the live production trigger for extract-session, so writing a fixture there would run the
    real pipeline on a real customer's folder. Turns go in from a file instead, and
    build_extraction_prompt -- the real one -- does the rest.

    Fixture turns are SYNTHETIC and must be labelled as such wherever the results are reported.
    They demonstrate the prompt's behaviour on Chinese input; they are not evidence about any
    real recording.
    """
    prompt, tstats = ex.build_extraction_prompt(
        user_folder, date, session_base, turns, n_segments)
    return prompt, n_segments, turns, tstats


def build_group_prompt_from_turns(date, turn_sets):
    """The multi-device merge prompt, from local turns.

    `build_group_prompt` reuses `_instructions_block()` verbatim, so any edit to the findings
    instruction changes group extractions too -- on the path where meetings, and therefore
    findings, matter most. Measuring only the solo prompt would ship an unmeasured surface while
    reporting the change as measured.

    The group path is not reachable from a single session id: it needs a claim artifact naming
    several devices. Rather than manufacture one in S3, the two fixture recordings stand in for
    two body-worn recorders that heard the same meeting. `extract_group` always calls with
    enable_thinking=True, so that is the only mode worth running here.
    """
    artifact = {"groupId": "fixture", "members": [{"date": date, "userFolder": "Fixture_User"}]}
    sources = [{"session_id": f"sidfixture_dev{i}", "turns": t}
               for i, t in enumerate(turn_sets, 1)]
    return ex.build_group_prompt(artifact, sources)


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
    """Counts, plus the TEXT of every finding and every action item.

    The action texts are not decoration. The acceptance criterion is "read the findings, do not
    count them": for each finding present in control and absent in the treatment arm, decide
    whether it MOVED to `action_items` or actually vanished. In the pilot the finding count fell
    while nothing was lost -- the scaffolding item had become an action, which was the better
    answer. Recording only observations makes that distinction unanswerable from the data and
    sends you back to the model for another hour of runs to ask a question you already paid for.
    """
    topics = (parsed or {}).get("topics") or []
    findings, actions, questions = [], [], []
    for t in topics:
        for f in t.get("findings") or []:
            findings.append((f.get("observation") or "").strip())
        for a in t.get("action_items") or []:
            actions.append((a.get("action") or "").strip())
        for q in t.get("questions") or []:
            questions.append((q.get("question") if isinstance(q, dict) else str(q) or "").strip())
    return {"topics": len(topics), "findings": len(findings),
            "actions": len(actions), "questions": len(questions),
            "observations": findings, "action_texts": actions,
            "summaries": [(t.get("summary") or "").strip() for t in topics]}


def _env_stamp():
    """Which model, at what temperature, with which flags -- recorded on every row.

    Without this the results are counts with no provenance: a reader cannot tell whether they
    describe the model production actually runs. The provider is env-driven and differs between
    environments, so "we measured it" is not a claim the data can support on its own.

    EMIT_EVIDENCE matters because it changes the PROMPT, not just the post-processing: prod ships
    it off and test ships it on, so the two environments do not send the same request.
    """
    return {
        "provider": llm_utils.LLM_PROVIDER,
        "model": (llm_utils.QWEN_MODEL if llm_utils.LLM_PROVIDER == "qwen"
                  else llm_utils.CLAUDE_MODEL),
        "temperature": os.environ.get("LLM_TEMPERATURE"),
        "emit_evidence": bool(getattr(ex, "EMIT_EVIDENCE", False)),
    }


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
    # The whole parsed object, kept verbatim. Every LLM call here costs real money and ~3 minutes
    # in thinking mode; a question thought of after the run should never require re-buying it.
    out["raw"] = parsed
    return out


def analyse(paths):
    """Pair the arms per session and show what MOVED versus what VANISHED.

    This is the acceptance criterion made mechanical up to the last step. It does not decide
    whether a drop is acceptable -- it presents each dropped observation next to the treatment
    arm's action items so a person can see at a glance which of the two happened.
    """
    import glob as _glob
    rows = []
    for pat in paths:
        for path in _glob.glob(pat):
            with open(path, encoding="utf-8") as fh:
                for line in fh:
                    line = line.strip()
                    if line:
                        rows.append(json.loads(line))

    by = {}
    for r in rows:
        by.setdefault((r.get("date"), r.get("session")), []).append(r)

    for (date, session), rs in sorted(by.items()):
        print(f"\n=== {date} {session} ===")
        for thinking in (False, True):
            arms = {}
            for v in ("baseline", "no_invent"):
                arms[v] = [r for r in rs if r.get("variant") == v
                           and r.get("thinking") == thinking and "observations" in r]
            if not arms["baseline"] or not arms["no_invent"]:
                continue
            def counts(v):
                return [r["findings"] for r in arms[v]]
            print(f"  thinking={thinking}  findings  baseline={counts('baseline')}  "
                  f"no_invent={counts('no_invent')}")

            # An observation is "kept" if ANY treatment run produced something similar. Comparing
            # run-to-run would report normal sampling noise as loss.
            treat_obs = [o for r in arms["no_invent"] for o in r["observations"]]
            treat_act = [a for r in arms["no_invent"] for a in r.get("action_texts", [])]
            seen = set()
            for r in arms["baseline"]:
                for text in r["observations"]:
                    k = _key(text)
                    if k in seen:
                        continue
                    seen.add(k)
                    if _matches_any(text, treat_obs):
                        continue
                    where = ("MOVED to action_items" if _matches_any(text, treat_act)
                             else "*** VANISHED ***")
                    print(f"    {where}: {text[:100]}")


def _key(text):
    """Content words, via the pipeline's own CJK-safe tokenizer.

    `re.findall(r"\\w+", ...)` would make a whole Chinese clause ONE token, because Chinese is not
    whitespace-delimited -- so overlap would degrade to exact string match and every CJK finding
    would read as vanished-and-new. This repo has shipped that bug twice (chunk_stitch._norm and
    lambda_ask_agent's lexical split), so the tokenizer is imported rather than rewritten.

    The `len > 3` filter that usually accompanies this is deliberately NOT applied: it drops every
    single-character CJK token, i.e. all of them.
    """
    return frozenset(agent_turn_filter._tokens(text or ""))


def _matches_any(text, candidates, threshold=0.4):
    """Two runs phrase the same item differently -- 'Six brackets ... are missing' vs 'Shortage of
    six brackets'. Exact comparison would report every item as both vanished and new, so identity
    is content-word overlap against the SHORTER side (a terse action item legitimately carries
    fewer words than the finding it came from; normalising by the union would sink it below any
    threshold)."""
    a = _key(text)
    if not a:
        return False
    for c in candidates:
        b = _key(c)
        if b and len(a & b) / min(len(a), len(b)) >= threshold:
            return True
    return False


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--analyse", nargs="+", metavar="JSONL",
                    help="read result files and print moved-vs-vanished per session; no LLM calls")
    ap.add_argument("--user", help="user_folder, as in transcripts/{user}/")
    ap.add_argument("--date")
    ap.add_argument("--session", help="session_base; omit with --list")
    ap.add_argument("--turns-file", help="JSON list of turns (abs_start_str/speaker/text) to use "
                                         "instead of reading S3. For the both-languages check.")
    ap.add_argument("--group-turns", nargs="+", metavar="JSON",
                    help="two or more turns files, treated as separate devices' recordings of one "
                         "meeting; exercises build_group_prompt, which shares the same "
                         "instructions block and would otherwise ship unmeasured")
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

    if args.analyse:
        analyse(args.analyse)
        return

    if not (args.user and args.date):
        ap.error("--user and --date are required (or use --analyse)")

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

    if args.group_turns:
        sets = []
        for path in args.group_turns:
            with open(path, encoding="utf-8") as fh:
                sets.append(json.load(fh))
        base_prompt = build_group_prompt_from_turns(args.date, sets)
        n_segments = sum(len(s) for s in sets)
        turns, tstats = [t for s in sets for t in s], {"truncated": False}
        print(f"# SYNTHETIC GROUP prompt from {len(sets)} fixture recordings", file=sys.stderr)
    elif args.turns_file:
        with open(args.turns_file, encoding="utf-8") as fh:
            fixture = json.load(fh)
        base_prompt, n_segments, turns, tstats = build_prompt_from_turns(
            args.user, args.date, args.session, fixture,
            max(1, len(fixture) // 10))
        print("# SYNTHETIC turns from " + args.turns_file, file=sys.stderr)
    else:
        base_prompt, n_segments, turns, tstats = build_prompt(
            args.bucket, args.user, args.date, args.session)
    print(f"# {args.user}/{args.date}/{args.session}", file=sys.stderr)
    print(f"# turns={len(turns)} segments={n_segments} prompt_chars={len(base_prompt)} "
          f"truncated={tstats.get('truncated')} max_tokens={ex.max_tokens_for(n_segments)}",
          file=sys.stderr)

    # Resume. A full matrix is ~72 calls over an hour and gets killed -- by a timeout, a laptop
    # lid, a stopped background task. Re-buying completed calls is the expensive kind of waste,
    # so an existing --out file is treated as work already done.
    done = set()
    if args.out and os.path.exists(args.out):
        with open(args.out, encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    d = json.loads(line)
                except ValueError:
                    continue          # a torn last line from a kill mid-write
                if "error" in d:
                    continue          # a failed call is not a completed one; retry it
                done.add((d.get("session"), d.get("variant"),
                          bool(d.get("thinking")), d.get("run")))
        if done:
            print(f"# resuming: {len(done)} results already present", file=sys.stderr)

    results = []
    for variant in variants:
        prompt = VARIANTS[variant](base_prompt)
        for thinking in modes:
            for i in range(args.runs):
                if (args.session, variant, bool(thinking), i + 1) in done:
                    continue
                r = run_once(prompt, n_segments, thinking)
                r.update(variant=variant, thinking=thinking, run=i + 1,
                         prompt_chars=len(prompt), session=args.session,
                         user=args.user, date=args.date, **_env_stamp())
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
