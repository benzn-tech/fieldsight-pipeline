"""`chunks.latest_matching_date` / `chunks.subject_coverage` against a real
PostgreSQL (Ask tools, spec 2026-10-15 §3.1 item 3, `latest_activity` with a
query; day rule from spec 2026-10-20 §3.2 and Amendment A §8: the latest day
reaching the window's highest subject-word coverage, at least min(2, n)).

The answer is a DATE, and a date is still a disclosure: a deleted recording, an
author the caller cannot see, or a report-family row that the session family
hides must not be the reason the tool says "the last time was the 12th". So the
visibility rules are the search's own (`search_sql._visibility_predicate`), and
these tests exercise each arm against the database -- a fake connection would
prove only that a string was passed somewhere.
"""
import pytest

from repositories import chunks, companies, sites, topics
from repositories.search_sql import build_latest_match_date_sql, subject_params

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


def _pick(db, site_ids, terms, **kw):
    """(day, k*) or None -- latest_matching_date's own return."""
    return chunks.latest_matching_date(db, site_ids, on_or_before="2026-10-15",
                                       subject_terms=terms, **kw)


def _latest(db, site_ids, terms, **kw):
    got = _pick(db, site_ids, terms, **kw)
    return None if got is None else got[0]


def test_latest_day_naming_the_subject_within_window(db):
    s = _site(db)
    chunks.insert_chunk(db, s["id"], "2026-10-01", "topic", "concrete pour", _v(0))
    chunks.insert_chunk(db, s["id"], "2026-10-12", "topic", "scaffold", _v(0))       # other subject
    chunks.insert_chunk(db, s["id"], "2026-06-01", "topic", "concrete old", _v(0))   # out of window
    assert _latest(db, [s["id"]], ["concrete"]) == "2026-10-01"


def test_window_edges_are_inclusive_and_the_future_is_excluded(db):
    s = _site(db)
    # window_days=92 ending 10-15 -> [2026-07-16, 2026-10-15]
    chunks.insert_chunk(db, s["id"], "2026-07-16", "topic", "pour first day", _v(0))
    chunks.insert_chunk(db, s["id"], "2026-07-15", "topic", "pour one before", _v(0))
    chunks.insert_chunk(db, s["id"], "2026-10-16", "topic", "pour tomorrow", _v(0))
    assert _latest(db, [s["id"]], ["pour"]) == "2026-07-16"
    assert _latest(db, [s["id"]], ["pour"], window_days=91) is None


def test_picks_the_latest_day_naming_every_word(db):
    """The day is chosen on the subject words alone: a later day that names only
    one of them, or only the asking ("last time"), does not count."""
    s = _site(db)
    chunks.insert_chunk(db, s["id"], "2026-10-12", "topic", "concrete pour on level 2", _v(0))
    chunks.insert_chunk(db, s["id"], "2026-10-13", "topic", "pour only", _v(0))
    chunks.insert_chunk(db, s["id"], "2026-10-14", "topic", "we talked last time", _v(0))
    assert _latest(db, [s["id"]], ["pour", "concrete"]) == "2026-10-12"


def test_stem_matches(db):
    s = _site(db)
    chunks.insert_chunk(db, s["id"], "2026-10-12", "topic", "scaffold up tomorrow", _v(0))
    assert _latest(db, [s["id"]], ["scaffolding"]) == "2026-10-12"


def test_don_crane_needs_both_words(db):
    """'don' is an english stopword: the per-term fold must still require it via
    the simple half (spec 2026-10-20 §3.2, Fable's probe)."""
    s = _site(db)
    chunks.insert_chunk(db, s["id"], "2026-10-12", "topic", "the crane arrived", _v(0))
    assert _latest(db, [s["id"]], ["don", "crane"]) is None
    chunks.insert_chunk(db, s["id"], "2026-10-13", "topic", "Don moved the crane", _v(0))
    assert _latest(db, [s["id"]], ["don", "crane"]) == "2026-10-13"


