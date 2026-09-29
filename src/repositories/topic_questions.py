"""Repository for topic_questions (migration 0073) -- Track B Task 5.

A question raised on a topic, as its OWN row with a stable_id, alongside (not instead of)
the jsonb `topics.open_questions` column upsert_topic already writes -- Step 4/Ruling R6
keeps the jsonb path unchanged; this table is what makes a question able to be marked
answered and STAY answered across a re-extraction (Task 4's carry_forward matches it by
text and moves the stable_id + status forward), which is the whole reason this table was
declined on 2026-09-07 (ids churned on every re-extraction) and viable now that Task 3 stopped
deleting superseded topics.

Style mirrors src/repositories/findings.py: module-level SQL, _COLS shared between
insert/list/get, conn.cursor(row_factory=dict_row).execute(...).fetchone()/.fetchall(), a
per-row insert loop (never a multi-VALUES batch)."""
import psycopg
from psycopg.rows import dict_row

from deleted_predicates import visible_topics_predicate

_COLS = ("id, topic_id, site_id, stable_id, carried_from, question, status, "
         "answered_by, answered_at, audience, created_at")


def insert_questions(conn, topic_id, site_id, questions: list) -> list[dict]:
    """Batch-insert one topic's open questions and return the new rows (RETURNING all
    cols, each starting `status='open'` per the column DEFAULT).

    Same defensive posture as findings.insert_findings: `questions` is Claude output via
    lambda_extract_session's EXTRACTION_SCHEMA, never trusted to match its own shape. Each
    entry may be the schema's {question: ...} dict OR a plain string -- tolerated because
    the jsonb path (upsert_topic's `open_questions=` kwarg, lambda_item_writer.py ~1141)
    tolerates both, and this insert must drop exactly the entries that kwarg drops so the
    row table and the jsonb mirror never disagree about which questions exist. A blank/
    falsy question text is skipped entirely. Empty input -> [] with no query executed."""
    if not questions:
        return []
    cur = conn.cursor(row_factory=dict_row)
    rows = []
    for q in questions:
        text = q.get("question") if isinstance(q, dict) else q
        if not text:
            continue
        rows.append(cur.execute(
            f"INSERT INTO topic_questions (topic_id, site_id, question) "
            f"VALUES (%s,%s,%s) RETURNING {_COLS}",
            (topic_id, site_id, text),
        ).fetchone())
    return rows


def list_for_topics(conn, topic_ids) -> list[dict]:
    """Batched read of questions for a set of topic ids -- mirrors
    findings.list_for_topics/topics.list_topics_for_date's children pattern: ONE query
    scoped with ANY(%s), never N+1. Not yet called by any reader (Ruling R6: the jsonb
    stays the served shape until a separate change flips readers over) -- kept to the same
    shape as findings' repo so that switch is a drop-in later."""
    return conn.cursor(row_factory=dict_row).execute(
        f"SELECT {_COLS} FROM topic_questions WHERE topic_id = ANY(%s) ORDER BY created_at",
        (list(topic_ids),),
    ).fetchall()


def list_for_carry_forward(conn, topic_ids, site_id) -> list[dict]:
    """Questions on a set of topics -- new OR retired -- shaped for Track B Task 4's
    carry_forward.match: `id`, `text` (the question's `question` column), `stable_id` plus a
    computed `human_touched`. Batched with ANY(%s); scoped to `site_id` (Ruling R9) so a
    stray topic id can never pull another tenant's -- or another site's -- rows into the
    match pool.

    human_touched = status <> 'open' OR audience <> 'internal' (Ruling R11) -- either half
    of a person's footprint on the row: answering/dropping a question, or (once the
    owner-publish flow exists) marking it owner-facing. For a freshly-inserted row (the
    "new" pool) this is always False."""
    if not topic_ids:
        return []
    return conn.cursor(row_factory=dict_row).execute(
        "SELECT id, topic_id, question AS text, stable_id, status, answered_by, "
        "answered_at, audience, "
        "(status <> 'open' OR audience <> 'internal') AS human_touched "
        "FROM topic_questions WHERE topic_id = ANY(%s) AND site_id = %s",
        (list(topic_ids), site_id),
    ).fetchall()


