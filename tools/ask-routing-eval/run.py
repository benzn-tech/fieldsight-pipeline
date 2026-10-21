"""Ask routing eval runner (plan 2026-10-16 phase 4, Task 3; spec §9).

    run.py --stack fieldsight-test --sub <cognito sub> --channel screen|voice|both
           --repeat 3 [--only id,id] [--site-id X] [--dry-run] [--confirm-calls N]

TEST only, and the only file in this directory that touches AWS. The stack name
is checked for "prod" before anything else, and `--dry-run` needs no AWS at all.
The harness never changes a flag or repo variable: it reads the deployed
worker's `ASK_AGENTIC` and refuses a channel that flag does not list.

Screen turns go through ApiFunction `/api/ask/start` + `/api/ask/result` with a
synthetic authorizer event (the screen_turn.py smoke shape). Voice turns put the
pending state with the service's own `ask_voice_turn.put_state` and Event-invoke
the worker with the payload `ask_voice_turn.start` sends. Each turn is joined to
its `ASK_ROUTE` line on `turn` and scored by `score.py`.

Answers are written only to `results/` (git-ignored); nothing here prints one.
"""
from __future__ import annotations

import argparse
import json
import math
import os
import secrets
import sys
import time
from datetime import datetime, timezone

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(os.path.dirname(HERE))
SRC = os.path.join(REPO, "src")
SET_PATH = os.path.join(HERE, "set.v1.jsonl")
RESULTS_DIR = os.path.join(HERE, "results")

# The service's own helpers, imported from src by path.
if SRC not in sys.path:
    sys.path.insert(0, SRC)
import ask_voice_turn  # noqa: E402
import voice_web  # noqa: E402


def _load_score():
    """score.py by path, under a name no other `score` module can shadow."""
    import importlib.util
    spec = importlib.util.spec_from_file_location("ask_routing_eval_score_for_run",
                                                  os.path.join(HERE, "score.py"))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


score = _load_score()

REGION = "ap-southeast-2"
TZ = "Pacific/Auckland"
TURN_TIMEOUT_S = 70.0
VOICE_POLL_S = 1.5
LOG_WAIT_S = 90.0
LOG_RETRY_S = 5.0
TERMINAL = ("done", "error", "cancelled")
CHANNELS = ("screen", "voice")
VISIBLE_SITE = "@visible_site"

PRECHECK_CALLS = 1
JUDGE_CALLS = 1
WEB_CALLS = 2.5
MULTI_ROUND_CALLS = 1
WEB_GROUPS = ("general", "mixed", "explicit_web", "web_cap")
MULTI_ROUND_GROUPS = ("mixed", "web_cap")

# Worker env recorded with the results. A whitelist: the worker env also holds keys.
FLAG_NAMES = ("ASK_AGENTIC", "ENABLE_WEB_ANSWER", "VOICE_WEB_ENABLED", "ASK_VOICE_CONVO",
              "ASK_JUDGE_MODEL", "ASK_JUDGE_FALLBACKS", "WEB_ANSWER_HARD_STOP")


# ------------------------------------------------------------------ the set

def load_set(path=SET_PATH):
    with open(path, encoding="utf-8") as f:
        return [json.loads(ln) for ln in f if ln.strip()]


def selection(items, channel, only):
    """[(item, channel)] in set order, screen first. `only`: ids or None."""
    chans = CHANNELS if channel == "both" else (channel,)
    out = []
    for ch in chans:
        for it in items:
            if only and it["id"] not in only:
                continue
            if ch in it.get("channels", CHANNELS):
                out.append((it, ch))
    return out


def turn_cost(group, channel):
    """Model calls one turn may cost. The screen sends `offer_web` (spec
    2026-10-16 §6), so a screen `general` turn is offered the web instead of
    running it: no web calls."""
    cost = PRECHECK_CALLS + JUDGE_CALLS
    if group in WEB_GROUPS and not (channel == "screen" and group == "general"):
        cost += WEB_CALLS
    if group in MULTI_ROUND_GROUPS:
        cost += MULTI_ROUND_CALLS
    return cost


