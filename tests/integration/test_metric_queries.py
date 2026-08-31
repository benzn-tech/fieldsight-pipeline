"""The metric queries, against a real PostgreSQL.

A fake connection records SQL without parsing it, and everything that matters
here is something only a database can answer: that the session fold is a fold,
that a date range built on the s3_key segment is the clock the rest of the
timeline uses, and that a deleted recording stops being counted.

Skipped without TEST_DATABASE_URL. A SKIP IS NOT A PASS — read the CI run.
"""
import pytest

from repositories import companies, recordings, sites, users

pytestmark = pytest.mark.integration


def _seed(db, folder):
    """`upsert_field_only_user` is the helper that takes a folder_name — there is
    no `create_user`, and `upsert_user` keys on a cognito_sub these rows do not
    have. Folder names are globally unique (migration 0012), so every test uses
    its own."""
    co = companies.create_company(db, "Acme-" + folder)
    site = sites.create_site(db, co["id"], "S-" + folder)
    u = users.upsert_field_only_user(db, co["id"], folder, folder, "", "worker")
    return co["id"], site["id"], u["id"]


def _chunk(db, co, site, uid, folder, date, sid, idx, dur=30, kind="audio"):
    """`insert_pending` is the writer; there is no `recordings.create`."""
    return recordings.insert_pending(
        db, co, uid, site, kind,
        f"users/{folder}/{kind}/{date}/dev_{date}_10-00-00_sid{sid}_c{idx:04d}.wav",
        f"{sid}-{idx}", f"{date}T10:00:00Z", duration_s=dur)


def test_a_session_is_one_session_however_many_chunks_it_arrived_in(db):
    """The whole reason this is not COUNT(*). Measured on prod: 2823 rows are 287
    sessions, a fold of 9.8x. Counting rows tells a person they made nearly ten
    times the recordings they made."""
    co, site, uid = _seed(db, "fold")
    for i in range(21):
        _chunk(db, co, site, uid, "fold", "2026-08-27", "a" * 32, i)

    out = recordings.range_stats(db, co, ["fold"], "2026-08-27", "2026-08-27")

    assert out["sessions"] == 1
    assert out["duration_s"] == 21 * 30


def test_a_pre_chunk_recording_is_still_one_session(db):
    """The fold value is the key itself when there is no sid, so legacy one-row
    recordings are unchanged — the fold is the identity for them."""
    co, site, uid = _seed(db, "legacy")
    recordings.insert_pending(
        db, co, uid, site, "audio",
        "users/legacy/audio/2026-08-27/dev_2026-08-27_10-00-00.wav",
        "L-1", "2026-08-27T10:00:00Z", duration_s=600)

    assert recordings.range_stats(db, co, ["legacy"], "2026-08-27", "2026-08-27")["sessions"] == 1


def test_the_range_is_matched_on_the_key_segment_not_started_at(db):
    """`started_at` is UTC and "yesterday" is the device's local day, so an
    evening recording moves to the next day — BUG-37's family. The key segment is
    the clock the topics and the timeline are on, and the one
    `query_slots.time_range` produces."""
    co, site, uid = _seed(db, "clock")
    recordings.insert_pending(
        db, co, uid, site, "audio",
        "users/clock/audio/2026-08-27/dev_x_sid" + "b" * 32 + "_c0001.wav",
        "c-1",
        "2026-08-27T23:30:00Z",     # 11:30am the NEXT day in UTC+12
        duration_s=60)

    assert recordings.range_stats(db, co, ["clock"], "2026-08-27", "2026-08-27")["sessions"] == 1
    assert recordings.range_stats(db, co, ["clock"], "2026-08-28", "2026-08-28")["sessions"] == 0


def test_a_range_spans_days(db):
    co, site, uid = _seed(db, "range")
    _chunk(db, co, site, uid, "range", "2026-08-24", "1" * 32, 1)
    _chunk(db, co, site, uid, "range", "2026-08-26", "2" * 32, 1)
    _chunk(db, co, site, uid, "range", "2026-08-30", "3" * 32, 1)

    out = recordings.range_stats(db, co, ["range"], "2026-08-24", "2026-08-27")
    assert out["sessions"] == 2


def test_a_session_with_no_duration_at_all_is_counted_and_named(db):
    """Measured: 1 of 287 sessions on prod can produce no duration. Dropping it
    from the total understates the answer silently."""
    co, site, uid = _seed(db, "nodur")
    recordings.insert_pending(
        db, co, uid, site, "audio",
        "users/nodur/audio/2026-08-27/dev_sid" + "c" * 32 + "_c0001.wav",
        "n-1", "2026-08-27T10:00:00Z", ended_at=None, duration_s=None)

    out = recordings.range_stats(db, co, ["nodur"], "2026-08-27", "2026-08-27")
    assert out["sessions"] == 1
    assert out["duration_s"] == 0
    assert out["unmeasured"] == 1