def test_match_uses_the_search_expression_not_substring(db):
    """A subject word matches what search's tsvector holds: the punctuation-split
    'simple' half, so 'PS4/light' matches 'ps4' -- and a bare substring ('gi'
    inside 'GIB') does not match, as it would under ILIKE."""
    s = _site(db)
    chunks.insert_chunk(db, s["id"], "2026-10-12", "topic", "check PS4/light fitting", _v(5))
    chunks.insert_chunk(db, s["id"], "2026-10-13", "topic", "the GIB delivery", _v(5))
    assert _latest(db, [s["id"]], ["ps4"]) == "2026-10-12"
    assert _latest(db, [s["id"]], ["gi"]) is None
    # the words are ANDed, not ORed (or_query's shape is gone from the day rule)
    assert _latest(db, [s["id"]], ["nothing", "gib"]) is None


def test_no_subject_terms_matches_nothing(db):
    s = _site(db)
    chunks.insert_chunk(db, s["id"], "2026-10-12", "topic", "anything at all", _v(5))
    assert _latest(db, [s["id"]], []) is None


def test_other_site_never_counts(db):
    co = companies.create_company(db, "A")
    s1 = sites.create_site(db, co["id"], "S1")
    s2 = sites.create_site(db, co["id"], "S2")
    chunks.insert_chunk(db, s2["id"], "2026-10-12", "topic", "concrete", _v(0))
    assert _latest(db, [s1["id"]], ["concrete"]) is None
    assert _latest(db, [], ["concrete"]) is None


def test_author_outside_author_ids_is_ignored(db):
    s = _site(db)
    me = _user(db, s["company_id"], "me")
    other = _user(db, s["company_id"], "other")
    chunks.insert_chunk(db, s["id"], "2026-10-05", "topic", "concrete mine", _v(0), user_id=me)
    chunks.insert_chunk(db, s["id"], "2026-10-12", "topic", "concrete theirs", _v(0),
                        user_id=other)
    assert _latest(db, [s["id"]], ["concrete"], author_ids=[me]) == "2026-10-05"
    assert _latest(db, [s["id"]], ["concrete"]) == "2026-10-12"


def test_deleted_topic_chunk_is_ignored(db):
    s = _site(db)
    t = topics.upsert_topic(db, s["id"], "2026-10-12", "Pour")
    chunks.insert_chunk(db, s["id"], "2026-10-05", "topic", "concrete kept", _v(0))
    chunks.insert_chunk(db, s["id"], "2026-10-12", "topic", "concrete removed", _v(0),
                        topic_id=t["id"])
    assert _latest(db, [s["id"]], ["concrete"]) == "2026-10-12"
    db.execute("INSERT INTO redactions (company_id, target_type, target_id, reason, scope) "
               "VALUES (%s, 'topic', %s, 'test', 'deleted')", (s["company_id"], t["id"]))
    assert _latest(db, [s["id"]], ["concrete"]) == "2026-10-05"


def test_deleted_recording_source_chunk_is_ignored(db):
    s = _site(db)
    key = "extractions/Ben/2026-10-12/sidaaa"
    chunks.insert_chunk(db, s["id"], "2026-10-12", "topic", "GIB delivery", _v(5),
                        source_s3_key=key + ".json")
    db.execute("INSERT INTO redactions (company_id, target_type, target_id, reason, scope, "
               "target_key) VALUES (%s, 'recording', gen_random_uuid(), 'test', 'deleted', %s)",
               (s["company_id"], key))
    assert _latest(db, [s["id"]], ["gib"]) is None


def test_superseded_topic_chunk_is_ignored(db):
    s = _site(db)
    t = topics.upsert_topic(db, s["id"], "2026-10-12", "Stale pass")
    chunks.insert_chunk(db, s["id"], "2026-10-12", "topic", "concrete stale", _v(0),
                        topic_id=t["id"])
    db.execute("UPDATE topics SET superseded_at = now() WHERE id = %s", (t["id"],))
    assert _latest(db, [s["id"]], ["concrete"]) is None


