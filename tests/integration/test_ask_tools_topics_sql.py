"""Ask tools topics SQL (spec 2026-10-15-ask-retrieval-tools §3.1), against a real PostgreSQL.

The family rule mirrors /timeline (lambda_org_api._render_timeline_for_user +
topics.has_topics_for_source_prefix): per (user, day) across the caller's site_ids, the
extraction family wins if ANY non-superseded extraction topic exists -- non_work and
deleted ones count for presence -- else the report family (`reports/` keys only).

Fixture keys use the real lake layout (src/lake_keys.py):
  extractions/{folder}/{date}/{base}.json   and   reports/{date}/{folder}/daily_report.json

Skipped without TEST_DATABASE_URL. A SKIP IS NOT A PASS — read the CI run.
"""
import pytest
from repositories import companies, findings, sites, topics, users

pytestmark = pytest.mark.integration

D = "2026-10-14"


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


def _ext(folder, day, base="a"):
    return f"extractions/{folder}/{day}/{base}.json"


def _rep(folder, day):
    return f"reports/{day}/{folder}/daily_report.json"


def _t(db, w, site, user, day, title, src, **kw):
    return topics.upsert_topic(db, w[site]["id"], day, title, user_id=w[user]["id"] if user else None,
                               source_s3_key=src, **kw)


def _delete_topic(db, w, topic):
    # redactions (0022 + 0041): company_id and reason are NOT NULL; the deletion
    # predicate reads target_type='topic', scope='deleted', reverted_at IS NULL.
    db.execute("INSERT INTO redactions (company_id, target_type, target_id, reason, scope) "
               "VALUES (%s, 'topic', %s, 'test', 'deleted')", (w["co"]["id"], topic["id"]))


def _ids(w, *names):
    return [w[n]["id"] for n in names]


def _titles(rows):
    return sorted(r["title"] for r in rows)


def test_lists_visible_work_topics_once_per_family(db, world):
    w = world
    _t(db, w, "s1", "ben", D, "Pour", _ext("Ben", D), summary="B2 slab", time_range="09:00 - 09:40")
    _t(db, w, "s1", "ben", D, "Pour (report)", _rep("Ben", D))
    _t(db, w, "s1", "ben", D, "Lunch chat", _ext("Ben", D, "b"), work_class="non_work")
    _t(db, w, "s1", "mike", D, "Scaffold", _rep("Mike", D))
    _t(db, w, "s2", "ben", D, "Other site", _ext("Ben", D, "c"))
    rows = topics.day_overview_rows(db, _ids(w, "s1"), D, D)
    # extraction family wins for Ben; report family stands for Mike (no extraction rows);
    # non_work and the out-of-ACL site are absent
    assert _titles(rows) == ["Pour", "Scaffold"]
    pour = next(r for r in rows if r["title"] == "Pour")
    assert pour["time_range"] == "09:00 - 09:40" and pour["site_name"] == "S1"


def test_rows_carry_the_site_slug_for_citation_cards(db, world):
    w = world
    db.execute("UPDATE sites SET slug='s-one' WHERE id=%s", (w["s1"]["id"],))
    _t(db, w, "s1", "ben", D, "Pour", _ext("Ben", D))
    _t(db, w, "s2", "ben", D, "Other", _ext("Ben", D, "c"))
    db.execute("UPDATE sites SET slug=NULL WHERE id=%s", (w["s2"]["id"],))
    rows = topics.day_overview_rows(db, _ids(w, "s1", "s2"), D, D)
    assert {r["title"]: r["site_slug"] for r in rows} == {"Pour": "s-one", "Other": None}


def test_graded_filter_hides_other_authors_and_null_user(db, world):
    w = world
    _t(db, w, "s1", "mike", D, "Mike's", _ext("Mike", D))
    _t(db, w, "s1", None, D, "Unattributed", _rep("x", D))
    _t(db, w, "s1", "ben", D, "Ben's", _ext("Ben", D))
    rows = topics.day_overview_rows(db, _ids(w, "s1"), D, D, author_ids={str(w["ben"]["id"])})
    assert [r["title"] for r in rows] == ["Ben's"]


