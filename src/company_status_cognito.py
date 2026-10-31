"""The Cognito side effects of a company status change (company offboarding, spec
docs/superpowers/specs/2026-11-01-company-offboarding-design.md §5.3, ruling R3, Task 8).

POST /api/org/companies/{id}/status (org-api, in the VPC, cannot invoke a Lambda) commits
the status, its audit event and the work rows in company_status_cognito, then leaves
`company_status_requests/{company_id}/{event_id}.json` in the export bucket. LakeOps picks
that up here, on its existing rate(10 minutes) ExportTick schedule (`tick`, run before
export_tick), or by a direct operator invoke (`run_task`) when a marker was lost:

    {"task": "company_status_cognito", "event_id": "<uuid>"}   and/or  "company_id"

tick: S3 FIRST. No markers -> return, no database connection (Aurora must be able to
auto-pause). Flag off (tenancy_status.enforce_enabled, ruling R1) -> one WARNING
`COMPANY_GATE_WOULD_CALL_COGNITO markers=N` and return: no connection, no Cognito call, no
marker deleted, rows stay pending. Otherwise, per company named by a valid marker (the key
path is the authority; the body must agree), sweep EVERY retryable row of that company --
not only the marker's event -- and delete its markers only when nothing retryable is left.

Retryable: state 'pending', or 'failed' with attempts < MAX_ATTEMPTS and a last_error that
does not start with 'superseded' (superseded rows are final).

One row = one transaction, committed on its own so progress survives a timeout:
  1. the company's status is re-read under FOR SHARE. The status endpoint takes FOR UPDATE
     on the same row before it changes the status or supersedes rows, so it cannot slip in
     between this check and the Cognito call (a reactivation waits for the disable to be
     recorded 'done', and then creates the matching enable row);
  2. 'disable' / 'signout' run only while the company is inactive (suspended / exited),
     'enable' only while it is active; otherwise the row is failed as
     SUPERSEDED_BY_STATUS_CHANGE (no call, attempts unchanged);
  3. claim (attempts + 1, updated_at = now()), call Cognito, record done / failed. Every
     UPDATE re-checks the row's state and attempts in its WHERE clause; rowcount 0 means
     someone else changed the row, and their change wins ("raced").
UserNotFoundException is 'done' with last_error USER_NOT_FOUND (nothing to do). Any other
error is 'failed' with the (truncated) error; the marker stays for the next tick.

Username is users.cognito_sub: org-api already calls admin_get_user with the sub.
"""
import json
import logging
import time
import uuid

from botocore.exceptions import ClientError

import tenancy_status
from repositories import company_status as repo

logger = logging.getLogger(__name__)

TASK = "company_status_cognito"
REQUEST_PREFIX = "company_status_requests/"
MAX_ATTEMPTS = 5
# The tick runs before export_tick on the same 10-minute schedule; it takes at most this
# much and leaves the rest to the export. A direct invoke may use most of the 900 s.
TICK_BUDGET_S = 120
TASK_BUDGET_S = 780
SAFETY_S = 30
ERROR_MAX = 500
SUPERSEDED_BY_STATUS_CHANGE = "superseded by status change"
USER_NOT_FOUND = "user not found in Cognito"

_CALLS = {"disable": "admin_disable_user",
          "signout": "admin_user_global_sign_out",
          "enable": "admin_enable_user"}


def _canon(value, what):
    """A canonical (lowercase, hyphenated) uuid string, exactly as org-api writes it."""
    try:
        out = str(uuid.UUID(str(value)))
    except (ValueError, AttributeError, TypeError):
        raise ValueError(f"{what} must be a UUID") from None
    if out != value:
        raise ValueError(f"{what} is not a canonical UUID")
    return out


def _deadline(context, cap):
    budget = cap
    remaining = getattr(context, "get_remaining_time_in_millis", None)
    if callable(remaining):
        budget = min(cap, remaining() / 1000 - SAFETY_S)
    return time.monotonic() + budget


def _wanted(action, status):
    if action == "enable":
        return status == tenancy_status.ACTIVE
    return status in (tenancy_status.SUSPENDED, tenancy_status.EXITED)


