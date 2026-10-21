"""Repository for decision_records (migration 0073) -- Track B Task 6a.

One row per GATED AI verdict -- accepted AND rejected -- plus (Task 6b) the
human's answer to it. Spec Sec 2.2 / Task 6 brief: prior tasks kept only the
accepted half of a verdict (`parse_verdict`/`parse_impact_verdicts` discarded
a rejected one entirely), so there was never a durable row for "the model
said X, the gate said no" -- exactly the row an eval/calibration pass needs
most.

`output` is jsonb and NEVER carries transcript text (plan Global
Constraint): callers strip any free-text rationale field (programme_match's
`evidence`, programme_impact's `note`) before calling `insert()` -- see
lambda_programme_matcher.py's `_process_suggestion`/`_process_impacts` for
which fields survive into `output`.

Style mirrors src/repositories/topic_decisions.py / findings.py:
module-level _COLS, conn.cursor(row_factory=dict_row), Jsonb() for the
jsonb column (chunks.py/findings.py convention)."""
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb

from deleted_predicates import visible_decision_records_predicate

_COLS = (
    "id", "company_id", "site_id", "kind", "subject_type", "subject_stable_id",
    "object_ref", "provider", "model", "model_version", "question_set",
    "input_key", "input_hash", "output", "score", "threshold", "auto_outcome",
    "human_outcome", "human_actor", "human_at", "created_at",
)
_RETURNING = ", ".join(_COLS)
_GENERATED = {"id", "created_at"}
_JSONB_COLS = {"output"}


def insert(conn, **cols) -> dict:
    """INSERT one decision_records row and return it (RETURNING every
    column). `cols` keys are validated against `_COLS` -- a typo'd kwarg
    (or a column renamed out from under a caller) raises ValueError
    immediately rather than being silently absorbed by a generic SQL
    builder, and `id`/`created_at` are DB-generated so a caller passing
    them is also rejected. `output` (jsonb) is wrapped in Jsonb() (the
    chunks.py/findings.py convention); every other value is bound as-is.

    company_id/kind/subject_type/subject_stable_id/provider/output/
    auto_outcome are NOT NULL on the table -- a caller that omits one gets
    psycopg's own NOT NULL violation, not a half-written row defaulted
    here."""
    unknown = set(cols) - set(_COLS)
    if unknown:
        raise ValueError(f"decision_records.insert: unknown column(s) {sorted(unknown)}")
    forbidden = _GENERATED & set(cols)
    if forbidden:
        raise ValueError(f"decision_records.insert: column(s) {sorted(forbidden)} are DB-generated")
    names = list(cols)
    values = [Jsonb(v) if (k in _JSONB_COLS and v is not None) else v for k, v in cols.items()]
    col_list = ", ".join(names)
    placeholders = ", ".join(["%s"] * len(names))
    return conn.cursor(row_factory=dict_row).execute(
        f"INSERT INTO decision_records ({col_list}) VALUES ({placeholders}) "
        f"RETURNING {_RETURNING}",
        values,
    ).fetchone()


