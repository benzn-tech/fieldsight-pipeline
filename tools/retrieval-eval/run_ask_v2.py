#!/usr/bin/env python3
"""Retrieval-eval ruler, Ask v2 mode: what the model-driven Ask loop puts in front of
the judge, and what it cites.

Spec docs/2026-10-22-ask-v2-mode-spec.md as amended by -review.md; plan -plan.md.
The real `ask_orchestrator.answer` runs in this process with the real `ask_tools` and the
real rag-search handler/dispatcher/repositories. Only transport is substituted: the
rag-search Lambda is a function call, and its Aurora connection is the read-only Data API
adapter (askv2/adapter.py). Nothing here is imported by a Lambda.

Per question it scores the funnel sql -> rows -> item -> seen -> final_view -> cited
(askv2/funnel.py) and records the tool calls the judge chose.

READ-ONLY, MECHANICALLY: every rag-search invoke is one Data API transaction opened
READ ONLY (SHOW must say `on`) and rolled back; templates pass an allow/deny list; step 0
proves a CREATE TEMP TABLE is refused with SQLSTATE 25006 before any question runs. No
web, no S3, no voice: urllib3 and boto3 guards refuse every other host and service.

USAGE
-----
    export AWS_PROFILE=fieldsight-deployer AWS_DEFAULT_REGION=ap-southeast-2
    # step 0 only (read-only AWS calls; no model or vendor call):
    python tools/retrieval-eval/run_ask_v2.py --gold tools/retrieval-eval/gold/v1.1.jsonl \\
        --label preflight --family report --database fieldsight_test --preflight-only
    # pilot: 3 questions, prints counts and the full-run extrapolation
    python tools/retrieval-eval/run_ask_v2.py ... --label pilot --family report --pilot
    # full run, gated on the pilot's printed counts
    python tools/retrieval-eval/run_ask_v2.py ... --label full --family report --yes \\
        --max-judge-calls N --max-tokens M
    python tools/retrieval-eval/run_ask_v2.py --diff A.json B.json
"""
import argparse
import hashlib
import json
import os
import secrets
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

from askv2 import adapter, funnel, report

HERE = Path(__file__).resolve().parent
REPO = HERE.parents[1]
TZ = "Pacific/Auckland"
CLUSTER = os.environ.get("EVAL_DB_CLUSTER_ARN",
                         "arn:aws:rds:ap-southeast-2:509194952652:cluster:"
                         "fieldsight-db-test-dbcluster-hywiixu8ihi9")
SECRET = os.environ.get("EVAL_DB_SECRET_ARN",
                        "arn:aws:secretsmanager:ap-southeast-2:509194952652:secret:"
                        "rds!cluster-1757a281-ee31-460d-b56e-950817921010-Ansbey")
DEFAULT_SITE = "4326dbc7-14a2-4251-8381-9d0067595a85"
DEFAULT_AUTHOR = "f531eb99-d79c-4e6c-bac1-ff944b802349"
PREFIX = {"fieldsight": "fieldsight-prod", "fieldsight_test": "fieldsight-test"}
# Review S6: two answerable (v1-03 also reaches the documents search when it is on) and
# one control. This gold has no "when did" question; latest_activity needs a gold v2.
PILOT_IDS = ("v1-01", "v1-03", "v1-20")
# 45 q x 2 families x 2 repeats + the same-config noise pair (45 x 2) -- review S8.
FULL_RUN_TURNS = 270


def _calls_by_round(rec):
    """The tool calls of each batch, round 1 (no judge call before it) first."""
    out, rnd = [], 1
    for b in rec.batches:
        if b["judge_calls_before"] > 0:
            rnd = b["judge_calls_before"] + 1
        out.append({"round": rnd, "calls": [{"name": c.get("name"), "args": c.get("args")}
                                            for c in b["calls"]]})
    return out


def _judge_summary(rec):
    out = []
    for j in rec.judge:
        turn = j.get("turn")
        out.append({"tool_choice": j.get("tool_choice") if isinstance(j.get("tool_choice"), str)
                    else "final_answer",
                    "tool_calls": [{"name": tc.get("name"), "args": tc.get("arguments")}
                                   for tc in (turn.tool_calls if turn else []) or []
                                   if tc.get("name") != "final_answer"],
                    "final": bool(turn and any(tc.get("name") == "final_answer"
                                               for tc in turn.tool_calls or [])),
                    "usage": j.get("usage"), "model": j.get("model"), "error": j.get("error"),
                    "ms": j.get("ms")})
    return out


