"""Repository for findings (migration 0010) -- Task 1 of the
programme-impact-link plan; see
docs/superpowers/plans/2026-07-13-programme-impact-link.md (Task 1) and
docs/superpowers/specs/2026-07-13-unified-extraction-labeling-design.md
(S4/S5).

A `findings` row is a rich per-topic extraction item (observation/domain/
severity/entity/recommended_action) PLUS the programme-impact link as
columns on the same row (programme_task_id/impact_severity/impact_note/
impact_task_name/impact_evidence/impact_matched_at) -- deliberately not a
second link table (spec S9: one link table stays
programme_progress_suggestions, 0008).

Style mirrors src/repositories/observations.py / programme_suggestions.py
(module-level SQL, conn.cursor(row_factory=dict_row).execute(...)
.fetchone()/.fetchall()). jsonb binding follows src/repositories/chunks.py's
Jsonb() convention.
"""
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb

_COLS = ("id, topic_id, site_id, observation, domain, severity, entity_name, "
         "entity_trade, recommended_action, programme_task_id, impact_severity, "
         "impact_note, impact_task_name, impact_evidence, impact_matched_at, "
         "status, created_at")

_VALID_DOMAINS = {"safety", "quality", "progress"}
_VALID_SEVERITIES = {"none", "minor", "major"}


def _clean_enum(value, valid) -> str | None:
    """Passes value through only if it matches the DB CHECK enum, else NULL.
    Never raises -- the extractor's Claude output can't be trusted to only
    emit the values it was told to (fail-open, same posture as the
    extractor's own _derive_safety_flags bridge)."""
    return value if value in valid else None


def insert_findings(conn, topic_id, site_id, findings: list[dict]) -> list[dict]:
    """Batch-insert one topic's rich extraction findings and return the new
    rows (RETURNING all cols, so callers get generated id/status/created_at
    back). Input dicts use the extractor's field names
    (lambda_extract_session.py EXTRACTION_SCHEMA findings[]: observation/
    domain/severity/entity{name,trade}/recommended_action) -- the nested
    entity dict is flattened HERE into entity_name/entity_trade columns.
    Defensive .get everywhere: this is Claude output, never trust its shape
    (a missing/non-dict entity degrades to {None, None}, not a KeyError/
    AttributeError). domain/severity values outside the CHECK enum are
    passed as NULL rather than raising -- one malformed finding must never
    abort the whole topic's insert.

    Impact columns (programme_task_id, impact_*) are left NULL here --
    they're filled later by apply_impact, downstream of the matcher/writer
    hop (D2 of the plan). Empty findings -> [] with no query executed."""
    if not findings:
        return []
    cur = conn.cursor(row_factory=dict_row)
    rows = []
    for f in findings:
        entity = f.get("entity")
        if not isinstance(entity, dict):
            entity = {}
        rows.append(cur.execute(
            f"INSERT INTO findings (topic_id, site_id, observation, domain, severity, "
            f"entity_name, entity_trade, recommended_action) "
            f"VALUES (%s,%s,%s,%s,%s,%s,%s,%s) RETURNING {_COLS}",
            (topic_id, site_id, f.get("observation"),
             _clean_enum(f.get("domain"), _VALID_DOMAINS),
             _clean_enum(f.get("severity"), _VALID_SEVERITIES),
             entity.get("name"), entity.get("trade"), f.get("recommended_action")),
        ).fetchone())
    return rows


def apply_impact(conn, finding_id, *, task_id, impact_severity, impact_note,
                 impact_task_name, impact_evidence: dict) -> dict | None:
    """Applies one matcher verdict to a finding row as an UPDATE (the
    in-VPC writer hop -- BUG-36: the matcher itself stays non-VPC and never
    touches Aurora directly). rowcount 0 is a NORMAL skip, not an error: the
    finding row may have vanished between the matcher's read and this write
    because of nightly supersession or a re-extraction racing in (D4/D5 of
    the plan) -- returns None, never raises. impact_evidence is wrapped in
    Jsonb (chunks.py convention)."""
    return conn.cursor(row_factory=dict_row).execute(
        f"UPDATE findings SET programme_task_id=%s, impact_severity=%s, "
        f"impact_note=%s, impact_task_name=%s, impact_evidence=%s, "
        f"impact_matched_at=now() WHERE id=%s RETURNING {_COLS}",
        (task_id, impact_severity, impact_note, impact_task_name,
         Jsonb(impact_evidence or {}), finding_id),
    ).fetchone()


def list_for_topics(conn, topic_ids) -> list[dict]:
    """Batched read of findings for a set of topic ids -- mirrors
    topics.list_topics_for_date's action_items/safety_observations children
    pattern (topics.py:143-156): ONE query scoped with ANY(%s), regardless
    of how many topic_ids are passed, never N+1."""
    return conn.cursor(row_factory=dict_row).execute(
        f"SELECT {_COLS} FROM findings WHERE topic_id = ANY(%s) ORDER BY created_at",
        (list(topic_ids),),
    ).fetchall()
