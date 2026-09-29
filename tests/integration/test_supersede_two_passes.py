"""Integration: Track B Task 3 -- re-extraction supersedes, it does not delete.

Two layers, against a real Postgres:

  * Repository-level (the `db` fixture -- rolled back): drives
    repositories.topics.supersede_topics_for_source[_prefix] directly across the
    two-pass shape re-extraction actually produces, and proves the retention sweep
    (`list_expired_non_work`) still reaches both the live and the superseded pass of a
    non_work day -- R3, proven for real rather than by reading the source.

  * End-to-end (a real committed connection, NOT the `db` fixture -- its rollback would
    hide the first call's writes from the second): calls
    `lambda_item_writer.write_extraction_items` itself, twice, live pass then final
    pass, on the SAME extraction key -- the actual shape a session produces
    (`extract_session.out_key` is computed once; live and final both write it). Before
    Task 3 the second call's DELETE removed the first pass's row entirely; this proves
    it now survives, superseded, and that the day's live read shows only the second
    pass.

Skipped without TEST_DATABASE_URL -- a skip is NOT a pass.
"""
import datetime as _dt
import io
import json
import uuid

import pytest

from db.connection import get_connection
from repositories import companies, memberships, redactions, sites, topics, users

pytestmark = pytest.mark.integration

DATE = "2026-09-29"


# ---------------------------------------------------------------------------
# Repository-level: supersede_topics_for_source[_prefix] driven directly
# ---------------------------------------------------------------------------

def _seed(db, tag_suffix=""):
    tag = uuid.uuid4().hex[:8] + tag_suffix
    co = companies.create_company(db, f"Supersede3-Co-{tag}")
    site = sites.create_site(db, co["id"], f"Supersede3-Site-{tag}")
    user = users.upsert_field_only_user(db, co["id"], f"Folder3-{tag}", "Fol", "Der", "worker")
    source = f"extractions/Folder3-{tag}/{DATE}/sid{'c' * 32}.json"
    return co, site, user, source


def test_supersede_topics_for_source_retires_the_row_and_returns_it(db):
    co, site, user, source = _seed(db)
    topic_a = topics.upsert_topic(
        db, site["id"], DATE, "Pour B2 -- live pass", user_id=user["id"],
        source_s3_key=source, summary="live summary",
        action_items=[{"text": "Order rebar", "status": "done"}])

    retired = topics.supersede_topics_for_source(db, source, "live:2026-09-29T10:00:00Z")

    assert [r["id"] for r in retired] == [topic_a["id"]]
    assert retired[0]["title"] == "Pour B2 -- live pass"
    assert retired[0]["summary"] == "live summary"

    superseded_at, superseded_by_run = db.execute(
        "SELECT superseded_at, superseded_by_run FROM topics WHERE id=%s",
        (topic_a["id"],)).fetchone()
    assert superseded_at is not None
    assert superseded_by_run == "live:2026-09-29T10:00:00Z"

    # The row survives -- its action item is NOT cascaded away (Task 3's whole point).
    still_there = db.execute(
        "SELECT text, status FROM action_items WHERE topic_id=%s", (topic_a["id"],)).fetchall()
    assert still_there == [("Order rebar", "done")]


def test_supersede_is_idempotent_a_second_call_retires_nothing_more(db):
    co, site, user, source = _seed(db)
    topic_a = topics.upsert_topic(db, site["id"], DATE, "T", user_id=user["id"],
                                  source_s3_key=source, summary="s")

    first = topics.supersede_topics_for_source(db, source, "run-1")
    assert [r["id"] for r in first] == [topic_a["id"]]

    second = topics.supersede_topics_for_source(db, source, "run-2")
    assert second == [], "a row already retired must not be touched by a later call"

    run = db.execute(
        "SELECT superseded_by_run FROM topics WHERE id=%s", (topic_a["id"],)).fetchone()[0]
    assert run == "run-1", "the run that actually retired the row, not the later no-op call"


