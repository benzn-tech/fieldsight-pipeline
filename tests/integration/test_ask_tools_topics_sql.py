"""Ask tools topics SQL (spec 2026-10-15-ask-retrieval-tools §3.1), against a real PostgreSQL.

Skipped without TEST_DATABASE_URL. A SKIP IS NOT A PASS — read the CI run.
"""
import pytest
from repositories import companies, sites, topics, users

pytestmark = pytest.mark.integration


def _user(db, co, folder):
    u = users.upsert_user(db, f"sub-{folder}", f"{folder}@x.nz", company_id=co["id"],
                          first_name=folder)
    db.execute("UPDATE users SET folder_name=%s WHERE id=%s", (folder, u["id"]))
    return u


@pytest.fixture
def world(db):
    co = companies.create_company(db, "Acme")
    s1 = sites.create_site(db, co["id"], "S1")
    s2 = sites.create_site(db, co["id"], "S2")
    ben = _user(db, co, "Ben")
    mike = _user(db, co, "Mike")
    return {"co": co, "s1": s1, "s2": s2, "ben": ben, "mike": mike}


def _t(db, w, site, user, day, title, src, **kw):
    return topics.upsert_topic(db, w[site]["id"], day, title, user_id=w[user]["id"] if user else None,
                               source_s3_key=src, **kw)


def _delete_topic(db, w, topic):
    # redactions (0022 + 0041): company_id and reason are NOT NULL; the deletion
    # predicate reads target_type='topic', scope='deleted', reverted_at IS NULL.
    db.execute("INSERT INTO redactions (company_id, target_type, target_id, reason, scope) "
               "VALUES (%s, 'topic', %s, 'test', 'deleted')", (w["co"]["id"], topic["id"]))


def test_lists_visible_work_topics_once_per_family(db, world):
    w = world
    _t(db, w, "s1", "ben", "2026-10-14", "Pour", "extractions/2026-10-14/Ben/a.json",
       summary="B2 slab", time_range="09:00 - 09:40")
    _t(db, w, "s1", "ben", "2026-10-14", "Pour (report)", "reports/2026-10-14/Ben/daily_report.json")
    _t(db, w, "s1", "ben", "2026-10-14", "Lunch chat", "extractions/2026-10-14/Ben/b.json",
       work_class="non_work")
    _t(db, w, "s1", "mike", "2026-10-14", "Scaffold", "reports/2026-10-14/Mike/daily_report.json")
    _t(db, w, "s2", "ben", "2026-10-14", "Other site", "extractions/2026-10-14/Ben/c.json")
    rows = topics.day_overview_rows(db, [w["s1"]["id"]], "2026-10-14", "2026-10-14")
    titles = sorted(r["title"] for r in rows)
    # extraction family wins for Ben; report family stands for Mike (no extraction rows);
    # non_work and the out-of-ACL site are absent
    assert titles == ["Pour", "Scaffold"]
    pour = next(r for r in rows if r["title"] == "Pour")
    assert pour["time_range"] == "09:00 - 09:40" and pour["site_name"] == "S1"


def test_graded_filter_hides_other_authors_and_null_user(db, world):
    w = world
    _t(db, w, "s1", "mike", "2026-10-14", "Mike's", "extractions/2026-10-14/Mike/a.json")
    _t(db, w, "s1", None, "2026-10-14", "Unattributed", "reports/2026-10-14/x/daily_report.json")
    _t(db, w, "s1", "ben", "2026-10-14", "Ben's", "extractions/2026-10-14/Ben/a.json")
    rows = topics.day_overview_rows(db, [w["s1"]["id"]], "2026-10-14", "2026-10-14",
                                    author_ids={str(w["ben"]["id"])})
    assert [r["title"] for r in rows] == ["Ben's"]


def test_deleted_topic_absent(db, world):
    w = world
    t = _t(db, w, "s1", "ben", "2026-10-14", "Gone", "extractions/2026-10-14/Ben/a.json")
    _delete_topic(db, w, t)
    assert topics.day_overview_rows(db, [w["s1"]["id"]], "2026-10-14", "2026-10-14") == []


def test_superseded_extraction_absent_and_does_not_suppress_report_family(db, world):
    """A superseded extraction topic is the stale half of a re-extraction: it must not
    show, and -- when it is the only extraction row for that (site,user,day) -- it must
    not count as 'an extraction topic exists', or the report family vanishes too and the
    day reads as empty. Covers LIVE_TOPIC_PREDICATE on both the outer row and the
    family rule's inner EXISTS."""
    w = world
    stale = _t(db, w, "s1", "ben", "2026-10-14", "Stale pass", "extractions/2026-10-14/Ben/a.json")
    db.execute("UPDATE topics SET superseded_at = now() WHERE id = %s", (stale["id"],))
    _t(db, w, "s1", "ben", "2026-10-14", "Pour (report)", "reports/2026-10-14/Ben/daily_report.json")
    rows = topics.day_overview_rows(db, [w["s1"]["id"]], "2026-10-14", "2026-10-14")
    assert [r["title"] for r in rows] == ["Pour (report)"]


def test_latest_visible_topic_date_skips_non_work_and_deleted(db, world):
    w = world
    _t(db, w, "s1", "ben", "2026-10-10", "Real", "extractions/2026-10-10/Ben/a.json")
    _t(db, w, "s1", "ben", "2026-10-12", "Chat", "extractions/2026-10-12/Ben/a.json",
       work_class="non_work")
    gone = _t(db, w, "s1", "ben", "2026-10-13", "Gone", "extractions/2026-10-13/Ben/a.json")
    _delete_topic(db, w, gone)
    assert topics.latest_visible_topic_date(db, [w["s1"]["id"]], "2026-10-14") == "2026-10-10"


def test_latest_visible_topic_date_respects_graded_filter(db, world):
    w = world
    _t(db, w, "s1", "mike", "2026-10-13", "Mike's", "extractions/2026-10-13/Mike/a.json")
    _t(db, w, "s1", "ben", "2026-10-09", "Ben's", "extractions/2026-10-09/Ben/a.json")
    assert topics.latest_visible_topic_date(
        db, [w["s1"]["id"]], "2026-10-14", author_ids={str(w["ben"]["id"])}) == "2026-10-09"
