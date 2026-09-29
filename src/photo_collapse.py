"""Collapse the photos that already hang off more than one topic.

The logic of scripts/collapse_multibound_photos.py, as a module, so it can run
where the database is. Aurora is not reachable from outside the VPC, so the
script's "run it somewhere that can reach Aurora" meant nowhere anybody could
stand; org-api runs this as a task (see lambda_org_api), and the script keeps
working for anyone who is inside.

A DRY RUN BY DEFAULT, and a dry run rolls back rather than trusting that it
only read. See the script's docstring for why this recomputes each day instead
of deleting the extra rows.
"""
import photo_binding
import photo_rebind
from repositories import topics

_MULTIBOUND = """
SELECT split_part(tp.s3_key, '/', 2)  AS folder,
       split_part(tp.s3_key, '/', 4)  AS day,
       count(DISTINCT tp.topic_id)    AS n,
       count(*)                       AS rows
  FROM topic_photos tp
 WHERE tp.source = 'binding'
 GROUP BY tp.s3_key
HAVING count(DISTINCT tp.topic_id) > 1
"""


def affected_days(conn):
    """{(folder, day): photos_bound_more_than_once}, worst first.

    Folder and day come out of the KEY (`users/{folder}/pictures/{date}/x.jpg`),
    not out of `topics`, for the same reason the binding scope does: the key is
    what the photo list was built from, and a topic's site/user can differ
    between two sessions of one day.
    """
    out = {}
    for r in conn.cursor().execute(_MULTIBOUND).fetchall():
        folder, day = r[0], r[1]
        out[(folder, day)] = out.get((folder, day), 0) + 1
    return dict(sorted(out.items(), key=lambda kv: -kv[1]))


def company_of(conn, folder):
    """The company that owns a folder's recordings, for the session spans.
    None is survivable: every span is then unknown and the session rule fails
    open. The day still collapses to one topic per photo."""
    row = conn.cursor().execute(
        "SELECT company_id FROM recordings WHERE s3_key LIKE %s LIMIT 1",
        (f"users/{folder}/%",)).fetchone()
    return row[0] if row else None


def multibound_photo_count(conn):
    """Photos bound to more than one topic by the machine binder, right now."""
    return len(conn.cursor().execute(_MULTIBOUND).fetchall())


def run(conn, s3, bucket, apply=False, folder=None, date=None):
    """Recompute every affected day. Returns a summary a person can read.

    `apply=False` computes what a rebind would write inside a savepoint that is
    always rolled back -- its own work only, never the caller's transaction.
    (It used to call conn.rollback(), which undid everything on the
    connection; the integration test caught it discarding its own seed.)
    `apply=True` rebinds each day through photo_rebind.rebind_day_photos,
    whose writer is bounded on source='binding' -- keyframes and human binds
    are never touched.
    """
    if apply:
        return _run(conn, s3, bucket, True, folder, date)
    with conn.transaction(force_rollback=True):
        return _run(conn, s3, bucket, False, folder, date)


def _run(conn, s3, bucket, apply, folder, date):
    days = affected_days(conn)
    if folder or date:
        days = {k: v for k, v in days.items()
                if (not folder or k[0] == folder) and (not date or k[1] == date)}
    before_photos = multibound_photo_count(conn)
    per_day = []
    total_before = total_after = 0
    for (f, day), n in days.items():
        photos = photo_binding.list_pictures(s3, bucket, f"users/{f}/pictures/{day}/")
        before = conn.cursor().execute(
            "SELECT count(*) FROM topic_photos tp JOIN topics t ON t.id = tp.topic_id "
            "WHERE tp.source = 'binding' AND tp.s3_key LIKE %s",
            (f"users/{f}/pictures/{day}/%",)).fetchone()[0]
        if apply:
            written = photo_rebind.rebind_day_photos(conn, company_of(conn, f), f, day, photos)
        else:
            day_topics = topics.list_day_topics_for_binding(conn, f, day)
            sessions = {i: photo_rebind.session_of(t.get("source_s3_key"))
                        for i, t in enumerate(day_topics)}
            written = sum(len(v) for v in photo_binding.photos_for_topics(
                photos, day_topics, topic_sessions=sessions).values())
        total_before += before
        total_after += written
        per_day.append({"folder": f, "day": day, "multibound_photos": n,
                        "rows_before": before, "rows_after": written})
    summary = {
        "mode": "APPLIED" if apply else "DRY RUN",
        "days": len(per_day),
        "multibound_photos_before": before_photos,
        "binding_rows_before": total_before,
        "binding_rows_after": total_after,
        "per_day": per_day,
    }
    if apply:
        # Counted again after the writes, in the same transaction: "the script
        # said it worked" is not the same as "the table now says so".
        summary["multibound_photos_after"] = multibound_photo_count(conn)
    return summary
