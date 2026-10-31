"""Action packs -- the background action agent's stored runs (migration 0095).

Spec: docs/superpowers/specs/2026-11-01-background-action-agent-design.md §3, §4.5, §5.2, §7,
§14 (D4, D6, S1, S9, S13, S16). Repositories never commit: the caller owns the transaction.
Every multi-statement write here runs inside `with conn.transaction():` (a savepoint when the
caller already has a transaction open), so a constraint failure never leaves the caller's
connection aborted.

Run lifecycle (pack.state)
--------------------------
    create_queued -> queued --claim--> running --store_result--> ready
                       |                  |----fail(reason)----> failed
                       |----fail--------> failed
                       |----cancel------> cancelled  <--cancel-- running
    sweep_stale: queued/running whose heartbeat_at is older than the threshold -> failed/internal

`heartbeat_at` is the last liveness stamp (NOT NULL, defaults to now() at insert): set at
create and at claim, refreshed by `heartbeat`
(the worker, after each round). ready/failed/cancelled are final: no function here moves a
pack out of them (archive only toggles archived_at).

Store payload (store_result) -- built by the agent worker (Task 10-12), passed by org-api's
internal "store" task (Task 6). Keys are the column names; unknown keys raise ValueError
(a typo must not silently drop a field). Every value may be omitted unless marked required.

    {
      "title":      str,                 # Plan stage's short title (S16)
      "reading":    str,                 # the run's stated interpretation (G1)
      "conclusion": [{"text": str, "refs": [ref, ...], "confidence": "supported"|"uncertain"}],
      "run_trace":  {...},               # counts / timings / truncated -- never free text
      "models":     {"planner": id, "writer": id, "verifier": id},   # ids actually used
      "as_of":      ISO-8601 str | datetime,   # when the evidence was read (apply compares)
      "sources": [{                      # one per cited ref; `ref` unique within the pack
          "ref": "R3",                    # required
          "kind": "topic"|"chunk"|"document"|"web_answer"|"web_source"|"item",   # required
          "provenance": "records"|"documents"|"web",                            # required
          #   web kinds <=> web provenance (DB CHECK)
          "topic_id": uuid, "chunk_id": uuid, "document_id": uuid, "revision": int,
          "page_from": int, "page_to": int, "stable_id": uuid,
          "title": str, "date": "YYYY-MM-DD", "site_id": uuid, "who": str,
          "excerpt": str,                 # the text the model read (ask_tools window)
          "url": str, "fetched_at": ISO str, "snapshot_s3_key": str,   # web only
      }, ...],
      "actions": [{
          "seq": int,                     # required, unique within the pack (display order)
          "type": "close_item"|"update_item"|"create_item"|"draft_rfi"|"draft_email"|"note",
          "target_stable_id": uuid,       # required for close_item / update_item (DB CHECK)
          "target_snapshot": {...},       # the target's status/deadline/responsible/priority/
                                          #   text/updated_at as the run read it
          "proposed": {...},              # fields to set; for create_item the new item
          "rationale": str,
          "refs": [ref, ...],             # required, >= 1 (DB CHECK)
          "verifier": "supported"|"weak"|"unsupported",   # required
      }, ...],
    }

Every ref cited by an action or a conclusion claim must be a `ref` in `sources` (ValueError
otherwise, before anything is written): a pack is self-contained. D6 (an unverified web
answer is never citable) is the worker's job -- drop it from sources and from every refs list.

Readers (§7, §14 D4) -- `PackReader`, built by org-api from facts it already has:

    reader = reader_for(caller, memberships.caller_site_roles(conn, caller["id"]))

  * the creator -- while the pack's company is the creator's own company, or while the
    creator still holds a live membership on the pack's site (an EXTERNAL member reads only
    the packs they created, and only while still a member of that site);
  * admin/GM of the pack's company (global_role admin/gm, home company only);
  * pm/site_manager (membership role) of the pack's site, home company only -- an external
    pm/site_manager is a creator-only reader like any external member.
  platform_admin is not a reader by role (it is not in the spec's reader list), but it
  always reads the packs it created (`cross_company`): its home company is the platform
  company and it holds no memberships, so the creator arm's company/membership condition
  would otherwise hide its own packs under ACTION_PACKS=internal. Company exit (tenancy_status) is the endpoint's gate, not this
  module's: these predicates do not look at companies.status.
"""
import datetime as dt
from dataclasses import dataclass, field

