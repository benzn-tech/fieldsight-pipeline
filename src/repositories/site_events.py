"""Site events: the decision loop's event hub (migration 0100, spec 2026-11-05 §5).

One row per event a source emits for a site; `seq` is the replay order. Dedupe is a partial
unique index on dedupe_key among rows that are not 'duplicate': a repeat is still stored, as
its own 'duplicate' row, so nothing a source said is lost. Every read and write is scoped by
company_id and site_id except claim_due, the hub's global queue pop."""
import uuid

from psycopg.rows import dict_row
from psycopg.types.json import Jsonb

_COLS = ("event_id, seq, company_id, site_id, source, type, subject_ref, bucket, occurred_at, "
         "received_at, summary, link, payload, chain_id, chain_depth, dedupe_key, status, "
         "rule_id, attempts, not_before, last_error")

# set_status may change only these columns besides status. Names are never taken from the
# caller into SQL: an unknown key is a ValueError before anything runs.
_UPDATABLE = ("rule_id", "attempts", "not_before", "last_error")

_INSERT = (
    "INSERT INTO site_events (event_id, company_id, site_id, source, type, subject_ref, bucket, "
    "occurred_at, received_at, summary, link, payload, chain_id, chain_depth, dedupe_key, "
    "status, rule_id, attempts, not_before, last_error) "
    "VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s) ")


def _params(ev, event_id, dedupe_key, status):
    payload = ev.get("payload")
    return (event_id, ev["company_id"], ev["site_id"], ev["source"], ev["type"],
            ev["subject_ref"], ev.get("bucket") or "", ev.get("occurred_at"),
            ev["received_at"], ev.get("summary"), ev.get("link"),
            Jsonb(payload) if payload is not None else None, ev.get("chain_id"),
            ev.get("chain_depth") or 0, dedupe_key, status, ev.get("rule_id"),
            ev.get("attempts") or 0, ev.get("not_before"), ev.get("last_error"))


def insert(conn, ev):
    """(row, is_new). The caller supplies every field, including status, dedupe_key, bucket and
    a tz-aware received_at/occurred_at; event_id is generated when absent. A dedupe_key that a
    live row already holds is stored again as status 'duplicate' with dedupe_key
    '<key>|dup|<event_id>', and is_new is False.

    ValueError when ev['status'] is 'duplicate': such a row sits outside the partial unique
    index and would bypass dedupe. Only the dedupe path below writes duplicate rows."""
    if ev["status"] == "duplicate":
        raise ValueError("site_events.insert: status 'duplicate' is reserved for the dedupe path")
    event_id = ev.get("event_id") or uuid.uuid4()
    cur = conn.cursor(row_factory=dict_row)
    row = cur.execute(
        _INSERT + "ON CONFLICT (dedupe_key) WHERE status <> 'duplicate' DO NOTHING "
        f"RETURNING {_COLS}",
        _params(ev, event_id, ev["dedupe_key"], ev["status"])).fetchone()
    if row is not None:
        return row, True
    row = cur.execute(
        _INSERT + f"RETURNING {_COLS}",
        _params(ev, event_id, f"{ev['dedupe_key']}|dup|{event_id}", "duplicate")).fetchone()
    return row, False


def claim_due(conn, now, limit=50, *, only_types=None, exclude_types=None):
    """Pop up to `limit` queued events whose not_before is NULL or <= now, lowest seq first,
    and mark them 'running'. FOR UPDATE: a concurrent claimer skips rows another
    transaction holds rather than waiting. attempts is NOT touched (the caller counts
    failures). Rows come back in seq order.

    only_types / exclude_types (sequences of event types) narrow the pop: the hub claims
    run-rule events one per transaction and every other event in batches. An empty
    only_types claims nothing; an empty exclude_types excludes nothing."""
    if only_types is not None and not list(only_types):
        return []
    where, args = "", [now]
    if only_types is not None:
        where += " AND type = ANY(%s)"
        args.append(list(only_types))
    if exclude_types:
        where += " AND NOT (type = ANY(%s))"
        args.append(list(exclude_types))
    args.append(limit)
    rows = conn.cursor(row_factory=dict_row).execute(
        "UPDATE site_events SET status = 'running' "
        "WHERE event_id IN (SELECT event_id FROM site_events "
        "  WHERE status = 'queued' AND (not_before IS NULL OR not_before <= %s)" + where +
        "  ORDER BY seq LIMIT %s FOR UPDATE SKIP LOCKED) "
        f"RETURNING {_COLS}",
        args).fetchall()
    return sorted(rows, key=lambda r: r["seq"])


def set_status(conn, event_id, company_id, site_id, status, **fields):
    """Set status (and any of rule_id, attempts, not_before, last_error). Returns the updated
    row, or None when no row of that tenant has that event_id. ValueError on any other key."""
    bad = sorted(set(fields) - set(_UPDATABLE))
    if bad:
        raise ValueError(f"set_status: fields not updatable: {bad}")
    names = [n for n in _UPDATABLE if n in fields]
    sets = ", ".join(["status = %s"] + [f"{n} = %s" for n in names])
    return conn.cursor(row_factory=dict_row).execute(
        f"UPDATE site_events SET {sets} "
        f"WHERE event_id = %s AND company_id = %s AND site_id = %s RETURNING {_COLS}",
        (status, *[fields[n] for n in names], event_id, company_id, site_id)).fetchone()


def queued_same_subject(conn, company_id, site_id, type, subject_ref):
    """Queued events of this tenant with the same type and subject, in seq order."""
    return conn.cursor(row_factory=dict_row).execute(
        f"SELECT {_COLS} FROM site_events "
        "WHERE company_id = %s AND site_id = %s AND type = %s AND subject_ref = %s "
        "AND status = 'queued' ORDER BY seq",
        (company_id, site_id, type, subject_ref)).fetchall()


def abandoned_running(conn, now, types):
    """'running' events of `types` whose open run (ended_at NULL) has a lease_until before
    `now`: a run-rule attempt whose Lambda died after its run row was committed. Each row is
    the event's columns plus `run_id` (that open run), locked FOR UPDATE SKIP LOCKED so two
    scans never recover the same event."""
    types = list(types or [])
    if not types:
        return []
    cols = ", ".join("e." + c.strip() for c in _COLS.split(","))
    rows = conn.cursor(row_factory=dict_row).execute(
        f"SELECT {cols}, r.run_id AS run_id FROM site_events e "
        "JOIN run_events re ON re.event_id = e.event_id "
        "JOIN event_runs r ON r.run_id = re.run_id "
        "WHERE e.status = 'running' AND e.type = ANY(%s) AND r.ended_at IS NULL "
        "AND r.lease_until IS NOT NULL AND r.lease_until < %s "
        "ORDER BY e.seq FOR UPDATE OF e SKIP LOCKED", (types, now)).fetchall()
    return rows