def _call(client, pool_id, action, sub):
    """(state, last_error) for one Cognito call."""
    try:
        getattr(client, _CALLS[action])(UserPoolId=pool_id, Username=sub)
    except ClientError as exc:
        err = exc.response.get("Error", {})
        if err.get("Code") == "UserNotFoundException":
            return "done", USER_NOT_FOUND
        return "failed", f"{err.get('Code')}: {err.get('Message')}"[:ERROR_MAX]
    except Exception as exc:  # noqa: BLE001 - timeouts, endpoint errors: retried later
        return "failed", f"{type(exc).__name__}: {exc}"[:ERROR_MAX]
    return "done", None


def _process_row(conn, client, pool_id, company_id, row):
    """One row in its own transaction (module doc). Returns the outcome:
    'done' | 'failed' | 'superseded' | 'raced' | 'skipped'."""
    key = (row["event_id"], row["user_sub"], row["action"])
    with conn.transaction():
        status = repo.lock_company_status(conn, company_id)
        if status is None:
            outcome = "skipped"
        elif not _wanted(row["action"], status):
            n = repo.supersede_cognito_row(conn, *key, row["state"], row["attempts"],
                                           SUPERSEDED_BY_STATUS_CHANGE)
            outcome = "superseded" if n else "raced"
        else:
            attempts = repo.claim_cognito_row(conn, *key, row["state"], row["attempts"],
                                              MAX_ATTEMPTS)
            if attempts is None:
                outcome = "raced"
            else:
                state, last_error = _call(client, pool_id, row["action"], row["user_sub"])
                n = repo.finish_cognito_row(conn, *key, attempts, state, last_error)
                outcome = state if n else "raced"
                if state == "failed":
                    logger.warning("company_status_cognito: %s %s failed (attempt %s): %s",
                                   row["action"], row["user_sub"], attempts, last_error)
    conn.commit()
    if outcome == "raced":
        logger.warning("company_status_cognito: row %s changed underneath, left as is", key)
    return outcome


def sweep_company(conn, cognito_factory, pool_id, company_id, deadline) -> dict:
    """Run every retryable row of the company until the deadline. Returns the counts and
    `retryable_left` (0 means the company's markers can go)."""
    # Every read sits in its own transaction block, so a failing query never leaves the
    # connection aborted for the next company of the same tick.
    with conn.transaction():
        rows = repo.list_retryable_cognito(conn, company_id, MAX_ATTEMPTS)
    conn.commit()           # end the read, so each row below is its own transaction
    counts = {"done": 0, "failed": 0, "superseded": 0, "raced": 0, "skipped": 0}
    more = False
    client = cognito_factory() if rows else None
    for row in rows:
        if time.monotonic() >= deadline:
            more = True
            break
        counts[_process_row(conn, client, pool_id, company_id, row)] += 1
    with conn.transaction():
        left = repo.count_retryable_cognito(conn, company_id, MAX_ATTEMPTS)
    conn.commit()
    return {**counts, "retryable_left": left, "more": more}


def _list_markers(s3_client, bucket, prefix):
    keys = []
    for page in s3_client.get_paginator("list_objects_v2").paginate(
            Bucket=bucket, Prefix=prefix):
        keys.extend(o["Key"] for o in page.get("Contents") or [])
    return sorted(k for k in keys if k.endswith(".json"))


def _delete(s3_client, bucket, key):
    try:
        s3_client.delete_object(Bucket=bucket, Key=key)
        return True
    except Exception:  # noqa: BLE001 - harmless: the next tick finds nothing to do
        logger.exception("company_status_cognito: could not delete %s", key)
        return False


def _add(total, part):
    for k, v in part.items():
        if isinstance(v, bool):
            total[k] = total.get(k, False) or v
        elif isinstance(v, int):
            total[k] = total.get(k, 0) + v