def _rows_record(item, turn_id, rec):
    """What the tools were given in one turn, for the offline item-window replay
    (replay_window.py): every batch's calls and envelope shapes (no item text), the raw
    rag-search invokes (rows WITH transcript text) and the rows each envelope was built
    from. Scratch only, never results/."""
    batches = []
    for b in rec.batches:
        envs = [{"tool": e.get("tool"), "id": e.get("id"), "meta": e.get("meta"),
                 "items": [{"ref": it.get("ref"), "kind": it.get("kind"),
                            "chunk_id": it.get("chunk_id"), "topic_id": it.get("topic_id"),
                            "date": it.get("date"), "text_len": len(it.get("text") or "")}
                           for it in e.get("items") or []]}
                for e in b.get("envelopes") or []]
        batches.append({"calls": [{"name": c.get("name"), "args": c.get("args")}
                                  for c in b["calls"] if isinstance(c, dict)],
                        "judge_calls_before": b["judge_calls_before"], "envelopes": envs})
    return {"id": item["id"], "expect": item.get("expect"), "date": item.get("date"),
            "turn": turn_id, "batches": batches, "invokes": list(rec.invokes),
            "post_search": list(rec.post_search)}


def run_turn(item, *, ev, chat, proxy, capture, now, caller_sub, scope_site=None,
             keep_renders=None, keep_rows=None):
    """One question through `ask_orchestrator.answer`, as `_work_screen` builds the body
    (spec §4): question, tz, pinned now, caller_sub; `site_id` under --scope-site."""
    import ask_orchestrator
    rec = ev.next()
    turn_id = secrets.token_hex(8)
    body = {"question": item["question"], "tz": TZ, "now": now, "caller_sub": caller_sub}
    if scope_site:
        body["site_id"] = scope_site
    try:
        result = ask_orchestrator.answer(body, channel="screen", on_state=rec.states.append,
                                         tools=proxy, chat=chat, turn_id=turn_id)
    finally:
        adapter.rollback_open()          # review B3.4: nothing stays open past a turn
    route = capture.routes.get(turn_id) or {}
    score = funnel.score_turn(item, rec, route, result)
    final = next((j["turn"] for j in reversed(rec.judge) if j.get("turn") is not None), None)
    fin = ask_orchestrator._final_of(final) if final is not None else None
    if keep_renders is not None:
        keep_renders.append({"id": item["id"], "turn": turn_id,
                             "messages": [j["messages"] for j in rec.judge]})
    if keep_rows is not None:
        keep_rows.append(_rows_record(item, turn_id, rec))
    stmt_ms = sorted(st["ms"] for st in rec.log.statements)
    return {
        "id": item["id"], "expect": item.get("expect"), "turn": turn_id, "funnel": score,
        "route": route, "states": list(rec.states),
        "tool_calls": _calls_by_round(rec), "judge": _judge_summary(rec),
        "answer": result.get("answer"),
        "final_raw": fin.get("answer") if isinstance(fin, dict) else None,
        "citations": len(result.get("citations") or []), "grounded": result.get("grounded"),
        "error": result.get("error"),
        "sql": {"statements": len(stmt_ms), "ms_total": round(sum(stmt_ms), 1),
                "ms_max": stmt_ms[-1] if stmt_ms else None,
                "transactions": rec.log.transactions, "errors": list(rec.log.errors),
                "warnings": sorted(set(rec.log.warnings)),
                "largest_response": rec.log.largest_response,
                "templates": sorted({(st["sha256"], st["head"]) for st in rec.log.statements})},
    }


# ============================================================== driver