def test_two_passes_leave_one_superseded_and_one_live_and_reads_return_only_the_live_one(db):
    co, site, user, source = _seed(db)

    topic_a = topics.upsert_topic(
        db, site["id"], DATE, "Pour B2 -- live pass", user_id=user["id"],
        source_s3_key=source, summary="s1")
    retired = topics.supersede_topics_for_source(db, source, "live:t1")
    assert [r["id"] for r in retired] == [topic_a["id"]]

    # migration 0071's idx_topics_live_source (partial unique on source_s3_key WHERE
    # superseded_at IS NULL) permits this second LIVE row only because the first is
    # already retired -- proves the ordering (supersede BEFORE insert) the writer and
    # ingest both follow is the one that actually works against the real constraint.
    topic_b = topics.upsert_topic(
        db, site["id"], DATE, "Pour B2 -- final pass", user_id=user["id"],
        source_s3_key=source, summary="s2")

    rows = topics.list_topics_for_date(db, [site["id"]], DATE)
    assert [r["id"] for r in rows] == [topic_b["id"]], (
        "the superseded pass must not be shown beside the pass that replaced it")

    both = db.execute(
        "SELECT id, superseded_at IS NOT NULL FROM topics WHERE source_s3_key=%s "
        "ORDER BY created_at", (source,)).fetchall()
    assert [(str(i), s) for i, s in both] == [
        (str(topic_a["id"]), True), (str(topic_b["id"]), False)], (
        "both physical rows must still exist -- one superseded, one live")


def test_supersede_topics_for_source_prefix_retires_every_row_under_the_prefix(db):
    co, site, user, _source = _seed(db, "px")
    prefix = f"extractions/{user['folder_name']}/{DATE}/"
    topic_a = topics.upsert_topic(db, site["id"], DATE, "A", user_id=user["id"],
                                  source_s3_key=prefix + "sid1.json", summary="s")
    topic_b = topics.upsert_topic(db, site["id"], DATE, "B", user_id=user["id"],
                                  source_s3_key=prefix + "sid2.json", summary="s")

    retired = topics.supersede_topics_for_source_prefix(
        db, prefix, "report:2026-09-29T20:00:00Z")

    assert {r["id"] for r in retired} == {topic_a["id"], topic_b["id"]}
    runs = db.execute(
        "SELECT superseded_by_run FROM topics WHERE source_s3_key LIKE %s ESCAPE '\\'",
        (prefix.replace('%', '\\%').replace('_', '\\_') + '%',)).fetchall()
    assert [r[0] for r in runs] == ["report:2026-09-29T20:00:00Z"] * 2


def test_nonwork_expiry_still_reaches_both_the_live_and_the_superseded_pass(db):
    """R3: the sweep must not just SEE a superseded non_work row (that's already covered
    by test_superseded_topics_invisible.py) -- it must still be able to ACT on it, and on
    the live pass that replaced it, in the SAME pass of the sweep."""
    co, site, user, source = _seed(db, "nw")

    topic_a = topics.upsert_topic(
        db, site["id"], DATE, "Personal call -- live pass", user_id=user["id"],
        source_s3_key=source, summary="s", work_class="non_work")
    topics.supersede_topics_for_source(db, source, "live:t1")
    topic_b = topics.upsert_topic(
        db, site["id"], DATE, "Personal call -- final pass", user_id=user["id"],
        source_s3_key=source, summary="s", work_class="non_work")
    # Backdate both into the sweep's [created_since, older_than) window -- relative to
    # now(), not a fixed calendar date, so the test does not depend on when it runs.
    db.execute("UPDATE topics SET created_at = now() - interval '40 days' "
              "WHERE source_s3_key=%s", (source,))

    due = topics.list_expired_non_work(
        db, older_than=_dt.datetime.now(_dt.timezone.utc) + _dt.timedelta(days=1),
        created_since=_dt.datetime.now(_dt.timezone.utc) - _dt.timedelta(days=365))
    due_ids = {r["id"] for r in due}
    assert {topic_a["id"], topic_b["id"]} <= due_ids, (
        "the sweep's candidate set must carry both passes")

    for r in due:
        if r["id"] in (topic_a["id"], topic_b["id"]):
            redactions.create_redaction(
                db, r["company_id"], r["id"], "non_work_auto_expiry",
                None, "system", scope="analysis")

    tombstoned = {row[0] for row in db.execute(
        "SELECT target_id FROM redactions WHERE target_id = ANY(%s) AND scope='analysis' "
        "AND reverted_at IS NULL", ([topic_a["id"], topic_b["id"]],)).fetchall()}
    assert tombstoned == {topic_a["id"], topic_b["id"]}, (
        "both the live and the superseded pass must actually get redacted, not just "
        "be found by the candidate query")


