"""Repository for topic_decisions (migration 0073) -- Track B Task 5.

A decision made on a topic, as its OWN row with a stable_id, alongside (not instead of) the
jsonb `topics.decisions` column upsert_topic already writes -- Step 4/Ruling R6 keeps the
jsonb path unchanged; this table is the row that Task 4's carry_forward and a future reader
switch build on. Declined as a table on 2026-09-07 because ids churned on every
re-extraction; supersession (Task 3) is what makes a durable stable_id possible now.

Style mirrors src/repositories/findings.py exactly: module-level SQL, _COLS shared between
insert/list, conn.cursor(row_factory=dict_row).execute(...).fetchone()/.fetchall(), a
per-row insert loop (never a multi-VALUES batch, so one malformed row raising never aborts
siblings written before it in the same call -- matches insert_findings)."""
from psycopg.rows import dict_row

_COLS = ("id, topic_id, site_id, stable_id, carried_from, decision, rationale, "
         "decided_by, audience, created_at, item_id")


def insert_decisions(conn, topic_id, site_id, decisions: list) -> list[dict]:
    """Batch-insert one topic's decisions and return the new rows (RETURNING all cols).

    Same defensive posture as findings.insert_findings: `decisions` is Claude output via
    lambda_extract_session's EXTRACTION_SCHEMA, never trusted to match its own shape. Each
    entry may be the schema's {decision, rationale, decided_by} dict OR a plain string --
    tolerated because the jsonb path (upsert_topic's `decisions=` kwarg, lambda_item_writer.py
    ~1152) tolerates both, and this insert must drop exactly the entries that kwarg drops so
    the row table and the jsonb mirror never disagree about which decisions exist. A blank/
    falsy decision text (missing key, empty string, or a bare falsy string entry) is skipped
    entirely -- it would render as a bullet with nothing in it. Empty input -> [] with no
    query executed."""
    if not decisions:
        return []
    cur = conn.cursor(row_factory=dict_row)
    rows = []
    for d in decisions:
        if isinstance(d, dict):
            text = d.get("decision")
            rationale = d.get("rationale")
            decided_by = d.get("decided_by")
            item_id = d.get("item_id")
        else:
            text = d
            rationale = None
            decided_by = None
            item_id = None
        if not text:
            continue
        rows.append(cur.execute(
            f"INSERT INTO topic_decisions (topic_id, site_id, decision, rationale, "
            f"decided_by, item_id) VALUES (%s,%s,%s,%s,%s,%s) RETURNING {_COLS}",
            (topic_id, site_id, text, rationale, decided_by, item_id),
        ).fetchone())
    return rows


def list_for_topics(conn, topic_ids) -> list[dict]:
    """Batched read of decisions for a set of topic ids -- mirrors
    findings.list_for_topics/topics.list_topics_for_date's children pattern: ONE query
    scoped with ANY(%s), never N+1. Not yet called by any reader (Ruling R6: the jsonb
    stays the served shape until a separate change flips readers over) -- kept to the same
    shape as findings' repo so that switch is a drop-in later."""
    return conn.cursor(row_factory=dict_row).execute(
        f"SELECT {_COLS} FROM topic_decisions WHERE topic_id = ANY(%s) ORDER BY created_at",
        (list(topic_ids),),
    ).fetchall()


def list_for_carry_forward(conn, topic_ids, site_id) -> list[dict]:
    """Decisions on a set of topics -- new OR retired -- shaped for Track B Task 4's
    carry_forward.match: `id`, `text` (the decision's `decision` column), `stable_id` plus a
    computed `human_touched`. Batched with ANY(%s); scoped to `site_id` (Ruling R9) so a
    stray topic id can never pull another tenant's -- or another site's -- rows into the
    match pool.

    human_touched = audience <> 'internal' (Ruling R11) -- the only field a person can set on
    a decision today (via the not-yet-built owner-publish flow). For a freshly-inserted row
    (the "new" pool) this is always False."""
    if not topic_ids:
        return []
    return conn.cursor(row_factory=dict_row).execute(
        "SELECT id, topic_id, decision AS text, stable_id, audience, item_id, "
        "(audience <> 'internal') AS human_touched "
        "FROM topic_decisions WHERE topic_id = ANY(%s) AND site_id = %s",
        (list(topic_ids), site_id),
    ).fetchall()


def carry_identity(conn, new_id, old_row) -> None:
    """Carry `old_row`'s stable identity onto the new decision `new_id` that replaced it
    (Track B Task 4/Ruling R11). Always moves stable_id/carried_from; when the old row was
    human-touched, also moves `audience` -- the only column a person can set on a decision.
    `rationale`/`decided_by` always come from the fresh extraction, never copied here."""
    if old_row.get("human_touched"):
        conn.execute(
            "UPDATE topic_decisions SET stable_id=%s, carried_from=%s, audience=%s "
            "WHERE id=%s",
            (old_row["stable_id"], old_row["id"], old_row["audience"], new_id),
        )
    else:
        conn.execute(
            "UPDATE topic_decisions SET stable_id=%s, carried_from=%s WHERE id=%s",
            (old_row["stable_id"], old_row["id"], new_id),
        )
