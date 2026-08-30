"""Integration: threading must not show, count, or match a recording the customer removed.

Every query in `repositories/threads.py` joins `topics`, and until 2026-08-31 not one asked
whether the topic still exists. Three different leaks, in rising order of harm:

* `list_pending` served a deleted topic's TITLE to the manager review queue;
* `thread_facts` / `facts_for_threads` counted it into `times_raised` and `open_items`, the
  numbers on the card that explains why an item is at the top of someone's list;
* `candidate_corpus` kept it MATCHABLE, so the matcher could mint brand new suggestions out
  of deleted content — the one that creates rather than merely shows.

This is an integration test and not a unit test on purpose. The unit doubles in this repo
record SQL strings without parsing them, so a predicate spliced into the wrong clause, or
one that references a column that is not in scope, passes every unit test in the file. The
`%%` escaping in `deleted_predicates` is exactly the kind of thing only a database can
judge. Two production crashes in this repo were shipped through that gap.

Both arms are exercised. The topic arm covers the row that exists now; the source arm covers
the row the nightly pipeline re-creates tomorrow with a new uuid that no topic-keyed
tombstone names. A read path carrying only the first passes every test written today and
leaks overnight, which is why `visible_topics_predicate` ANDs them and why this file asserts
each separately.

Runs only where `TEST_DATABASE_URL` is set — CI provides a real Postgres.
"""
import uuid

import pytest

from repositories import companies, redactions, sites, threads, topics, users

pytestmark = pytest.mark.integration

DATE = "2026-08-30"
EARLIER = "2026-08-28"
SOURCE = f"extractions/Folder/{EARLIER}/sid{'a' * 32}.json"


def _seed(db):
    co = companies.create_company(db, f"Thr-Co-{uuid.uuid4().hex[:6]}")
    s = sites.create_site(db, co["id"], "Thr-Site")
    u = users.upsert_user(db, f"sub-{uuid.uuid4().hex[:8]}", "t@x.nz", company_id=co["id"])
    return co, s, u


def _topic(db, site_id, user_id, title, date, source_s3_key):
    return topics.upsert_topic(
        db, site_id, date, title, user_id=user_id, source_s3_key=source_s3_key,
        summary="s", action_items=[{"text": "do it", "status": "open"}])


def _delete_topic(db, company_id, topic_id, user_id):
    redactions.create_redaction(db, company_id, topic_id, "customer removed", user_id,
                                "admin", target_type="topic", scope="deleted")


def _delete_recording(db, company_id, prefix, user_id):
    redactions.create_recording_tombstone(db, company_id, prefix, "customer removed",
                                          user_id, "admin")


@pytest.mark.parametrize("arm", ["topic", "source"])
def test_a_deleted_topic_is_not_a_thread_candidate(db, arm):
    """The arm that CREATES. A matchable deleted topic mints new suggestions carrying its
    text, so this is the one leak that grows while nobody is looking."""
    co, site, user = _seed(db)
    t = _topic(db, site["id"], user["id"], "Door hardware", EARLIER, SOURCE)

    assert [r["id"] for r in threads.candidate_corpus(db, site["id"], DATE, 30)] == [t["id"]]

    if arm == "topic":
        _delete_topic(db, co["id"], t["id"], user["id"])
    else:
        _delete_recording(db, co["id"], SOURCE, user["id"])

    assert threads.candidate_corpus(db, site["id"], DATE, 30) == [], (
        f"the {arm} arm did not hide a deleted topic from the matcher")


@pytest.mark.parametrize("arm", ["topic", "source"])
def test_a_deleted_topic_is_not_in_the_review_queue(db, arm):
    co, site, user = _seed(db)
    live = _topic(db, site["id"], user["id"], "Live topic", DATE,
                  f"extractions/Folder/{DATE}/sid{'b' * 32}.json")
    gone = _topic(db, site["id"], user["id"], "Deleted topic", EARLIER, SOURCE)
    threads.upsert_suggestion(db, gone["id"], parent_topic_id=live["id"],
                              score=0.9, gap_days=2)

    assert len(threads.list_pending(db, [site["id"]])) == 1

    if arm == "topic":
        _delete_topic(db, co["id"], gone["id"], user["id"])
    else:
        _delete_recording(db, co["id"], SOURCE, user["id"])

    assert threads.list_pending(db, [site["id"]]) == [], (
        f"the {arm} arm did not hide a deleted topic's title from the review queue")


def test_a_suggestion_whose_parent_was_deleted_stays_but_loses_the_parent(db):
    """A live topic's suggestion is still a real suggestion. Dropping it with its parent
    would hide work that was never deleted — the guard must not become a blanket refusal."""
    co, site, user = _seed(db)
    parent = _topic(db, site["id"], user["id"], "Parent topic", EARLIER, SOURCE)
    child = _topic(db, site["id"], user["id"], "Child topic", DATE,
                   f"extractions/Folder/{DATE}/sid{'c' * 32}.json")
    threads.upsert_suggestion(db, child["id"], parent_topic_id=parent["id"],
                              score=0.9, gap_days=2)

    _delete_topic(db, co["id"], parent["id"], user["id"])

    rows = threads.list_pending(db, [site["id"]])
    assert len(rows) == 1, "the suggestion vanished with its deleted parent"
    assert rows[0]["parent_title"] is None, "the deleted parent's title was still served"


@pytest.mark.parametrize("arm", ["topic", "source"])
def test_a_deleted_topic_stops_counting_on_the_thread_card(db, arm):
    co, site, user = _seed(db)
    keep = _topic(db, site["id"], user["id"], "Kept", DATE,
                  f"extractions/Folder/{DATE}/sid{'d' * 32}.json")
    gone = _topic(db, site["id"], user["id"], "Gone", EARLIER, SOURCE)
    th = threads.create_thread(db, site["id"], "Door hardware", EARLIER, DATE)
    threads.attach_topic(db, keep["id"], th["id"], DATE)
    threads.attach_topic(db, gone["id"], th["id"], EARLIER)

    before = threads.thread_facts(db, th["id"])
    assert before["times_raised"] == 2 and before["open_items"] == 2

    if arm == "topic":
        _delete_topic(db, co["id"], gone["id"], user["id"])
    else:
        _delete_recording(db, co["id"], SOURCE, user["id"])

    after = threads.thread_facts(db, th["id"])
    assert after["times_raised"] == 1, f"{arm} arm: times_raised still counts a deleted topic"
    assert after["open_items"] == 1, f"{arm} arm: open_items still counts a deleted topic"
    assert str(after["first_seen"]) == DATE, "first_seen still points at the deleted day"

    batched = threads.facts_for_threads(db, [th["id"]])[str(th["id"])]
    assert batched["times_raised"] == 1, (
        "facts_for_threads disagrees with thread_facts — the batched query is the one the "
        "timeline actually renders, and the two drifting is how a fix reaches one of them")
