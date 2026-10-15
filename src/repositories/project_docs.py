"""Project documents (spec docs/superpowers/specs/2026-10-16-project-documents-design.md).

Documents live in their own two tables and are read only by callers that ask for them
(contamination rule 1). Nothing in the extraction / report / checklist pipeline may import
this module (rule 2; tests/unit/test_project_docs_contamination.py).

Locks: every write that touches a series (insert of a revision, delete, apply) takes
pg_advisory_xact_lock on that series first, so the revision number, the supersede and the
"bring back the predecessor" step see one consistent series (spec ruling 8).
"""
import uuid

from psycopg.rows import dict_row

from repositories import project_docs_sql

DEFAULT_MAX_DISTANCE = 0.55
EMBED_DIM = 1024
MSG_INVALID = "Something went wrong indexing this document -- try again."

_COLS = ("d.id, d.company_id, d.site_id, d.series_id, d.revision, d.title, d.filename, "
         "d.source_key, d.content_type, d.status, d.reason, d.pages, d.chars, d.chunks, "
         "d.uploaded_by, d.created_at, d.requested_at, d.indexed_at, d.superseded_at, "
         "d.deleted_at")
_UPLOADER = "NULLIF(btrim(concat_ws(' ', u.first_name, u.last_name)), '') AS uploaded_by_name"


def _lock_series(conn, series_id):
    conn.execute("SELECT pg_advisory_xact_lock(hashtext(%s))",
                 (f"project_doc_series:{series_id}",))


def get_document(conn, doc_id):
    return conn.cursor(row_factory=dict_row).execute(
        f"SELECT {_COLS} FROM project_documents d WHERE d.id = %s", (str(doc_id),)).fetchone()


def insert_document(conn, *, doc_id, site_id, company_id, title, filename, source_key,
                    content_type, uploaded_by, series_id=None):
    """The new row, or None when `series_id` has no live revision on this site (R8: a new
    revision of a fully deleted series, or of another project's series, is a 404)."""
    cur = conn.cursor(row_factory=dict_row)
    if series_id is None:
        series_id, revision = str(uuid.uuid4()), 1
    else:
        _lock_series(conn, series_id)
        rows = cur.execute(
            "SELECT revision, deleted_at FROM project_documents "
            "WHERE series_id = %s AND site_id = %s FOR UPDATE",
            (str(series_id), str(site_id))).fetchall()
        if not any(r["deleted_at"] is None for r in rows):
            return None
        revision = max(r["revision"] for r in rows) + 1
    cur.execute(
        "INSERT INTO project_documents (id, company_id, site_id, series_id, revision, title, "
        "filename, source_key, content_type, uploaded_by) "
        "VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)",
        (str(doc_id), str(company_id), str(site_id), str(series_id), revision, title,
         filename, source_key, content_type, str(uploaded_by)))
    return get_document(conn, doc_id)


def list_current(conn, site_id):
    """The latest live revision of every series on the project, newest first."""
    return conn.cursor(row_factory=dict_row).execute(
        "SELECT * FROM ("
        f"  SELECT DISTINCT ON (d.series_id) {_COLS}, {_UPLOADER} "
        "  FROM project_documents d LEFT JOIN users u ON u.id = d.uploaded_by "
        "  WHERE d.site_id = %s AND d.deleted_at IS NULL "
        "  ORDER BY d.series_id, d.revision DESC) cur "
        "ORDER BY cur.created_at DESC, cur.id", (str(site_id),)).fetchall()


def list_series(conn, site_id, series_id):
    """A document's live revisions, newest first."""
    return conn.cursor(row_factory=dict_row).execute(
        f"SELECT {_COLS}, {_UPLOADER} FROM project_documents d "
        "LEFT JOIN users u ON u.id = d.uploaded_by "
        "WHERE d.site_id = %s AND d.series_id = %s AND d.deleted_at IS NULL "
        "ORDER BY d.revision DESC", (str(site_id), str(series_id))).fetchall()


def rename(conn, doc_id, title):
    return conn.execute(
        "UPDATE project_documents SET title = %s WHERE id = %s AND deleted_at IS NULL "
        "RETURNING id", (title, str(doc_id))).fetchone() is not None


def mark_pending(conn, doc_id):
    """Retry: back to pending, reason cleared, the 20-minute clock restarted."""
    return conn.execute(
        "UPDATE project_documents SET status = 'pending', reason = NULL, requested_at = now() "
        "WHERE id = %s AND deleted_at IS NULL RETURNING id", (str(doc_id),)).fetchone() is not None


