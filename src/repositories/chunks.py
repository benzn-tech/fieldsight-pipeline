from psycopg.rows import dict_row
from psycopg.types.json import Jsonb
from repositories.search_sql import build_latest_date_sql, build_search_sql  # re-export

__all__ = ["build_search_sql", "build_latest_date_sql", "insert_chunk", "search_chunks",
           "latest_visible_date",
           "delete_chunks_for_source", "delete_chunks_for_topic",
           "archive_chunks_for_session", "restore_chunks_for_batch",
           "SESSION_CHUNK_PREDICATE"]

# Which chunks belong to one recording.
#
# TWO arms, and the second is the one that was missing. `topic_id` covers the windows that
# `chunk_transcripts` managed to bucket under a topic. It does NOT cover the "unassigned"
# bucket -- turns that fell outside every topic's `time_range +- 120s` get `topic_id =
# NULL`, and that bucket holds verbatim speech. A topic-only rule leaves it searchable,
# which is exactly how a deleted recording stayed findable.
#
# The second arm reads `metadata.source_files`, which `_window_metadata` writes on EVERY
# window (`chunking.py`), assigned or not. Those are transcript filenames and they carry
# the session id, so the match is exact rather than inferred -- no time arithmetic, none of
# the `time_range` guessing the pipeline forbids.
# The `session_base` bind is a LIKE PATTERN, not a literal: `%` and `_` in it are wildcards.
# It arrives from the request body, so it must be escaped by `_escape_like` before binding —
# `sessionBase='%'` made this `LIKE '%%%'`, which is every row in the table.
SESSION_CHUNK_PREDICATE = (
    "(topic_id = ANY(%(topic_ids)s::uuid[]) OR EXISTS ("
    "  SELECT 1 FROM jsonb_array_elements_text(COALESCE(metadata->'source_files', '[]'::jsonb)) f"
    "  WHERE f LIKE '%%' || %(session_base)s || '%%' ESCAPE '\\'))"
)

# `report_chunks` is shared by every tenant and `site_id` is its only route to one:
# NOT NULL REFERENCES sites(id) (0004_report_chunks.sql:5), and `sites` carries company_id.
# Without this, a predicate built from caller input reaches every customer's rows.
COMPANY_SCOPE = " AND site_id IN (SELECT id FROM sites WHERE company_id = %(company_id)s)"


def _escape_like(value) -> str:
    """Escape LIKE wildcards in caller input. Session ids are '_'-joined for legacy
    recordings, so `_` is literal data here and not a single-character wildcard — the same
    reasoning as `repositories.topics._escape_like`, kept local so this module does not
    import a sibling repository for one helper."""
    return str(value).replace("\\", "\\\\").replace("%", r"\%").replace("_", r"\_")


def insert_chunk(conn, site_id, report_date, chunk_type, chunk_text, embedding, *,
                 user_id=None, source_s3_key=None, topic_id=None, metadata=None) -> dict:
    return conn.cursor(row_factory=dict_row).execute(
        "INSERT INTO report_chunks (site_id, user_id, source_s3_key, topic_id, report_date, "
        "chunk_type, chunk_text, embedding, metadata) "
        # embedding cast to %s::vector for consistency with search_sql's %(q)s::vector; with the
        # A bound Python list arrives as float8[] (register_vector only adds
        # numpy dumpers), and pgvector casts double precision[] -> vector; a
        # bound '[...]' string casts text -> vector. Both callers work.
        "VALUES (%s,%s,%s,%s,%s,%s,%s,%s::vector,%s) "
        "RETURNING id, site_id, topic_id, chunk_type, report_date, created_at",
        (site_id, user_id, source_s3_key, topic_id, report_date, chunk_type,
         chunk_text, embedding, Jsonb(metadata or {})),
    ).fetchone()


def search_chunks(conn, query_embedding, accessible_site_ids, k=5,
                  date_from=None, date_to=None, author_ids=None) -> list[dict]:
    return conn.cursor(row_factory=dict_row).execute(
        build_search_sql(),
        {"q": query_embedding, "site_ids": list(accessible_site_ids), "k": k,
         "date_from": date_from, "date_to": date_to,
         "author_ids": list(author_ids) if author_ids is not None else None},
    ).fetchall()


def latest_visible_date(conn, accessible_site_ids, *, author_ids=None, on_or_before=None):
    """The most recent report_date this caller can see, or None.

    Used only by Ask's widening step, so that "nothing yesterday" becomes "the
    27th, and here is what it said" rather than an empty answer. Returns an ISO
    string: it goes straight back into `search_chunks`'s date params and into
    the response's scope, and a `date` object would need converting at both.
    """
    row = conn.cursor(row_factory=dict_row).execute(
        build_latest_date_sql(),
        {"site_ids": list(accessible_site_ids),
         "author_ids": list(author_ids) if author_ids is not None else None,
         "on_or_before": on_or_before},
    ).fetchone()
    latest = (row or {}).get("latest")
    return latest.isoformat() if latest is not None else None


