"""Aurora index of which company owns each S3 lake object (migration 0087).

DEPLOY TOLERANCE: `sam deploy` runs before `migrate`, so for about a minute this code runs
against a database without lake_objects. Every public function catches UndefinedTable /
UndefinedColumn under a savepoint, logs a WARNING and returns its documented empty value, so
it never poisons the caller's transaction and never raises into a user's request path.
"""
import logging

from psycopg.rows import dict_row

import lake_keys

logger = logging.getLogger(__name__)

__all__ = ["upsert", "record_best_effort", "upsert_many", "reassign_site", "owners_for_company", "count",
           "unresolved", "list_reports", "report_folders_for_day"]

_COLS = ("s3_key, company_id, site_id, user_id, session_base, folder, day, kind, size_bytes, "
         "source, reason, created_at, updated_at, deleted_at")

# A later write with NULL never erases what an earlier one knew. reason follows company_id:
# a write that supplies a company clears a stale reason, one that does not keeps it.
_UPSERT = (
    "INSERT INTO lake_objects (s3_key, company_id, site_id, user_id, session_base, folder, day, "
    "kind, size_bytes, source, reason) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s) "
    "ON CONFLICT (s3_key) DO UPDATE SET "
    "company_id = COALESCE(EXCLUDED.company_id, lake_objects.company_id), "
    "site_id = COALESCE(EXCLUDED.site_id, lake_objects.site_id), "
    "user_id = COALESCE(EXCLUDED.user_id, lake_objects.user_id), "
    "size_bytes = COALESCE(EXCLUDED.size_bytes, lake_objects.size_bytes), "
    "kind = COALESCE(EXCLUDED.kind, lake_objects.kind), "
    "folder = COALESCE(EXCLUDED.folder, lake_objects.folder), "
    "day = COALESCE(EXCLUDED.day, lake_objects.day), "
    "session_base = COALESCE(EXCLUDED.session_base, lake_objects.session_base), "
    "source = COALESCE(EXCLUDED.source, lake_objects.source), "
    # A row that already has a company never takes a "why it has none" reason from a write
    # that did not supply a company (the COALESCE kept the company, so the reason would lie).
    "reason = CASE WHEN EXCLUDED.company_id IS NOT NULL THEN EXCLUDED.reason "
    "WHEN lake_objects.company_id IS NOT NULL THEN lake_objects.reason "
    "ELSE COALESCE(EXCLUDED.reason, lake_objects.reason) END, "
    "deleted_at = NULL, updated_at = now() "
    # Skip the write (and the updated_at bump) when nothing would change.
    "WHERE lake_objects.deleted_at IS NOT NULL "
    + "".join(f"OR (EXCLUDED.{c} IS NOT NULL AND EXCLUDED.{c} IS DISTINCT FROM lake_objects.{c}) "
              for c in ("company_id", "site_id", "user_id", "size_bytes", "kind", "folder", "day",
                        "session_base", "source"))
    # reason: a write that supplies a company_id owns the reason too (NULL clears a stale one);
    # without a company_id only a non-NULL reason counts.
    + "OR ((EXCLUDED.company_id IS NOT NULL "
      "OR (EXCLUDED.reason IS NOT NULL AND lake_objects.company_id IS NULL)) "
      "AND EXCLUDED.reason IS DISTINCT FROM lake_objects.reason)")

_REASSIGN = ("UPDATE lake_objects SET company_id=%s, reason=NULL, updated_at=now() "
             "WHERE site_id=%s AND company_id IS DISTINCT FROM %s")


def _is_missing_schema(exc):
    import psycopg.errors as pe
    return isinstance(exc, (pe.UndefinedColumn, pe.UndefinedTable))


def _run(conn, fn, empty):
    """Run fn() in a savepoint; a missing table/column yields `empty` plus a WARNING."""
    try:
        with conn.transaction():
            return fn()
    except Exception as exc:  # noqa: BLE001
        if not _is_missing_schema(exc):
            raise
        logger.warning("lake_objects missing (migration 0087 not applied yet); returning %r",
                       empty)
        return empty


def _params(key, company_id, site_id, user_id, kind, folder, day, session_base, size_bytes,
            source, reason):
    return (key, company_id, site_id, user_id, session_base, folder, day, kind, size_bytes,
            source, reason)


def upsert(conn, key, *, company_id, site_id=None, user_id=None, kind, folder=None, day=None,
           session_base=None, size_bytes=None, source, reason=None) -> bool:
    """Record one object. NULL arguments never erase an already-known owner. False when the
    table is missing (the caller keeps going; the drain/backfill is the safety net).
    `reason` (e.g. "split_day", ruling C2) explains a deliberate company_id NULL."""
    p = _params(key, company_id, site_id, user_id, kind, folder, day, session_base, size_bytes,
                source, reason)

    def go():
        conn.execute(_UPSERT, p)
        return True
    return _run(conn, go, False)


