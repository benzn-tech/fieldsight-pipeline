"""The live half: submit the gold set's instructions to org-api on TEST and fetch the packs.

AFTER MERGE, TEST ONLY (README). Nothing here runs in tests or CI: every network call goes
through an injected `client(method, url, headers, body) -> (status, json_body_or_None)`;
`urllib_client` builds the real one and only the CLI's `submit`/`fetch` call it.

* The token is a Cognito id token for a platform_admin on TEST (ACTION_PACKS=internal), sent
  as the raw `Authorization` header like the web app does.
* `submit` is SEQUENTIAL: org-api allows one queued/running pack per creator (409 "you
  already have a pack in progress"), so each pack is polled to a terminal state before the
  next POST. A 409 anyway (a pack started outside the eval) is waited out: the POST is
  retried every poll_s until max_wait_s, then recorded as `post_409`.
* POST needs a site uuid when the caller sees more than one site: a gold `site` that is not
  a uuid is resolved through `site_map` ({name: uuid}); unresolved entries are skipped and
  recorded (`error: site_unresolved`), their expectations then count as missed.
"""
from __future__ import annotations

import json
import time
import uuid

TERMINAL = ("ready", "failed", "cancelled")


def urllib_client(timeout):
    """The real HTTP client (stdlib urllib). Constructed only by the CLI's live commands."""
    import urllib.error
    import urllib.request

    def call(method, url, headers, body):
        data = body.encode("utf-8") if isinstance(body, str) else body
        req = urllib.request.Request(url, data=data, headers=headers, method=method)
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:      # noqa: S310
                status, raw = resp.status, resp.read()
        except urllib.error.HTTPError as exc:
            status, raw = exc.code, exc.read()
        try:
            return status, json.loads(raw.decode("utf-8")) if raw else None
        except ValueError:
            return status, None
    return call


def _headers(token):
    return {"Authorization": token, "Content-Type": "application/json"}


def resolve_site(site, site_map):
    try:
        return str(uuid.UUID(str(site)))
    except ValueError:
        return (site_map or {}).get(site)


def submit(gold, *, base_url, token, client, site_map=None, sleep=None, clock=None,
           poll_s=15, max_wait_s=900, config_label=None, run_id=None,
           gold_sha256=None, inputs_sha256=None) -> dict:
    sleep = sleep or time.sleep
    clock = clock or time.monotonic
    base = base_url.rstrip("/")
    rows = []
    for entry in gold:
        row = {"gold_id": entry["id"], "pack_id": None, "pack": None}
        rows.append(row)
        site_id = resolve_site(entry["site"], site_map)
        if site_id is None:
            row["error"] = "site_unresolved"
            continue
        payload = json.dumps({"instruction": entry["instruction"], "site_id": site_id})
        waited_from = clock()
        while True:
            status, body = client("POST", f"{base}/action-packs", _headers(token), payload)
            # 409: this caller already has a queued/running pack (one per creator) -- a
            # pack started outside the eval. Wait for it, bounded by max_wait_s.
            if status != 409 or clock() - waited_from >= max_wait_s:
                break
            row["conflict_retries"] = row.get("conflict_retries", 0) + 1
            sleep(poll_s)
        row["http_status"] = status
        if status != 202 or not isinstance(body, dict) or not body.get("id"):
            row["error"] = f"post_{status}"
            continue
        pid = row["pack_id"] = str(body["id"])
        start = clock()
        while True:
            status, body = client("GET", f"{base}/action-packs/{pid}", _headers(token), None)
            if status != 200 or not isinstance(body, dict):
                row["error"] = f"get_{status}"
                break
            if body.get("state") in TERMINAL:
                row["pack"] = body
                row["client_elapsed_s"] = clock() - start
                break
            elapsed = clock() - start
            if elapsed >= max_wait_s:
                row["error"] = "timeout"
                row["client_elapsed_s"] = elapsed     # a censored latency (>= max wait)
                break
            sleep(poll_s)
    return {"run_id": run_id, "config_label": config_label, "gold_sha256": gold_sha256,
            "inputs_sha256": inputs_sha256, "packs": rows}


def fetch(prior, *, base_url, token, client) -> dict:
    """Re-GET every pack a prior run recorded (by pack_id or pack.id). A pack the GET does
    not return keeps pack null and records the status. run_trace/models already attached
    to the prior pack are kept (GET does not return them)."""
    base = base_url.rstrip("/")
    rows = []
    for old in prior.get("packs") or []:
        row = dict(old)
        pid = old.get("pack_id") or (old.get("pack") or {}).get("id")
        if not pid:
            rows.append(row)
            continue
        status, body = client("GET", f"{base}/action-packs/{pid}", _headers(token), None)
        if status == 200 and isinstance(body, dict):
            for k in ("run_trace", "models"):
                if k not in body and (old.get("pack") or {}).get(k) is not None:
                    body[k] = old["pack"][k]
            row.update(pack_id=str(pid), pack=body)
            row.pop("error", None)
        else:
            row.update(pack_id=str(pid), pack=None, error=f"get_{status}")
        rows.append(row)
    return {**{k: v for k, v in prior.items() if k != "packs"}, "packs": rows}