def estimate(sel, repeat):
    """Model calls per channel (rounded up) and in total."""
    per = {}
    for it, ch in sel:
        per[ch] = per.get(ch, 0) + turn_cost(it["group"], ch) * repeat
    out = {ch: int(math.ceil(v)) for ch, v in per.items()}
    out["total"] = sum(out.values())
    return out


def _flag_channels(raw):
    return {p.strip() for p in (raw or "").split(",") if p.strip()}


# ---------------------------------------------------------------- the event

def api_event(sub, method, path, body=None, query=None):
    ev = {"httpMethod": method, "path": path, "queryStringParameters": query,
          "requestContext": {"authorizer": {"claims": {"sub": sub}}},
          "headers": {"User-Agent": "ask-routing-eval"}}
    if body is not None:
        ev["body"] = json.dumps(body)
    return ev


def _call(lam, fn, event):
    resp = lam.invoke(FunctionName=fn, InvocationType="RequestResponse",
                      Payload=json.dumps(event).encode("utf-8"))
    out = json.loads(resp["Payload"].read())
    raw = out.get("body") if isinstance(out, dict) else None
    try:
        body = json.loads(raw) if isinstance(raw, str) and raw else raw
    except ValueError:
        body = None
    return (out or {}).get("statusCode"), body


def visible_site(lam, stack, sub):
    """The first site `/api/org/sites` returns for this sub, or None."""
    code, body = _call(lam, stack + "-org-api", dict(
        api_event(sub, "GET", "/api/org/sites"),
        rawPath="/api/org/sites", resource="/api/org/sites"))
    sites = (body or {}).get("sites") if code == 200 and isinstance(body, dict) else None
    return (sites[0] or {}).get("id") if sites else None


# ------------------------------------------------------------------ drivers

def screen_turn(lam, fn, sub, item, site_id, *, clock=time.monotonic, sleep=time.sleep):
    """(turn_id, terminal, wall_s). terminal["status"] is done/error/cancelled,
    or the harness's own start_error / http_error / timeout.

    The body opts in to the web offer (`offer_web`, spec 2026-10-16 §6), as the
    screen client does; an `explicit_web` item also sends `web_intent`, as a
    click on the offer would."""
    t0 = clock()
    body = {"question": item["question"], "tz": TZ, "offer_web": True}
    if item.get("group") == "explicit_web":
        body["web_intent"] = True
    if (item.get("body") or {}).get("site_id") == VISIBLE_SITE:
        body["site_id"] = site_id
    code, st = _call(lam, fn, api_event(sub, "POST", "/api/ask/start", body=body))
    turn = (st or {}).get("turn_id") if isinstance(st, dict) else None
    if code != 200 or not turn:
        return None, {"status": "start_error", "code": code}, clock() - t0
    since = "pending"
    while True:
        code, r = _call(lam, fn, api_event(sub, "GET", "/api/ask/result",
                                           query={"turn": turn, "since": since}))
        status = (r or {}).get("status") if isinstance(r, dict) else None
        if code != 200:
            return turn, {"status": "http_error", "code": code}, clock() - t0
        if status in TERMINAL:
            return turn, r, clock() - t0
        if clock() - t0 >= TURN_TIMEOUT_S:
            return turn, {"status": "timeout", "last": status}, clock() - t0
        if status == since:
            sleep(0.5)
        since = status or since


def voice_turn(lam, s3, worker_fn, bucket, sub, item, *, clock=time.monotonic,
               sleep=time.sleep, put_state=None):
    """(turn_id, terminal without audio, wall_s), driven the way `start` drives
    the worker: pending state first, then an Event invoke."""
    put_state = put_state or ask_voice_turn.put_state
    t0 = clock()
    turn = secrets.token_hex(16)
    transcript = item["question"]
    web_intent = voice_web.detect_intent(transcript)[0]
    put_state(s3, bucket, sub, turn, {"status": "pending", "created_at": time.time()})
    payload = {"turn_id": turn, "caller_sub": sub, "transcript": transcript, "tz": TZ,
               "web_intent": web_intent, "offer_web": True}
    lam.invoke(FunctionName=worker_fn, InvocationType="Event",
               Payload=json.dumps(payload).encode("utf-8"))
    while True:
        st = ask_voice_turn.get_state(s3, bucket, sub, turn) or {}
        if st.get("status") in TERMINAL:
            st = {k: v for k, v in st.items() if k != "audioBase64"}
            return turn, st, clock() - t0
        if clock() - t0 >= TURN_TIMEOUT_S:
            return turn, {"status": "timeout", "last": st.get("status")}, clock() - t0
        sleep(VOICE_POLL_S)


