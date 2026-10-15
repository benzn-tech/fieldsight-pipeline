"""project_docs against a real Postgres: revisions, supersede/delete, the visibility
predicate and hybrid search. A fake connection never runs SQL (repo lesson), so the
lifecycle rules of spec R8 and the search rules of R11/R12 are only proven here."""
import uuid

import pytest

from repositories import companies, project_docs, sites, users

pytestmark = pytest.mark.integration


def vec(i):
    """A unit vector (cosine against a zero vector is undefined)."""
    v = [0.0] * 1024
    v[i] = 1.0
    return v


@pytest.fixture
def world(db):
    tag = uuid.uuid4().hex[:8]
    a = companies.create_company(db, f"DocA-{tag}")
    b = companies.create_company(db, f"DocB-{tag}")
    s1 = sites.create_site(db, a["id"], f"Tower-{tag}")
    s2 = sites.create_site(db, a["id"], f"Annex-{tag}")
    s3 = sites.create_site(db, b["id"], f"Other-{tag}")
    u = users.upsert_user(db, f"sub-{tag}", f"{tag}@x.nz", company_id=a["id"])
    return dict(db=db, a=a, b=b, s1=s1, s2=s2, s3=s3, u=u)


def _insert(w, site=None, series_id=None, filename="Spec.pdf"):
    site = site or w["s1"]
    doc_id = str(uuid.uuid4())
    return project_docs.insert_document(
        w["db"], doc_id=doc_id, site_id=site["id"], company_id=site["company_id"],
        title=filename, filename=filename,
        source_key=f"project_doc_sources/{site['company_id']}/{doc_id}.pdf",
        content_type="application/pdf", uploaded_by=w["u"]["id"], series_id=series_id)


def _result(row, status="indexed", chunks=None, company=None, **kw):
    out = {"document_id": str(row["id"]), "company_id": str(company or row["company_id"]),
           "site_id": str(row["site_id"]), "series_id": str(row["series_id"]),
           "revision": row["revision"], "status": status, "reason": kw.get("reason"),
           "pages": kw.get("pages", 3), "chars": kw.get("chars", 900),
           "chunks": chunks if chunks is not None else [
               {"ordinal": 0, "heading": "3 Concrete > 3.2 Formwork", "page_from": 2,
                "page_to": 2, "chunk_text": "Formwork tolerance is 5 mm.", "embedding": vec(1)}]}
    return out


def _search(w, site_ids, qv=None, q_text="", k=8):
    return project_docs.search(w["db"], [str(s) for s in site_ids], qv or vec(1), q_text, k=k)


def _status(w, row):
    return project_docs.get_document(w["db"], row["id"])


# ---- revisions ---------------------------------------------------------------------

def test_a_new_series_starts_at_revision_one_and_revisions_count_up(world):
    r1 = _insert(world)
    assert r1["revision"] == 1 and r1["status"] == "pending"
    r2 = _insert(world, series_id=r1["series_id"])
    r3 = _insert(world, series_id=r1["series_id"])
    assert (r2["revision"], r3["revision"]) == (2, 3)
    assert r2["series_id"] == r1["series_id"]


def test_the_same_filename_twice_is_two_series(world):
    a, b = _insert(world), _insert(world)
    assert a["series_id"] != b["series_id"]


def test_a_series_of_another_project_is_not_a_series_here(world):
    r1 = _insert(world, site=world["s2"])
    assert _insert(world, site=world["s1"], series_id=r1["series_id"]) is None


def test_a_series_whose_every_revision_is_deleted_takes_no_new_revision(world):
    r1 = _insert(world)
    assert project_docs.soft_delete(world["db"], r1["id"]) is True
    assert _insert(world, series_id=r1["series_id"]) is None


def test_series_and_revision_are_unique(world):
    rows = world["db"].execute(
        "SELECT conname FROM pg_constraint WHERE conname = 'project_documents_series_revision'"
    ).fetchall()
    assert rows


# ---- apply + visibility --------------------------------------------------------------