@pytest.fixture
def family_site(db):
    """Every row says "note"; only some name "concrete".
    10-12: a concrete report row, and a session row (not concrete) that covers the day.
    10-10: a concrete report row, uncovered (session falls back to it).
    10-08: a concrete session row."""
    s = _site(db, "fam")
    chunks.insert_chunk(db, s["id"], "2026-10-12", "topic", "note: concrete report 12", _v(0),
                        source_s3_key="reports/2026-10-12/Ben/daily_report.json")
    chunks.insert_chunk(db, s["id"], "2026-10-12", "topic", "note: scaffold session 12", _v(5),
                        source_s3_key="extractions/Ben/2026-10-12/sid12.json")
    chunks.insert_chunk(db, s["id"], "2026-10-10", "topic", "note: concrete report 10", _v(0),
                        source_s3_key="reports/2026-10-10/Ben/daily_report.json")
    chunks.insert_chunk(db, s["id"], "2026-10-08", "topic", "note: concrete session 08", _v(0),
                        source_s3_key="extractions/Ben/2026-10-08/sid08.json")
    return s


def test_family_session_hides_a_covered_days_report_row(db, family_site):
    ids = [family_site["id"]]
    # 10-12's concrete row is report-family and the day is covered by a session row
    # (which does not name concrete) -> 10-10's uncovered report row falls back in.
    assert _latest(db, ids, ["concrete"], source_family="session") == "2026-10-10"
    assert _latest(db, ids, ["concrete"], source_family="report") == "2026-10-12"
    assert _latest(db, ids, ["concrete"]) == "2026-10-12"


@pytest.mark.parametrize("family", [None, "session", "report"])
def test_family_agrees_with_latest_visible_date_when_every_row_names_the_word(
        db, family_site, family):
    """With a word present in every row, the only filter left is visibility -- so
    the answer must be latest_visible_date's own, under each family setting."""
    ids = [family_site["id"]]
    assert _latest(db, ids, ["note"], source_family=family) \
        == chunks.latest_visible_date(db, ids, on_or_before="2026-10-15",
                                      source_family=family)


def test_subject_coverage_counts_the_words_each_row_names(db):
    s = _site(db)
    both = chunks.insert_chunk(db, s["id"], "2026-10-12", "topic", "Precast panels arrive", _v(0))
    one = chunks.insert_chunk(db, s["id"], "2026-10-12", "topic", "panels stacked", _v(0))
    none = chunks.insert_chunk(db, s["id"], "2026-10-12", "topic", "last time we talked", _v(0))
    ids = [both["id"], one["id"], none["id"]]
    assert chunks.subject_coverage(db, ids, ["precast", "panel"]) == {
        str(both["id"]).lower(): 2, str(one["id"]).lower(): 1, str(none["id"]).lower(): 0}
    assert chunks.subject_coverage(db, ids, []) == {}
    assert chunks.subject_coverage(db, [], ["panels"]) == {}


# ---- Amendment A (spec 2026-10-20 §8 / §8.1): coverage first, not ALL ----

FIVE = ["pour", "concrete", "b2", "slab", "level"]


def test_a_day_with_four_of_five_words_is_chosen_when_none_has_all(db):
    s = _site(db)
    chunks.insert_chunk(db, s["id"], "2026-10-12", "topic", "pour concrete for the B2 slab", _v(0))
    chunks.insert_chunk(db, s["id"], "2026-10-14", "topic", "concrete on level 3", _v(0))
    assert _pick(db, [s["id"]], FIVE) == ("2026-10-12", 4)


def test_an_older_full_day_beats_a_newer_partial_one(db):
    """k* is the window maximum (§8 trade-off): 5/5 on the 10th beats 4/5 on the 13th."""
    s = _site(db)
    chunks.insert_chunk(db, s["id"], "2026-10-10", "topic",
                        "pour concrete for the B2 slab on level 2", _v(0))
    chunks.insert_chunk(db, s["id"], "2026-10-13", "topic", "pour concrete for the B2 slab", _v(0))
    assert _pick(db, [s["id"]], FIVE) == ("2026-10-10", 5)


