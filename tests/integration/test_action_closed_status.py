"""Integration: the `closed` action status (owner, 2026-10-27) against a real Postgres.

`done` = the work was done; `closed` = it no longer needs doing. Both leave the list,
only `done` is a completion. Every query below is new or changed SQL, and a connection
double would only record the string -- so each is executed here.
"""
import uuid

import pytest

from repositories import companies, content_edits, sites, threads, topics, users

pytestmark = pytest.mark.integration

TZ = "Pacific/Auckland"
MON, TUE, WED = "2026-08-17", "2026-08-18", "2026-08-19"


def _noon(day):
    # 00:00 UTC is 12:00 NZ (UTC+12 in August): the middle of the local day.
    return f"{day}T00:00:00Z"


def _seed(db):
    co = companies.create_company(db, f"Closed-Co-{uuid.uuid4().hex[:6]}")
    s = sites.create_site(db, co["id"], "Closed-Site")
    u = users.upsert_user(db, f"sub-{uuid.uuid4().hex[:8]}", "c@x.nz", company_id=co["id"],
                          first_name="Sarah", last_name="Lee")
    return co, s, u


def _action(db, s, u, text, day=MON):
    t = topics.upsert_topic(db, s["id"], day, "Slab", user_id=u["id"],
                            action_items=[{"text": text}])
    return db.execute("SELECT id FROM action_items WHERE topic_id=%s",
                      (t["id"],)).fetchone()[0]


def _status(db, co, u, aid, before, after, day):
    row = content_edits.append_content_edit(
        db, co["id"], "action_items", aid, "status", before, after, u["id"], "pm")
    db.execute("UPDATE content_edits SET created_at=%s WHERE id=%s", (_noon(day), row["id"]))
    db.execute("UPDATE action_items SET status=%s WHERE id=%s", (after, aid))


def test_closed_is_counted_beside_done_never_into_it(db):
    co, s, u = _seed(db)
    _status(db, co, u, _action(db, s, u, "Order steel"), "open", "done", MON)
    _status(db, co, u, _action(db, s, u, "Book crane"), "open", "closed", MON)

    done = content_edits.count_action_closures_by_day(
        db, [s["id"]], MON, MON, company_id=co["id"], tz=TZ)
    dropped = content_edits.count_action_closures_by_day(
        db, [s["id"]], MON, MON, company_id=co["id"], tz=TZ,
        status=content_edits.NO_LONGER_NEEDED_STATUS)

    assert done == {MON: 1}
    assert dropped == {MON: 1}


def test_finishes_list_both_kinds_with_day_and_actor(db):
    co, s, u = _seed(db)
    _status(db, co, u, _action(db, s, u, "Order steel"), "open", "done", MON)
    _status(db, co, u, _action(db, s, u, "Book crane"), "in_progress", "closed", TUE)

    out = content_edits.list_action_finishes(
        db, [s["id"]], MON, WED, company_id=co["id"], tz=TZ)

    assert [(r["text"], r["status"], r["closed_on"], r["closed_by"]) for r in out] == [
        ("Book crane", "closed", TUE, "Sarah Lee"),     # newest first
        ("Order steel", "done", MON, "Sarah Lee"),
    ]


def test_reopened_inside_the_window_is_not_listed(db):
    co, s, u = _seed(db)
    aid = _action(db, s, u, "Chase delivery")
    _status(db, co, u, aid, "open", "done", MON)
    _status(db, co, u, aid, "done", "open", TUE)

    assert content_edits.list_action_finishes(
        db, [s["id"]], MON, WED, company_id=co["id"], tz=TZ) == []


def test_a_closure_before_the_window_is_not_listed(db):
    co, s, u = _seed(db)
    _status(db, co, u, _action(db, s, u, "Pour slab"), "open", "done", MON)

    assert content_edits.list_action_finishes(
        db, [s["id"]], TUE, WED, company_id=co["id"], tz=TZ) == []


