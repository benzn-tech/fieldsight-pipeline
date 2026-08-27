"""Subject threads: the same job, restated across days
(spec docs/superpowers/specs/2026-08-05-recurring-item-threading-design.md).

A `thread_id` on a topic means A PERSON SAID YES. Nothing here ever sets one
from a match — the matcher's output lands in `topic_thread_suggestions` and
waits. That split is the whole safety property: a wrong link silently closes
or escalates the wrong work, and nobody finds out, so the first join into a
thread is a human's call.
"""
from psycopg.rows import dict_row

from deleted_predicates import visible_topics_predicate

# Every query in this file joins `topics`, and until 2026-08-31 not one of them asked
# whether the topic still exists. A recording the customer removed kept its titles in the
# manager review queue, kept counting toward `times_raised` on the card that explains why an
# item is at the top of someone's list, and -- worst of the three -- stayed in the matcher's
# candidate corpus, where it could mint BRAND NEW suggestions out of deleted content.
#
# `visible_topics_predicate` carries BOTH arms deliberately (see deleted_predicates): the
# topic arm for the rows that exist now, the source arm for the rows the nightly pipeline
# re-creates tomorrow with new uuids that no topic-keyed tombstone names. A read path that
# takes only the first passes every test written today and leaks overnight.
_VISIBLE_T = visible_topics_predicate("t")
_VISIBLE_P = visible_topics_predicate("p")

_THREAD_COLS = "id, site_id, title, first_seen, last_raised, created_at, updated_at"
_SUGG_COLS = ("id, topic_id, thread_id, parent_topic_id, score, gap_days, "
              "status, created_at, resolved_at, resolved_by")


# ---------------------------------------------------------------- candidates

def candidate_corpus(conn, site_id, before_date, window_days):
    """The topics a new topic could be a restatement of: same site, earlier,
    inside the window, and CARRYING OPEN WORK.

    That last clause is the filter that made lexical matching usable — in the
    unfiltered probe every false candidate had zero open items on both sides,
    because the extractor's recording-artifact topics ("Unclear Communication
    Recording", "Unintelligible Audio Segment") match each other perfectly and
    mean nothing. A thread tracks outstanding commitments; a topic with none
    is not one.

    Shaped for thread_match.find_candidates (title / summary / open_items).

    ⚠ `open_items` here and in thread_facts / facts_for_threads is NOT collapsed,
    deliberately, and that is a precondition of Phase B rather than an oversight.
    All three counts are inflated by same-day duplicates exactly as the todo
    lists were before ENABLE_TODO_COLLAPSE.

    Safe to leave today: thread_facts and facts_for_threads describe CONFIRMED
    threads, of which prod has zero, and the queue that would show them is
    switched off for customers. candidate_corpus only feeds proposal generation.

    Must be done before Phase B ships: a thread reporting "raised 3 times" when
    one man said it once per recording on a single evening is the feature lying
    about the only number it exists to produce — and mention count is ranked
    ABOVE priority in the ordering. Collapsing per (site, day) first is what
    makes the human judgement affordable; spec §3."""
    return conn.cursor(row_factory=dict_row).execute(
        "SELECT t.id, t.report_date, t.site_id, t.title, t.summary, t.thread_id, "
        "       count(a.id) FILTER (WHERE a.status='open') AS open_items "
        "FROM topics t LEFT JOIN action_items a ON a.topic_id = t.id "
        "WHERE t.site_id = %s AND t.report_date < %s "
        "  AND t.report_date >= (%s::date - %s::int) "
        # A deleted topic must not be matchable. This is the arm that CREATES rather than
        # merely shows: without it the matcher keeps proposing threads built out of a
        # recording the customer removed, and each proposal is a new row carrying its text.
        f"  AND {_VISIBLE_T} "
        "GROUP BY t.id HAVING count(a.id) FILTER (WHERE a.status='open') > 0",
        (site_id, before_date, before_date, window_days)).fetchall()


# ------------------------------------------------------------------ threads

