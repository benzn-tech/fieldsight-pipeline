"""Acceptance check (spec §4.4a): day_overview for one person-day must list
every topic the Aurora-rendered /timeline shows for that user-day.

Read-only. Prints per day: timeline count, tool count, missing, extra,
truncated flag. Topic ids are compared (timeline topic_row_id vs tool row id); nothing is printed.

    export AWS_PROFILE=fieldsight-deployer MSYS_NO_PATHCONV=1
    python scripts/ask_tools_timeline_parity.py --env test --sub <sub> \
        --folder <folder_name> --user-id <uuid> 2026-10-12 2026-10-09 ...
"""
from __future__ import annotations

import argparse
import json
import sys

import boto3


def _invoke(lam, fn, payload):
    resp = lam.invoke(FunctionName=fn, InvocationType="RequestResponse",
                      Payload=json.dumps(payload))
    if resp.get("FunctionError"):
        raise RuntimeError(f"{fn} FunctionError")
    return json.loads(resp["Payload"].read())


def timeline_titles(lam, env, sub, folder, day):
    path = "/api/org/timeline"
    ev = {"requestContext": {"authorizer": {"claims": {"sub": sub}}, "http": {"method": "GET"}},
          "httpMethod": "GET", "rawPath": path, "path": path, "resource": path,
          "queryStringParameters": {"date": day, "user": folder}, "body": None}
    out = _invoke(lam, f"fieldsight-{env}-org-api", ev)
    if out.get("statusCode") != 200:
        raise RuntimeError(f"timeline {day}: status {out.get('statusCode')}")
    body = json.loads(out["body"]) if isinstance(out.get("body"), str) else out.get("body")
    topics = body.get("topics") or (body.get("report") or {}).get("topics") or []
    # The timeline shows non_work topics to their owner (to confirm/correct the
    # classification); Ask never answers from them (spec: shared visibility rules).
    return [str(t.get("topic_row_id")) for t in topics if t.get("work_class") != "non_work"]


def tool_titles(lam, env, sub, user_id, day):
    call = {"id": "1", "name": "day_overview",
            "args": {"date_from": day, "date_to": day, "person": user_id}}
    out = _invoke(lam, f"fieldsight-{env}-rag-search",
                  {"mode": "tools", "sub": sub, "deadline_s": 20, "calls": [call]})
    r = out["results"][0]
    return [str(row.get("id")) for row in r.get("rows") or []], r.get("meta") or {}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--env", default="test")
    ap.add_argument("--sub", required=True)
    ap.add_argument("--folder", required=True)
    ap.add_argument("--user-id", required=True)
    ap.add_argument("days", nargs="+")
    a = ap.parse_args()
    lam = boto3.client("lambda", region_name="ap-southeast-2")
    ok = True
    for day in a.days:
        tl = timeline_titles(lam, a.env, a.sub, a.folder, day)
        tt, meta = tool_titles(lam, a.env, a.sub, a.user_id, day)
        missing = len(set(tl) - set(tt))
        extra = len(set(tt) - set(tl))
        full = not meta.get("truncated")
        good = (missing == 0 or not full) and extra == 0
        ok &= good
        print(f"{day} timeline={len(tl)} tool={len(tt)} missing={missing} extra={extra} "
              f"truncated={meta.get('truncated')} err={meta.get('error')} "
              f"{'OK' if good else 'MISMATCH'}")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