def test_an_indexed_document_is_searchable_and_counts_are_set(world):
    r1 = _insert(world)
    assert project_docs.apply_result(world["db"], _result(r1)) == "indexed"
    row = _status(world, r1)
    assert row["status"] == "indexed" and row["chunks"] == 1 and row["indexed_at"] is not None
    hits = _search(world, [world["s1"]["id"]])
    assert [h["document_id"] for h in hits] == [r1["id"]]
    assert hits[0]["heading"] == "3 Concrete > 3.2 Formwork" and hits[0]["page_from"] == 2


def test_applying_the_same_artifact_twice_is_idempotent(world):
    r1 = _insert(world)
    project_docs.apply_result(world["db"], _result(r1))
    project_docs.apply_result(world["db"], _result(r1))
    n = world["db"].execute("SELECT count(*) FROM project_doc_chunks WHERE document_id=%s",
                            (r1["id"],)).fetchone()[0]
    assert n == 1


def test_a_new_indexed_revision_supersedes_the_previous(world):
    r1 = _insert(world)
    project_docs.apply_result(world["db"], _result(r1))
    r2 = _insert(world, series_id=r1["series_id"])
    project_docs.apply_result(world["db"], _result(r2))
    assert _status(world, r1)["superseded_at"] is not None
    assert {h["document_id"] for h in _search(world, [world["s1"]["id"]])} == {r2["id"]}


def test_an_unreadable_new_revision_leaves_the_previous_searchable(world):
    r1 = _insert(world)
    project_docs.apply_result(world["db"], _result(r1))
    r2 = _insert(world, series_id=r1["series_id"])
    out = project_docs.apply_result(world["db"], _result(
        r2, status="unreadable", chunks=[], reason="This looks like a scanned PDF"))
    assert out == "unreadable"
    assert _status(world, r1)["superseded_at"] is None
    assert _status(world, r2)["reason"] == "This looks like a scanned PDF"
    assert {h["document_id"] for h in _search(world, [world["s1"]["id"]])} == {r1["id"]}


def test_a_late_older_revision_lands_superseded(world):
    """Review Focus 1: Rev.2 still pending, Rev.3 uploaded and indexed first."""
    r1 = _insert(world)
    r2 = _insert(world, series_id=r1["series_id"])
    project_docs.apply_result(world["db"], _result(r2))
    project_docs.apply_result(world["db"], _result(r1))      # arrives late
    assert _status(world, r1)["superseded_at"] is not None
    assert {h["document_id"] for h in _search(world, [world["s1"]["id"]])} == {r2["id"]}


def test_deleting_the_current_revision_brings_back_its_predecessor(world):
    r1 = _insert(world)
    project_docs.apply_result(world["db"], _result(r1))
    r2 = _insert(world, series_id=r1["series_id"])
    project_docs.apply_result(world["db"], _result(r2))
    assert project_docs.soft_delete(world["db"], r2["id"]) is True
    assert _status(world, r1)["superseded_at"] is None
    assert {h["document_id"] for h in _search(world, [world["s1"]["id"]])} == {r1["id"]}
    current = project_docs.list_current(world["db"], world["s1"]["id"])
    assert [d["id"] for d in current] == [r1["id"]]


def test_deleting_a_non_current_revision_only_removes_it_from_history(world):
    r1 = _insert(world)
    project_docs.apply_result(world["db"], _result(r1))
    r2 = _insert(world, series_id=r1["series_id"])
    project_docs.apply_result(world["db"], _result(r2))
    project_docs.soft_delete(world["db"], r1["id"])
    assert _status(world, r2)["superseded_at"] is None
    hist = project_docs.list_series(world["db"], world["s1"]["id"], r1["series_id"])
    assert [d["revision"] for d in hist] == [2]


def test_an_artifact_for_a_deleted_document_is_ignored(world):
    r1 = _insert(world)
    project_docs.soft_delete(world["db"], r1["id"])
    assert project_docs.apply_result(world["db"], _result(r1)) == "deleted"
    assert _status(world, r1)["status"] == "pending"


def test_an_artifact_for_a_missing_or_foreign_document_is_ignored(world):
    ghost = {"id": uuid.uuid4(), "company_id": world["a"]["id"], "site_id": world["s1"]["id"],
             "series_id": uuid.uuid4(), "revision": 1}
    assert project_docs.apply_result(world["db"], _result(ghost)) == "missing"
    r1 = _insert(world)
    assert project_docs.apply_result(world["db"], _result(r1, company=world["b"]["id"])) == "mismatch"