def parse_args(argv=None):
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--diff", nargs=2, metavar=("A", "B"),
                    help="compare two result files (refused across judge model chains)")
    ap.add_argument("--gold")
    ap.add_argument("--label")
    ap.add_argument("--family", choices=["report", "session"],
                    help="SEARCH_INDEX_READ for this process (required)")
    ap.add_argument("--repeat", type=int, default=2)
    ap.add_argument("--scope-site", dest="scope_site", action="store_true", default=True,
                    help="send site_id=<gold site> (default; the closest thing to run.py's pin)")
    ap.add_argument("--no-scope-site", dest="scope_site", action="store_false")
    ap.add_argument("--now", default=None, help="pinned request time (default: run start)")
    ap.add_argument("--database", default="fieldsight", choices=sorted(PREFIX),
                    help="`fieldsight` = prod, `fieldsight_test` = TEST (same cluster)")
    ap.add_argument("--prefix", default=None, help="deployed stack prefix (from --database)")
    ap.add_argument("--src-ref", default=os.environ.get("EVAL_SRC_REF", "origin/main"))
    ap.add_argument("--site", default=DEFAULT_SITE)
    ap.add_argument("--author", default=DEFAULT_AUTHOR)
    ap.add_argument("--pilot", action="store_true")
    ap.add_argument("--preflight-only", action="store_true",
                    help="run step 0 (read-only probes) and stop")
    ap.add_argument("--yes", action="store_true", help="required for a full run")
    ap.add_argument("--max-judge-calls", type=int, default=None)
    ap.add_argument("--max-tokens", type=int, default=None, help="prompt + completion")
    ap.add_argument("--keep-renders", default=None,
                    help="directory (scratchpad, never results/) for full judge messages")
    ap.add_argument("--keep-rows", default=None,
                    help="directory (scratchpad, never results/) for the rows the tools "
                         "were given (transcript text), for replay_window.py")
    ap.add_argument("--out", default=str(HERE / "results"))
    args = ap.parse_args(argv)
    if args.diff:
        return args
    if not (args.gold and args.label and args.family):
        ap.error("--gold, --label and --family are required")
    full = not (args.pilot or args.preflight_only)
    if full and not args.yes:
        raise SystemExit("a full run needs --yes (read the pilot's printed counts first)")
    if full and (args.max_judge_calls is None or args.max_tokens is None):
        raise SystemExit("a full run needs --max-judge-calls and --max-tokens (from the pilot)")
    if args.keep_renders and Path(args.keep_renders).resolve().is_relative_to(
            (HERE / "results").resolve()):
        raise SystemExit("--keep-renders must not point into results/ (spec §8)")
    if args.keep_rows and Path(args.keep_rows).resolve().is_relative_to(
            (HERE / "results").resolve()):
        raise SystemExit("--keep-rows must not point into results/ (rows hold transcript "
                         "text; spec §8)")
    args.prefix = args.prefix or PREFIX[args.database]
    return args


def select_items(gold, pilot):
    items = [json.loads(x) for x in Path(gold).read_text(encoding="utf-8").splitlines()
             if x.strip()]
    if pilot:
        by_id = {i["id"]: i for i in items}
        return [by_id[q] for q in PILOT_IDS]
    return items


def result_name(stamp, label, family, now):
    """Review N8: `now` drives latest_activity's default bound and the prompt's Today."""
    return "%s-askv2-%s-%s-now%s.json" % (stamp, label, family, now[:10].replace("-", ""))


def _git_commit():
    try:
        return subprocess.run(["git", "-C", str(REPO), "rev-parse", "HEAD"],
                              capture_output=True, text=True, check=True).stdout.strip()
    except Exception:                                       # noqa: BLE001
        return None


def model_chain(values):
    fallbacks = [m.strip() for m in (values.get("ASK_JUDGE_FALLBACKS") or "").split(",")
                 if m.strip()]
    return [values.get("ASK_JUDGE_MODEL")] + fallbacks + [
        "provider=%s" % values.get("ASK_JUDGE_PROVIDER"),
        "reasoning=%s" % values.get("ASK_JUDGE_REASONING")]


def _line(t):
    f = t["funnel"]
    st = f["stages"]
    marks = "".join(("+" if st[s] else "-") for s in funnel.STAGES) if st else "control"
    seq = " ".join("/".join(c["name"] for c in b["calls"]) for b in t["tool_calls"])
    return "%-6s %-16s %-7s lost=%-18s judge=%d tools=[%s]" % (
        t["id"], f["status"], marks, f["lost_at"], len(t["judge"]), seq)