def set_human_outcome(conn, kind, subject_type, subject_stable_id, object_ref,
                      outcome, actor) -> int:
    """Stamp human_outcome/human_actor/human_at=now() on the LATEST decision
    record for this (kind, subject_type, subject_stable_id, object_ref) --
    never every record ever written for that subject/object. Re-extraction
    (Task 3) can write a fresh accepted-or-rejected record for the SAME
    topic/task pair on every pass, so only the newest one is "the" verdict
    a human is confirming or rejecting now; stamping an older row would let
    a stale record read as confirmed while the current one stays open.

    `ORDER BY created_at DESC, id DESC LIMIT 1` picks that row. NULLS LAST
    is irrelevant here -- created_at is NOT NULL on every row (column
    DEFAULT now()), so there is never a NULL to sort around. `id DESC` is
    only a deterministic tie-break for two records sharing one `created_at`
    instant (a real but rare case under concurrent matcher/writer
    invokes), not a second meaningful ordering key.

    `object_ref` may be legitimately NULL -- e.g. a programme_match verdict
    where Claude picked no task at all. `IS NOT DISTINCT FROM` is used
    instead of `=` so a NULL `object_ref` argument matches a NULL
    `object_ref` column; plain `=` against NULL is NULL, never TRUE, which
    would make this UPDATE match zero rows for exactly the case it needs to
    hit.

    Returns the number of rows updated -- 0 (no matching record) or 1 (the
    subquery can only ever pick at most one row)."""
    cur = conn.execute(
        "UPDATE decision_records SET human_outcome=%s, human_actor=%s, human_at=now() "
        "WHERE id = ("
        "  SELECT id FROM decision_records "
        "  WHERE kind=%s AND subject_type=%s AND subject_stable_id=%s "
        "    AND object_ref IS NOT DISTINCT FROM %s "
        "  ORDER BY created_at DESC, id DESC LIMIT 1"
        ")",
        (outcome, actor, kind, subject_type, subject_stable_id, object_ref),
    )
    return cur.rowcount


def list_for_eval(conn, company_id, kind, since) -> list[dict]:
    """Every decision_records row for one company/kind created at or after
    `since`, newest first -- Task 6 brief: "for Track A's export to switch
    to once this lands".

    Ruling R7 (Task 6b): carries `visible_decision_records_predicate` --
    the deletion arms only (topic id + source prefix), deliberately NOT the
    live/supersession arm. A row whose subject a customer actually deleted
    is excluded; a row whose subject topic was merely superseded by a later
    extraction pass is NOT -- an eval export that lost a record every time
    Task 3 re-extracted would defeat the reason decision_records exists.
    See tests/unit/test_live_topic_predicate_everywhere.py's UNFILTERED
    entry for this function for the same reasoning, enforced."""
    return conn.cursor(row_factory=dict_row).execute(
        f"SELECT {', '.join('d.' + c for c in _COLS)} FROM decision_records d "
        f"WHERE d.company_id=%s AND d.kind=%s AND d.created_at >= %s "
        f"AND {visible_decision_records_predicate('d')} "
        f"ORDER BY d.created_at DESC",
        (company_id, kind, since),
    ).fetchall()


def object_ref_for_accepted(conn, kind, subject_type, subject_stable_id):
    """The `object_ref` the writer stamped on the latest 'accepted' record for this
    subject, or None if there is none.

    Exists for exactly one seam: `topic_thread_suggestions` (migration 0032, predates
    Track B) carries `thread_id XOR parent_topic_id` -- when a new topic's best-scoring
    thread candidate already belongs to an existing thread, the suggestion row stores
    only that THREAD's id, not the specific candidate TOPIC id `_suggest_threads_inner`
    scored it against. But `decision_records.object_ref` for a `kind='thread'` verdict is
    always "the earlier topic id" (`lambda_item_writer._suggest_threads_inner`'s
    `object_ref=str(c["id"])`, never the thread id) -- so a caller holding only
    `thread_id` cannot reconstruct the object_ref `set_human_outcome` needs to find the
    right row.

    Safe because a topic id is never reused (topics are not re-keyed across
    re-extraction -- decision_records.subject_stable_id's own column comment) and
    `_suggest_threads_inner` runs at most once per topic (when it is first written), so
    at most one 'accepted' thread record ever exists for a given subject_stable_id;
    `ORDER BY created_at DESC LIMIT 1` is a safety net, not a real disambiguation."""
    row = conn.cursor(row_factory=dict_row).execute(
        "SELECT object_ref FROM decision_records "
        "WHERE kind=%s AND subject_type=%s AND subject_stable_id=%s "
        "AND auto_outcome='accepted' ORDER BY created_at DESC LIMIT 1",
        (kind, subject_type, subject_stable_id),
    ).fetchone()
    return row["object_ref"] if row else None