def get_thread(conn, thread_id):
    return conn.cursor(row_factory=dict_row).execute(
        f"SELECT {_THREAD_COLS} FROM topic_threads WHERE id=%s",
        (thread_id,)).fetchone()


def create_thread(conn, site_id, title, first_seen, last_raised):
    return conn.cursor(row_factory=dict_row).execute(
        f"INSERT INTO topic_threads (site_id, title, first_seen, last_raised) "
        f"VALUES (%s,%s,%s,%s) RETURNING {_THREAD_COLS}",
        (site_id, title, first_seen, last_raised)).fetchone()


def attach_topic(conn, topic_id, thread_id, report_date):
    """Put a topic on a thread and move the thread's last_raised forward.

    Only ever called from a confirmation path. `last_raised` uses GREATEST so
    attaching an older topic (a backfill, or a human linking a mention they
    found late) cannot drag the thread's recency backwards and bury it."""
    conn.cursor().execute(
        "UPDATE topics SET thread_id=%s WHERE id=%s", (thread_id, topic_id))
    return conn.cursor(row_factory=dict_row).execute(
        f"UPDATE topic_threads SET last_raised = GREATEST(last_raised, %s), "
        f"updated_at = now() WHERE id=%s RETURNING {_THREAD_COLS}",
        (report_date, thread_id)).fetchone()


def thread_facts(conn, thread_id):
    """The facts a card can state without anyone confirming them: how many
    days this subject was raised on, when it was last raised, how much is
    still open.

    Derived, never stored. A stored counter drifts the moment a topic is
    redacted, re-extracted or un-threaded, and these numbers are going to sit
    on a card explaining WHY an item is at the top of someone's list —
    the one place a stale number is least forgivable."""
    return conn.cursor(row_factory=dict_row).execute(
        "SELECT count(DISTINCT t.report_date) AS times_raised, "
        "       min(t.report_date) AS first_seen, "
        "       max(t.report_date) AS last_raised, "
        "       count(a.id) FILTER (WHERE a.status='open') AS open_items "
        "FROM topics t LEFT JOIN action_items a ON a.topic_id = t.id "
        f"WHERE t.thread_id = %s AND {_VISIBLE_T}", (thread_id,)).fetchone()


def facts_for_threads(conn, thread_ids):
    """thread_facts for MANY threads in one query, as {thread_id: facts}.

    Batched because the caller is the timeline render, which runs over every
    topic on a day: per-topic lookups would be the N+1 that list_topics_for_date
    already avoids for action_items and findings. Empty in, empty out -- and
    never "all threads", which is what a missing guard would turn `= ANY(NULL)`
    into for a day with nothing threaded."""
    if not thread_ids:
        return {}
    rows = conn.cursor(row_factory=dict_row).execute(
        "SELECT t.thread_id, "
        "       count(DISTINCT t.report_date) AS times_raised, "
        "       min(t.report_date) AS first_seen, "
        "       max(t.report_date) AS last_raised, "
        "       count(a.id) FILTER (WHERE a.status='open') AS open_items "
        "FROM topics t LEFT JOIN action_items a ON a.topic_id = t.id "
        f"WHERE t.thread_id = ANY(%s) AND {_VISIBLE_T} GROUP BY t.thread_id",
        (list(thread_ids),)).fetchall()
    return {str(r["thread_id"]): r for r in rows}


# -------------------------------------------------------------- suggestions

def upsert_suggestion(conn, topic_id, *, thread_id=None, parent_topic_id=None,
                      score, gap_days):
    """Record (or refresh) the single live proposal for this topic.

    Re-extraction re-runs the matcher, so this must replace rather than
    accumulate: a reviewer facing five near-identical questions about one
    topic stops reading the queue, and the queue is the only thing standing
    between a guess and a committed link.

    A resolved proposal is never revived — if someone rejected this link,
    proposing it again is arguing with them."""
    row = conn.cursor(row_factory=dict_row).execute(
        f"UPDATE topic_thread_suggestions SET thread_id=%s, parent_topic_id=%s, "
        f"score=%s, gap_days=%s, created_at=now() "
        f"WHERE topic_id=%s AND status='pending' RETURNING {_SUGG_COLS}",
        (thread_id, parent_topic_id, score, gap_days, topic_id)).fetchone()
    if row is not None:
        return row
    if already_resolved(conn, topic_id, thread_id=thread_id,
                        parent_topic_id=parent_topic_id):
        return None
    return conn.cursor(row_factory=dict_row).execute(
        f"INSERT INTO topic_thread_suggestions "
        f"(topic_id, thread_id, parent_topic_id, score, gap_days) "
        f"VALUES (%s,%s,%s,%s,%s) RETURNING {_SUGG_COLS}",
        (topic_id, thread_id, parent_topic_id, score, gap_days)).fetchone()


