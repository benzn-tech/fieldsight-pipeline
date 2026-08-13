"""Redaction tombstones for life-conversation separation (2026-07-21 spec §4).
A redaction soft-excludes a topic from company-tier reads without deleting it;
reverted_at IS NULL = active. company_excluded_topic_ids is the single choke
point every company-tier read routes through (rollup, RAG embed/reindex)."""
from psycopg.rows import dict_row

_COLS = ("id, company_id, target_type, target_id, reason, actor_user_id, "
         "actor_role, scope, created_at, reverted_at, batch_id, target_key")


def create_redaction(conn, company_id, target_id, reason, actor_user_id, actor_role,
                     *, target_type="topic", scope="analysis",
                     batch_id=None, target_key=None, skip_if_already_deleted=False):
    """A soft, reversible, audited exclusion.

    `scope='analysis'` hides a topic from company-tier reads only — the
    life-conversation separation this table was built for. `scope='deleted'` is the
    customer-facing delete: hidden at EVERY tier.

    `target_key` is the source prefix, and it is what makes a delete survive the
    pipeline. `lambda_ingest` deletes a day's topics and re-inserts them with new
    uuids when the nightly report supersedes the live extraction, so a tombstone
    holding only a topic uuid stops matching within a day and the deleted content
    returns overnight.

    `batch_id` groups one delete action, so one revert can restore exactly what one
    delete hid — the only check that proves the feature is reversible.

    `skip_if_already_deleted` makes the INSERT idempotent against
    `uq_redactions_active_deleted`. Without it, a retried delete — or a customer who
    selects a recording twice, or two recordings whose topics overlap — raises
    UniqueViolation, aborts the whole request, and writes nothing while the customer is
    told the delete failed. It is opt-in because the existing `analysis` redactions
    legitimately repeat and must keep raising nothing and returning their row.

    It returns None when it skipped, so callers can count what they actually hid rather
    than what they attempted.
    """
    conflict = " ON CONFLICT DO NOTHING" if skip_if_already_deleted else ""
    return conn.cursor(row_factory=dict_row).execute(
        f"INSERT INTO redactions (company_id, target_type, target_id, reason, "
        f"actor_user_id, actor_role, scope, batch_id, target_key) "
        f"VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s){conflict} "
        f"RETURNING {_COLS}",
        (company_id, target_type, target_id, reason, actor_user_id, actor_role, scope,
         batch_id, target_key),
    ).fetchone()


def get_redaction(conn, redaction_id):
    return conn.cursor(row_factory=dict_row).execute(
        f"SELECT {_COLS} FROM redactions WHERE id=%s", (redaction_id,)).fetchone()


def revert_redaction(conn, redaction_id, company_id):
    """Un-tombstone (spec §4). Company-guarded; sets reverted_at so audit
    survives. None if missing / wrong company / already reverted."""
    return conn.cursor(row_factory=dict_row).execute(
        f"UPDATE redactions SET reverted_at=now() "
        f"WHERE id=%s AND company_id=%s AND reverted_at IS NULL RETURNING {_COLS}",
        (redaction_id, company_id)).fetchone()


def is_topic_redacted(conn, topic_id) -> bool:
    return conn.cursor(row_factory=dict_row).execute(
        "SELECT 1 FROM redactions WHERE target_type='topic' AND target_id=%s "
        "AND reverted_at IS NULL LIMIT 1", (topic_id,)).fetchone() is not None


def company_excluded_topic_ids(conn, site_ids) -> set:
    """Topic ids a COMPANY-tier read excludes across site_ids: any topic
    classified non_work (auto-held) OR carrying an active redaction. The
    site/self tier does NOT use this. Empty site_ids -> empty set (no query)."""
    if not site_ids:
        return set()
    rows = conn.cursor(row_factory=dict_row).execute(
        "SELECT id FROM topics WHERE site_id = ANY(%s) AND ("
        "  work_class='non_work' "
        "  OR id IN (SELECT target_id FROM redactions "
        "            WHERE target_type='topic' AND reverted_at IS NULL))",
        (list(site_ids),)).fetchall()
    return {r["id"] for r in rows}


