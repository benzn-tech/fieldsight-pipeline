from psycopg.rows import dict_row
from psycopg.types.json import Jsonb

from deleted_predicates import LIVE_TOPIC_PREDICATE
from repositories.search_sql import (build_latest_date_sql,  # re-export
                                     build_latest_match_date_sql, build_search_sql,
                                     build_subject_coverage_sql, subject_floor,
                                     subject_params)

__all__ = ["build_search_sql", "build_latest_date_sql", "insert_chunk", "search_chunks",
           "latest_visible_date", "latest_matching_date", "build_latest_match_date_sql",
           "subject_coverage", "build_subject_coverage_sql", "subject_floor",
           "delete_chunks_for_source", "delete_chunks_for_sources", "delete_chunks_for_topic",
           "archive_chunks_for_session", "archive_chunks_for_sources",
           "restore_chunks_for_batch",
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
                  date_from=None, date_to=None, author_ids=None, query_text="",
                  source_family=None) -> list[dict]:
    """Vector + keyword search, unioned (Task 3, 2026-09-20 spec: "a literal
    token is findable"). Every returned row now carries `lexical_hit: bool` --
    True for a row only the keyword arm found, False for a row the vector arm
    found. BOTH carry the real cosine distance to the query embedding (the
    keyword arm used to emit NULL; a real `<=>` value is not fabricated, and
    NULL made rankers score such rows 1.0). Use `lexical_hit`, not
    `distance is None`, to tell the arms apart. A row found by both arms is
    returned once, as the vector copy (build_search_sql's DISTINCT ON).

    `query_text` defaults to "" so every existing caller that does not pass it
    keeps working: `websearch_to_tsquery('simple', '')` returns an empty
    tsquery that matches nothing, which is correct for "no text was given to
    search on" (never matches everything).

    `query_text` is bound VERBATIM into build_search_sql()'s keyword arm as a
    parameter -- this function does no term extraction or OR-joining of its
    own. A caller that wants OR semantics over a natural-language question
    (2026-09-22 fix, "the keyword arm can actually fire") must build that
    string itself, e.g. `lexical_terms.or_query(lexical_terms.lexical_terms(q))`
    -- see lambda_rag_search.py's `_search`, the only production caller that
    receives a raw question rather than an already-literal term.
    """
    return conn.cursor(row_factory=dict_row).execute(
        build_search_sql(),
        {"q": query_embedding, "q_text": query_text, "site_ids": list(accessible_site_ids), "k": k,
         "date_from": date_from, "date_to": date_to,
         "author_ids": list(author_ids) if author_ids is not None else None,
         "source_family": source_family},
    ).fetchall()


def latest_visible_date(conn, accessible_site_ids, *, author_ids=None, on_or_before=None,
                        source_family=None):
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
         "on_or_before": on_or_before,
         "source_family": source_family},
    ).fetchone()
    latest = (row or {}).get("latest")
    return latest.isoformat() if latest is not None else None


def latest_matching_date(conn, accessible_site_ids, *, on_or_before, subject_terms,
                         window_days=92, author_ids=None, source_family=None):
    """`(day, k)` for the window [on_or_before - window_days + 1, on_or_before], or
    None (Ask tools, spec 2026-10-15 §3.1 item 3; day rule from spec 2026-10-20
    §3.2 and Amendment A §8). Callers test `is None`.

    `subject_terms` is `lexical_terms.subject_terms(question)`; each word matches
    its english stem or its exact token (search_sql.subject_term_query). k is the
    highest number of them any visible chunk in the window names, at least
    subject_floor(n) = min(2, n); day is the latest report_date with a chunk at k.
    No words -> None, and no SQL runs. Visibility is the search's own
    (build_latest_match_date_sql reuses _scope_predicate): site pin, author
    narrowing, deletion tombstones, supersession and the source family -- a date
    is a disclosure too.

    The prefilter is served by the GIN expression index; the window bounds the
    rest (spec 2026-10-15 §3.4: shrink the window first if p95 is missed).
    """
    terms = list(subject_terms or [])
    if not accessible_site_ids or not terms:
        return None
    if window_days < 1:
        raise ValueError("window_days must be >= 1")
    from datetime import date, timedelta
    hi = date.fromisoformat(str(on_or_before))
    lo = hi - timedelta(days=window_days - 1)
    row = conn.cursor(row_factory=dict_row).execute(
        build_latest_match_date_sql(len(terms)),
        {**subject_params(terms), "floor": subject_floor(len(terms)),
         "site_ids": list(accessible_site_ids),
         "author_ids": list(author_ids) if author_ids is not None else None,
         "date_from": lo, "date_to": hi,
         "source_family": source_family},
    ).fetchone()
    if not row or row.get("report_date") is None:
        return None
    return row["report_date"].isoformat(), int(row["k"])


