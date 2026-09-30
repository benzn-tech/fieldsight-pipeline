"""decision_records for the extractor's continuity claims (spec 2026-09-30 D6).

The extractor runs outside the VPC and cannot reach Aurora, so a claim it makes lives only in
the extraction artifact (`continuity.claims`, item_continuity.resolve) until the item writer
turns it into a row here, after carry-forward has moved the stable_ids the claim needs to name.
Every claim becomes a record -- accepted AND rejected -- because a rejected claim is exactly the
row Track B's "every gated AI verdict is recorded" rule exists for: the model asked to carry an
item, the guard said no, and that verdict needs its own durable trace same as any other.

Ids and enums only in `output` -- never text (Track B Global Constraint): a claim's alias and
item ids are opaque identifiers, never the item's wording."""
from repositories import decision_records

import item_continuity

# list_name (the extraction JSON's own key) -> the table that carries that kind's stable_id.
# Keys match item_continuity.KINDS exactly; the table names are the DB tables Track A's
# carry-forward already reads (repositories/action_items.py, findings.py, topic_decisions.py,
# topic_questions.py).
_TABLE = {"action_items": "action_items", "findings": "findings",
          "decisions": "topic_decisions", "questions": "topic_questions"}


def _stable_id_for(conn, list_name, item_id, topic_ids):
    """The stable_id of the ONE row in this kind's table carrying `item_id`, scoped to
    `topic_ids` (the same old/new pool carry-forward used) so a stray item_id from another
    site or another pass can never resolve here. None if there is no such row, or more than
    one -- a duplicated item_id within the topic pool names no single row, same fail-closed
    posture as item_continuity.clean_item_ids taking a duplicate out of consideration."""
    if item_id is None or not topic_ids:
        return None
    rows = conn.execute(
        f"SELECT stable_id FROM {_TABLE[list_name]} WHERE item_id = %s AND topic_id = ANY(%s)",
        (item_id, list(topic_ids)),
    ).fetchall()
    return rows[0][0] if len(rows) == 1 else None


def _already(conn, key, extracted_at, alias, new_item_id):
    """Re-delivery dedupe: the same extraction (same input_key/input_hash) re-processed by a
    retried or duplicate writer invocation must not double the record. Scoped to `object_ref`
    (the alias) and `output->>'new_item_id'` rather than the whole `output` blob, matching
    spec D6's key exactly -- two claims of a genuine double claim (same alias, two different
    new_item_id values, item_continuity resolve()'s one_to_one guard) must both still land."""
    return conn.execute(
        "SELECT 1 FROM decision_records WHERE kind='item_continuity' AND input_key=%s "
        "AND input_hash=%s AND object_ref IS NOT DISTINCT FROM %s "
        "AND output->>'new_item_id' = %s LIMIT 1",
        (key, extracted_at, alias, str(new_item_id) if new_item_id is not None else None),
    ).fetchone() is not None


def record_claims(conn, extraction, extraction_key, company_id, site_id,
                  old_topic_ids, new_topic_ids):
    """Turn every claim in `extraction["continuity"]["claims"]` into a decision_records row
    (kind='item_continuity'), called by the item writer inside its own savepoint AFTER
    carry-forward has run (so the new row's stable_id, and the retired row's, both already
    exist to be looked up). Returns `{"inserted", "skipped_duplicate", "unresolved"}`.

    `input_hash` is stamped with the extraction's `extracted_at` timestamp, not an actual hash
    of anything -- a stretch of the column's usual meaning (spec D6), accepted because it is
    the one value that is both stable across identical re-deliveries of the same pass (so
    dedupe works) and distinct across two different passes of the same key (so a later pass's
    claims are never mistaken for a re-delivery of an earlier one).

    `subject_type`/`subject_stable_id` follow whichever row supplied the stable_id: the
    retired row's, for an accepted claim that resolves there; the new row's otherwise
    (rejected, or an accepted claim whose prior row cannot be found -- D9, stale priors are
    recorded, not prevented). Both live in the SAME table (`_TABLE[list_name]`), because the
    kind guard in item_continuity.resolve already forced the claim's prior and new items to
    share one kind before it could ever pass."""
    stats = {"inserted": 0, "skipped_duplicate": 0, "unresolved": 0}
    cont = extraction.get("continuity") or {}
    claims = cont.get("claims") or []
    if not claims:
        return stats
    extracted_at = extraction.get("extracted_at")
    question_set = cont.get("question_set")
    provider = extraction.get("llm_provider") or "unknown"
    model = extraction.get("llm_model")

    for c in claims:
        list_name = c.get("list_name")
        if list_name not in _TABLE:
            stats["unresolved"] += 1
            continue
        alias = c.get("alias")
        new_item_id = c.get("new_item_id")
        outcome = c.get("outcome")
        if _already(conn, extraction_key, extracted_at, alias, new_item_id):
            stats["skipped_duplicate"] += 1
            continue

        subject = None
        if outcome == "accepted" and c.get("prior_item_id"):
            subject = _stable_id_for(conn, list_name, c["prior_item_id"], old_topic_ids)
        if subject is None:
            subject = _stable_id_for(conn, list_name, new_item_id, new_topic_ids)
        if subject is None:
            stats["unresolved"] += 1
            continue

        decision_records.insert(
            conn, company_id=company_id, site_id=site_id, kind="item_continuity",
            subject_type=item_continuity.KINDS[list_name].record_type,
            subject_stable_id=subject, object_ref=alias,
            provider=provider, model=model, question_set=question_set,
            input_key=extraction_key, input_hash=extracted_at,
            output={"prior_item_id": c.get("prior_item_id"), "new_item_id": new_item_id,
                    "outcome": outcome, "guard": c.get("guard")},
            auto_outcome=outcome,
        )
        stats["inserted"] += 1
    return stats