def list_active_for_topics(conn, topic_ids) -> dict:
    """Active redaction row keyed by target topic id (UI 'removed' area). {}."""
    if not topic_ids:
        return {}
    return {r["target_id"]: r for r in conn.cursor(row_factory=dict_row).execute(
        f"SELECT {_COLS} FROM redactions WHERE target_type='topic' "
        f"AND target_id = ANY(%s) AND reverted_at IS NULL", (list(topic_ids),)).fetchall()}


# ============================================================
# The two arms of a user-facing delete (spec 2026-08-14)
# ============================================================
# TWO predicates, not one, and a filter carrying only the first is the defect this
# feature was reviewed for:
#
#   * by TOPIC ID   — the rows that exist right now;
#   * by SOURCE KEY — the rows the pipeline will re-create tomorrow. `lambda_ingest`
#     deletes a day's topics and re-inserts them with NEW uuids when the nightly report
#     supersedes the live extraction, so a tombstone naming only a uuid stops matching
#     within a day and the deleted content returns overnight.
#
# `scope = 'deleted'` is load-bearing in both: the `'analysis'` redactions belong to
# life-conversation separation and are supposed to stay visible to the person they belong
# to. Dropping that condition would hide them from everyone.

# The predicate strings moved to `deleted_predicates` — a module with no psycopg import —
# so that `repositories/search_sql.py` can carry the SAME text. Search runs SQL against
# report_chunks and is forbidden from importing psycopg, so the only alternatives were a
# shared pure module or a second copy of the SQL. Re-exported here because the merged read
# paths already import these names from this module.
from deleted_predicates import (  # noqa: E402,F401
    DELETED_TOPIC_PREDICATE,
    DELETED_SOURCE_PREDICATE,
    DELETED_CHUNK_TOPIC_PREDICATE,
    visible_topics_predicate,
    visible_chunks_predicate,
)


def create_recording_tombstone(conn, company_id, source_prefix, reason, actor_user_id,
                               actor_role, *, batch_id=None):
    """Tombstone one recording by its extraction/source prefix.

    `target_id` still has to hold a uuid (0022 declared it NOT NULL and this feature does
    not get to change that for existing rows), so it carries a deterministic uuid5 of the
    prefix: stable across retries, which is what lets the partial unique index do its job.

    Which it can only do if the INSERT declines the conflict instead of raising it, hence
    `skip_if_already_deleted` — a stable uuid5 plus a unique index plus a plain INSERT is a
    guaranteed UniqueViolation on the second attempt, and the second attempt is exactly the
    case the determinism was for.
    """
    import uuid as _uuid

    target_id = str(_uuid.uuid5(_uuid.NAMESPACE_URL, source_prefix))
    return create_redaction(conn, company_id, target_id, reason, actor_user_id, actor_role,
                            target_type="recording", scope="deleted",
                            batch_id=batch_id, target_key=source_prefix,
                            skip_if_already_deleted=True)


def deleted_source_prefixes(conn, folder=None, date=None) -> list:
    """Active deleted source prefixes, optionally narrowed to one folder/date.

    The non-VPC readers cannot run this — they have no database — which is why the plan
    also mirrors the set to S3. This is the authority; that mirror is a copy.
    """
    sql = ("SELECT DISTINCT target_key FROM redactions "
           "WHERE target_type = 'recording' AND scope = 'deleted' "
           "AND reverted_at IS NULL AND target_key IS NOT NULL")
    params = []
    if folder and date:
        sql += " AND target_key LIKE %s"
        params.append(f"%/{folder}/{date}/%")
    rows = conn.cursor(row_factory=dict_row).execute(sql, tuple(params)).fetchall()
    return [r["target_key"] for r in rows]