def already_resolved(conn, topic_id, *, thread_id=None, parent_topic_id=None):
    """Has a human already answered this exact proposal? Includes rejections
    on purpose: re-proposing a link someone turned down is the fastest way to
    train them to ignore the queue."""
    return conn.cursor(row_factory=dict_row).execute(
        "SELECT 1 FROM topic_thread_suggestions WHERE topic_id=%s "
        "AND status <> 'pending' "
        "AND thread_id IS NOT DISTINCT FROM %s "
        "AND parent_topic_id IS NOT DISTINCT FROM %s LIMIT 1",
        (topic_id, thread_id, parent_topic_id)).fetchone() is not None


def get_suggestion(conn, suggestion_id):
    """One proposal plus the site it belongs to, so the caller can ACL it
    without a second round trip. site_id comes from the TOPIC rather than
    being denormalised onto the suggestion: a topic's site can be corrected
    (BUG-41's whole subject), and a stale copy here would decide access."""
    return conn.cursor(row_factory=dict_row).execute(
        f"SELECT s.{ ', s.'.join(_SUGG_COLS.split(', ')) }, "
        "       t.site_id, t.report_date AS topic_date, t.title AS topic_title "
        "FROM topic_thread_suggestions s JOIN topics t ON t.id = s.topic_id "
        "WHERE s.id = %s", (suggestion_id,)).fetchone()


def list_pending(conn, site_ids, limit=50):
    """The review queue, newest first. Empty site_ids returns nothing rather
    than everything — [] means "restrict to nothing", never "no restriction"
    (CLAUDE.md: conflating those once handed every user's reports to an
    account that should have seen none)."""
    if not site_ids:
        return []
    return conn.cursor(row_factory=dict_row).execute(
        f"SELECT s.{ ', s.'.join(_SUGG_COLS.split(', ')) }, "
        "       t.title AS topic_title, t.report_date AS topic_date, t.site_id, "
        "       p.title AS parent_title, p.report_date AS parent_date, "
        "       th.title AS thread_title "
        "FROM topic_thread_suggestions s "
        "JOIN topics t ON t.id = s.topic_id "
        # The parent is filtered in the JOIN, not the WHERE: a suggestion whose PARENT was
        # deleted is still a real suggestion about a live topic, so it stays in the queue
        # with its parent columns null, rather than vanishing with it.
        f"LEFT JOIN topics p ON p.id = s.parent_topic_id AND {_VISIBLE_P} "
        "LEFT JOIN topic_threads th ON th.id = s.thread_id "
        f"WHERE s.status='pending' AND t.site_id = ANY(%s) AND {_VISIBLE_T} "
        "ORDER BY s.created_at DESC LIMIT %s",
        (list(site_ids), limit)).fetchall()


def resolve_suggestion(conn, suggestion_id, status, resolved_by):
    """Mark a proposal confirmed or rejected. Does NOT attach the topic —
    the caller does that inside its own transaction, so a confirmation that
    fails to attach never leaves a resolved row claiming work that did not
    happen."""
    if status not in ("confirmed", "rejected"):
        raise ValueError(f"status must be confirmed|rejected, got {status!r}")
    return conn.cursor(row_factory=dict_row).execute(
        f"UPDATE topic_thread_suggestions SET status=%s, resolved_at=now(), "
        f"resolved_by=%s WHERE id=%s AND status='pending' RETURNING {_SUGG_COLS}",
        (status, resolved_by, suggestion_id)).fetchone()