def test_site_id_and_user_id_kwargs_narrow(db, world):
    w = world
    _t(db, w, "s1", "ben", D, "Ben S1", _ext("Ben", D))
    _t(db, w, "s2", "ben", D, "Ben S2", _ext("Ben", D, "b"))
    _t(db, w, "s1", "mike", D, "Mike S1", _ext("Mike", D))
    both = _ids(w, "s1", "s2")
    assert _titles(topics.day_overview_rows(db, both, D, D, site_id=w["s2"]["id"])) == ["Ben S2"]
    assert _titles(topics.day_overview_rows(db, both, D, D, user_id=w["mike"]["id"])) == ["Mike S1"]
    assert topics.latest_visible_topic_date(db, both, "2026-10-15", site_id=w["s2"]["id"],
                                            user_id=w["mike"]["id"]) is None


def test_deleted_topic_absent(db, world):
    w = world
    t = _t(db, w, "s1", "ben", D, "Gone", _ext("Ben", D))
    _delete_topic(db, w, t)
    assert topics.day_overview_rows(db, _ids(w, "s1"), D, D) == []


def test_non_work_only_extraction_day_hides_the_report_too(db, world):
    """non_work counts for family presence (as on /timeline), so the report summary --
    which can paraphrase the personal talk -- never stands in for it."""
    w = world
    _t(db, w, "s1", "ben", D, "Lunch chat", _ext("Ben", D), work_class="non_work")
    _t(db, w, "s1", "ben", D, "Day (report)", _rep("Ben", D))
    assert topics.day_overview_rows(db, _ids(w, "s1"), D, D) == []


def test_all_deleted_extraction_day_hides_the_report_too(db, world):
    """Presence ignores deletion, mirroring has_topics_for_source_prefix/_should_defer."""
    w = world
    gone = _t(db, w, "s1", "ben", D, "Gone", _ext("Ben", D))
    _delete_topic(db, w, gone)
    _t(db, w, "s1", "ben", D, "Day (report)", _rep("Ben", D))
    assert topics.day_overview_rows(db, _ids(w, "s1"), D, D) == []


def test_superseded_extraction_absent_and_does_not_suppress_report_family(db, world):
    """A superseded extraction topic is the stale half of a re-extraction: it must not
    show, and -- when it is the only extraction row for that user-day -- it must not count
    as presence, or the day reads as empty (LIVE_TOPIC_PREDICATE on both sides)."""
    w = world
    stale = _t(db, w, "s1", "ben", D, "Stale pass", _ext("Ben", D))
    db.execute("UPDATE topics SET superseded_at = now() WHERE id = %s", (stale["id"],))
    _t(db, w, "s1", "ben", D, "Pour (report)", _rep("Ben", D))
    rows = topics.day_overview_rows(db, _ids(w, "s1"), D, D)
    assert [r["title"] for r in rows] == ["Pour (report)"]


def test_presence_spans_reachable_sites_only(db, world):
    """Presence is per (user, day) across the caller's site_ids -- decided before the
    narrowed site_id kwarg, never across sites the caller cannot reach."""
    w = world
    _t(db, w, "s2", "ben", D, "Ben S2 ext", _ext("Ben", D))
    _t(db, w, "s1", "ben", D, "Ben S1 report", _rep("Ben", D))
    assert _titles(topics.day_overview_rows(db, _ids(w, "s1", "s2"), D, D)) == ["Ben S2 ext"]
    assert _titles(topics.day_overview_rows(db, _ids(w, "s1"), D, D)) == ["Ben S1 report"]
    assert topics.day_overview_rows(db, _ids(w, "s1", "s2"), D, D, site_id=w["s1"]["id"]) == []


def test_null_user_presence_is_per_site_and_day(db, world):
    w = world
    _t(db, w, "s1", None, D, "Anon S1 ext", _ext("x", D))
    _t(db, w, "s1", None, D, "Anon S1 report", _rep("x", D))
    _t(db, w, "s2", None, D, "Anon S2 report", _rep("x", D))
    rows = topics.day_overview_rows(db, _ids(w, "s1", "s2"), D, D)
    assert _titles(rows) == ["Anon S1 ext", "Anon S2 report"]


def test_presence_is_per_day(db, world):
    w = world
    _t(db, w, "s1", "ben", "2026-10-13", "Mon report", _rep("Ben", "2026-10-13"))
    _t(db, w, "s1", "ben", D, "Tue ext", _ext("Ben", D))
    rows = topics.day_overview_rows(db, _ids(w, "s1"), "2026-10-13", D)
    assert [r["title"] for r in rows] == ["Mon report", "Tue ext"]


