"""
Integration tests for the subject-thread repository against a real PostgreSQL.

These exist because the parts that can go wrong here are all SQL: a partial
unique index, an XOR check, a GREATEST that stops a backfill dragging a
thread's recency backwards, and an ON DELETE SET NULL that must un-thread
rather than delete. Connection doubles enforce none of that — CLAUDE.md
records a cascade that ate local programme rows while 1598 unit tests passed
either side of the fix.

Skipped unless TEST_DATABASE_URL is set (tests/conftest.py).
"""
import pytest

from repositories import threads

pytestmark = pytest.mark.integration


def _seed(db):
    cid = db.execute(
        "INSERT INTO companies (name) VALUES ('C') RETURNING id").fetchone()[0]
    sid = db.execute(
        "INSERT INTO sites (company_id, name) VALUES (%s,'S') RETURNING id",
        (cid,)).fetchone()[0]
    return sid


def _topic(db, site_id, title, day, open_items=0):
    tid = db.execute(
        "INSERT INTO topics (site_id, report_date, title, source_s3_key) "
        "VALUES (%s,%s,%s,'extractions/x') RETURNING id",
        (site_id, day, title)).fetchone()[0]
    for i in range(open_items):
        db.execute(
            "INSERT INTO action_items (topic_id, site_id, text, status) "
            "VALUES (%s,%s,%s,'open')", (tid, site_id, f"do {i}"))
    return tid


# ---- the corpus filter ----------------------------------------------------

def test_only_topics_with_open_work_are_candidates(db):
    """The filter that made lexical matching usable. Every false candidate in
    the prod probe had zero open items on both sides -- the recording-artifact
    topics match each other perfectly and mean nothing."""
    sid = _seed(db)
    _topic(db, sid, "Has open work", "2026-07-01", open_items=2)
    _topic(db, sid, "All done", "2026-07-02", open_items=0)
    got = threads.candidate_corpus(db, sid, "2026-07-20", 45)
    assert [r["title"] for r in got] == ["Has open work"]


def test_the_corpus_is_bounded_by_site_and_window(db):
    sid, other = _seed(db), _seed(db)
    _topic(db, sid, "In window", "2026-07-10", open_items=1)
    _topic(db, sid, "Too old", "2026-01-01", open_items=1)
    _topic(db, other, "Other site", "2026-07-10", open_items=1)
    got = {r["title"] for r in threads.candidate_corpus(db, sid, "2026-07-20", 45)}
    assert got == {"In window"}


def test_the_same_day_is_not_a_candidate(db):
    """Two topics from one recording are two subjects, not one restated."""
    sid = _seed(db)
    _topic(db, sid, "Morning", "2026-07-20", open_items=1)
    assert threads.candidate_corpus(db, sid, "2026-07-20", 45) == []


# ---- attaching ------------------------------------------------------------

def test_attaching_moves_last_raised_forward_but_never_backward(db):
    """A human linking a mention they found late, or a backfill, must not drag
    a live thread's recency backwards and bury it in the queue."""
    sid = _seed(db)
    th = threads.create_thread(db, sid, "Walls", "2026-07-01", "2026-07-10")
    late = _topic(db, sid, "Walls again", "2026-07-20", open_items=1)
    threads.attach_topic(db, late, th["id"], "2026-07-20")
    assert str(threads.get_thread(db, th["id"])["last_raised"]) == "2026-07-20"

    old = _topic(db, sid, "Walls earlier", "2026-07-05", open_items=1)
    threads.attach_topic(db, old, th["id"], "2026-07-05")
    assert str(threads.get_thread(db, th["id"])["last_raised"]) == "2026-07-20"


def test_deleting_a_thread_unthreads_its_topics_and_keeps_them(db):
    """ON DELETE SET NULL, not CASCADE. The programme version of this mistake
    deleted real work."""
    sid = _seed(db)
    th = threads.create_thread(db, sid, "Walls", "2026-07-01", "2026-07-01")
    tid = _topic(db, sid, "Walls", "2026-07-02", open_items=1)
    threads.attach_topic(db, tid, th["id"], "2026-07-02")

    db.execute("DELETE FROM topic_threads WHERE id=%s", (th["id"],))
    row = db.execute(
        "SELECT thread_id FROM topics WHERE id=%s", (tid,)).fetchone()
    assert row is not None, "the topic must survive its thread"
    assert row[0] is None