import psycopg
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb

from repositories import acl

STATES = ("queued", "running", "ready", "failed", "cancelled")
FAILURE_REASONS = ("budget", "model", "tools", "verifier", "internal", "company_inactive")
ACTION_STATES = ("proposed", "applied", "dismissed", "stale", "failed")
# Legal action transitions: everything leaves `proposed` once; nothing leaves anything else
# (applied is terminal; a stale action is re-proposed by a NEW run, never revived -- §5.2).
ACTION_TRANSITIONS = {"proposed": ("applied", "dismissed", "stale", "failed")}
_MANAGER_ROLES = ("pm", "site_manager")
_ONE_ACTIVE_INDEX = "action_packs_one_active_per_creator"

_PACK_FIELDS = ("title", "reading", "conclusion", "run_trace", "models", "as_of")
_SOURCE_FIELDS = ("ref", "kind", "provenance", "topic_id", "chunk_id", "document_id",
                  "revision", "page_from", "page_to", "stable_id", "title", "date", "site_id",
                  "who", "excerpt", "url", "fetched_at", "snapshot_s3_key")
_ACTION_FIELDS = ("seq", "type", "target_stable_id", "target_snapshot", "proposed",
                  "rationale", "refs", "verifier")
_ACTION_JSONB = ("target_snapshot", "proposed")

_PACK_COLS = ("p.id, p.company_id, p.site_id, p.created_by, p.title, p.instruction, p.reading, "
              "p.conclusion, p.state, p.failure, p.heartbeat_at, p.run_trace, p.models, "
              "p.as_of, p.archived_at, p.created_at, p.updated_at")
_LIST_COLS = ("p.id, p.company_id, p.site_id, p.created_by, p.title, p.instruction, p.state, "
              "p.failure, p.as_of, p.archived_at, p.created_at, p.updated_at")
_ACTION_COLS = ("id, pack_id, seq, type, target_stable_id, target_snapshot, proposed, "
                "rationale, refs, verifier, state, applied_by, applied_at, applied_result, "
                "created_item_stable_id, created_at, updated_at")
_SOURCE_COLS = "id, pack_id, " + ", ".join(_SOURCE_FIELDS) + ", created_at"


class ActivePackExists(Exception):
    """The creator already has a queued or running pack (one active pack per caller).
    org-api answers 409."""


@dataclass(frozen=True)
class PackReader:
    """Who is reading, as plain facts (see module docstring).
    user_id          -- caller users.id
    company_id       -- caller's home company
    company_admin    -- admin/gm of company_id
    site_roles       -- {site_id: membership role} for the caller's LIVE memberships
                        (memberships.caller_site_roles); {} means none, never "no filter".
    cross_company    -- platform_admin: reads its OWN packs on any company's site (only the
                        creator arm; never other people's packs)"""
    user_id: str
    company_id: str
    company_admin: bool = False
    site_roles: dict = field(default_factory=dict)
    cross_company: bool = False


def reader_for(caller: dict, site_roles: dict) -> PackReader:
    """PackReader from org-api's resolved caller (id, company_id, global_role) and
    memberships.caller_site_roles(conn, caller["id"])."""
    role = caller.get("global_role")
    return PackReader(
        user_id=str(caller["id"]),
        company_id=str(caller["company_id"]),
        company_admin=(acl.resolve_scope(role) == "ALL" and not acl.is_cross_company(role)),
        site_roles={str(k): v for k, v in (site_roles or {}).items()},
        cross_company=acl.is_cross_company(role),
    )


# ------------------------------------------------------------------------- run lifecycle