def test_the_span_is_the_fallback_when_duration_s_is_absent(db):
    """`day_stats` sums `duration_s` only — 97.9% of sessions on prod. The span
    fallback lives in `duration_for_media` and is what takes it to 99.7%. Without
    it here, 5 sessions in 287 contribute zero and the total is quietly short."""
    co, site, uid = _seed(db, "span")
    recordings.insert_pending(
        db, co, uid, site, "audio",
        "users/span/audio/2026-08-27/dev_sid" + "d" * 32 + "_c0001.wav",
        "s-1", "2026-08-27T10:00:00Z",
        ended_at="2026-08-27T10:02:00Z", duration_s=None)

    out = recordings.range_stats(db, co, ["span"], "2026-08-27", "2026-08-27")
    assert out["duration_s"] == 120
    assert out["unmeasured"] == 0


def test_photos_are_their_own_number_and_do_not_join_the_session_fold(db):
    """Photos are rows in the same table with `kind='photo'`. Mixing them into
    the session count is how the fold ratio was first miscomputed — 304 of the
    3127 rows on prod are photos, and a session count is not over them."""
    co, site, uid = _seed(db, "pix")
    _chunk(db, co, site, uid, "pix", "2026-08-27", "e" * 32, 1)
    for i in range(3):
        recordings.insert_pending(
            db, co, uid, site, "photo",
            f"users/pix/pictures/2026-08-27/IMG_{i}.jpg",
            f"p-{i}", "2026-08-27T10:00:00Z")

    out = recordings.range_stats(db, co, ["pix"], "2026-08-27", "2026-08-27")
    assert out["sessions"] == 1
    assert out["photos"] == 3
    assert out["duration_s"] == 30


def test_a_deleted_session_stops_being_counted(db):
    """The assertion that proves the key-space translation rather than the
    intention. Tombstones live in the `extractions/` space and `recordings` has
    no `source_s3_key` at all, so the predicate used everywhere else matches
    nothing here — and a count that includes deleted recordings is a way to
    observe what was deleted."""
    co, site, uid = _seed(db, "del")
    _chunk(db, co, site, uid, "del", "2026-08-27", "f" * 32, 1)
    _chunk(db, co, site, uid, "del", "2026-08-27", "0" * 32, 1)

    before = recordings.range_stats(db, co, ["del"], "2026-08-27", "2026-08-27")
    after = recordings.range_stats(db, co, ["del"], "2026-08-27", "2026-08-27",
                                   deleted_bases={"sid" + "f" * 32})

    assert before["sessions"] == 2
    assert after["sessions"] == 1, "a deleted recording is still being counted"


def test_either_spelling_of_the_deleted_base_works(db):
    """The mirror carries whatever `sessionBase` the delete endpoint had; this
    key is always `sid{hex}`. Two spellings of a session are equal as sessions
    and not as strings."""
    co, site, uid = _seed(db, "spell")
    _chunk(db, co, site, uid, "spell", "2026-08-27", "9" * 32, 1)

    bare = recordings.range_stats(db, co, ["spell"], "2026-08-27", "2026-08-27",
                                  deleted_bases={"9" * 32})
    assert bare["sessions"] == 0


def test_another_companys_recordings_are_never_counted(db):
    co_a, site_a, uid_a = _seed(db, "tenanta")
    co_b, site_b, uid_b = _seed(db, "tenantb")
    _chunk(db, co_a, site_a, uid_a, "tenanta", "2026-08-27", "1" * 32, 1)
    _chunk(db, co_b, site_b, uid_b, "tenantb", "2026-08-27", "2" * 32, 1)

    out = recordings.range_stats(db, co_a, ["tenanta", "tenantb"],
                                 "2026-08-27", "2026-08-27")
    assert out["sessions"] == 1, "a folder name reached across tenants"


def test_an_empty_folder_list_counts_nothing_rather_than_everything(db):
    """`= ANY('{}')` matches no rows, which is the correct deny-by-default. An
    implementation that skipped the filter when the list is empty would count the
    whole company."""
    co, site, uid = _seed(db, "empty")
    _chunk(db, co, site, uid, "empty", "2026-08-27", "8" * 32, 1)

    assert recordings.range_stats(db, co, [], "2026-08-27", "2026-08-27")["sessions"] == 0
