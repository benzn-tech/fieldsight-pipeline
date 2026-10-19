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
import nz_time
from repositories import inspection_windows, meeting_session, recordings, sites, users

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


def _site(conn, user, session):
    """The site item-writer files this recording under, by its rungs in the same
    order (lambda_item_writer, the ladder before `owner_company_id`): the app's
    tagged recording, the session's own site, then the day's majority site. The
    windows it wrote are under that site's company (project-owned tenancy), so a
    different answer here reads nobody's windows.

    The group-lead rung is left out: a merged artifact's base is `grp{gid}`, and
    this reads one device session's live state, never a merged one. resolve_site
    (the legacy membership guess) is left out too: it is a company-pinned guess,
    and the session's or his own company is the same guess without a query."""
    folder = user.get("folder_name")
    opened = session.get("opened_at")
    local = nz_time.to_nz(opened) if opened is not None else None
    date = local.date().isoformat() if local is not None else None
    base = "sid%s" % session["session_id"]
    if folder and date:
        site = recordings.site_for_media(conn, user["id"], folder, date, base)
        if site:
            return site
    if session.get("site_id"):
        site = sites.get_site(conn, session["site_id"])
        if site:
            return site
    if folder and date:
        return recordings.site_for_day(conn, user["id"], folder, date)
    return None


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
        site = _site(conn, user, session)
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
