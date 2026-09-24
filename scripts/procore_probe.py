"""Procore developer-sandbox probe. Read-only. Answers the open questions in
docs/superpowers/specs/2026-09-09-procore-integration-feasibility.md §7 with
numbers instead of documentation.

What it does, in order, and stops at the first failure with the HTTP status
and body so the failure is legible:

  1. token      client_credentials against the sandbox login host
  2. companies  GET /rest/v1.0/companies  (WITH Procore-Company-Id -- spec §6:
                under DMSA even this endpoint needs the header)
  3. projects   GET /rest/v1.0/projects?company_id=...
  4. users      GET /rest/v1.0/companies/{id}/users   (directory -> Block B)
  5. schedule   GET /rest/v1.0/projects/{pid}/schedule/tasks  (-> Block C)
  6. writes     for each of daily_logs, images, rfis, observations:
                a GET on the collection, to learn whether the manifest's
                permissions reach the tool at all (403 = not granted,
                404 = not visible, spec §3.6). No POST is ever sent.
  7. webhooks   GET /rest/v1.0/webhooks/hooks?company_id=...  (what exists)
  8. limits     every response's X-Rate-Limit-Limit / -Remaining / -Reset,
                printed once per call, so the actual values are on record
                (spec §3.3: they are not published).

Never writes to Procore, S3 or Aurora. Never prints the secret. The token is
held in memory for the run and discarded.

Env (never committed; sandbox credentials are separate from production and
do not cross environments, spec §3.2):
    PROCORE_CLIENT_ID, PROCORE_CLIENT_SECRET   the SANDBOX pair from the
                                              app's "OAuth Credentials" section
    PROCORE_COMPANY_ID                         the developer sandbox company id
    PROCORE_LOGIN_HOST   default https://login-sandbox.procore.com
    PROCORE_API_HOST     default https://sandbox.procore.com

Usage:
    python scripts/procore_probe.py            # everything
    python scripts/procore_probe.py --step 3   # stop after projects
    python scripts/procore_probe.py --json out.json
"""
import argparse
import json
import os
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

LOGIN_HOST = os.environ.get("PROCORE_LOGIN_HOST", "https://login-sandbox.procore.com")
API_HOST = os.environ.get("PROCORE_API_HOST", "https://sandbox.procore.com")

RATE_HEADERS = ("X-Rate-Limit-Limit", "X-Rate-Limit-Remaining", "X-Rate-Limit-Reset")

# Collections whose reachability under our manifest is a spec §7 open question.
# GET only. A 403 means the permission is not granted; a 404 means the tool is
# not visible on that project (spec §3.6: 404 is not "gone").
WRITE_TARGETS = (
    ("daily_logs", "/rest/v1.0/projects/{pid}/daily_logs/daily_construction_report_logs"),
    ("images", "/rest/v1.0/projects/{pid}/images"),
    ("rfis", "/rest/v1.0/projects/{pid}/rfis"),
    ("observations", "/rest/v1.0/observations/items?project_id={pid}"),
)


def _env(name):
    v = os.environ.get(name)
    if not v:
        sys.exit(f"missing env {name}")
    return v