def test_a_carried_item_is_listed_once_by_stable_id(db):
    """Re-extraction gives the item a new row with the same stable_id. A tick on the
    old row and a later status change on the new one are one item, read at its last
    change."""
    co, s, u = _seed(db)
    old = _action(db, s, u, "Order steel", day=MON)
    new = _action(db, s, u, "Order the steel", day=TUE)
    db.execute("UPDATE action_items SET stable_id=(SELECT stable_id FROM action_items "
               "WHERE id=%s), carried_from=%s WHERE id=%s", (old, old, new))
    _status(db, co, u, old, "open", "done", MON)
    _status(db, co, u, new, "done", "closed", TUE)

    out = content_edits.list_action_finishes(
        db, [s["id"]], MON, WED, company_id=co["id"], tz=TZ)

    assert [(r["text"], r["status"]) for r in out] == [("Order the steel", "closed")]


def test_finishes_respect_the_author_filter_and_tenant(db):
    co, s, u = _seed(db)
    other = users.upsert_user(db, f"sub-{uuid.uuid4().hex[:8]}", "o@x.nz",
                              company_id=co["id"])
    _status(db, co, u, _action(db, s, u, "Mine"), "open", "done", MON)
    _status(db, co, other, _action(db, s, other, "Theirs"), "open", "done", MON)

    mine = content_edits.list_action_finishes(
        db, [s["id"]], MON, MON, company_id=co["id"], author_ids=[u["id"]], tz=TZ)
    wrong_tenant = content_edits.list_action_finishes(
        db, [s["id"]], MON, MON, company_id=uuid.uuid4(), tz=TZ)

    assert [r["text"] for r in mine] == ["Mine"]
    assert wrong_tenant == []
    assert content_edits.list_action_finishes(
        db, [s["id"]], MON, MON, author_ids=[], tz=TZ) == []


def test_a_non_work_topic_is_not_listed(db):
    co, s, u = _seed(db)
    aid = _action(db, s, u, "Pick up the kids")
    db.execute("UPDATE topics SET work_class='non_work' WHERE id="
               "(SELECT topic_id FROM action_items WHERE id=%s)", (aid,))
    _status(db, co, u, aid, "open", "done", MON)

    assert content_edits.list_action_finishes(
        db, [s["id"]], MON, MON, company_id=co["id"], tz=TZ) == []


def test_thread_open_items_count_in_progress_and_blocked_not_done_or_closed(db):
    """Open = not finished. The count used to be `status='open'` alone, so an item
    somebody had STARTED dropped out of a thread's outstanding work."""
    co, s, u = _seed(db)
    t = topics.upsert_topic(db, s["id"], MON, "Slab", user_id=u["id"], action_items=[
        {"text": "a"}, {"text": "b"}, {"text": "c"}, {"text": "d"}, {"text": "e"}])
    for text, status in (("b", "in_progress"), ("c", "blocked"), ("d", "done"),
                         ("e", "closed")):
        db.execute("UPDATE action_items SET status=%s WHERE topic_id=%s AND text=%s",
                   (status, t["id"], text))
    th = threads.create_thread(db, s["id"], "Slab", MON, MON)
    threads.attach_topic(db, t["id"], th["id"], MON)

    assert threads.thread_facts(db, th["id"])["open_items"] == 3
    assert threads.facts_for_threads(db, [th["id"]])[str(th["id"])]["open_items"] == 3


def test_site_rollup_open_actions_count_everything_not_finished(db):
    from repositories import rollup
    co, s, u = _seed(db)
    t = topics.upsert_topic(db, s["id"], MON, "Slab", user_id=u["id"], action_items=[
        {"text": "a"}, {"text": "b"}, {"text": "c"}, {"text": "d"}])
    for text, status in (("b", "in_progress"), ("c", "done"), ("d", "closed")):
        db.execute("UPDATE action_items SET status=%s WHERE topic_id=%s AND text=%s",
                   (status, t["id"], text))

    counts = rollup.portfolio_counts(db, [s["id"]])[str(s["id"])]

    assert counts["total_actions"] == 4
    assert counts["open_actions"] == 2
