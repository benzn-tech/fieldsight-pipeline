"""Read-only latency/shape probe for rag-search `mode: "tools"` (Ask retrieval
tools, spec 2026-10-15 §3.4 / plan Task 7).

Invokes the deployed rag-search Lambda directly with a real caller sub and
prints, per (caller, tool): call count, server-side ms p50/p95 (meta.ms),
wall ms p50/p95, row-count range and empty_reason / error tallies. Never
prints row text, query text or ids.

    export AWS_PROFILE=fieldsight-deployer
    export DASHSCOPE_API_KEY=...   # shell only, never written to a file
    python scripts/ask_tools_probe.py --function fieldsight-test-rag-search \
        --sub <cognito-sub> [--sub <another>] --runs 30 --today 2026-10-16
"""
from __future__ import annotations

import argparse
import collections
import json
import os
import statistics
import sys
import time
import urllib.request
from datetime import date, timedelta

import boto3

QUERIES = ["concrete pour", "scaffolding inspection", "Level 2 inspection", "safety"]


def embed(texts):
    key = os.environ["DASHSCOPE_API_KEY"]
    base = os.environ.get("DASHSCOPE_BASE_URL",
                          "https://dashscope-intl.aliyuncs.com/compatible-mode/v1")
    req = urllib.request.Request(
        base + "/embeddings",
        data=json.dumps({"model": "text-embedding-v4", "input": texts,
                         "dimensions": 1024}).encode(),
        headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=30) as resp:
        body = json.load(resp)
    return [d["embedding"] for d in sorted(body["data"], key=lambda d: d["index"])]


def pct(values, p):
    if not values:
        return None
    s = sorted(values)
    return s[min(len(s) - 1, int(round(p / 100 * (len(s) - 1))))]


def calls_for(today, vecs):
    y = (today - timedelta(days=1)).isoformat()
    wk = (today - timedelta(days=6)).isoformat()
    t = today.isoformat()
    out = []
    for i, q in enumerate(QUERIES):
        out.append(("search_records", {"query": q, "k": 8}, vecs[i]))
        out.append(("latest_activity", {"query": q, "on_or_before": t}, vecs[i]))
    out += [
        ("day_overview", {"date_from": y, "date_to": y}, None),
        ("day_overview", {"date_from": wk, "date_to": t}, None),
        ("latest_activity", {"on_or_before": t}, None),
        ("find_people", {"name": "Ben"}, None),
        ("count_metric", {"metric": "duration", "date_from": wk, "date_to": t}, None),
    ]
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--function", required=True)
    ap.add_argument("--sub", action="append", required=True)
    ap.add_argument("--runs", type=int, default=30)
    ap.add_argument("--today", default=date.today().isoformat())
    a = ap.parse_args()
    today = date.fromisoformat(a.today)
    vecs = embed(QUERIES)
    lam = boto3.client("lambda", region_name="ap-southeast-2")
    for n, sub in enumerate(a.sub, 1):
        stats = collections.defaultdict(lambda: {"server": [], "wall": [], "rows": [],
                                                 "empty": collections.Counter(),
                                                 "err": collections.Counter()})
        for _ in range(a.runs):
            for name, args, vec in calls_for(today, vecs):
                call = {"id": "1", "name": name, "args": args}
                if vec is not None:
                    call["query_embedding"] = vec
                t0 = time.monotonic()
                resp = lam.invoke(FunctionName=a.function, InvocationType="RequestResponse",
                                  Payload=json.dumps({"mode": "tools", "sub": sub,
                                                      "deadline_s": 20, "calls": [call]}))
                wall = (time.monotonic() - t0) * 1000
                key = name + ("(query)" if name == "latest_activity" and "query" in args else "")
                st = stats[key]
                if resp.get("FunctionError"):
                    st["err"]["FunctionError"] += 1
                    continue
                r = json.loads(resp["Payload"].read())["results"][0]
                meta = r.get("meta") or {}
                st["wall"].append(wall)
                if "ms" in meta:
                    st["server"].append(meta["ms"])
                st["rows"].append(len(r.get("rows") or []))
                if meta.get("empty_reason"):
                    st["empty"][meta["empty_reason"]] += 1
                if meta.get("error"):
                    st["err"][meta["error"]] += 1
        print(f"caller #{n}")
        for key, st in sorted(stats.items()):
            print(f"  {key:24s} n={len(st['wall']):3d} "
                  f"server p50={pct(st['server'], 50)} p95={pct(st['server'], 95)} "
                  f"wall p50={pct(st['wall'], 50) and round(pct(st['wall'], 50))} "
                  f"p95={pct(st['wall'], 95) and round(pct(st['wall'], 95))} "
                  f"rows={min(st['rows'], default=0)}-{max(st['rows'], default=0)} "
                  f"empty={dict(st['empty'])} err={dict(st['err'])}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