# ---------------------------------------------------------------------------
# End-to-end: lambda_item_writer.write_extraction_items, two real passes
# ---------------------------------------------------------------------------

lambda_item_writer = pytest.importorskip(
    "lambda_item_writer", reason="requires psycopg (installed in CI)")


class _FakeS3:
    """Minimal S3 double -- same shape as tests/unit/test_lambda_item_writer.py's, kept
    local rather than cross-imported (integration tests in this repo do not import from
    tests/unit)."""

    def __init__(self, objects):
        self.objects = objects

    def get_object(self, Bucket, Key):
        body = self.objects[Key]
        raw = body.encode("utf-8") if isinstance(body, str) else body
        return {"Body": io.BytesIO(raw)}

    def get_paginator(self, op):
        assert op == "list_objects_v2"
        return _FakePaginator(self.objects)


class _FakePaginator:
    def __init__(self, objects):
        self.objects = objects

    def paginate(self, Bucket, Prefix):
        yield {"Contents": [{"Key": k} for k in self.objects if k.startswith(Prefix)]}


def _extraction(tier, extracted_at, title):
    return {
        "schema_version": 1,
        "tier": tier,
        "extracted_at": extracted_at,
        "topics": [{
            "topic_title": title,
            "category": "progress",
            "summary": f"{title} summary",
            "time_range": "10:00 – 10:05",
            "participants": [],
            "action_items": [],
            "safety_flags": [],
        }],
    }


