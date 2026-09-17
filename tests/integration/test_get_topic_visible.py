"""`get_topic_visible` against a real PostgreSQL.

The unit file pins the statement's text; only a database can say what it
returns. Each case below is one reason a topic must NOT be pinned into an Ask
prompt, plus the NULL-author case that must not collapse to "match nothing"
when no author restriction applies. Skips cleanly without TEST_DATABASE_URL.
"""
import pytest

from repositories import companies, redactions, sites, topics, users

pytestmark = pytest.mark.integration

DAY = "2026-09-03"


def _world(db, tag):
    co = companies.create_company(db, f"Scoped-{tag}")
    s1 = sites.create_site(db, co["id"], f"S1-{tag}")
    s2 = sites.create_site(db, co["id"], f"S2-{tag}")
    user = users.upsert_field_only_user(db, co["id"], f"Scoped_{tag}", "F", "W", "worker")
    return co, s1, s2, user


def _sid(site):
    return [str(site["id"])]


def test_a_work_topic_in_reach_comes_back_with_its_action_items(db):
    co, s1, _, user = _world(db, "plain")
    t = topics.upsert_topic(db, s1["id"], DAY, "Scaffold handover", user_id=user["id"],
                            summary="Signed off.", work_class="work",
                            action_items=[{"text": "Send tag photos", "responsible": "Ben"}])

    got = topics.get_topic_visible(db, str(t["id"]), _sid(s1), None)

    assert got["title"] == "Scaffold handover"
    assert got["report_date"] == DAY
    assert got["site_id"] == str(s1["id"]) and got["site_name"] == "S1-plain"
    assert got["user_id"] == str(user["id"])
    assert got["action_items"] == [{"text": "Send tag photos", "responsible": "Ben",
                                    "deadline": None, "status": "open"}]


def test_an_action_item_with_no_deadline_date_falls_back_to_deadline_text(db):
    """topics.py ~723/~820 select both deadline and deadline_text; get_topic_visible
    used to select only deadline, so a text-only deadline (e.g. "next week") was
    silently dropped from the Ask prompt."""
    _, s1, _, user = _world(db, "deadlinetext")
    t = topics.upsert_topic(db, s1["id"], DAY, "Scaffold handover", user_id=user["id"],
                            work_class="work",
                            action_items=[{"text": "Send tag photos", "responsible": "Ben",
                                          "deadline_text": "next week"}])

    got = topics.get_topic_visible(db, str(t["id"]), _sid(s1), None)

    assert got["action_items"] == [{"text": "Send tag photos", "responsible": "Ben",
                                    "deadline": "next week", "status": "open"}]


def test_an_unclassified_topic_is_visible(db):
    """`IS DISTINCT FROM`, not `<>`: work_class NULL must not be excluded."""
    _, s1, _, user = _world(db, "nullclass")
    t = topics.upsert_topic(db, s1["id"], DAY, "Unclassified", user_id=user["id"])
    assert topics.get_topic_visible(db, str(t["id"]), _sid(s1), None) is not None


def test_a_site_outside_reach_is_none(db):
    _, s1, s2, user = _world(db, "site")
    t = topics.upsert_topic(db, s2["id"], DAY, "Other site", user_id=user["id"], work_class="work")
    assert topics.get_topic_visible(db, str(t["id"]), _sid(s1), None) is None


def test_an_author_outside_the_author_set_is_none(db):
    co, s1, _, user = _world(db, "author")
    other = users.upsert_field_only_user(db, co["id"], "Scoped_author_other", "O", "W", "worker")
    t = topics.upsert_topic(db, s1["id"], DAY, "Theirs", user_id=user["id"], work_class="work")
    assert topics.get_topic_visible(db, str(t["id"]), _sid(s1), [str(other["id"])]) is None
    assert topics.get_topic_visible(db, str(t["id"]), _sid(s1), [str(user["id"])]) is not None


def test_an_empty_author_list_is_none_not_unfiltered(db):
    """author_ids=[] must fail closed: it means the caller's allow-set resolved to nobody,
    never "no restriction applies" (that is author_ids=None). A topic visible with no
    author set, or with the author's own id in the set, must still come back None here."""
    _, s1, _, user = _world(db, "emptyauthors")
    t = topics.upsert_topic(db, s1["id"], DAY, "Theirs", user_id=user["id"], work_class="work")
    assert topics.get_topic_visible(db, str(t["id"]), _sid(s1), None) is not None
    assert topics.get_topic_visible(db, str(t["id"]), _sid(s1), []) is None


def test_non_work_is_none(db):
    _, s1, _, user = _world(db, "nonwork")
    t = topics.upsert_topic(db, s1["id"], DAY, "Lunch", user_id=user["id"], work_class="non_work")
    assert topics.get_topic_visible(db, str(t["id"]), _sid(s1), None) is None


def test_an_active_analysis_redaction_hides_it_and_a_revert_restores_it(db):
    co, s1, _, user = _world(db, "redacted")
    t = topics.upsert_topic(db, s1["id"], DAY, "Family call", user_id=user["id"], work_class="work")
    red = redactions.create_redaction(db, co["id"], t["id"], "privacy", None, "admin")
    assert red["scope"] == "analysis"
    assert topics.get_topic_visible(db, str(t["id"]), _sid(s1), None) is None

    redactions.revert_redaction(db, red["id"], co["id"])
    assert topics.get_topic_visible(db, str(t["id"]), _sid(s1), None) is not None


def test_a_null_author_topic_is_visible_without_an_author_set_and_never_with_one(db):
    """`= ANY(ARRAY[...])` against NULL is never true -- wanted when a set applies,
    and the `IS NULL` arm must keep it visible when none does."""
    _, s1, _, user = _world(db, "nulluser")
    t = topics.upsert_topic(db, s1["id"], DAY, "Bridge miss", user_id=None, work_class="work")

    got = topics.get_topic_visible(db, str(t["id"]), _sid(s1), None)
    assert got is not None and got["user_id"] is None
    assert topics.get_topic_visible(db, str(t["id"]), _sid(s1), [str(user["id"])]) is None


def test_a_deleted_recordings_topic_is_none(db):
    co, s1, _, user = _world(db, "deleted")
    prefix = f"extractions/Scoped_deleted/{DAY}/sid" + "0" * 32
    t = topics.upsert_topic(db, s1["id"], DAY, "Removed", user_id=user["id"], work_class="work",
                            source_s3_key=prefix + ".json")
    assert topics.get_topic_visible(db, str(t["id"]), _sid(s1), None) is not None

    redactions.create_recording_tombstone(db, co["id"], prefix, "deleted", None, "admin")
    assert topics.get_topic_visible(db, str(t["id"]), _sid(s1), None) is None


def test_an_unknown_id_is_none(db):
    _, s1, _, _ = _world(db, "unknown")
    assert topics.get_topic_visible(db, "00000000-0000-4000-8000-000000000000", _sid(s1), None) is None