def _call(method, url, *, token=None, company_id=None, data=None, log):
    headers = {"Accept": "application/json"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    if company_id:
        headers["Procore-Company-Id"] = str(company_id)
    body = None
    if data is not None:
        body = urllib.parse.urlencode(data).encode()
        headers["Content-Type"] = "application/x-www-form-urlencoded"
    req = urllib.request.Request(url, data=body, method=method, headers=headers)
    t0 = time.time()
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            status = resp.status
            raw = resp.read()
            hdrs = {k: resp.headers.get(k) for k in RATE_HEADERS if resp.headers.get(k)}
            link = resp.headers.get("Link")
            total = resp.headers.get("Total")
    except urllib.error.HTTPError as e:
        status = e.code
        raw = e.read()
        hdrs = {k: e.headers.get(k) for k in RATE_HEADERS if e.headers.get(k)}
        link, total = None, None
    ms = int((time.time() - t0) * 1000)
    try:
        parsed = json.loads(raw) if raw else None
    except ValueError:
        parsed = raw[:300].decode("utf-8", "replace")
    entry = {"method": method, "url": url.replace(API_HOST, "").replace(LOGIN_HOST, ""),
             "status": status, "ms": ms, "rate": hdrs}
    if link or total:
        entry["pagination"] = {"link": link, "total": total}
    log.append(entry)
    print(f"{status} {method} {entry['url']} {ms}ms rate={hdrs}")
    return status, parsed


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--step", type=int, default=8)
    ap.add_argument("--json", help="write the call log here")
    args = ap.parse_args()

    cid = _env("PROCORE_CLIENT_ID")
    secret = _env("PROCORE_CLIENT_SECRET")
    company_id = _env("PROCORE_COMPANY_ID")
    log, findings = [], {}

    # 1. token. No refresh token under client_credentials; a new one is requested on expiry.
    status, tok = _call("POST", f"{LOGIN_HOST}/oauth/token", log=log,
                        data={"grant_type": "client_credentials",
                              "client_id": cid, "client_secret": secret})
    if status != 200 or not isinstance(tok, dict) or "access_token" not in tok:
        sys.exit(f"token failed: {status} {tok}")
    token = tok["access_token"]
    findings["token_expires_in"] = tok.get("expires_in")
    findings["token_has_refresh"] = "refresh_token" in tok
    if args.step < 2:
        return _finish(args, log, findings)

    # 2. companies -- with the header, deliberately (spec §6 MPR row).
    status, companies = _call("GET", f"{API_HOST}/rest/v1.0/companies",
                              token=token, company_id=company_id, log=log)
    findings["companies"] = [{"id": c.get("id"), "name": c.get("name")}
                             for c in (companies or [])] if isinstance(companies, list) else companies
    if args.step < 3:
        return _finish(args, log, findings)

    # 3. projects
    status, projects = _call("GET", f"{API_HOST}/rest/v1.0/projects?company_id={company_id}",
                             token=token, company_id=company_id, log=log)
    projects = projects if isinstance(projects, list) else []
    findings["projects"] = [{"id": p.get("id"), "name": p.get("name"),
                             "time_zone": p.get("time_zone")} for p in projects]
    pid = projects[0]["id"] if projects else None
    if args.step < 4:
        return _finish(args, log, findings)

    # 4. directory users -> Block B
    status, users = _call("GET", f"{API_HOST}/rest/v1.0/companies/{company_id}/users",
                          token=token, company_id=company_id, log=log)
    findings["users_n"] = len(users) if isinstance(users, list) else status
    findings["users_sample_keys"] = sorted(users[0].keys()) if isinstance(users, list) and users else None
    if args.step < 5 or pid is None:
        return _finish(args, log, findings)

    # 5. schedule tasks -> Block C
    status, tasks = _call("GET", f"{API_HOST}/rest/v1.0/projects/{pid}/schedule/tasks",
                          token=token, company_id=company_id, log=log)
    findings["schedule_tasks_n"] = len(tasks) if isinstance(tasks, list) else status
    findings["schedule_task_keys"] = sorted(tasks[0].keys()) if isinstance(tasks, list) and tasks else None
    if args.step < 6:
        return _finish(args, log, findings)

    # 6. reachability of the write targets, GET only.
    reach = {}
    for name, path in WRITE_TARGETS:
        status, _ = _call("GET", f"{API_HOST}{path.format(pid=pid)}",
                          token=token, company_id=company_id, log=log)
        reach[name] = {200: "reachable", 403: "permission not granted",
                       404: "not visible (spec 3.6)"}.get(status, f"http {status}")
    findings["write_targets"] = reach
    if args.step < 7:
        return _finish(args, log, findings)

    # 7. webhooks that exist today (probably none), and whether the endpoint is reachable.
    status, hooks = _call("GET", f"{API_HOST}/rest/v1.0/webhooks/hooks?company_id={company_id}",
                          token=token, company_id=company_id, log=log)
    findings["webhooks"] = {"status": status,
                            "existing": len(hooks) if isinstance(hooks, list) else None}

    # 8. limits are collected on every call above.
    seen = [e["rate"] for e in log if e.get("rate")]
    findings["rate_limit_headers_seen"] = seen[-1] if seen else "none returned"
    return _finish(args, log, findings)


def _finish(args, log, findings):
    print("\n== findings ==")
    print(json.dumps(findings, indent=2, default=str))
    if args.json:
        with open(args.json, "w", encoding="utf-8") as f:
            json.dump({"findings": findings, "calls": log}, f, indent=2, default=str)
        print(f"wrote {args.json}")


if __name__ == "__main__":
    main()