def delete_chunks_for_source(conn, source_s3_key) -> int:
    """Delete report_chunks rows produced from one source report.

    Keyed on source_s3_key — the only key that is UNIQUE per report and
    immune to identity-resolution drift. A (site, date, user_id) scope key
    was tried first and failed review: two same-site/same-date reports whose
    user bridge both miss (user_id NULL — real case: MPI1 + MPI2 share
    primary_site 'mpi') would silently delete each other's rows, and a
    remediation rerun after fixing a mapping would duplicate instead of
    replace (Fable Phase 4a review C1/I1)."""
    cur = conn.execute(
        "DELETE FROM report_chunks WHERE source_s3_key=%s",
        (source_s3_key,),
    )
    return cur.rowcount


def delete_chunks_for_topic(conn, topic_id) -> int:
    """Delete report_chunks rows for one topic (spec §5.3, D6 per-topic
    re-index). Sibling of delete_chunks_for_source, keyed on the durable
    topic_id that lambda_ingest stamps onto each chunk (topic_seq_to_id).
    Used by the reindex apply step: delete this topic's chunks, then
    re-insert the freshly-embedded corrected chunks."""
    cur = conn.execute(
        "DELETE FROM report_chunks WHERE topic_id=%s",
        (topic_id,),
    )
    return cur.rowcount


def archive_chunks_for_session(conn, session_base, topic_ids, batch_id, *,
                               company_id=None) -> int:
    """Move one recording's search index out of `report_chunks`. Returns the row count.

    MOVE, not filter. The read-time predicate cannot reach these rows: every chunk is
    stamped `source_s3_key = reports/…`, so the tombstone's source arm never matches, and
    the unassigned transcript windows have `topic_id = NULL`, where the topic arm is
    trivially true. Both arms miss, and the missed rows are the verbatim ones.

    MOVE, not delete, because the delete has to be reversible. Rebuilding a chunk means
    re-embedding it, and the vectors arrive in a sidecar keyed by sha256 of the text --
    long gone by the time anyone undeletes. Carrying the row across is exact and cannot
    fail halfway.

    `topic_ids` may be empty; the session_base arm still applies. Logging the count is the
    caller's job and it must log zero too -- "archived nothing" and "never ran" are
    otherwise the same observation, which is how this defect survived a review that
    explicitly asked about search.
    """
    base = str(session_base or "").strip()
    if not base:
        # An empty base makes the LIKE arm `'%' || '' || '%'` — every row with any
        # source_files entry. The endpoint checks this too; the guard belongs where the
        # damage would happen, not only where the request arrives.
        raise ValueError("session_base is required: an empty one selects every chunk")
    if not company_id:
        raise ValueError("company_id is required: report_chunks is shared by every tenant")

    params = {"topic_ids": list(topic_ids or []), "session_base": _escape_like(base),
              "batch_id": batch_id, "company_id": company_id}
    conn.execute(
        f"INSERT INTO report_chunks_archive "
        f"SELECT *, %(batch_id)s::uuid, now() FROM report_chunks "
        f"WHERE {SESSION_CHUNK_PREDICATE}{COMPANY_SCOPE}", params)
    cur = conn.execute(
        f"DELETE FROM report_chunks WHERE {SESSION_CHUNK_PREDICATE}{COMPANY_SCOPE}", params)
    return cur.rowcount


def restore_chunks_for_batch(conn, batch_id) -> int:
    """Put one delete batch's chunks back. Returns the row count.

    The inverse of `archive_chunks_for_session`, keyed on the batch so one undelete
    restores exactly what one delete removed — the same rule the redaction rows follow.

    Column list is explicit here rather than `SELECT *`: the archive carries two extra
    columns (`batch_id`, `archived_at`) that `report_chunks` does not have, so a bare
    `INSERT INTO report_chunks SELECT *` would fail on column count. It failing loudly
    would be fine; it silently mapping the wrong columns if the shapes ever lined up
    would not.
    """
    cols = ("id, site_id, user_id, source_s3_key, topic_id, report_date, "
            "chunk_type, chunk_text, embedding, metadata, created_at")
    # `topic_id` is resolved through `topics` instead of copied, and the reason is a clock:
    # `report_chunks.topic_id` is `REFERENCES topics(id) ON DELETE SET NULL`, but the
    # archive is `LIKE report_chunks`, which copies no foreign keys. While a chunk waits in
    # the archive the nightly ingest hard-deletes and rebuilds that day's topics
    # (`delete_topics_for_source`, whose own comment says "always"), and nothing nulls the
    # archived copy. Re-inserting it then violates the FK and fails the whole undelete —
    # `revert_batch` with it. Delete works tonight; restore would break one nightly run
    # later, which is the worst possible schedule for a promise of reversibility.
    #
    # The scalar subquery yields NULL for a topic that is gone: not a workaround, but
    # exactly what ON DELETE SET NULL would have done had the row never left the table.
    select_cols = ("a.id, a.site_id, a.user_id, a.source_s3_key, "
                   "(SELECT t.id FROM topics t WHERE t.id = a.topic_id), "
                   "a.report_date, a.chunk_type, a.chunk_text, a.embedding, "
                   "a.metadata, a.created_at")
    conn.execute(
        f"INSERT INTO report_chunks ({cols}) "
        f"SELECT {select_cols} FROM report_chunks_archive a WHERE a.batch_id=%s "
        f"ON CONFLICT (id) DO NOTHING", (batch_id,))
    cur = conn.execute(
        "DELETE FROM report_chunks_archive WHERE batch_id=%s", (batch_id,))
    return cur.rowcount
