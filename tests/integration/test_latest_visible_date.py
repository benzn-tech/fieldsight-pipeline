"""`latest_visible_date` against a real PostgreSQL.

A fake connection records SQL without parsing it, so a unit test here would
prove only that a string was passed somewhere. Everything this function has to
get right -- that `max()` over an empty set is NULL and not an error, that the
site pin holds, that a deleted recording's DATE is hidden and not merely its
text -- is answered by the database or by nothing.

The deletion case is the one worth stating. Ask widening onto a day whose only
recording was deleted would disclose that something was recorded that day: the
chunks stay hidden, and the answer says "the nearest records are from the 27th"
over a recording the customer removed. Hiding text while leaking the date is the
shape this repository has already shipped once.
"""
import pytest

from repositories import chunks, companies, sites, topics

pytestmark = pytest.mark.integration


def _vec(hot=0, dim=1024):
    v = [0.0] * dim
    v[hot] = 1.0
    return v


def _site(db, name="S1"):
    co = companies.create_company(db, f"Acme-{name}")
    return sites.create_site(db, co["id"], name)


def test_returns_none_when_the_caller_can_see_nothing(db):
    """max() over no rows is NULL. The caller must get None, not a crash and
    not a bare date object -- widening on an empty corpus is a normal state."""
    site = _site(db, "empty")
    assert chunks.latest_visible_date(db, [site["id"]]) is None


def test_returns_the_most_recent_day(db):
    site = _site(db, "recent")
    for d in ("2026-08-17", "2026-08-27", "2026-08-24"):
        chunks.insert_chunk(db, site["id"], d, "topic", f"note {d}", _vec())

    assert chunks.latest_visible_date(db, [site["id"]]) == "2026-08-27"


def test_the_bound_looks_backwards_only(db):
    """Asking about last week must not be answered with something recorded
    after it. The bound is the reason widening cannot travel forward."""
    site = _site(db, "bounded")
    for d in ("2026-08-10", "2026-08-27"):
        chunks.insert_chunk(db, site["id"], d, "topic", f"note {d}", _vec())

    assert chunks.latest_visible_date(db, [site["id"]], on_or_before="2026-08-20") == "2026-08-10"
    assert chunks.latest_visible_date(db, [site["id"]], on_or_before="2026-08-01") is None


def test_another_sites_day_is_not_visible(db):
    """Same pin as the search itself. A date is small, but it is still someone
    else's site telling you it had a meeting."""
    mine = _site(db, "mine")
    theirs = _site(db, "theirs")
    chunks.insert_chunk(db, mine["id"], "2026-08-10", "topic", "mine", _vec())
    chunks.insert_chunk(db, theirs["id"], "2026-08-27", "topic", "theirs", _vec())

    assert chunks.latest_visible_date(db, [mine["id"]]) == "2026-08-10"


def test_an_archived_recordings_day_disappears_with_it(db):
    """Deleting a recording moves its chunks to the archive table. The date has
    to go with them: a widening that lands on the 27th because a deleted
    recording was made that day reports the deletion to the person who asked."""
    site = _site(db, "deleted")
    t = topics.upsert_topic(db, site["id"], "2026-08-27", "Coordination")
    chunks.insert_chunk(db, site["id"], "2026-08-10", "topic", "kept", _vec())
    chunks.insert_chunk(db, site["id"], "2026-08-27", "topic", "removed", _vec(), topic_id=t["id"])

    assert chunks.latest_visible_date(db, [site["id"]]) == "2026-08-27"

    import uuid
    chunks.archive_chunks_for_session(
        db, "sid" + "0" * 32, [t["id"]], uuid.uuid4(), company_id=site["company_id"])

    assert chunks.latest_visible_date(db, [site["id"]]) == "2026-08-10"