def test_thread_facts_are_derived_from_the_members(db):
    sid = _seed(db)
    th = threads.create_thread(db, sid, "Walls", "2026-07-01", "2026-07-01")
    for day, n in (("2026-07-01", 2), ("2026-07-08", 1), ("2026-07-08", 3)):
        threads.attach_topic(db, _topic(db, sid, "Walls", day, open_items=n),
                             th["id"], day)
    facts = threads.thread_facts(db, th["id"])
    # Raised on two DAYS, not three times: two topics from one recording are
    # one raising of the subject.
    assert facts["times_raised"] == 2
    assert facts["open_items"] == 6
    assert str(facts["last_raised"]) == "2026-07-08"


# ---- suggestions ----------------------------------------------------------

def test_a_second_run_refreshes_the_live_proposal_instead_of_stacking(db):
    """Re-extraction re-runs the matcher. A reviewer facing five
    near-identical questions about one topic stops reading the queue."""
    sid = _seed(db)
    th = threads.create_thread(db, sid, "Walls", "2026-07-01", "2026-07-01")
    tid = _topic(db, sid, "Walls", "2026-07-05", open_items=1)

    threads.upsert_suggestion(db, tid, thread_id=th["id"], score=0.3, gap_days=4)
    threads.upsert_suggestion(db, tid, thread_id=th["id"], score=0.5, gap_days=4)

    rows = db.execute(
        "SELECT score FROM topic_thread_suggestions WHERE topic_id=%s", (tid,)
    ).fetchall()
    assert len(rows) == 1
    assert rows[0][0] == pytest.approx(0.5)


def test_a_rejected_link_is_never_proposed_again(db):
    """Re-proposing a link someone turned down is arguing with them, and is
    the fastest way to train them to ignore the queue."""
    sid = _seed(db)
    th = threads.create_thread(db, sid, "Walls", "2026-07-01", "2026-07-01")
    tid = _topic(db, sid, "Walls", "2026-07-05", open_items=1)

    s = threads.upsert_suggestion(db, tid, thread_id=th["id"], score=0.3, gap_days=4)
    threads.resolve_suggestion(db, s["id"], "rejected", "someone")

    assert threads.upsert_suggestion(
        db, tid, thread_id=th["id"], score=0.9, gap_days=4) is None


def test_a_different_target_can_still_be_proposed_after_a_rejection(db):
    """Rejecting "is this the walls thread?" says nothing about "is this the
    scaffold thread?"."""
    sid = _seed(db)
    walls = threads.create_thread(db, sid, "Walls", "2026-07-01", "2026-07-01")
    scaffold = threads.create_thread(db, sid, "Scaffold", "2026-07-01", "2026-07-01")
    tid = _topic(db, sid, "Something", "2026-07-05", open_items=1)

    s = threads.upsert_suggestion(db, tid, thread_id=walls["id"], score=0.3, gap_days=4)
    threads.resolve_suggestion(db, s["id"], "rejected", "someone")

    assert threads.upsert_suggestion(
        db, tid, thread_id=scaffold["id"], score=0.4, gap_days=4) is not None


def test_resolving_twice_is_refused(db):
    sid = _seed(db)
    th = threads.create_thread(db, sid, "Walls", "2026-07-01", "2026-07-01")
    tid = _topic(db, sid, "Walls", "2026-07-05", open_items=1)
    s = threads.upsert_suggestion(db, tid, thread_id=th["id"], score=0.3, gap_days=4)

    assert threads.resolve_suggestion(db, s["id"], "confirmed", "a") is not None
    assert threads.resolve_suggestion(db, s["id"], "confirmed", "b") is None


def test_an_unknown_status_is_refused_before_it_reaches_the_database(db):
    with pytest.raises(ValueError):
        threads.resolve_suggestion(db, "00000000-0000-0000-0000-000000000000",
                                   "maybe", "a")


def test_the_queue_is_scoped_to_the_callers_sites(db):
    sid, other = _seed(db), _seed(db)
    for s in (sid, other):
        th = threads.create_thread(db, s, "Walls", "2026-07-01", "2026-07-01")
        tid = _topic(db, s, "Walls", "2026-07-05", open_items=1)
        threads.upsert_suggestion(db, tid, thread_id=th["id"], score=0.3, gap_days=4)

    assert len(threads.list_pending(db, [sid])) == 1
    assert len(threads.list_pending(db, [sid, other])) == 2


def test_no_sites_returns_nothing_not_everything(db):
    """[] means "restrict to nothing", never "no restriction" -- conflating
    those once handed every user's reports to an account that should have
    seen none."""
    sid = _seed(db)
    th = threads.create_thread(db, sid, "Walls", "2026-07-01", "2026-07-01")
    tid = _topic(db, sid, "Walls", "2026-07-05", open_items=1)
    threads.upsert_suggestion(db, tid, thread_id=th["id"], score=0.3, gap_days=4)

    assert threads.list_pending(db, []) == []