def subject_coverage(conn, chunk_ids, subject_terms) -> dict:
    """{lower-case id: k} -- how many subject words each of `chunk_ids` names, by
    the same coverage expression latest_matching_date chose the day by, so a
    row's k means what the day's k meant. No ids or no words -> {}.
    """
    ids = [str(i) for i in chunk_ids if i is not None]
    terms = list(subject_terms or [])
    if not ids or not terms:
        return {}
    rows = conn.cursor(row_factory=dict_row).execute(
        build_subject_coverage_sql(len(terms)),
        {"ids": ids, **subject_params(terms)}).fetchall()
    return {str(r["id"]).lower(): int(r["k"]) for r in rows}


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


def delete_chunks_for_sources(conn, keys, company_id) -> int:
    """Delete report_chunks rows produced from any of these source keys, inside one company.

    The search-index apply replaces a session's chunks (and the chunks of the member
    sessions a group pass supersedes) by their `source_s3_key`. `report_chunks` is shared
    by every tenant and the keys arrive in an S3 object, so the delete is company-scoped
    like the archive: a request can only ever remove its own company's rows."""
    if not company_id:
        raise ValueError("company_id is required: report_chunks is shared by every tenant")
    cur = conn.execute(
        f"DELETE FROM report_chunks WHERE source_s3_key = ANY(%(keys)s){COMPANY_SCOPE}",
        {"keys": list(keys), "company_id": company_id})
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


def archive_chunks_for_sources(conn, keys, batch_id, *, company_id) -> int:
    """Move the SESSION-family chunks of these extraction keys into `report_chunks_archive`.
    Returns the row count.

    Selects by `source_s3_key` only -- the session index stamps its chunks with the
    extraction key, whereas report-family rows carry `reports/...`. Unlike
    `archive_chunks_for_session` this never reaches a report-family row (prod reads that
    family during rollout) nor another session's chunks that merely list this session's
    base in `metadata.source_files` (a group's chunks list its members). The
    `extractions/` guard keeps a malformed key from ever naming a report row.
    Company-scoped like every delete on this shared table."""
    keys = [k for k in (keys or []) if k]
    if not keys:
        raise ValueError("keys is required: an empty list selects nothing and hides a bug")
    if not company_id:
        raise ValueError("company_id is required: report_chunks is shared by every tenant")
    pred = ("source_s3_key = ANY(%(keys)s) AND source_s3_key LIKE 'extractions/%%'"
            + COMPANY_SCOPE)
    params = {"keys": keys, "batch_id": batch_id, "company_id": company_id}
    conn.execute(
        f"INSERT INTO report_chunks_archive "
        f"SELECT *, %(batch_id)s::uuid, now() FROM report_chunks WHERE {pred}", params)
    cur = conn.execute(f"DELETE FROM report_chunks WHERE {pred}", params)
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
    # archive is `LIKE report_chunks`, which copies no foreign keys. Before Track B Task 3,
    # while a chunk waited in the archive the nightly ingest could hard-delete and rebuild
    # that day's topics (`delete_topics_for_source`, whose own comment said "always"), and
    # nothing nulled the archived copy. Re-inserting it then violated the FK and failed the
    # whole undelete — `revert_batch` with it. Delete worked tonight; restore would break one
    # nightly run later, which is the worst possible schedule for a promise of reversibility.
    #
    # Task 3 retargeted the nightly ingest's report-key clear to
    # `supersede_topics_for_source`, which marks a row instead of removing it, so the topic a
    # chunk was archived against normally still exists (superseded, not gone) by the time a
    # restore runs. This resolution stays regardless — belt-and-suspenders for whatever
    # DOES still physically remove a topics row (`delete_topics_for_source[_prefix]` remain
    # in this repo for a caller that means an actual, irreversible delete), so a restore
    # never fails the whole undelete transaction on a dangling FK.
    #
    # The scalar subquery yields NULL for a topic that is gone OR superseded (fix round 1,
    # PR #972 review): "exists" is not enough any more. A chunk can be archived while its
    # topic is live and restored after that SAME topic was later superseded by an unrelated
    # re-extraction (the topic row still exists — Task 3 stopped deleting it) — binding the
    # restored chunk to it would put it right back under `visible_chunks_predicate`'s
    # superseded-topic arm, hidden, exactly the state R21 exists to repair for a supersede's
    # own chunks. `LIVE_TOPIC_PREDICATE` (deleted_predicates — one definition, not an inlined
    # `superseded_at` check that could drift from Task 2's) makes this the same "does the
    # parent still count" test every other live read uses, so a restored chunk with a
    # since-superseded topic comes back unbound rather than invisible.
    select_cols = ("a.id, a.site_id, a.user_id, a.source_s3_key, "
                   f"(SELECT t.id FROM topics t WHERE t.id = a.topic_id "
                   f"AND {LIVE_TOPIC_PREDICATE.format(alias='t')}), "
                   "a.report_date, a.chunk_type, a.chunk_text, a.embedding, "
                   "a.metadata, a.created_at")
    conn.execute(
        f"INSERT INTO report_chunks ({cols}) "
        f"SELECT {select_cols} FROM report_chunks_archive a WHERE a.batch_id=%s "
        f"ON CONFLICT (id) DO NOTHING", (batch_id,))
    cur = conn.execute(
        "DELETE FROM report_chunks_archive WHERE batch_id=%s", (batch_id,))
    return cur.rowcount
