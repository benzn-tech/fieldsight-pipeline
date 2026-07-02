from psycopg.rows import dict_row

_TOPIC_COLS = ("id, site_id, user_id, source_s3_key, report_date, occurred_at, "
               "category, title, summary, created_at")


def upsert_topic(conn, site_id, report_date, title, *, user_id=None, source_s3_key=None,
                 occurred_at=None, category=None, summary=None,
                 action_items=None, safety=None, photos=None) -> dict:
    """Insert a topic with its children. NOTE: currently insert-only —
    no ON CONFLICT dedup; re-running extraction will duplicate topics
    (dedup key TBD in Phase 4)."""
    cur = conn.cursor(row_factory=dict_row)
    topic = cur.execute(
        f"INSERT INTO topics (site_id, user_id, source_s3_key, report_date, occurred_at, "
        f"category, title, summary) VALUES (%s,%s,%s,%s,%s,%s,%s,%s) RETURNING {_TOPIC_COLS}",
        (site_id, user_id, source_s3_key, report_date, occurred_at, category, title, summary),
    ).fetchone()
    tid = topic["id"]
    for a in (action_items or []):
        conn.execute(
            "INSERT INTO action_items (topic_id, site_id, text, responsible, deadline, priority, status) "
            "VALUES (%s,%s,%s,%s,%s,%s,%s)",
            (tid, site_id, a["text"], a.get("responsible"), a.get("deadline"),
             a.get("priority"), a.get("status", "open")),
        )
    for o in (safety or []):
        conn.execute(
            "INSERT INTO safety_observations (topic_id, site_id, observation, risk_level, location, status) "
            "VALUES (%s,%s,%s,%s,%s,%s)",
            (tid, site_id, o["observation"], o.get("risk_level"), o.get("location"),
             o.get("status", "open")),
        )
    for p in (photos or []):
        conn.execute(
            "INSERT INTO topic_photos (topic_id, s3_key, caption_text) VALUES (%s,%s,%s)",
            (tid, p["s3_key"], p.get("caption_text")),
        )
    return topic


def list_site_topics(conn, site_id, report_date) -> list[dict]:
    return conn.cursor(row_factory=dict_row).execute(
        f"SELECT {_TOPIC_COLS} FROM topics WHERE site_id=%s AND report_date=%s "
        f"ORDER BY occurred_at NULLS LAST, created_at",
        (site_id, report_date),
    ).fetchall()


def get_topic_photos(conn, topic_id) -> list[dict]:
    return conn.cursor(row_factory=dict_row).execute(
        "SELECT id, topic_id, s3_key, caption_text, created_at FROM topic_photos "
        "WHERE topic_id=%s ORDER BY created_at",
        (topic_id,),
    ).fetchall()
