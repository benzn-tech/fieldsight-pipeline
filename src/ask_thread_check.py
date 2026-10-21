"""Ask threads: re-check stored answers against the caller's access now (D5).

Spec docs/superpowers/specs/2026-10-22-ask-conversation-history-design.md section 6.

A stored answer is a frozen copy of record content, so deletion and access changes
must reach it when the thread is opened. `withheld_turns` evaluates each stored
turn's source manifest with the primitives every search mode uses -- the graded
`scope.visible_scope`, the recording tombstones, and search's own report_chunks
visibility predicate -- and returns the turns to withhold. One query per kind for
the whole thread; any failure withholds every turn that has a record source (fail
closed).

Nothing here logs a source key, a document id or a folder: counts only.
"""
import logging
import re

from psycopg.rows import dict_row

from deleted_predicates import DELETED_SOURCE_PREDICATE
from repositories import redactions, scope, search_sql

logger = logging.getLogger()

_UUID = re.compile(r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-"
                   r"[0-9a-fA-F]{12}$")

# Rule 2: a key is live when one report_chunks row carries it AND passes search's own
# visibility rule (site pin, author narrowing, both tombstone arms, supersession,
# family) -- imported, never copied, so a row search would hide cannot keep an
# answer alive. By source key, never by chunk id: re-ingest deletes and re-inserts
# a key's rows, so ids churn. No date bound.
_LIVE_KEYS_SQL = ("SELECT DISTINCT c.source_s3_key FROM report_chunks c "
                  "WHERE c.source_s3_key = ANY(%(keys)s) AND "
                  + search_sql._visibility_predicate("c", "TRUE"))

# Rule 1: the keys a recording tombstone covers (prefix match). The same predicate
# the chunk arm carries, over the manifest's keys, so a tombstone withholds even
# while a stale chunk row is still there.
_TOMBSTONED_KEYS_SQL = ("SELECT k.source_s3_key "
                        "FROM unnest(%(keys)s::text[]) AS k(source_s3_key) "
                        "WHERE NOT " + DELETED_SOURCE_PREDICATE.format(alias="k"))

# Rule 3: the document still exists (not soft-deleted) in a site the caller sees.
# The document's site is read from the row, never from the manifest.
_LIVE_DOCS_SQL = ("SELECT d.id::text AS id FROM project_documents d "
                  "WHERE d.id = ANY(%(ids)s::uuid[]) AND d.deleted_at IS NULL "
                  "AND d.site_id = ANY(%(site_ids)s::uuid[])")


def _manifest(t):
    """(record keys, document ids, report days) of one stored turn, or None when the
    turn carries no manifest at all."""
    src = t.get("sources") if isinstance(t, dict) else None
    if not isinstance(src, dict):
        return None
    keys = [k for k in src.get("record_keys") or [] if isinstance(k, str) and k]
    docs = [str(d["document_id"]) for d in src.get("documents") or []
            if isinstance(d, dict) and d.get("document_id") is not None]
    days = [(d["folder"], d["report_date"]) for d in src.get("report_days") or []
            if isinstance(d, dict) and isinstance(d.get("folder"), str)
            and isinstance(d.get("report_date"), str)]
    return keys, docs, days


def _rows(conn, sql, params):
    return conn.cursor(row_factory=dict_row).execute(sql, params).fetchall()


def withheld_turns(conn, caller, turns) -> set:
    """The `turn_id`s to withhold from `caller` now (spec §6 rules 1-4). A turn with
    no record or document source (web only) is never withheld; a turn with no
    manifest at all always is."""
    manifests = {}
    for t in turns or []:
        if isinstance(t, dict) and t.get("turn_id"):
            manifests[t["turn_id"]] = _manifest(t)
    sourced = {tid for tid, m in manifests.items() if m is None or any(m)}
    if not sourced:
        return set()
    all_keys = sorted({k for m in manifests.values() if m for k in m[0]})
    all_docs = sorted({d for m in manifests.values() if m for d in m[1]})
    all_days = sorted({d for m in manifests.values() if m for d in m[2]})
    try:
        sc = scope.visible_scope(conn, caller)
        site_ids = sorted(str(s) for s in sc["site_ids"])
        author_ids = (sorted(str(a) for a in sc["author_ids"])
                      if sc["author_ids"] is not None else None)
        dead_keys = set()
        if all_keys:
            live = (set() if not site_ids else
                    {r["source_s3_key"] for r in _rows(conn, _LIVE_KEYS_SQL, {
                        "keys": all_keys, "site_ids": site_ids, "author_ids": author_ids,
                        "source_family": None})})
            tomb = {r["source_s3_key"] for r in _rows(conn, _TOMBSTONED_KEYS_SQL,
                                                      {"keys": all_keys})}
            dead_keys = (set(all_keys) - live) | tomb
        dead_docs = set()
        if all_docs:
            ids = [d for d in all_docs if _UUID.match(d)]
            live = (set() if not (ids and site_ids) else
                    {r["id"].lower() for r in _rows(conn, _LIVE_DOCS_SQL,
                                                    {"ids": ids, "site_ids": site_ids})})
            dead_docs = {d for d in all_docs if d.lower() not in live}
        # Rule 4: a day-level report key survives a deleted session inside that day
        # (its own key is never tombstoned), so any recording tombstone for the
        # report's folder and date withholds it.
        dead_days = {day for day in all_days
                     if redactions.deleted_source_prefixes(conn, day[0], day[1])}
    except Exception as exc:  # noqa: BLE001
        logger.error("ask thread check failed, withholding sourced turns: %s",
                     type(exc).__name__)
        return sourced
    out = set()
    for tid, m in manifests.items():
        if m is None:
            out.add(tid)
            continue
        keys, docs, days = m
        if (dead_keys & set(keys)) or (dead_docs & set(docs)) or (dead_days & set(days)):
            out.add(tid)
    logger.info("ask thread check: turns=%d withheld=%d keys=%d docs=%d days=%d",
                len(manifests), len(out), len(all_keys), len(all_docs), len(all_days))
    return out
