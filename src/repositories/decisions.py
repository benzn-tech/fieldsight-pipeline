"""Repository for topic decisions (migration 0038).

`lambda_extract_session` has asked the model for decisions since the extraction
schema was written, and the model supplies them -- measured at 101 of 1,127
topics across 90 real extractions. Nothing stored them: no column, no table, no
reference in item-writer. They survived only inside the S3 artifact, which is
why this is a gap rather than a loss and why a backfill is possible.

Style mirrors src/repositories/findings.py (module-level SQL constant,
conn.cursor(row_factory=dict_row)). One thing is deliberately NOT mirrored:
insert_findings passes `observation` straight into a NOT NULL column, so a
single malformed row aborts the whole topics transaction. Here a decision with
no text is skipped instead. The mirror is for the shape, not for the hazard.
"""
from psycopg.rows import dict_row

_COLS = "id, topic_id, decision, rationale, decided_by, created_at"


def insert_decisions(conn, topic_id, decisions) -> list[dict]:
    """One row per decision that actually has text. Returns the inserted rows.

    An empty or absent list is a no-op that never touches the database: legacy
    extraction JSON has no `decisions` key at all, and the report/ingest path
    never has one.
    """
    if not decisions:
        return []
    cur = conn.cursor(row_factory=dict_row)
    rows = []
    for d in decisions:
        if not isinstance(d, dict):
            continue
        text = (d.get("decision") or "").strip()
        if not text:
            continue
        rows.append(cur.execute(
            f"INSERT INTO topic_decisions (topic_id, decision, rationale, decided_by) "
            f"VALUES (%s,%s,%s,%s) RETURNING {_COLS}",
            (topic_id, text, d.get("rationale"), d.get("decided_by")),
        ).fetchone())
    return rows


def list_for_topics(conn, topic_ids) -> list[dict]:
    """Batched read for a set of topic ids -- ONE query scoped with ANY(%s),
    however many ids are passed, never N+1. Mirrors findings.list_for_topics."""
    return conn.cursor(row_factory=dict_row).execute(
        f"SELECT {_COLS} FROM topic_decisions WHERE topic_id = ANY(%s) "
        f"ORDER BY created_at",
        (list(topic_ids),),
    ).fetchall()