def create_queued(conn, *, site_id, created_by, instruction) -> dict | None:
    """Insert a queued pack for one site. company_id is the SITE's company (D4), read from
    `sites` here -- never taken from the caller. Returns the pack row, None when the site
    does not exist. Raises ActivePackExists when `created_by` already has a queued/running
    pack (the caller's transaction stays usable). Site scope/visibility checks are the
    endpoint's (§7): this function trusts that `site_id` is in the caller's reach."""
    try:
        with conn.transaction():
            return conn.cursor(row_factory=dict_row).execute(
                "INSERT INTO action_packs AS p (company_id, site_id, created_by, instruction, "
                "heartbeat_at) "
                "SELECT s.company_id, s.id, %s, %s, now() FROM sites s WHERE s.id = %s "
                f"RETURNING {_PACK_COLS}",
                (created_by, instruction, site_id)).fetchone()
    except psycopg.errors.UniqueViolation as e:
        if e.diag.constraint_name == _ONE_ACTIVE_INDEX:
            raise ActivePackExists(str(created_by)) from e
        raise


def claim(conn, pack_id) -> bool:
    """Compare-and-swap queued -> running (S1). True for exactly one caller: a second S3
    delivery (or a concurrent one, which waits on the row lock and re-checks) gets False."""
    row = conn.execute(
        "UPDATE action_packs SET state='running', heartbeat_at=now(), updated_at=now() "
        "WHERE id=%s AND state='queued' RETURNING id", (pack_id,)).fetchone()
    return row is not None


def heartbeat(conn, pack_id) -> str | None:
    """Refresh heartbeat_at while running. Returns the pack's current state either way
    ('cancelled' tells the worker to stop), None when the pack does not exist."""
    row = conn.execute(
        "UPDATE action_packs SET heartbeat_at=now() WHERE id=%s AND state='running' "
        "RETURNING state", (pack_id,)).fetchone()
    if row is None:
        row = conn.execute("SELECT state FROM action_packs WHERE id=%s", (pack_id,)).fetchone()
    return row[0] if row else None


def fail(conn, pack_id, reason, run_trace=None) -> bool:
    """queued/running -> failed with a reason code. Never overwrites ready/cancelled/failed.
    `run_trace` (counts only, §4.5), when given, replaces the stored trace."""
    if reason not in FAILURE_REASONS:
        raise ValueError(f"unknown failure reason: {reason!r}")
    row = conn.execute(
        "UPDATE action_packs SET state='failed', failure=%s, "
        "run_trace=COALESCE(%s, run_trace), updated_at=now() "
        "WHERE id=%s AND state IN ('queued','running') RETURNING id",
        (reason, Jsonb(run_trace) if run_trace is not None else None, pack_id)).fetchone()
    return row is not None


def cancel(conn, pack_id) -> bool:
    """queued/running -> cancelled. False when the pack is already final (or missing)."""
    row = conn.execute(
        "UPDATE action_packs SET state='cancelled', updated_at=now() "
        "WHERE id=%s AND state IN ('queued','running') RETURNING id", (pack_id,)).fetchone()
    return row is not None


def sweep_stale(conn, older_than: dt.timedelta) -> int:
    """S9: fail (reason 'internal') every queued or running pack whose heartbeat_at is older
    than `older_than` -- a worker that died mid-run, or a request that never got claimed.
    Returns how many packs were failed."""
    cur = conn.execute(
        "UPDATE action_packs SET state='failed', failure='internal', updated_at=now() "
        "WHERE state IN ('queued','running') AND heartbeat_at < now() - %s",
        (older_than,))
    return cur.rowcount


# ------------------------------------------------------------------------- store

def _check_keys(obj, allowed, what):
    extra = set(obj) - set(allowed)
    if extra:
        raise ValueError(f"unknown {what} keys: {sorted(extra)}")