# ----------------------------------------------------------------- log join

def parse_route(message):
    """The JSON of an `ASK_ROUTE {...}` log message, or None."""
    i = (message or "").find("ASK_ROUTE ")
    if i < 0:
        return None
    try:
        out = json.loads(message[i + len("ASK_ROUTE "):].strip())
    except ValueError:
        return None
    return out if isinstance(out, dict) else None


def _fetch(logs, group, start_ms, end_ms):
    found, token = {}, None
    while True:
        kw = {"logGroupName": group, "startTime": int(start_ms), "endTime": int(end_ms),
              "filterPattern": '"ASK_ROUTE"'}
        if token:
            kw["nextToken"] = token
        page = logs.filter_log_events(**kw)
        for ev in page.get("events") or []:
            r = parse_route(ev.get("message"))
            if r and isinstance(r.get("turn"), str):
                found[r["turn"]] = r
        token = page.get("nextToken")
        if not token:
            return found


def join_routes(logs, group, turn_ids, start_ms, end_ms, *, clock=time.monotonic,
                sleep=time.sleep, wait_s=LOG_WAIT_S):
    """{turn_id: route} for the turns whose ASK_ROUTE line was found. CloudWatch
    ingests late, so the fetch is retried until every turn joined or `wait_s`.
    A `_rag_answer` line (no `turn`) never joins."""
    want = set(t for t in turn_ids if t)
    t0 = clock()
    while True:
        found = _fetch(logs, group, start_ms - 5000, end_ms + 120000)
        got = {t: found[t] for t in want if t in found}
        if len(got) == len(want) or clock() - t0 >= wait_s:
            return got
        sleep(LOG_RETRY_S)


# --------------------------------------------------------------------- main

def _clients():
    import boto3
    from botocore.config import Config
    cfg = Config(read_timeout=90, retries={"max_attempts": 1})
    return {"lambda": boto3.client("lambda", region_name=REGION, config=cfg),
            "logs": boto3.client("logs", region_name=REGION),
            "s3": boto3.client("s3", region_name=REGION)}


def _args(argv):
    p = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    p.add_argument("--stack", required=True)
    p.add_argument("--sub", required=True)
    p.add_argument("--channel", choices=("screen", "voice", "both"), default="both")
    p.add_argument("--repeat", type=int, default=3)
    p.add_argument("--only", default=None)
    p.add_argument("--site-id", default=None)
    p.add_argument("--dry-run", action="store_true")
    p.add_argument("--confirm-calls", type=int, default=None)
    return p.parse_args(argv)


def _nz_date():
    try:
        from zoneinfo import ZoneInfo
        return datetime.now(ZoneInfo(TZ)).date().isoformat()
    except Exception:                                      # noqa: BLE001
        return None