def _step0(args, session):
    """Pilot step 0 (plan T11): every probe read-only; any failure stops the run before a
    question. Returns what the questions need, with the guards armed."""
    from askv2 import env, harness, preflight
    print("== step 0: read-only probes (database %s, functions %s-*) =="
          % (args.database, args.prefix))
    iam = preflight.iam_check(session.client("iam"), session.client("sts"))
    print("iam: %s" % json.dumps(iam))
    copied, injected, envfp = env.fetch_configs(session.client("lambda"), args.prefix)
    src, src_sha = harness.prepare_imports(args.src_ref, args.family, {**copied, **injected})
    del injected
    harness.assert_seams(args.family, os.environ.get("ENABLE_RERANK", "false").lower() == "true")
    import corroboration_client
    import dashscope_utils
    values = envfp["values"]
    chain = model_chain(values)
    print("judge chain: %s" % chain)
    print("LLM_PROVIDER=%s (fallback synthesis: %s) qwen host=%s ENABLE_RERANK=%s "
          "PROJECT_DOCUMENTS=%s" % (values.get("LLM_PROVIDER"), envfp["fallback_synthesis"],
                                    envfp["qwen_base_host"], values.get("ENABLE_RERANK"),
                                    values.get("PROJECT_DOCUMENTS")))
    print("SEARCH_INDEX_READ deployed=%s measured=%s" % (values.get("SEARCH_INDEX_READ"),
                                                       args.family))
    print("deployed: %s   src %s = %s" % (json.dumps(envfp["deployed"]), args.src_ref, src_sha))
    print("secrets present: %s" % envfp["secrets"])

    frozen = harness.freeze_session(session)
    rds = frozen.client("rds-data")
    guards = [harness.arm_boto_guard({"rds-data"}),
              harness.arm_url_guard(env.allowed_hosts(
                  copied, judge_url=corroboration_client.API_URL,
                  dashscope_urls=[dashscope_utils.DASHSCOPE_BASE_URL,
                                  dashscope_utils.DASHSCOPE_RERANK_URL]))]
    try:
        probe = preflight.ro_probe(rds, CLUSTER, SECRET, args.database)
        print("read-only probe: %s" % probe)
        with _conn(rds, args.database) as c:
            missing = preflight.schema_missing(c)
        if missing:
            raise SystemExit("schema preflight: %s lacks %s -- no rewriting inside imported "
                             "repositories; stop" % (args.database, ", ".join(missing)))
        print("schema preflight: ok")
        with _conn(rds, args.database) as c:
            caller_sub, ident = preflight.identity(c, args.author, args.site)
        print("caller: %s" % json.dumps(ident))
        if not ident["ok"]:
            raise SystemExit("identity gate failed (review B1 / spec Q1): ask the owner whose "
                             "login to run as")
        if ident["user_scope"] in ("ALL", "SITE"):
            print("NOTE: the caller's scope is %s, not run.py's author pin: v2 numbers are "
                  "%s-scope%s" % (ident["user_scope"], ident["user_scope"].lower(),
                                  " narrowed to the gold site" if args.scope_site else ""))
        first = select_items(args.gold, pilot=False)[0]
        with _conn(rds, args.database) as c:
            parity = preflight.parity_smoke(c, first["question"], args.site, args.author,
                                            args.family)
        print("binding parity: %s" % parity)
    except BaseException:
        for g in guards:
            g.restore()
        raise
    fp = {"src_ref": args.src_ref, "src_sha": src_sha, "env": envfp, "model_chain": chain,
          "caller": ident, "preflight": {"iam": iam, "ro_probe": probe, "schema_missing": [],
                                         "parity": parity}}
    return rds, caller_sub, fp, guards


def _conn(rds, database, log=None):
    from askv2.adapter import AdapterLog, DataApiConnection
    return DataApiConnection(rds, cluster=CLUSTER, secret=SECRET, database=database,
                             log=log or AdapterLog())


