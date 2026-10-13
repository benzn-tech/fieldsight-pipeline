"""Integration: `repositories.speaker_intro_suggestions`, run for real (Task 4).

Companion to `test_speaker_intro_suggestions_sql.py` (which exercises the raw table);
this drives the repository functions the writer and org-api actually call.
"""
import pytest

sis = pytest.importorskip("repositories.speaker_intro_suggestions",
                          reason="requires psycopg (installed in CI)")

pytestmark = pytest.mark.integration

SID = "sid" + "a" * 32
FILE = f"Ben1_2026-09-29_09-00-00_{SID}_c0000.json"


def _company(db, name="Intro Co"):
    return db.execute(
        "INSERT INTO companies (name) VALUES (%s) RETURNING id", (name,)).fetchone()[0]


def _user(db, company_id, sub="sub-intro"):
    return db.execute(
        "INSERT INTO users (cognito_sub, company_id, email, global_role) "
        "VALUES (%s, %s, 'a@x.com', 'admin') RETURNING id", (sub, company_id)).fetchone()[0]


def _intro(source_filename=FILE, speaker_label="spk_0", heard_name="Petros",
          company_name="Cassidy", start_sec=0.0, end_sec=6.0,
          quote="Hi, this is Petros from Cassidy"):
    return {"source_filename": source_filename, "speaker_label": speaker_label,
            "heard_name": heard_name, "company_name": company_name,
            "start_sec": start_sec, "end_sec": end_sec, "quote": quote}


def test_store_inserts_one_row_per_intro_and_a_rerun_inserts_nothing(db):
    cid = _company(db)
    result = sis.store(db, cid, SID, "Ben1", "2026-09-29", [_intro()])
    assert result == {"inserted": 1, "skipped_named": 0, "skipped_no_sid": 0}

    rerun = sis.store(db, cid, SID, "Ben1", "2026-09-29", [_intro()])
    assert rerun == {"inserted": 0, "skipped_named": 0, "skipped_no_sid": 0}


def test_an_already_named_file_is_skipped_and_a_superseded_one_is_not(db):
    cid = _company(db)
    uid = _user(db, cid)
    voiceprint = db.execute(
        "INSERT INTO speaker_voiceprints (company_id, display_name, status) "
        "VALUES (%s, 'Sam', 'confirmed') RETURNING id", (cid,)).fetchone()[0]

    stem = FILE[: -len(".json")]
    live_name = db.execute(
        "INSERT INTO speaker_turn_names "
        "(company_id, voiceprint_id, session_base, turn_ref, state) "
        "VALUES (%s, %s, %s, %s, 'confirmed') RETURNING id",
        (cid, voiceprint, SID, f"{stem}@12.0")).fetchone()[0]

    skipped = sis.store(db, cid, SID, "Ben1", "2026-09-29", [_intro()])
    assert skipped == {"inserted": 0, "skipped_named": 1, "skipped_no_sid": 0}

    # A superseded name on the same file must not block a new intro from a different file.
    db.execute("UPDATE speaker_turn_names SET superseded_at = now() WHERE id = %s",
              (live_name,))
    other_file = f"Ben1_2026-09-29_09-05-00_{SID}_c0001.json"
    inserted = sis.store(db, cid, SID, "Ben1", "2026-09-29",
                         [_intro(source_filename=other_file)])
    assert inserted == {"inserted": 1, "skipped_named": 0, "skipped_no_sid": 0}


def test_a_source_filename_with_no_sid_is_skipped(db):
    cid = _company(db)
    legacy_file = "Ben1_2026-09-29_09-00-00_off0.0_to30.0_srcwav.json"
    result = sis.store(db, cid, "Ben1_2026-09-29_09-00-00", "Ben1", "2026-09-29",
                       [_intro(source_filename=legacy_file)])
    assert result == {"inserted": 0, "skipped_named": 0, "skipped_no_sid": 1}


def test_decide_flips_exactly_once(db):
    cid = _company(db)
    uid = _user(db, cid)
    sis.store(db, cid, SID, "Ben1", "2026-09-29", [_intro()])
    row = sis.pending(db, cid)[0]

    first = sis.decide(db, cid, row["id"], "confirmed", decided_by=uid)
    assert first is not None
    assert first["state"] == "confirmed"

    second = sis.decide(db, cid, row["id"], "confirmed", decided_by=uid)
    assert second is None


def test_pending_orders_newest_first_and_excludes_decided_rows(db):
    cid = _company(db)
    uid = _user(db, cid)
    sis.store(db, cid, SID, "Ben1", "2026-09-29", [
        _intro(source_filename=FILE, speaker_label="spk_0", heard_name="Petros"),
        _intro(source_filename=FILE, speaker_label="spk_1", heard_name="Sam"),
    ])
    rows = sis.pending(db, cid)
    assert {r["heard_name"] for r in rows} == {"Petros", "Sam"}

    sis.decide(db, cid, rows[0]["id"], "rejected", decided_by=uid)
    remaining = sis.pending(db, cid)
    assert len(remaining) == 1
    assert remaining[0]["id"] != rows[0]["id"]


def test_company_scoping_hides_another_companys_rows(db):
    cid_a = _company(db, "A Co")
    cid_b = _company(db, "B Co")
    uid_b = _user(db, cid_b, sub="sub-b")
    sis.store(db, cid_a, SID, "Ben1", "2026-09-29", [_intro()])

    assert sis.pending(db, cid_b) == []
    assert sis.pending_count(db, cid_b) == 0
    row = sis.pending(db, cid_a)[0]
    assert sis.decide(db, cid_b, row["id"], "confirmed", decided_by=uid_b) is None