def test_a_malformed_chunk_marks_the_document_error_not_a_crash(world):
    r1 = _insert(world)
    bad = [{"ordinal": 0, "heading": None, "page_from": None, "page_to": None,
            "chunk_text": "x", "embedding": [1.0, 0.0]}]
    assert project_docs.apply_result(world["db"], _result(r1, chunks=bad)) == "invalid"
    assert _status(world, r1)["status"] == "error"


# ---- search --------------------------------------------------------------------------

def test_search_never_returns_another_projects_document(world):
    r1 = _insert(world, site=world["s2"])
    project_docs.apply_result(world["db"], _result(r1))
    assert _search(world, [world["s1"]["id"]]) == []
    assert _search(world, [world["s1"]["id"], world["s2"]["id"]])[0]["document_id"] == r1["id"]


def test_pending_and_error_documents_are_not_searchable(world):
    r1 = _insert(world)
    project_docs.apply_result(world["db"], _result(r1, status="error", chunks=[], reason="boom"))
    _insert(world)                                        # pending
    assert _search(world, [world["s1"]["id"]]) == []


def test_the_vector_arm_drops_a_distant_chunk(world):
    r1 = _insert(world)
    project_docs.apply_result(world["db"], _result(r1))
    assert _search(world, [world["s1"]["id"]], qv=vec(7)) == []   # orthogonal: distance 1


def test_the_keyword_arm_finds_a_term_in_the_heading(world):
    r1 = _insert(world)
    project_docs.apply_result(world["db"], _result(r1))
    hits = _search(world, [world["s1"]["id"]], qv=vec(7), q_text="formwork")
    assert [h["document_id"] for h in hits] == [r1["id"]] and hits[0]["lexical_hit"] is True


def test_a_chinese_term_is_found_by_the_keyword_arm(world):
    """Review Focus 3: 'simple' keeps CJK; 0061's regexp_replace would have erased it."""
    r1 = _insert(world, filename="规范.md")
    chunk = {"ordinal": 0, "heading": "混凝土", "page_from": None, "page_to": None,
             "chunk_text": "保护层 厚度 40 毫米", "embedding": vec(3)}
    project_docs.apply_result(world["db"], _result(r1, chunks=[chunk]))
    hits = _search(world, [world["s1"]["id"]], qv=vec(7), q_text="保护层")
    assert [h["document_id"] for h in hits] == [r1["id"]]


def test_list_current_shows_the_latest_live_revision_per_series_newest_first(world):
    r1 = _insert(world, filename="A.pdf")
    _insert(world, series_id=r1["series_id"], filename="A.pdf")
    other = _insert(world, filename="B.pdf")
    rows = project_docs.list_current(world["db"], world["s1"]["id"])
    assert {(d["filename"], d["revision"]) for d in rows} == {("B.pdf", 1), ("A.pdf", 2)}
    assert "uploaded_by_name" in rows[0]
    # One transaction gives every row the same now(); order is checked on an explicit time.
    world["db"].execute("UPDATE project_documents SET created_at = now() + interval '1 minute' "
                        "WHERE id = %s", (other["id"],))
    rows = project_docs.list_current(world["db"], world["s1"]["id"])
    assert rows[0]["id"] == other["id"]


def test_rename_and_mark_pending(world):
    r1 = _insert(world)
    project_docs.apply_result(world["db"], _result(r1, status="error", chunks=[], reason="x"))
    assert project_docs.rename(world["db"], r1["id"], "Structural spec") is True
    assert project_docs.mark_pending(world["db"], r1["id"]) is True
    row = _status(world, r1)
    assert row["title"] == "Structural spec" and row["status"] == "pending" and row["reason"] is None


def test_document_chunks_have_no_hnsw_index(world):
    defs = [r[0] for r in world["db"].execute(
        "SELECT indexdef FROM pg_indexes WHERE tablename = 'project_doc_chunks'").fetchall()]
    assert defs and not any("hnsw" in d.lower() for d in defs)
