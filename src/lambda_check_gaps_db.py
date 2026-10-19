"""
Lambda: check-gaps-db -- the in-VPC hop of "check done" (spec 2026-10-20).

Invoked synchronously by CheckGapsFunction (non-VPC), the same split as
AskAgent -> RagSearch: the VPC has no egress, so the function that calls the STT
vendor and the model cannot reach Aurora, and this one, which can, never leaves
the VPC. Read-only; no S3.

Event:  {"caller_sub": "<cognito sub>", "pressed_at": <epoch seconds>}
Result: {"user": {id, company_id, folder_name} | None,
         "owner_company_id": str | None,
         "session": {session_id, company_id, site_id, kind, status, opened_at,
                     last_segment_at} | None,
         "templates": [{id, name, scope, body}],
         "live_windows": [{name, kind, start_at, end_at, end_source, template_id,
                           report_date}]}
        or {"error": "..."}. Never raises: the caller is waiting with a person
        on the other end.

The only person this reads about is `caller_sub`, which the Ask worker took from
the API authorizer. No session, folder or company is accepted from the event.
"""
import json
import logging

from db.connection import close_cached_connection, get_cached_connection
from repositories import inspection_windows, meeting_session, sites, users

logger = logging.getLogger()
logger.setLevel(logging.INFO)

SESSION_WINDOW_S = 300


def lambda_handler(event, context):
    """Release the pooled connection on every exit (lambda_rag_search's reason:
    a warm container holding one keeps Aurora from pausing)."""
    try:
        return _facts(event or {})
    except Exception:                                 # noqa: BLE001 -- see "Never raises"
        logger.exception("check-gaps-db: failed")
        return {"error": "db"}
    finally:
        close_cached_connection()


def _jsonable(value):
    # uuids, timestamps and dates as strings: Lambda serialises the return value
    # with the plain json encoder.
    return json.loads(json.dumps(value, default=lambda v: v.isoformat() if hasattr(v, "isoformat") else str(v)))


def _facts(event):
    sub = event.get("caller_sub")
    if not sub:
        return {"error": "caller_sub required"}
    at = int(event.get("pressed_at") or 0)
    out = {"user": None, "owner_company_id": None, "session": None, "templates": [],
           "live_windows": []}
    conn = get_cached_connection()
    user = users.get_user_by_sub(conn, sub)
    if not user:
        return out
    out["user"] = {"id": user["id"], "company_id": user.get("company_id"),
                   "folder_name": user.get("folder_name")}
    session = meeting_session.latest_for_user_near(conn, user["id"], at, SESSION_WINDOW_S)
    if session:
        # The company item-writer writes this recording's checks under, and matches
        # its templates against: the SITE's (project-owned tenancy), else the
        # session's, else his own.
        site = sites.get_site(conn, session["site_id"]) if session.get("site_id") else None
        owner = (site or {}).get("company_id") or session.get("company_id") or user.get("company_id")
        out["owner_company_id"] = owner
        out["session"] = {k: session.get(k) for k in (
            "session_id", "company_id", "site_id", "kind", "status", "opened_at", "last_segment_at")}
        out["templates"] = inspection_windows.checklist_templates(conn, owner, user["id"])
        companies = list(dict.fromkeys(str(c) for c in (owner, session.get("company_id"),
                                                        user.get("company_id")) if c))
        out["live_windows"] = inspection_windows.for_session(
            conn, companies, user.get("folder_name"), "sid%s" % session["session_id"])
    logger.info("check-gaps-db: session=%s templates=%d windows=%d",
                bool(out["session"]), len(out["templates"]), len(out["live_windows"]))
    return _jsonable(out)
