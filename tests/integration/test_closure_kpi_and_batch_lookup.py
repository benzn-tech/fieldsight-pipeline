"""Integration: two repository reads that a sweep found uncovered.

Neither is broken — I checked before writing, and say so rather than implying a rescue.
What they have in common is the property that made tonight's two crashes possible: their
SQL had never been executed by anything. A connection double records the string.

`count_action_closures_by_day` backs a dashboard KPI (`lambda_org_api.py:2900`) and is the
most structurally involved uncovered query in the tree — a dynamic WHERE list, a join
across `row_id`, which migration 0019 documents as a SOFT polymorphic reference with no
foreign key, and two `AT TIME ZONE` conversions. The timezone arithmetic in particular
cannot be checked by looking: a closure at 23:00 UTC belongs to the NEXT local day, and
whether the query agrees is a question only Postgres answers.

`list_deleted_batches_for_prefix` is here for its return shape. Its sibling
`active_batches_for_day` was changed tonight to return strings after a UUID-vs-string
mismatch showed up — a mismatch that cannot raise, only make a comparison quietly False.
"""
import uuid
from datetime import timedelta

import pytest

from repositories import (companies, content_edits, redactions, sites, topics, users)

pytestmark = pytest.mark.integration

DATE = "2026-08-17"
TZ = "Pacific/Auckland"


def _seed(db):
    co = companies.create_company(db, f"KPI-Co-{uuid.uuid4().hex[:6]}")
    s = sites.create_site(db, co["id"], "KPI-Site")
    u = users.upsert_user(db, f"sub-{uuid.uuid4().hex[:8]}", "k@x.nz", company_id=co["id"])
    return co, s, u


def _closed_action(db, co, s, u, *, at=None, text="Order steel"):
    """One action item plus the audit row that says somebody closed it."""
    t = topics.upsert_topic(db, s["id"], DATE, "Slab", user_id=u["id"],
                            action_items=[{"text": text}])
    aid = db.execute(
        "SELECT id FROM action_items WHERE topic_id=%s", (t["id"],)).fetchone()[0]
    row = content_edits.append_content_edit(
        db, co["id"], "action_items", aid, "status", "open", "closed", u["id"], "pm")
    if at is not None:
        db.execute("UPDATE content_edits SET created_at=%s WHERE id=%s", (at, row["id"]))
    return aid


def test_the_kpi_counts_a_closure_on_its_local_day(db):
    co, s, u = _seed(db)
    _closed_action(db, co, s, u)

    counts = content_edits.count_action_closures_by_day(
        db, [s["id"]], DATE, DATE, company_id=co["id"], tz=TZ)

    assert sum(counts.values()) == 1, counts


def test_an_edit_that_is_not_a_closure_is_not_counted(db):
    """`field='status'` and `after_text='closed'` are both load-bearing: a priority tweak
    writes an audit row too, and counting it would inflate the number a manager reads."""
    co, s, u = _seed(db)
    t = topics.upsert_topic(db, s["id"], DATE, "Slab", user_id=u["id"],
                            action_items=[{"text": "Book pump"}])
    aid = db.execute(
        "SELECT id FROM action_items WHERE topic_id=%s", (t["id"],)).fetchone()[0]
    content_edits.append_content_edit(
        db, co["id"], "action_items", aid, "priority", "low", "high", u["id"], "pm")

    counts = content_edits.count_action_closures_by_day(
        db, [s["id"]], DATE, DATE, company_id=co["id"], tz=TZ)

    assert counts == {}


def test_reopening_and_closing_again_is_one_closure_per_event(db):
    """`before_text IS DISTINCT FROM 'closed'` — a save that leaves it closed must not
    count a second time."""
    co, s, u = _seed(db)
    t = topics.upsert_topic(db, s["id"], DATE, "Slab", user_id=u["id"],
                            action_items=[{"text": "Chase delivery"}])
    aid = db.execute(
        "SELECT id FROM action_items WHERE topic_id=%s", (t["id"],)).fetchone()[0]
    content_edits.append_content_edit(
        db, co["id"], "action_items", aid, "status", "open", "closed", u["id"], "pm")
    content_edits.append_content_edit(
        db, co["id"], "action_items", aid, "status", "closed", "closed", u["id"], "pm")

    counts = content_edits.count_action_closures_by_day(
        db, [s["id"]], DATE, DATE, company_id=co["id"], tz=TZ)

    assert sum(counts.values()) == 1, counts


def test_the_day_boundary_is_local_not_utc(db):
    """The one thing reading the SQL cannot settle. A closure at 11:30 UTC on the 17th is
    23:30 NZ on the SAME day; at 12:30 UTC it is 00:30 NZ on the 18th and belongs to the
    next bucket. If the `AT TIME ZONE` pair were wrong or inverted, both would land in the
    same bucket and nobody would notice until a manager queried a quiet day."""
    co, s, u = _seed(db)
    before = db.execute("SELECT %s::timestamptz", ("2026-08-17T11:30:00Z",)).fetchone()[0]
    after = before + timedelta(hours=1)
    _closed_action(db, co, s, u, at=before, text="Late on the 17th")
    _closed_action(db, co, s, u, at=after, text="Early on the 18th")

    counts = content_edits.count_action_closures_by_day(
        db, [s["id"]], "2026-08-17", "2026-08-18", company_id=co["id"], tz=TZ)

    assert counts.get("2026-08-17") == 1, counts
    assert counts.get("2026-08-18") == 1, counts


def test_an_empty_reach_returns_nothing_without_asking(db):
    """`[]` must mean 'nothing accessible', never 'no filter'. This repo has shipped the
    other reading once already."""
    assert content_edits.count_action_closures_by_day(db, [], DATE, DATE) == {}


def test_another_tenants_closure_is_not_counted(db):
    co_a, s_a, u_a = _seed(db)
    co_b, s_b, u_b = _seed(db)
    _closed_action(db, co_a, s_a, u_a)
    _closed_action(db, co_b, s_b, u_b)

    counts = content_edits.count_action_closures_by_day(
        db, [s_a["id"], s_b["id"]], DATE, DATE, company_id=co_a["id"], tz=TZ)

    assert sum(counts.values()) == 1, counts


# ---- the batch lookup's return shape ----------------------------------

def test_batches_for_prefix_returns_the_same_shape_as_its_sibling(db):
    """`active_batches_for_day` was changed tonight to return strings after a UUID/str
    mismatch turned up — the kind that cannot raise, only make a comparison quietly False.
    Two sibling functions returning different shapes for the same id is how the next one
    happens."""
    co, _s, _u = _seed(db)
    prefix = f"extractions/Folder/{DATE}/sidCCC"
    batch = str(uuid.uuid4())
    redactions.create_recording_tombstone(db, co["id"], prefix, "test", None, "admin",
                                          batch_id=batch)

    rows = redactions.list_deleted_batches_for_prefix(db, prefix + ".json")

    assert rows, "the prefix lookup found nothing for a tombstone that covers it"
    assert all(isinstance(r["batch_id"], str) for r in rows), \
        "returns UUID objects where its sibling returns strings"
    assert batch in [r["batch_id"] for r in rows]
