import pytest

pytestmark = pytest.mark.integration


def test_site_voice_tables_exist(db):
    # Both tables are created by 0016; a bare INSERT/SELECT proves the schema.
    db.execute(
        "INSERT INTO ws_connections (connection_id, user_id, company_id) "
        "VALUES ('c-smoke', gen_random_uuid(), gen_random_uuid())")
    assert db.execute(
        "SELECT count(*) FROM ws_connections WHERE connection_id='c-smoke'"
    ).fetchone()[0] == 1
    row = db.execute(
        "INSERT INTO voice_messages (company_id, site_id, sender_user_id, s3_key) "
        "VALUES (gen_random_uuid(), gen_random_uuid(), gen_random_uuid(), 'voice/x.wav') "
        "RETURNING id, duration_s, created_at").fetchone()
    assert row[0] is not None and row[1] is None and row[2] is not None