def carry_identity(conn, new_id, old_row) -> None:
    """Carry `old_row`'s stable identity onto the new question `new_id` that replaced it
    (Track B Task 4/Ruling R11). Always moves stable_id/carried_from; when the old row was
    human-touched, also moves `status`/`answered_by`/`answered_at`/`audience` -- the fields
    a person (or the PATCH endpoint below) can set on a question -- so an answer survives
    the re-extraction that reworded the question's text around it (Task 5's own integration
    test: test_question_answered_survives.py)."""
    if old_row.get("human_touched"):
        conn.execute(
            "UPDATE topic_questions SET stable_id=%s, carried_from=%s, status=%s, "
            "answered_by=%s, answered_at=%s, audience=%s WHERE id=%s",
            (old_row["stable_id"], old_row["id"], old_row["status"], old_row["answered_by"],
             old_row["answered_at"], old_row["audience"], new_id),
        )
    else:
        conn.execute(
            "UPDATE topic_questions SET stable_id=%s, carried_from=%s WHERE id=%s",
            (old_row["stable_id"], old_row["id"], new_id),
        )


def get_live_by_stable_id(conn, stable_id) -> dict | None:
    """The one LIVE question addressed by `stable_id`, for PATCH /api/org/questions/{stable_id}
    (Task 5 Step 2). Joined to its topic (for `visible_topics_predicate` -- a superseded pass
    of the same source key can share this stable_id via carry_forward's carried_from chain,
    and only the live topic's row is ever the one to answer) and to `sites` for
    `company_id`, which the handler's ACL check needs and no column on topic_questions
    carries directly.

    `ORDER BY created_at DESC LIMIT 1` rather than relying on the predicate to be exactly
    one row: two rows can share a stable_id for a single commit's width in an unlikely
    concurrent-pass race, and picking the newest is the same "latest wins" posture
    supersede_topics_for_source already takes for topics themselves.

    None on missing OR a malformed stable_id (invalid UUID text raises psycopg.Error on the
    server, caught here so a bad path segment reads as 404 rather than 500 -- mirrors
    action_items.get_action_item's malformed-id handling)."""
    try:
        return conn.cursor(row_factory=dict_row).execute(
            f"SELECT q.id, q.topic_id, q.site_id, q.stable_id, q.carried_from, q.question, "
            f"q.status, q.answered_by, q.answered_at, q.audience, q.created_at, "
            f"s.company_id "
            f"FROM topic_questions q "
            f"JOIN topics t ON t.id = q.topic_id "
            f"JOIN sites s ON s.id = q.site_id "
            f"WHERE q.stable_id = %s AND {visible_topics_predicate('t')} "
            f"ORDER BY q.created_at DESC LIMIT 1",
            (stable_id,),
        ).fetchone()
    except psycopg.Error:
        conn.rollback()
        return None


def set_status(conn, question_id, status, answered_by) -> dict | None:
    """UPDATE topic_questions.status (org-api PATCH .../questions/{stable_id}, Task 5 Step 2).

    `answered_by` is the caller's users.id when leaving 'open' (answered/dropped), and both
    it and `answered_at` are cleared back to NULL when the question is reopened to 'open' --
    "who answered this" is meaningless once nobody has. `answered_at` is set to the
    database's `now()`, not a Python timestamp passed in, matching every other timestamptz
    write in this codebase's write paths (e.g. lambda_org_api's `programmes.updated_at`)."""
    is_open = status == "open"
    return conn.cursor(row_factory=dict_row).execute(
        f"UPDATE topic_questions SET status=%s, answered_by=%s, "
        f"answered_at={'NULL' if is_open else 'now()'} "
        f"WHERE id=%s RETURNING {_COLS}",
        (status, None if is_open else answered_by, question_id),
    ).fetchone()
