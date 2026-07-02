import pytest

pytestmark = pytest.mark.integration


def _seed_site(db):
    cid = db.execute("INSERT INTO companies (name) VALUES ('C') RETURNING id").fetchone()[0]
    return db.execute("INSERT INTO sites (company_id, name) VALUES (%s,'S') RETURNING id", (cid,)).fetchone()[0]


def test_report_chunks_accepts_1024_vector(db):
    site_id = _seed_site(db)
    vec = [0.0] * 1024
    vec[0] = 1.0
    db.execute(
        "INSERT INTO report_chunks (site_id, report_date, chunk_type, chunk_text, embedding) "
        "VALUES (%s, '2026-07-02', 'topic', 'hello', %s)",
        (site_id, vec),
    )
    n = db.execute("SELECT count(*) FROM report_chunks").fetchone()[0]
    assert n == 1