def _validate_payload(payload):
    _check_keys(payload, _PACK_FIELDS + ("sources", "actions"), "pack")
    sources = payload.get("sources") or []
    actions = payload.get("actions") or []
    for s in sources:
        _check_keys(s, _SOURCE_FIELDS, "source")
    for a in actions:
        _check_keys(a, _ACTION_FIELDS, "action")
    known = {s.get("ref") for s in sources}
    for a in actions:
        missing = set(a.get("refs") or []) - known
        if missing:
            raise ValueError(f"action seq {a.get('seq')} cites refs with no source: "
                             f"{sorted(missing)}")
    for claim_ in payload.get("conclusion") or []:
        missing = set(claim_.get("refs") or []) - known
        if missing:
            raise ValueError(f"conclusion cites refs with no source: {sorted(missing)}")
    return sources, actions


def _json(v):
    return Jsonb(v) if v is not None else None


def store_result(conn, pack_id, payload: dict) -> str:
    """Write a finished run in ONE transaction: the pack fields (state -> ready), its sources
    and its actions. Only from `running`: a cancelled/failed/ready (or missing) pack is not
    overwritten and nothing is written -> 'refused'. Returns 'ok' on success.

    Raises ValueError for a malformed payload (unknown keys, refs with no source) before any
    write, and lets a database error (a CHECK / UNIQUE violation in a source or action)
    propagate after rolling back the savepoint -- the pack stays `running` with no rows; the
    caller then fails it ('internal')."""
    sources, actions = _validate_payload(payload)
    with conn.transaction():
        row = conn.execute(
            "UPDATE action_packs SET state='ready', title=%s, reading=%s, conclusion=%s, "
            "run_trace=%s, models=%s, as_of=%s, updated_at=now() "
            "WHERE id=%s AND state='running' RETURNING id",
            (payload.get("title"), payload.get("reading"), _json(payload.get("conclusion")),
             _json(payload.get("run_trace")), _json(payload.get("models")),
             payload.get("as_of"), pack_id)).fetchone()
        if row is None:
            return "refused"
        if sources:
            cols = ", ".join(_SOURCE_FIELDS)
            marks = ", ".join(["%s"] * (len(_SOURCE_FIELDS) + 1))
            with conn.cursor() as cur:
                cur.executemany(
                    f"INSERT INTO action_pack_sources (pack_id, {cols}) VALUES ({marks})",
                    [(pack_id, *(s.get(k) for k in _SOURCE_FIELDS)) for s in sources])
        if actions:
            cols = ", ".join(_ACTION_FIELDS)
            marks = ", ".join(["%s"] * (len(_ACTION_FIELDS) + 1))
            with conn.cursor() as cur:
                cur.executemany(
                    f"INSERT INTO action_pack_actions (pack_id, {cols}) VALUES ({marks})",
                    [(pack_id, *((_json(a.get(k)) if k in _ACTION_JSONB else a.get(k))
                                 for k in _ACTION_FIELDS)) for a in actions])
    return "ok"


# ------------------------------------------------------------------------- actions / archive

def set_action_state(conn, pack_id, action_id, new_state, applied_by=None,
                     applied_result=None, created_item_stable_id=None) -> dict | None:
    """Move one action out of `proposed` (the only legal source state). applied_by /
    applied_at / applied_result record who resolved it and how -- for dismissed/stale/failed
    too (stale: both values; failed: the error). Returns the updated action row, None when
    the action is not in this pack or is not `proposed` (applied is terminal; a stale action
    cannot be applied). Raises ValueError for a state that is never a target."""
    allowed = [src for src, targets in ACTION_TRANSITIONS.items() if new_state in targets]
    if not allowed:
        raise ValueError(f"illegal action target state: {new_state!r}")
    with conn.transaction():
        return conn.cursor(row_factory=dict_row).execute(
            "UPDATE action_pack_actions SET state=%s, applied_by=%s, applied_at=now(), "
            "applied_result=%s, created_item_stable_id=COALESCE(%s, created_item_stable_id), "
            "updated_at=now() "
            "WHERE id=%s AND pack_id=%s AND state = ANY(%s) "
            f"RETURNING {_ACTION_COLS}",
            (new_state, applied_by, _json(applied_result), created_item_stable_id,
             action_id, pack_id, allowed)).fetchone()