def record_best_effort(conn, key, *, company_id, site_id=None, user_id=None, source="upload",
                       **fields) -> bool:
    """Record the owner of a key a request path just minted or wrote. NEVER raises and never
    touches the caller's transaction: the upload routes are synchronous with no retry (BUG-43),
    so a failed index write must not fail or slow the request. Runs in its own savepoint; any
    exception (not only a missing table) is logged at WARNING and reported as False. The
    drain/backfill is the safety net for a row that was not recorded.

    kind/folder/day/session_base come from lake_keys.parse(key); `fields` may override them
    (or add size_bytes/reason)."""
    try:
        parsed = lake_keys.parse(key) or {}
        kw = {"kind": parsed.get("kind") or key.split("/", 1)[0],
              "folder": parsed.get("folder"), "day": parsed.get("day"),
              "session_base": parsed.get("session_base")}
        kw.update(fields)
        with conn.transaction():
            return bool(upsert(conn, key, company_id=company_id,
                               site_id=site_id or parsed.get("site_id"),
                               user_id=user_id, source=source, **kw))
    except Exception:  # noqa: BLE001 -- the contract is "never raises"
        logger.warning("lake_objects: could not record %s (continuing)", key, exc_info=True)
        return False


def upsert_many(conn, rows, *, on_missing=0):
    """rows: dicts with the keyword names of upsert(), s3 key under `key`. Returns the number of
    rows actually written (inserted or changed); unchanged rows are skipped by the guard and
    not counted. `on_missing` is what a missing table returns: the default 0 is
    indistinguishable from "nothing changed", so the drain passes None and must not delete a
    manifest object on it."""
    params = [_params(r["key"], r.get("company_id"), r.get("site_id"), r.get("user_id"),
                      r["kind"], r.get("folder"), r.get("day"), r.get("session_base"),
                      r.get("size_bytes"), r["source"], r.get("reason")) for r in rows]
    if not params:
        return 0

    def go():
        with conn.cursor() as cur:
            cur.executemany(_UPSERT, params)
            return cur.rowcount
    return _run(conn, go, on_missing)


def reassign_site(conn, site_id, company_id) -> int:
    """A site changed company: move every row of that site. Rows with no site are untouched."""
    def go():
        cur = conn.execute(_REASSIGN, (company_id, site_id, company_id))
        return cur.rowcount
    return _run(conn, go, 0)


def owners_for_company(conn, company_id, *, after_key=None, limit=1000) -> list:
    """Live rows owned by company_id, keyset-paginated on s3_key."""
    def go():
        cur = conn.cursor(row_factory=dict_row)
        return cur.execute(
            f"SELECT {_COLS} FROM lake_objects WHERE company_id=%s AND deleted_at IS NULL "
            "AND (%s::text IS NULL OR s3_key > %s) ORDER BY s3_key LIMIT %s",
            (company_id, after_key, after_key, limit)).fetchall()
    return _run(conn, go, [])


def count(conn, company_id=None) -> int:
    """Live row count; company_id None counts every row."""
    def go():
        row = conn.execute(
            "SELECT count(*) FROM lake_objects WHERE deleted_at IS NULL "
            "AND (%s::uuid IS NULL OR company_id=%s)", (company_id, company_id)).fetchone()
        return int(row["count"] if isinstance(row, dict) else row[0])
    return _run(conn, go, 0)


def unresolved(conn, limit=100, after_key=None) -> list:
    """Live rows with company_id NULL (unresolved or split_day), keyset-paginated."""
    def go():
        cur = conn.cursor(row_factory=dict_row)
        return cur.execute(
            f"SELECT {_COLS} FROM lake_objects WHERE company_id IS NULL AND deleted_at IS NULL "
            "AND (%s::text IS NULL OR s3_key > %s) ORDER BY s3_key LIMIT %s",
            (after_key, after_key, limit)).fetchall()
    return _run(conn, go, [])


def _ids(company_ids):
    return None if company_ids is None else [str(c) for c in company_ids]


def list_reports(conn, *, company_ids=None, folders=None, limit=200) -> list:
    """Report objects (kind='report'), newest day first, optionally limited to companies/folders."""
    def go():
        cur = conn.cursor(row_factory=dict_row)
        ids = _ids(company_ids)
        return cur.execute(
            f"SELECT {_COLS} FROM lake_objects WHERE kind='report' AND deleted_at IS NULL "
            "AND (%s::uuid[] IS NULL OR company_id = ANY(%s::uuid[])) "
            "AND (%s::text[] IS NULL OR folder = ANY(%s::text[])) "
            "ORDER BY day DESC NULLS LAST, s3_key LIMIT %s",
            (ids, ids, folders, folders, limit)).fetchall()
    return _run(conn, go, [])


def report_folders_for_day(conn, day, company_ids=None) -> list:
    """Distinct folders that have a report object on `day`."""
    def go():
        ids = _ids(company_ids)
        rows = conn.execute(
            "SELECT DISTINCT folder FROM lake_objects WHERE kind='report' AND day=%s "
            "AND deleted_at IS NULL AND folder IS NOT NULL "
            "AND (%s::uuid[] IS NULL OR company_id = ANY(%s::uuid[])) ORDER BY folder",
            (day, ids, ids)).fetchall()
        return [r["folder"] if isinstance(r, dict) else r[0] for r in rows]
    return _run(conn, go, [])
