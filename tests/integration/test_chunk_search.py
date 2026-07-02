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


from repositories import companies, sites, topics, chunks


def _unit_vec(dim, hot):
    v = [0.0] * dim
    v[hot] = 1.0
    return v


def test_search_ranks_by_similarity_and_enforces_acl(db):
    co = companies.create_company(db, "Acme")
    s1 = sites.create_site(db, co["id"], "S1")
    s2 = sites.create_site(db, co["id"], "S2")
    t1 = topics.upsert_topic(db, s1["id"], "2026-07-02", "Concrete pour", summary="B2 slab")

    chunks.insert_chunk(db, s1["id"], "2026-07-02", "topic", "concrete", _unit_vec(1024, 0), topic_id=t1["id"])
    chunks.insert_chunk(db, s1["id"], "2026-07-02", "topic", "scaffolding", _unit_vec(1024, 5))
    chunks.insert_chunk(db, s2["id"], "2026-07-02", "topic", "secret other site", _unit_vec(1024, 0))

    results = chunks.search_chunks(db, _unit_vec(1024, 0), [s1["id"]], k=5)

    texts = [r["chunk_text"] for r in results]
    assert texts[0] == "concrete", "nearest by cosine must rank first"
    assert "secret other site" not in texts, "ACL must exclude non-accessible sites"
    assert results[0]["topic_title"] == "Concrete pour", "small-to-big returns parent topic"