def is_source_deleted(conn, source_s3_key) -> bool:
    """Whether this object's source has been deleted. Prefix match, not equality: one
    tombstone covers a session's whole folder."""
    if not source_s3_key:
        return False
    return conn.cursor(row_factory=dict_row).execute(
        "SELECT 1 FROM redactions WHERE target_type = 'recording' AND scope = 'deleted' "
        "AND reverted_at IS NULL AND target_key IS NOT NULL "
        "AND %s LIKE target_key || '%%' LIMIT 1", (source_s3_key,)).fetchone() is not None


def list_batch(conn, batch_id, company_id, *, cross_company=False) -> list:
    if cross_company:
        return conn.cursor(row_factory=dict_row).execute(
            f"SELECT {_COLS} FROM redactions WHERE batch_id = %s", (batch_id,)).fetchall()
    return conn.cursor(row_factory=dict_row).execute(
        f"SELECT {_COLS} FROM redactions WHERE batch_id = %s AND company_id = %s",
        (batch_id, company_id)).fetchall()


def active_batches_for_day(conn, folder, date, exclude_batch=None) -> list:
    """Batch ids that still hide a recording in `folder` on `date`.

    Undelete needs this to answer "is this day still partly deleted?". The day's report is
    a rollup that mixes every session, so its stale topics have to stay hidden while ANY of
    that day's recordings is deleted — but they can only be hidden under a batch that is
    still active, or nothing will ever bring them back.
    """
    sql = ("SELECT DISTINCT batch_id FROM redactions "
           "WHERE target_type = 'recording' AND scope = 'deleted' "
           "AND reverted_at IS NULL AND batch_id IS NOT NULL "
           "AND target_key LIKE %s")
    params = [f"extractions/{folder}/{date}/%"]
    if exclude_batch:
        sql += " AND batch_id <> %s"
        params.append(exclude_batch)
    sql += " ORDER BY batch_id"
    rows = conn.cursor(row_factory=dict_row).execute(sql, tuple(params)).fetchall()
    return [r["batch_id"] for r in rows]


def revert_batch(conn, batch_id, company_id, *, cross_company=False) -> list:
    """Undo exactly one delete action.

    Company-guarded, because an unguarded revert crosses tenants. `reverted_at IS NULL` in
    the WHERE so reverting twice does not re-stamp rows and inflate the count that is the
    only evidence the delete and the revert matched.

    `cross_company` is the platform_admin span, and it is opt-in per call for the reason
    this project has already been bitten by: platform_admin spans companies automatically
    on graded READ paths only, so every write path has to be taught separately. A delete
    made across companies is stamped with the TARGET's company, so an affected company can
    always undo its own — this span is for the case where nobody at that company can.
    """
    if cross_company:
        return conn.cursor(row_factory=dict_row).execute(
            f"UPDATE redactions SET reverted_at = now() "
            f"WHERE batch_id = %s AND reverted_at IS NULL "
            f"RETURNING {_COLS}", (batch_id,)).fetchall()
    return conn.cursor(row_factory=dict_row).execute(
        f"UPDATE redactions SET reverted_at = now() "
        f"WHERE batch_id = %s AND company_id = %s AND reverted_at IS NULL "
        f"RETURNING {_COLS}", (batch_id, company_id)).fetchall()


def list_deleted_batches_for_prefix(conn, prefix) -> list:
    """The active delete batches whose tombstone covers this source key.

    Used by the re-stamp: a topic the pipeline just re-created must join the SAME batch as
    the tombstone that hid its predecessor, or one revert stops restoring exactly what one
    delete hid — and that equality is the only check that proves the feature is reversible.
    """
    return conn.cursor(row_factory=dict_row).execute(
        "SELECT DISTINCT batch_id, company_id FROM redactions "
        "WHERE target_type = 'recording' AND scope = 'deleted' AND reverted_at IS NULL "
        "AND target_key IS NOT NULL AND %s LIKE target_key || '%%'", (prefix,)).fetchall()