def tick(context, *, s3_client, bucket, cognito_factory, get_connection, pool_id) -> dict:
    """The scheduled pickup of company_status_requests/ (module doc)."""
    keys = _list_markers(s3_client, bucket, REQUEST_PREFIX)
    if not keys:
        return {"markers": 0}
    if not tenancy_status.enforce_enabled():
        logger.warning("COMPANY_GATE_WOULD_CALL_COGNITO markers=%d", len(keys))
        return {"markers": len(keys), "enforced": False}
    if not pool_id:
        logger.error("company_status_cognito: COGNITO_USER_POOL_ID is not set; %d markers "
                     "left for later", len(keys))
        return {"markers": len(keys), "error": "COGNITO_USER_POOL_ID not set"}
    by_company, skipped = {}, 0
    for key in keys:
        parts = key[len(REQUEST_PREFIX):-len(".json")].split("/")
        try:
            if len(parts) != 2:
                raise ValueError("unexpected key shape")
            cid = _canon(parts[0], "company_id")
            eid = _canon(parts[1], "event_id")
            doc = json.loads(s3_client.get_object(Bucket=bucket, Key=key)["Body"].read())
            if doc.get("company_id") != cid or doc.get("event_id") != eid:
                raise ValueError("marker body does not match its key")
        except Exception as exc:  # noqa: BLE001
            logger.warning("company_status_cognito: skipping %s: %s", key, exc)
            skipped += 1
            continue
        by_company.setdefault(cid, []).append((key, eid))
    out = {"markers": len(keys), "enforced": True, "skipped": skipped, "companies": 0,
           "dropped": 0, "markers_deleted": 0, "errors": 0, "more": False,
           "done": 0, "failed": 0, "superseded": 0, "raced": 0}
    if not by_company:
        return out
    deadline = _deadline(context, TICK_BUDGET_S)
    with get_connection() as conn:
        for cid, markers in by_company.items():
            if time.monotonic() >= deadline:
                out["more"] = True
                break
            try:
                live = []
                for key, eid in markers:
                    with conn.transaction():
                        owner = repo.event_company(conn, eid)
                    if owner is None or str(owner) != cid:
                        # Written after the event committed, so this is not a lag: the
                        # marker names nothing. Drop it, or it would wake Aurora forever.
                        logger.warning("company_status_cognito: dropping %s: event %s",
                                       key, "not found" if owner is None
                                       else f"belongs to company {owner}")
                        out["dropped"] += 1
                        _delete(s3_client, bucket, key)
                    else:
                        live.append(key)
                conn.commit()
                if not live:
                    continue
                out["companies"] += 1
                res = sweep_company(conn, cognito_factory, pool_id, cid, deadline)
                _add(out, {k: v for k, v in res.items() if k != "retryable_left"})
                if res["retryable_left"] == 0:
                    out["markers_deleted"] += sum(_delete(s3_client, bucket, k) for k in live)
            except Exception:  # noqa: BLE001 - one company must not stop the others
                logger.exception("company_status_cognito: company %s failed", cid)
                out["errors"] += 1
    return out


def run_task(event, context, *, s3_client, bucket, cognito_factory, get_connection,
             pool_id) -> dict:
    """The direct operator task: sweep one company (named by event_id and/or company_id)
    without needing a marker; deletes the company's markers when nothing is left."""
    if not tenancy_status.enforce_enabled():
        logger.warning("COMPANY_GATE_WOULD_CALL_COGNITO task event=%s company=%s",
                       event.get("event_id"), event.get("company_id"))
        return {"enforced": False}
    eid, cid = event.get("event_id"), event.get("company_id")
    if not eid and not cid:
        return {"error": "event_id or company_id is required"}
    try:   # an operator's hand-typed id may be upper case: canonicalise, do not refuse
        eid = _canon(str(eid).lower(), "event_id") if eid else None
        cid = _canon(str(cid).lower(), "company_id") if cid else None
    except ValueError as exc:
        return {"error": str(exc)}
    if not pool_id:
        return {"error": "COGNITO_USER_POOL_ID not set"}
    with get_connection() as conn:
        if eid:
            with conn.transaction():
                owner = repo.event_company(conn, eid)
            conn.commit()
            if owner is None:
                return {"error": f"event not found: {eid}"}
            if cid and str(owner) != cid:
                return {"error": f"event {eid} does not belong to company {cid}"}
            cid = str(owner)
        res = sweep_company(conn, cognito_factory, pool_id, cid,
                            _deadline(context, TASK_BUDGET_S))
    deleted = 0
    if res["retryable_left"] == 0:
        deleted = sum(_delete(s3_client, bucket, k)
                      for k in _list_markers(s3_client, bucket, f"{REQUEST_PREFIX}{cid}/"))
    return {"enforced": True, "company_id": cid, **res, "markers_deleted": deleted}
