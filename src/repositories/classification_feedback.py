"""Privacy-preserving classifier feedback (2026-07-21 spec §7). Stores only the
human verdict + classifier confidence + coarse category -- never any transcript
or personal text. summary() is the metadata-only accuracy roll-up."""
from psycopg.rows import dict_row


def append_feedback(conn, company_id, topic_id, human_verdict, *,
                    classifier_verdict=None, classifier_confidence=None,
                    topic_category=None, actor_user_id=None):
    return conn.cursor(row_factory=dict_row).execute(
        "INSERT INTO classification_feedback (company_id, topic_id, "
        "classifier_verdict, classifier_confidence, human_verdict, topic_category, "
        "actor_user_id) VALUES (%s,%s,%s,%s,%s,%s,%s) "
        "RETURNING id, company_id, topic_id, classifier_verdict, classifier_confidence, "
        "human_verdict, topic_category, actor_user_id, created_at",
        (company_id, topic_id, classifier_verdict, classifier_confidence,
         human_verdict, topic_category, actor_user_id)).fetchone()


def summary(conn, company_id) -> dict:
    rows = conn.cursor(row_factory=dict_row).execute(
        "SELECT human_verdict, count(*) AS n FROM classification_feedback "
        "WHERE company_id=%s GROUP BY human_verdict", (company_id,)).fetchall()
    by = {r["human_verdict"]: r["n"] for r in rows}
    tp, fp, fn = by.get("confirm_non_work", 0), by.get("reject_is_work", 0), by.get("missed_personal", 0)
    return {"confirm_non_work": tp, "reject_is_work": fp, "missed_personal": fn,
            "precision": (tp / (tp + fp)) if (tp + fp) else None}