def main(argv=None, *, make_clients=None, clock=time.monotonic, sleep=time.sleep,
         results_dir=None):
    a = _args(argv)
    # Review focus 1: prod is refused before anything else, AWS clients included.
    if "prod" in a.stack.lower():
        print("REFUSED: stack %r contains 'prod'; this eval is TEST only." % a.stack)
        return 2
    if a.repeat < 1:
        print("REFUSED: --repeat must be >= 1")
        return 2
    only = [s.strip() for s in a.only.split(",") if s.strip()] if a.only else None
    sel = selection(load_set(), a.channel, only)
    if not sel:
        print("REFUSED: nothing selected")
        return 2
    est = estimate(sel, a.repeat)
    print("estimated model calls (--channel %s --repeat %d):" % (a.channel, a.repeat))
    for ch in CHANNELS:
        if ch in est:
            print("  %s: %d" % (ch, est[ch]))
    print("  total: %d" % est["total"])
    if a.dry_run:
        return 0
    if a.confirm_calls is None or a.confirm_calls < est["total"]:
        print("REFUSED: pass --confirm-calls N with N >= %d (the estimate)." % est["total"])
        return 2

    clients = (make_clients or _clients)()
    lam, logs, s3 = clients["lambda"], clients["logs"], clients["s3"]
    worker = a.stack + "-ask-voice-worker"
    env = (lam.get_function_configuration(FunctionName=worker).get("Environment") or {}
           ).get("Variables") or {}
    flags = {k: env.get(k) for k in FLAG_NAMES if k in env}
    listed = _flag_channels(env.get("ASK_AGENTIC"))
    chans = sorted({ch for _, ch in sel})
    missing = [ch for ch in chans if ch not in listed]
    if missing:
        print("REFUSED: deployed ASK_AGENTIC=%r does not include %s. This harness never "
              "sets it." % (env.get("ASK_AGENTIC", ""), ",".join(missing)))
        return 2
    bucket = env.get("ASK_TURN_BUCKET")
    if "voice" in chans and not bucket:
        print("REFUSED: worker has no ASK_TURN_BUCKET")
        return 2

    site_id = a.site_id
    if not site_id and any(ch == "screen" and (it.get("body") or {}).get("site_id")
                           == VISIBLE_SITE for it, ch in sel):
        site_id = visible_site(lam, a.stack, a.sub)
        if not site_id:
            print("REFUSED: no visible site for this sub; pass --site-id")
            return 2

    started = datetime.now(timezone.utc)
    turns = []
    group = "/aws/lambda/" + worker
    for ch in chans:
        for rep in range(1, a.repeat + 1):
            batch_start = time.time() * 1000
            batch = []
            for it, c in sel:
                if c != ch:
                    continue
                if ch == "screen":
                    turn, term, wall = screen_turn(lam, a.stack + "-api", a.sub, it, site_id,
                                                   clock=clock, sleep=sleep)
                else:
                    turn, term, wall = voice_turn(lam, s3, worker, bucket, a.sub, it,
                                                  clock=clock, sleep=sleep)
                rec = {"item": it, "channel": ch, "rep": rep, "turn_id": turn,
                       "terminal": term, "result": score.normalize(ch, term),
                       "wall_s": round(wall, 2)}
                if ch == "screen":
                    rec["offer_web"] = True
                print("  %s/%s rep%d turn=%s status=%s %.1fs"
                      % (it["id"], ch, rep, turn, term.get("status"), wall))
                batch.append(rec)
            routes = join_routes(logs, group, [r["turn_id"] for r in batch], batch_start,
                                 time.time() * 1000, clock=clock, sleep=sleep)
            for r in batch:
                r["route"] = routes.get(r["turn_id"])
                r["verdict"] = score.score_turn(r["item"], r["result"], r["route"])
                print("  %s/%s rep%d %s" % (r["item"]["id"], ch, rep, r["verdict"]["verdict"]))
            turns.extend(batch)

    g = score.gate(turns)
    out = {"stack": a.stack, "channel": a.channel, "repeat": a.repeat, "only": only,
           "started_utc": started.isoformat(),
           "run_date_utc": started.date().isoformat(), "run_date_nz": _nz_date(),
           "relative_items": sorted({it["id"] for it, _ in sel
                                     if (it.get("expect") or {}).get("relative")}),
           "flags": flags, "estimate": est, "site_id": site_id,
           "turns": turns, "gate": g}
    rdir = results_dir or RESULTS_DIR
    os.makedirs(rdir, exist_ok=True)
    path = os.path.join(rdir, "%s-%s.json" % (started.strftime("%Y%m%dT%H%M%SZ"), a.channel))
    with open(path, "w", encoding="utf-8") as f:
        json.dump(out, f, indent=1, ensure_ascii=False, default=str)
    print("results: %s" % path)
    print("GATE %s" % ("PASS" if g["pass"] else "FAIL"))
    for ln in g["lines"]:
        print("  " + ln)
    return 0 if g["pass"] else 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
