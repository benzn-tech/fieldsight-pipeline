"""`chunks.latest_matching_date` against a real PostgreSQL (Ask tools, spec
2026-10-15 §3.1 item 3, `latest_activity` with a query).

The answer is a DATE, and a date is still a disclosure: a deleted recording, an
author the caller cannot see, or a report-family row that the session family
hides must not be the reason the tool says "the last time was the 12th". So the
visibility rules are the search's own (`search_sql._visibility_predicate`), and
these tests exercise each arm against the database -- a fake connection would
prove only that a string was passed somewhere.
"""
import pytest

from repositories import chunks, companies, sites, topics

pytestmark = pytest.mark.integration


def _v(hot):
    v = [0.0] * 1024
    v[hot] = 1.0
    return v


def _site(db, name="S"):
    co = companies.create_company(db, f"Acme-{name}")
    return sites.create_site(db, co["id"], name)


def _user(db, company_id, tag):
    return db.execute(
        "INSERT INTO users (company_id, email, global_role, folder_name) "
        "VALUES (%s, %s, 'worker', %s) RETURNING id",
        (company_id, f"{tag}@x.com", tag)).fetchone()[0]


def test_latest_day_within_gate_and_window(db):
    s = _site(db)
    chunks.insert_chunk(db, s["id"], "2026-10-01", "topic", "concrete pour", _v(0))
    chunks.insert_chunk(db, s["id"], "2026-10-12", "topic", "scaffold", _v(5))      # far
    chunks.insert_chunk(db, s["id"], "2026-06-01", "topic", "concrete old", _v(0))  # out of window
    got = chunks.latest_matching_date(db, _v(0), [s["id"]], on_or_before="2026-10-15")
    assert got == "2026-10-01"


def test_window_edges_are_inclusive_and_the_future_is_excluded(db):
    s = _site(db)
    # window_days=92 ending 10-15 -> [2026-07-16, 2026-10-15]
    chunks.insert_chunk(db, s["id"], "2026-07-16", "topic", "first day", _v(0))
    chunks.insert_chunk(db, s["id"], "2026-07-15", "topic", "one before", _v(0))
    chunks.insert_chunk(db, s["id"], "2026-10-16", "topic", "tomorrow", _v(0))
    assert chunks.latest_matching_date(db, _v(0), [s["id"]], on_or_before="2026-10-15") \
        == "2026-07-16"
    assert chunks.latest_matching_date(db, _v(0), [s["id"]], on_or_before="2026-10-15",
                                       window_days=91) is None


def test_keyword_hit_passes_gate(db):
    s = _site(db)
    chunks.insert_chunk(db, s["id"], "2026-10-12", "topic", "the GIB delivery", _v(5))
    got = chunks.latest_matching_date(db, _v(0), [s["id"]], on_or_before="2026-10-15",
                                      query_text="GIB")
    assert got == "2026-10-12"


def test_keyword_arm_uses_the_search_expression_not_substring(db):
    """'a keyword hit counts' means what search means: the punctuation-split
    'simple' tsvector, so 'PS4/light' matches 'PS4' -- and a bare substring
    ('GI' inside 'GIB') does not match, as it would under ILIKE."""
    s = _site(db)
    chunks.insert_chunk(db, s["id"], "2026-10-12", "topic", "check PS4/light fitting", _v(5))
    chunks.insert_chunk(db, s["id"], "2026-10-13", "topic", "the GIB delivery", _v(5))
    assert chunks.latest_matching_date(db, _v(0), [s["id"]], on_or_before="2026-10-15",
                                       query_text="PS4") == "2026-10-12"
    assert chunks.latest_matching_date(db, _v(0), [s["id"]], on_or_before="2026-10-15",
                                       query_text="GI") is None
    # an OR query (lexical_terms.or_query's shape) matches either term
    assert chunks.latest_matching_date(db, _v(0), [s["id"]], on_or_before="2026-10-15",
                                       query_text="nothing or GIB") == "2026-10-13"


def test_empty_query_text_matches_nothing(db):
    s = _site(db)
    chunks.insert_chunk(db, s["id"], "2026-10-12", "topic", "anything at all", _v(5))
    assert chunks.latest_matching_date(db, _v(0), [s["id"]], on_or_before="2026-10-15",
                                       query_text="") is None


def test_other_site_never_counts(db):
    co = companies.create_company(db, "A")
    s1 = sites.create_site(db, co["id"], "S1")
    s2 = sites.create_site(db, co["id"], "S2")
    chunks.insert_chunk(db, s2["id"], "2026-10-12", "topic", "concrete", _v(0))
    assert chunks.latest_matching_date(db, _v(0), [s1["id"]], on_or_before="2026-10-15") is None
    assert chunks.latest_matching_date(db, _v(0), [], on_or_before="2026-10-15") is None