def test_coverage_is_per_chunk_not_per_day(db):
    """Two chunks on one day naming two words each are coverage 2, not 4."""
    s = _site(db)
    chunks.insert_chunk(db, s["id"], "2026-10-12", "topic", "pour concrete", _v(0))
    chunks.insert_chunk(db, s["id"], "2026-10-12", "topic", "B2 slab", _v(0))
    assert _pick(db, [s["id"]], FIVE) == ("2026-10-12", 2)


def test_one_word_never_chooses_a_day_when_there_are_two_or_more(db):
    s = _site(db)
    chunks.insert_chunk(db, s["id"], "2026-10-12", "topic", "concrete only", _v(0))
    assert _pick(db, [s["id"]], ["pour", "concrete"]) is None
    assert _pick(db, [s["id"]], FIVE) is None


def test_two_words_are_both_required(db):
    s = _site(db)
    chunks.insert_chunk(db, s["id"], "2026-10-13", "topic", "pour tomorrow", _v(0))
    chunks.insert_chunk(db, s["id"], "2026-10-12", "topic", "concrete pour", _v(0))
    assert _pick(db, [s["id"]], ["pour", "concrete"]) == ("2026-10-12", 2)


def test_one_word_has_a_floor_of_one(db):
    """§8.1 item 4: a single (even generic) subject word picks the latest day naming it."""
    s = _site(db)
    chunks.insert_chunk(db, s["id"], "2026-10-11", "topic", "site tidy", _v(0))
    chunks.insert_chunk(db, s["id"], "2026-10-13", "topic", "the site was busy", _v(0))
    assert _pick(db, [s["id"]], ["site"]) == ("2026-10-13", 1)


def test_day_query_uses_the_text_index_with_a_generic_term(db):
    """§8.1 item 1: with a term present in almost every row ("site"), an OR of the
    single terms is not GIN-served (the planner walked the site/date index). The
    pairwise-AND prefilter is, because every pair has a rare term."""
    s = _site(db)
    for i in range(500):
        chunks.insert_chunk(db, s["id"], "2026-10-12", "topic",
                            f"site electrical note number {i}", _v(i % 1024))
    chunks.insert_chunk(db, s["id"], "2026-10-12", "topic", "site precast crane lift", _v(0))
    db.execute("ANALYZE report_chunks")
    db.execute("SET LOCAL enable_seqscan = off")
    terms = ["site", "precast", "crane"]
    plan = "\n".join(r[0] for r in db.execute(
        "EXPLAIN " + build_latest_match_date_sql(3),
        {**subject_params(terms), "floor": 2,
         "site_ids": [s["id"]], "author_ids": None,
         "date_from": "2026-07-16", "date_to": "2026-10-15",
         "source_family": None},
    ).fetchall())
    assert "idx_report_chunks_tsv_multi_config" in plan, plan
    assert _pick(db, [s["id"]], terms) == ("2026-10-12", 3)


def test_day_query_uses_the_text_index(db):
    """The day query is served by idx_report_chunks_tsv_multi_config (migration
    0061): the subject match keeps _lexical_vector on its left side, so any
    tsquery shape rides the GIN expression index. That is why the exact-scan
    rationale in rag_tools_dispatch no longer applies."""
    s = _site(db)
    for i in range(200):
        chunks.insert_chunk(db, s["id"], "2026-10-12", "topic",
                            f"unrelated electrical note number {i}", _v(i % 1024))
    db.execute("ANALYZE report_chunks")
    db.execute("SET LOCAL enable_seqscan = off")
    plan = "\n".join(r[0] for r in db.execute(
        "EXPLAIN " + build_latest_match_date_sql(1),
        {**subject_params(["precast"]), "floor": 1,
         "site_ids": [s["id"]], "author_ids": None,
         "date_from": "2026-07-16", "date_to": "2026-10-15",
         "source_family": None},
    ).fetchall())
    assert "idx_report_chunks_tsv_multi_config" in plan, plan