def archive(conn, pack_id, archived: bool) -> bool:
    """Set (archived=True) or clear archived_at. False when the pack does not exist."""
    row = conn.execute(
        "UPDATE action_packs SET archived_at = CASE WHEN %s THEN COALESCE(archived_at, now()) "
        "ELSE NULL END, updated_at=now() WHERE id=%s RETURNING id",
        (bool(archived), pack_id)).fetchone()
    return row is not None


# ------------------------------------------------------------------------- reads

def _reader_clause(reader: PackReader):
    """(sql, params) -- the one read predicate over alias `p` (module docstring)."""
    managed = [sid for sid, role in reader.site_roles.items() if role in _MANAGER_ROLES]
    sql = (
        "((p.created_by = %s AND (%s OR p.company_id = %s OR EXISTS ("
        "   SELECT 1 FROM memberships m WHERE m.user_id = %s AND m.site_id = p.site_id "
        "   AND m.archived_at IS NULL)))"
        " OR (%s AND p.company_id = %s)"
        " OR (p.company_id = %s AND p.site_id = ANY(%s::uuid[])))")
    params = (reader.user_id, bool(reader.cross_company), reader.company_id, reader.user_id,
              bool(reader.company_admin), reader.company_id,
              reader.company_id, managed)
    return sql, params


def _zero_counts(counts):
    out = dict.fromkeys(ACTION_STATES, 0)
    out.update(counts or {})
    return out


def list_for_reader(conn, reader: PackReader, *, state=None, site_id=None, archived=False,
                    limit=50) -> list[dict]:
    """Packs this reader may read, newest first, in the light list shape (no conclusion /
    reading / trace) plus `action_counts` = {action state: n} over all five states.
    Filters: state (one of STATES), site_id, archived (False = not archived [default],
    True = archived only, None = both)."""
    if state is not None and state not in STATES:
        raise ValueError(f"unknown pack state: {state!r}")
    where, params = _reader_clause(reader)
    clauses, extra = [where], list(params)
    if state is not None:
        clauses.append("p.state = %s")
        extra.append(state)
    if site_id is not None:
        clauses.append("p.site_id = %s")
        extra.append(site_id)
    if archived is None:
        pass
    elif archived:
        clauses.append("p.archived_at IS NOT NULL")
    else:
        clauses.append("p.archived_at IS NULL")
    extra.append(int(limit))
    rows = conn.cursor(row_factory=dict_row).execute(
        f"SELECT {_LIST_COLS}, "
        "(SELECT jsonb_object_agg(c.state, c.n) FROM (SELECT a.state, count(*) AS n "
        "   FROM action_pack_actions a WHERE a.pack_id = p.id GROUP BY a.state) c) "
        "  AS action_counts "
        "FROM action_packs p WHERE " + " AND ".join(clauses) +
        " ORDER BY p.created_at DESC, p.id DESC LIMIT %s",
        extra).fetchall()
    for r in rows:
        r["action_counts"] = _zero_counts(r["action_counts"])
    return rows


def get_for_reader(conn, reader: PackReader, pack_id) -> dict | None:
    """The full pack plus `sources` (by ref) and `actions` (by seq), or None when it does not
    exist or this reader may not read it (the same answer, so existence never leaks).
    Read-time hiding of removed/invisible sources (S13) is the endpoint's job."""
    where, params = _reader_clause(reader)
    cur = conn.cursor(row_factory=dict_row)
    pack = cur.execute(
        f"SELECT {_PACK_COLS} FROM action_packs p WHERE p.id = %s AND {where}",
        (pack_id, *params)).fetchone()
    if pack is None:
        return None
    pack["sources"] = cur.execute(
        f"SELECT {_SOURCE_COLS} FROM action_pack_sources WHERE pack_id=%s ORDER BY ref",
        (pack_id,)).fetchall()
    pack["actions"] = cur.execute(
        f"SELECT {_ACTION_COLS} FROM action_pack_actions WHERE pack_id=%s ORDER BY seq",
        (pack_id,)).fetchall()
    return pack