def test_write_extraction_items_two_passes_leave_one_superseded_and_one_live(
        monkeypatch, migrated_db_url):
    """Drives the real writer, not a stand-in for it -- a monkeypatched connection
    factory hands it REAL, separately-committed connections (mirrors one Lambda
    invocation each), on a company/site/user/membership seeded for real so the
    identity bridge (lambda_ingest.resolve_site, DB-only, no S3 mapping file needed)
    resolves without any repository function being stubbed out."""
    tag = uuid.uuid4().hex[:8]
    seed = get_connection(migrated_db_url, autocommit=True)
    # Guard every value the `finally` cleanup needs against a failure before it is assigned
    # -- this connection is autocommit, unlike every other test in this file (the `db`
    # fixture rolls its writes back), so a leaked row here survives past this test and into
    # every test that runs after it for the rest of the session. That is not hypothetical:
    # this exact leak broke three unrelated integration tests' "empty DB" skip guards
    # (test_topic_decisions_sql.py, test_topic_evidence_sql.py, test_speaker_employer_sql.py)
    # by leaving a site/company behind for them to pick up instead of skipping.
    co = site = user = extraction_key = None
    try:
        company_name = f"Writer3-Co-{tag}"
        co = companies.create_company(seed, company_name)
        site = sites.create_site(seed, co["id"], f"Writer3-Site-{tag}")
        folder = f"Writer3-{tag}"
        user = users.upsert_field_only_user(seed, co["id"], folder, "Fol", "Der", "worker")
        memberships.add_membership(seed, user["id"], site["id"], "worker")

        date = DATE
        session_base = f"sid{tag}"
        extraction_key = f"extractions/{folder}/{date}/{session_base}.json"
        fake_s3 = _FakeS3({
            extraction_key: json.dumps(
                _extraction("live", "2026-09-29T10:00:00Z", "Pour B2 -- live pass")),
        })

        # write_extraction_items resolves the company via `lambda_ingest.resolve_company`
        # (reused by import, per the module docstring), which reads lambda_ingest's OWN
        # COMPANY_NAME constant -- not lambda_item_writer's.
        monkeypatch.setattr(lambda_item_writer.lambda_ingest, "COMPANY_NAME", company_name)
        monkeypatch.setattr(lambda_item_writer, "_s3_client", fake_s3)
        monkeypatch.setattr(lambda_item_writer, "get_connection",
                            lambda *a, **k: get_connection(migrated_db_url))
        # The only stub in this test: emit does a real S3 put_object, which nothing here
        # provides a bucket for. Everything else -- identity bridge, upsert, findings,
        # the supersede call itself -- runs for real against the seeded rows above.
        monkeypatch.setattr(lambda_item_writer.match_request, "emit", lambda *a, **k: None)

        result_live = lambda_item_writer.write_extraction_items(date, folder, extraction_key)
        assert result_live == {"skipped": False, "topics": 1}, result_live

        # The FINAL pass: same key (extract_session computes out_key once; both tiers
        # write it), new body -- exactly what a real live-then-final session produces.
        fake_s3.objects[extraction_key] = json.dumps(
            _extraction("final", "2026-09-29T10:30:00Z", "Pour B2 -- final pass"))

        result_final = lambda_item_writer.write_extraction_items(date, folder, extraction_key)
        assert result_final == {"skipped": False, "topics": 1}, result_final

        rows = seed.execute(
            "SELECT title, superseded_at IS NOT NULL, superseded_by_run FROM topics "
            "WHERE source_s3_key=%s ORDER BY created_at", (extraction_key,)).fetchall()
        assert [r[0] for r in rows] == ["Pour B2 -- live pass", "Pour B2 -- final pass"], (
            "both passes' rows must exist -- the second call must not have deleted the "
            "first's")
        assert [r[1] for r in rows] == [True, False], (
            "the first pass must be superseded, the second live")
        # superseded_by_run names the pass that DID the retiring (migration 0071: "of the
        # pass that replaced it"), so the live row's row carries the FINAL call's own run,
        # not its own.
        assert rows[0][2] == "final:2026-09-29T10:30:00Z", (
            "superseded_by_run must name the pass that replaced this row")
        assert rows[1][2] is None

        live_rows = topics.list_topics_for_date(seed, [site["id"]], date)
        assert [r["title"] for r in live_rows] == ["Pour B2 -- final pass"], (
            "the day's live read must show only the pass that replaced the other")
    finally:
        # Captured BEFORE the deletes below, not re-derived afterwards: the topics/
        # memberships proof needs the site's id to still mean something after the site row
        # itself is gone, and a subquery through the (by-then-deleted) sites table would
        # vacuously find nothing whether or not a leak actually happened.
        co_id = co["id"] if co is not None else None
        site_id = site["id"] if site is not None else None
        user_id = user["id"] if user is not None else None

        # sites.id -> topics.site_id and sites.id -> memberships.site_id are both
        # ON DELETE CASCADE (0002_core_relational.sql, 0003_dashboard_readmodel.sql), and
        # topics.id -> {action_items,findings,safety_observations,topic_photos,...} all
        # CASCADE too -- deleting the site alone takes the whole tree this test grew. Users
        # and the company have nothing else pointing at them once the site is gone.
        if site_id is not None:
            seed.execute("DELETE FROM sites WHERE id=%s", (site_id,))
        if user_id is not None:
            seed.execute("DELETE FROM users WHERE id=%s", (user_id,))
        if co_id is not None:
            seed.execute("DELETE FROM companies WHERE id=%s", (co_id,))

        # Prove it, rather than assert it: nothing this test created is still reachable by
        # any of the identifiers it used, through any of the tables it touched. Skipped only
        # when nothing was ever created (co is None means creation itself failed before
        # anything could leak).
        if co_id is not None:
            remaining = seed.execute(
                "SELECT "
                "(SELECT count(*) FROM companies WHERE id=%s), "
                "(SELECT count(*) FROM sites WHERE id=%s), "
                "(SELECT count(*) FROM users WHERE id=%s), "
                "(SELECT count(*) FROM memberships WHERE site_id=%s), "
                "(SELECT count(*) FROM topics WHERE site_id=%s)",
                (co_id, site_id, user_id, site_id, site_id),
            ).fetchone()
            assert remaining == (0, 0, 0, 0, 0), f"leaked rows after cleanup: {remaining}"
        seed.close()
