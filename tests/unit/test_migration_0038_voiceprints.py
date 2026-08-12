"""Guards on migration 0038 — the speaker-identity schema contract.

No live Postgres here, so these assert the migration TEXT: that it is idempotent (migrations
re-run on every deploy) and that it declares the things the design will not work without.

Design: docs/superpowers/specs/2026-08-09-speaker-identity-v2.md §6, §8.

Two of these guards are about withdrawal rather than matching, and they are the ones worth
naming. A voiceprint is biometric data under the NZ Privacy Act: the subject can ask for it
to be removed, and "removed" has to mean the vectors are gone AND everything they justified
is un-named. Neither is possible unless the schema records, at write time, which correction
produced a sample and which turns a profile went on to name. Both are cheap now and
un-reconstructable later — after the fact, un-naming means grepping S3 artifacts.
"""
from pathlib import Path

SQL = Path("src/migrations/0038_speaker_voiceprints.sql").read_text(encoding="utf-8").lower()


def test_every_table_is_created_idempotently():
    """Migrations re-run on every deploy."""
    for table in ("speaker_voiceprints", "speaker_voiceprint_samples", "speaker_turn_names"):
        assert f"create table if not exists {table}" in SQL, table


def test_the_embedding_is_the_192_dimensions_the_design_specifies():
    """ECAPA-TDNN is 192-d and so is the column. A mismatch is only discovered on the first
    real insert, which is the worst possible moment."""
    assert "vector(192)" in SQL


def test_every_table_is_scoped_to_a_company_and_cannot_be_null():
    """v2 §8: no company_id-less query may exist on these tables. That is only enforceable
    if the column cannot be null in the first place."""
    assert SQL.count("company_id") >= 3
    assert "company_id    uuid not null" in SQL or "company_id uuid not null" in SQL


def test_consent_is_a_column_rather_than_an_assumption():
    """The subject of a voiceprint is the person recorded, not the person who labelled them
    (v2 §6). A profile with no consent_at must be inert, and that is only checkable if the
    timestamp is stored."""
    assert "consent_at" in SQL


def test_withdrawn_is_one_of_the_states():
    for state in ("tentative", "confirmed", "withdrawn"):
        assert state in SQL, state


def test_a_sample_remembers_the_audio_and_the_correction_that_produced_it():
    """§6 requires a withdrawal to take out everything the bad enrolment justified. Without
    a pointer back to the correction, "everything it justified" is not enumerable."""
    assert "s3_key" in SQL
    assert "correction_ref" in SQL
    assert "created_by" in SQL


def test_named_turns_are_recorded_so_a_withdrawal_can_un_name_them():
    """The provenance table. Without it, honouring a withdrawal means grepping S3 artifacts
    for a name — which is not a thing anyone will do correctly under time pressure."""
    assert "create table if not exists speaker_turn_names" in SQL
    for col in ("voiceprint_id", "session_base", "turn_ref", "score", "margin"):
        assert col in SQL, col


def test_a_turn_name_records_how_sure_it_was():
    """`state` distinguishes a name that was shown as fact from one shown as a guess. After
    a threshold change, only one of those needs revisiting."""
    assert "state" in SQL


def test_deleting_a_company_takes_its_biometric_data_with_it():
    assert "on delete cascade" in SQL