def test_author_outside_author_ids_is_ignored(db):
    s = _site(db)
    me = _user(db, s["company_id"], "me")
    other = _user(db, s["company_id"], "other")
    chunks.insert_chunk(db, s["id"], "2026-10-05", "topic", "mine", _v(0), user_id=me)
    chunks.insert_chunk(db, s["id"], "2026-10-12", "topic", "theirs", _v(0), user_id=other)
    assert chunks.latest_matching_date(db, _v(0), [s["id"]], on_or_before="2026-10-15",
                                       author_ids=[me]) == "2026-10-05"
    assert chunks.latest_matching_date(db, _v(0), [s["id"]], on_or_before="2026-10-15") \
        == "2026-10-12"


def test_deleted_topic_chunk_is_ignored(db):
    s = _site(db)
    t = topics.upsert_topic(db, s["id"], "2026-10-12", "Pour")
    chunks.insert_chunk(db, s["id"], "2026-10-05", "topic", "kept", _v(0))
    chunks.insert_chunk(db, s["id"], "2026-10-12", "topic", "removed", _v(0), topic_id=t["id"])
    assert chunks.latest_matching_date(db, _v(0), [s["id"]], on_or_before="2026-10-15") \
        == "2026-10-12"
    db.execute("INSERT INTO redactions (company_id, target_type, target_id, reason, scope) "
               "VALUES (%s, 'topic', %s, 'test', 'deleted')", (s["company_id"], t["id"]))
    assert chunks.latest_matching_date(db, _v(0), [s["id"]], on_or_before="2026-10-15") \
        == "2026-10-05"


def test_deleted_recording_source_chunk_is_ignored_on_the_keyword_arm_too(db):
    s = _site(db)
    key = "extractions/Ben/2026-10-12/sidaaa"
    chunks.insert_chunk(db, s["id"], "2026-10-12", "topic", "GIB delivery", _v(5),
                        source_s3_key=key + ".json")
    db.execute("INSERT INTO redactions (company_id, target_type, target_id, reason, scope, "
               "target_key) VALUES (%s, 'recording', gen_random_uuid(), 'test', 'deleted', %s)",
               (s["company_id"], key))
    assert chunks.latest_matching_date(db, _v(5), [s["id"]], on_or_before="2026-10-15",
                                       query_text="GIB") is None


def test_superseded_topic_chunk_is_ignored(db):
    s = _site(db)
    t = topics.upsert_topic(db, s["id"], "2026-10-12", "Stale pass")
    chunks.insert_chunk(db, s["id"], "2026-10-12", "topic", "stale", _v(0), topic_id=t["id"])
    db.execute("UPDATE topics SET superseded_at = now() WHERE id = %s", (t["id"],))
    assert chunks.latest_matching_date(db, _v(0), [s["id"]], on_or_before="2026-10-15") is None


@pytest.fixture
def family_site(db):
    """10-12: a near report row, and a FAR session row that covers the day.
    10-10: a near report row, uncovered (session falls back to it).
    10-08: a near session row."""
    s = _site(db, "fam")
    chunks.insert_chunk(db, s["id"], "2026-10-12", "topic", "report 12", _v(0),
                        source_s3_key="reports/2026-10-12/Ben/daily_report.json")
    chunks.insert_chunk(db, s["id"], "2026-10-12", "topic", "session 12 far", _v(5),
                        source_s3_key="extractions/Ben/2026-10-12/sid12.json")
    chunks.insert_chunk(db, s["id"], "2026-10-10", "topic", "report 10", _v(0),
                        source_s3_key="reports/2026-10-10/Ben/daily_report.json")
    chunks.insert_chunk(db, s["id"], "2026-10-08", "topic", "session 08", _v(0),
                        source_s3_key="extractions/Ben/2026-10-08/sid08.json")
    return s


def test_family_session_hides_a_covered_days_report_row(db, family_site):
    ids = [family_site["id"]]
    # 10-12's near row is report-family and the day is covered by a session row
    # (which is itself out of the gate) -> 10-10's uncovered report row falls back in.
    assert chunks.latest_matching_date(db, _v(0), ids, on_or_before="2026-10-15",
                                       source_family="session") == "2026-10-10"
    assert chunks.latest_matching_date(db, _v(0), ids, on_or_before="2026-10-15",
                                       source_family="report") == "2026-10-12"
    assert chunks.latest_matching_date(db, _v(0), ids, on_or_before="2026-10-15") == "2026-10-12"


@pytest.mark.parametrize("family", [None, "session", "report"])
def test_family_agrees_with_latest_visible_date_when_the_gate_admits_everything(
        db, family_site, family):
    """With a gate every row passes, the only filter left is visibility -- so the
    answer must be latest_visible_date's own, under each family setting."""
    ids = [family_site["id"]]
    assert chunks.latest_matching_date(db, _v(0), ids, on_or_before="2026-10-15",
                                       gate=2.0, source_family=family) \
        == chunks.latest_visible_date(db, ids, on_or_before="2026-10-15",
                                      source_family=family)
