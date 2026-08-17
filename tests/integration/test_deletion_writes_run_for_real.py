"""Integration: the deletion-path SQL written tonight, executed against a real database.

Every function exercised here was added tonight and, until this file, had only ever run
against a hand-written connection double. A double does not type-check and does not parse
SQL — it records the string. So these queries were asserted, never executed:

* `chunks.archive_chunks_for_session` — `%(topic_ids)s::uuid[]` bound with an EMPTY list on
  the nightly-sweep path, `jsonb_array_elements_text` over a jsonb field that may be
  absent, `LIKE … ESCAPE '\\'`, and an `INSERT … SELECT *, %(batch_id)s::uuid, now()` whose
  column count has to line up with a table defined by `LIKE report_chunks`;
* `chunks.restore_chunks_for_batch` — the inverse, with an explicit column list that must
  match what the archive holds;
* `redactions.active_batches_for_day`;
* `programme_suggestions.VISIBLE` — a correlated NOT EXISTS spliced into two queries.

This matters more than usual right now: a parallel session found that a `CASE WHEN %s IS
NULL` returned **500 in production while 3082 tests passed**, for exactly this reason. The
unit tests for these functions assert on call arguments and on SQL text; nothing had ever
asked Postgres whether the SQL was valid.

Runs only where `TEST_DATABASE_URL` is set — CI provides a real Postgres, so this executes
on every PR.
"""
import uuid

import pytest

from repositories import (chunks, companies, programme_suggestions, redactions, sites,
                          topics, users)

pytestmark = pytest.mark.integration

VEC = "[" + ",".join(["0.01"] * 1024) + "]"
BASE = "sid9f8c1e2a4b6d47f0a1b2c3d4e5f60718"
DATE = "2026-08-17"


def _seed(db, name="Del-Co"):
    co = companies.create_company(db, f"{name}-{uuid.uuid4().hex[:6]}")
    s = sites.create_site(db, co["id"], "Del-Site")
    u = users.upsert_user(db, f"sub-{uuid.uuid4().hex[:8]}", "d@x.nz", company_id=co["id"])
    return co, s, u


def _chunk(db, site_id, *, topic_id=None, source_files=None, text="hello"):
    return chunks.insert_chunk(
        db, site_id, DATE, "transcript_window", text, VEC,
        source_s3_key=f"reports/{DATE}/Folder/daily_report.json",
        topic_id=topic_id,
        metadata={"source_files": source_files or []})


def _live_ids(db, site_id):
    return {str(r[0]) for r in db.execute(
        "SELECT id FROM report_chunks WHERE site_id=%s", (site_id,)).fetchall()}


# ---- the archive, with the arguments production actually passes -------

def test_archive_matches_the_unassigned_bucket_by_source_file(db):
    """`topic_id` is NULL for every turn that fell outside a topic's time_range, and that
    bucket holds verbatim speech. The nightly sweep calls this with an EMPTY topic list, so
    the `%(topic_ids)s::uuid[]` bind and the source-file arm both have to work alone."""
    co, s, _ = _seed(db)
    gone = _chunk(db, s["id"], source_files=[f"F_{DATE}_10-00-00_{BASE}_c0001.json"])
    kept = _chunk(db, s["id"], source_files=[f"F_{DATE}_11-00-00_sidOTHER_c0001.json"])
    batch = str(uuid.uuid4())

    n = chunks.archive_chunks_for_session(db, BASE, [], batch, company_id=co["id"])

    assert n == 1
    live = _live_ids(db, s["id"])
    assert str(kept["id"]) in live and str(gone["id"]) not in live


def test_archive_also_takes_the_topic_linked_chunks(db):
    co, s, u = _seed(db)
    t = topics.upsert_topic(db, s["id"], DATE, "Pour", user_id=u["id"],
                            source_s3_key=f"extractions/Folder/{DATE}/{BASE}.json")
    linked = _chunk(db, s["id"], topic_id=t["id"])
    batch = str(uuid.uuid4())

    n = chunks.archive_chunks_for_session(db, BASE, [t["id"]], batch, company_id=co["id"])

    assert n == 1
    assert str(linked["id"]) not in _live_ids(db, s["id"])


def test_archive_cannot_reach_another_tenant(db):
    """`report_chunks` is shared and `site_id` is its only route to a company. A predicate
    built from caller input without the company scope reaches every customer's rows."""
    co_a, s_a, _ = _seed(db, "A")
    _co_b, s_b, _ = _seed(db, "B")
    mine = _chunk(db, s_a["id"], source_files=[f"x_{BASE}_c1.json"])
    theirs = _chunk(db, s_b["id"], source_files=[f"x_{BASE}_c1.json"])

    n = chunks.archive_chunks_for_session(db, BASE, [], str(uuid.uuid4()),
                                          company_id=co_a["id"])

    assert n == 1
    assert str(theirs["id"]) in _live_ids(db, s_b["id"])
    assert str(mine["id"]) not in _live_ids(db, s_a["id"])


def test_a_session_base_is_data_not_a_like_pattern(db):
    """`%` and `_` are LIKE wildcards and the base arrives from a request body. Unescaped,
    `sessionBase='%'` selects every chunk that has any source_files entry at all."""
    co, s, _ = _seed(db)
    c = _chunk(db, s["id"], source_files=[f"x_{BASE}_c1.json"])

    n = chunks.archive_chunks_for_session(db, "%", [], str(uuid.uuid4()),
                                          company_id=co["id"])

    assert n == 0, "a wildcard base archived rows it does not name"
    assert str(c["id"]) in _live_ids(db, s["id"])