def test_null_or_foreign_key_topic_never_shown(db, world):
    w = world
    _t(db, w, "s1", "ben", D, "No key", None)
    _t(db, w, "s1", "ben", D, "Other prefix", f"other/{D}/Ben/x.json")
    assert topics.day_overview_rows(db, _ids(w, "s1"), D, D) == []
    assert topics.latest_visible_topic_date(db, _ids(w, "s1"), "2026-10-15") is None


def test_rows_are_chronological(db, world):
    w = world
    _t(db, w, "s1", "ben", D, "Afternoon", _ext("Ben", D, "c"),
       occurred_at="2026-10-14T03:00:00+00:00")
    _t(db, w, "s1", "mike", "2026-10-13", "Yesterday", _ext("Mike", "2026-10-13"))
    _t(db, w, "s1", "ben", D, "Untimed", _ext("Ben", D, "b"))
    _t(db, w, "s1", "ben", D, "Morning", _ext("Ben", D, "a"),
       occurred_at="2026-10-13T21:00:00+00:00")
    rows = topics.day_overview_rows(db, _ids(w, "s1"), "2026-10-13", D)
    assert [r["title"] for r in rows] == ["Yesterday", "Morning", "Afternoon", "Untimed"]


def test_counts_tasks_and_findings(db, world):
    w = world
    t = _t(db, w, "s1", "ben", D, "Pour", _ext("Ben", D),
           action_items=[{"text": "order rebar"}, {"text": "book pump"}])
    findings.insert_findings(db, t["id"], w["s1"]["id"], [{"observation": "x", "domain": "safety"}])
    _t(db, w, "s1", "ben", D, "Bare", _ext("Ben", D, "b"))
    rows = {r["title"]: r for r in topics.day_overview_rows(db, _ids(w, "s1"), D, D)}
    assert (rows["Pour"]["n_tasks"], rows["Pour"]["n_findings"]) == (2, 1)
    assert (rows["Bare"]["n_tasks"], rows["Bare"]["n_findings"]) == (0, 0)


def test_fetches_limit_plus_one_so_the_caller_can_see_truncation(db, world):
    w = world
    for i in range(3):
        _t(db, w, "s1", "ben", D, f"T{i}", _ext("Ben", D, f"b{i}"))
    assert len(topics.day_overview_rows(db, _ids(w, "s1"), D, D, limit=1)) == 2


def test_latest_visible_topic_date_skips_non_work_and_deleted(db, world):
    w = world
    _t(db, w, "s1", "ben", "2026-10-10", "Real", _ext("Ben", "2026-10-10"))
    _t(db, w, "s1", "ben", "2026-10-12", "Chat", _ext("Ben", "2026-10-12"), work_class="non_work")
    gone = _t(db, w, "s1", "ben", "2026-10-13", "Gone", _ext("Ben", "2026-10-13"))
    _delete_topic(db, w, gone)
    assert topics.latest_visible_topic_date(db, _ids(w, "s1"), D) == "2026-10-10"


def test_latest_visible_topic_date_applies_the_family_rule(db, world):
    w = world
    _t(db, w, "s1", "ben", "2026-10-10", "Real", _ext("Ben", "2026-10-10"))
    _t(db, w, "s1", "ben", "2026-10-12", "Chat", _ext("Ben", "2026-10-12"), work_class="non_work")
    _t(db, w, "s1", "ben", "2026-10-12", "Report", _rep("Ben", "2026-10-12"))
    assert topics.latest_visible_topic_date(db, _ids(w, "s1"), D) == "2026-10-10"


def test_latest_visible_topic_date_is_strictly_before(db, world):
    w = world
    _t(db, w, "s1", "ben", "2026-10-11", "Earlier", _ext("Ben", "2026-10-11"))
    _t(db, w, "s1", "ben", D, "On the day", _ext("Ben", D))
    assert topics.latest_visible_topic_date(db, _ids(w, "s1"), D) == "2026-10-11"


def test_latest_visible_topic_date_respects_graded_filter(db, world):
    w = world
    _t(db, w, "s1", "mike", "2026-10-13", "Mike's", _ext("Mike", "2026-10-13"))
    _t(db, w, "s1", "ben", "2026-10-09", "Ben's", _ext("Ben", "2026-10-09"))
    assert topics.latest_visible_topic_date(
        db, _ids(w, "s1"), D, author_ids={str(w["ben"]["id"])}) == "2026-10-09"