def main(argv=None):
    args = parse_args(argv)
    if args.diff:
        a, b = (json.loads(Path(p).read_text(encoding="utf-8")) for p in args.diff)
        print(json.dumps(report.diff(a, b), indent=1))
        return
    import boto3
    from askv2 import harness

    started = datetime.now(timezone.utc)
    now = args.now or started.astimezone().isoformat(timespec="seconds")
    session = boto3.session.Session(region_name="ap-southeast-2")
    rds, caller_sub, fp, guards = _step0(args, session)
    url_guard = guards[1]
    capture = harness.install_log_capture()
    try:
        if args.preflight_only:
            print("http per host: %s" % url_guard.counts)
            print("step 0 passed; --preflight-only: stopping before any question")
            return
        import llm_tools_client
        record = {"label": args.label, "mode": "ask-v2",
                  "ran_at": started.isoformat(timespec="seconds"), "aborted": None, "runs": [],
                  "fingerprint": dict(
                      fp, git_commit=_git_commit(), database=args.database, family=args.family,
                      now=now, scope_site=args.site if args.scope_site else None,
                      gold_file=os.path.relpath(args.gold, REPO),
                      gold_sha256=hashlib.sha256(Path(args.gold).read_bytes()).hexdigest(),
                      deadline_screen_s=120.0, pilot=args.pilot)}
        items = select_items(args.gold, pilot=args.pilot)
        ev = harness.EvalTurns()
        fake = harness.FakeLambda(ev)
        patches = harness.install_patches(fake, lambda: _conn(rds, args.database, ev.rec.log))
        proxy = harness.ToolsProxy(ev, fake)
        chat = harness.chat_wrapper(ev, llm_tools_client.chat)
        caps = report.Caps(args.max_judge_calls, args.max_tokens)
        renders = [] if args.keep_renders else None
        kept_rows = [] if args.keep_rows else None
        done = []
        try:
            for i in range(args.repeat):
                turns = []
                record["runs"].append({"repeat": i + 1, "turns": turns})
                for item in items:
                    t = run_turn(item, ev=ev, chat=chat, proxy=proxy, capture=capture, now=now,
                                 caller_sub=caller_sub,
                                 scope_site=args.site if args.scope_site else None,
                                 keep_renders=renders, keep_rows=kept_rows)
                    turns.append(t)
                    done.append(t)
                    print("run %d %s" % (i + 1, _line(t)))
                    invalid = sum(x["funnel"]["status"] == "invalid" for x in done)
                    if invalid > 2:
                        raise report.CapExceeded("%d invalid turns (spec §2.2: > 2 aborts)"
                                                 % invalid)
                    caps.check(done)
        except report.CapExceeded as exc:
            record["aborted"] = str(exc)
            print("ABORTED: %s" % exc)
        finally:
            patches.restore()
        for run in record["runs"]:
            run["summary"] = report.summarise(run["turns"])
        fl = report.flips([r["turns"] for r in record["runs"]])
        record.update(flips=fl, min_detectable_delta=len(fl))
        record["fingerprint"].update(
            template_registry=sorted({tuple(x) for t in done for x in t["sql"]["templates"]}),
            http_counts=dict(url_guard.counts), fallback_llm_usage=capture.usage)
        if args.pilot:
            record["extrapolation"] = report.extrapolate(done, FULL_RUN_TURNS)
        if renders is not None:
            Path(args.keep_renders).mkdir(parents=True, exist_ok=True)
            dest_r = Path(args.keep_renders) / ("renders-%s.json" % args.label)
            dest_r.write_text(json.dumps(renders, ensure_ascii=False), encoding="utf-8")
            print("renders (scratch only): %s" % dest_r)
        if kept_rows is not None:
            Path(args.keep_rows).mkdir(parents=True, exist_ok=True)
            dest_k = Path(args.keep_rows) / ("rows-%s-%s.json" % (args.label, args.family))
            dest_k.write_text(json.dumps(kept_rows, ensure_ascii=False, default=str),
                              encoding="utf-8")
            print("rows (scratch only): %s" % dest_k)
        Path(args.out).mkdir(parents=True, exist_ok=True)
        dest = Path(args.out) / result_name(started.strftime("%Y%m%dT%H%M%SZ"), args.label,
                                           args.family, now)
        dest.write_text(json.dumps(record, ensure_ascii=False, indent=1, default=str),
                        encoding="utf-8")
        print("\nsummary per run (counts; minimum detectable delta = %d flipped question(s)):"
              % len(fl))
        for run in record["runs"]:
            print("  run %d  %s" % (run["repeat"], json.dumps(run["summary"])))
        print("http per host: %s" % url_guard.counts)
        print("template registry: %d templates" % len(record["fingerprint"]["template_registry"]))
        if args.pilot:
            print("full-run extrapolation (%d turns): %s"
                  % (FULL_RUN_TURNS, json.dumps(record["extrapolation"])))
        print("wrote %s" % dest)
    finally:
        adapter.rollback_open()
        capture.restore()
        for g in guards:
            g.restore()


if __name__ == "__main__":
    sys.path.insert(0, str(HERE))
    main()