def test_an_empty_base_is_refused_rather_than_matching_everything(db):
    co, _s, _ = _seed(db)
    with pytest.raises(ValueError):
        chunks.archive_chunks_for_session(db, "", [], str(uuid.uuid4()),
                                          company_id=co["id"])


def test_a_missing_company_is_refused(db):
    with pytest.raises(ValueError):
        chunks.archive_chunks_for_session(db, BASE, [], str(uuid.uuid4()))


# ---- and back again ---------------------------------------------------

def test_restore_puts_back_exactly_one_batch(db):
    """One undelete restores exactly what one delete removed — the same rule the redaction
    rows follow. The explicit column list in the restore has to match what the archive
    holds, and `SELECT *` on either side would not."""
    co, s, _ = _seed(db)
    a = _chunk(db, s["id"], source_files=[f"x_{BASE}_c1.json"], text="batch one")
    b = _chunk(db, s["id"], source_files=["x_sidSECOND_c1.json"], text="batch two")
    batch_a, batch_b = str(uuid.uuid4()), str(uuid.uuid4())

    chunks.archive_chunks_for_session(db, BASE, [], batch_a, company_id=co["id"])
    chunks.archive_chunks_for_session(db, "sidSECOND", [], batch_b, company_id=co["id"])
    assert _live_ids(db, s["id"]) == set()

    n = chunks.restore_chunks_for_batch(db, batch_a)

    assert n == 1
    live = _live_ids(db, s["id"])
    assert str(a["id"]) in live and str(b["id"]) not in live


def test_restore_is_idempotent(db):
    """A retried undelete must not raise on the primary key it already restored."""
    co, s, _ = _seed(db)
    _chunk(db, s["id"], source_files=[f"x_{BASE}_c1.json"])
    batch = str(uuid.uuid4())
    chunks.archive_chunks_for_session(db, BASE, [], batch, company_id=co["id"])

    assert chunks.restore_chunks_for_batch(db, batch) == 1
    assert chunks.restore_chunks_for_batch(db, batch) == 0


def test_the_archive_table_carries_the_delete_that_made_it(db):
    co, s, _ = _seed(db)
    _chunk(db, s["id"], source_files=[f"x_{BASE}_c1.json"])
    batch = str(uuid.uuid4())
    chunks.archive_chunks_for_session(db, BASE, [], batch, company_id=co["id"])

    row = db.execute(
        "SELECT batch_id, archived_at FROM report_chunks_archive WHERE batch_id=%s",
        (batch,)).fetchone()
    assert row is not None and row[1] is not None


# ---- the tombstone lookups -------------------------------------------

def test_active_batches_for_day_finds_and_forgets(db):
    co, _s, _ = _seed(db)
    prefix = f"extractions/Folder/{DATE}/{BASE}"
    batch = str(uuid.uuid4())
    redactions.create_recording_tombstone(db, co["id"], prefix, "test", None, "admin",
                                          batch_id=batch)

    # Strings, not UUID objects: `batch_id` is a string everywhere else in this feature,
    # and a caller comparing the two forms gets False from two values that are the same id.
    # This assertion caught exactly that on its first real run.
    assert batch in redactions.active_batches_for_day(db, "Folder", DATE)
    assert all(isinstance(b, str)
               for b in redactions.active_batches_for_day(db, "Folder", DATE))
    assert redactions.active_batches_for_day(db, "Folder", DATE,
                                             exclude_batch=batch) == []

    redactions.revert_batch(db, batch, co["id"])
    assert redactions.active_batches_for_day(db, "Folder", DATE) == []


# ---- programme suggestions carry frozen copies of the words ----------

def test_a_deleted_recording_drops_out_of_programme_suggestions(db):
    """This table stores `topic_title`/`topic_summary` as COPIES, beside a `topic_id` that
    is SET NULL. Both arms are needed and only a real database can show that the SQL
    splices in correctly."""
    co, s, u = _seed(db)
    src = f"extractions/Folder/{DATE}/{BASE}.json"
    t = topics.upsert_topic(db, s["id"], DATE, "Steel", user_id=u["id"], source_s3_key=src)
    programme_suggestions.upsert_suggestion(
        db, site_id=s["id"], task_id="T-1", topic_id=t["id"],
        topic_title="Steel reinforcement", topic_summary="the stairs",
        topic_user_id=u["id"], report_date=DATE, source_s3_key=src,
        task_name="Stairs", task_status_before="in_progress", task_progress_before=10,
        suggested_status="completed", suggested_progress=100, confidence=0.9,
        match_evidence={})

    assert len(programme_suggestions.list_for_site(db, s["id"])) == 1

    batch = str(uuid.uuid4())
    redactions.create_recording_tombstone(
        db, co["id"], f"extractions/Folder/{DATE}/{BASE}", "deleted", None, "admin",
        batch_id=batch)

    assert programme_suggestions.list_for_site(db, s["id"]) == [], \
        "the recording's words survived in a frozen copy"

    redactions.revert_batch(db, batch, co["id"])
    assert len(programme_suggestions.list_for_site(db, s["id"])) == 1, \
        "an undelete must bring it back"