def soft_delete(conn, doc_id):
    """Soft delete. Deleting the CURRENT revision (no live higher revision) clears
    superseded_at on its immediate live predecessor in the same transaction, so the list
    and search agree on what the document now says (R8)."""
    row = get_document(conn, doc_id)
    if row is None or row["deleted_at"] is not None:
        return False
    _lock_series(conn, row["series_id"])
    conn.execute("UPDATE project_documents SET deleted_at = now() "
                 "WHERE id = %s AND deleted_at IS NULL", (str(doc_id),))
    higher = conn.execute(
        "SELECT 1 FROM project_documents WHERE series_id = %s AND revision > %s "
        "AND deleted_at IS NULL", (str(row["series_id"]), row["revision"])).fetchone()
    if higher is None:
        conn.execute(
            "UPDATE project_documents SET superseded_at = NULL WHERE id = ("
            "  SELECT id FROM project_documents WHERE series_id = %s AND revision < %s "
            "  AND deleted_at IS NULL ORDER BY revision DESC LIMIT 1)",
            (str(row["series_id"]), row["revision"]))
    return True


def _valid_chunks(chunks):
    for c in chunks:
        emb = c.get("embedding")
        if (not isinstance(c.get("chunk_text"), str) or not c["chunk_text"].strip()
                or not isinstance(emb, list) or len(emb) != EMBED_DIM
                or not isinstance(c.get("ordinal"), int)):
            return False
    return True


def apply_result(conn, result):
    """Apply one vectors artifact (Task 3's shape) inside the caller's transaction.

    Idempotent: the document's chunks are deleted and re-inserted. Ignored (no write) when
    the document is missing, deleted, or owned by another company than the artifact says.
    `indexed` supersedes every lower revision, pending ones included -- so the artifact of a
    lower revision that arrives AFTER a higher one was indexed lands already superseded
    (apply never clears its own superseded_at; spec ruling 5). An unreadable/error result
    supersedes nothing: the previous revision stays searchable."""
    doc_id = str(result.get("document_id") or "")
    try:
        uuid.UUID(doc_id)
    except ValueError:
        return "missing"
    row = conn.cursor(row_factory=dict_row).execute(
        "SELECT id, company_id, site_id, series_id, revision, deleted_at "
        "FROM project_documents WHERE id = %s", (doc_id,)).fetchone()
    if row is None:
        return "missing"
    _lock_series(conn, row["series_id"])
    row = conn.cursor(row_factory=dict_row).execute(
        "SELECT id, company_id, site_id, series_id, revision, deleted_at "
        "FROM project_documents WHERE id = %s FOR UPDATE", (doc_id,)).fetchone()
    if row["deleted_at"] is not None:
        return "deleted"
    if str(row["company_id"]) != str(result.get("company_id")):
        return "mismatch"
    conn.execute("DELETE FROM project_doc_chunks WHERE document_id = %s", (doc_id,))
    status = result.get("status")
    chunks = result.get("chunks") or []
    if status == "indexed" and (not chunks or not _valid_chunks(chunks)):
        conn.execute("UPDATE project_documents SET status = 'error', reason = %s, chunks = 0, "
                     "indexed_at = NULL WHERE id = %s", (MSG_INVALID, doc_id))
        return "invalid"
    if status == "indexed":
        conn.cursor().executemany(
            "INSERT INTO project_doc_chunks (document_id, site_id, ordinal, heading, page_from, "
            "page_to, chunk_text, embedding) VALUES (%s, %s, %s, %s, %s, %s, %s, %s::vector)",
            [(doc_id, str(row["site_id"]), c["ordinal"], c.get("heading"), c.get("page_from"),
              c.get("page_to"), c["chunk_text"], c["embedding"]) for c in chunks])
        conn.execute(
            "UPDATE project_documents SET status = 'indexed', reason = NULL, pages = %s, "
            "chars = %s, chunks = %s, indexed_at = now() WHERE id = %s",
            (result.get("pages"), result.get("chars"), len(chunks), doc_id))
        conn.execute(
            "UPDATE project_documents SET superseded_at = now() WHERE series_id = %s "
            "AND revision < %s AND superseded_at IS NULL",
            (str(row["series_id"]), row["revision"]))
        return "indexed"
    final = status if status in ("unreadable", "error") else "error"
    conn.execute(
        "UPDATE project_documents SET status = %s, reason = %s, pages = %s, chars = %s, "
        "chunks = 0, indexed_at = NULL WHERE id = %s",
        (final, result.get("reason") or MSG_INVALID, result.get("pages"), result.get("chars"),
         doc_id))
    return final


def search(conn, site_ids, query_vec, query_text, k=8, max_distance=DEFAULT_MAX_DISTANCE):
    """Hybrid search over the visible documents of `site_ids` (the caller's ACL-resolved
    reach; no author narrowing -- documents belong to the project). [] for no sites."""
    site_ids = [str(s) for s in site_ids or []]
    if not site_ids:
        return []
    return conn.cursor(row_factory=dict_row).execute(
        project_docs_sql.build_doc_search_sql(),
        {"q": query_vec, "q_text": query_text or "", "site_ids": site_ids, "k": int(k),
         "max_distance": float(max_distance)}).fetchall()
