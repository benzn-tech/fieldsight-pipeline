"""The apply route against a real Postgres: one artifact from the worker's shape becomes
chunks and a status, through apply_key (the SQL the unit test cannot run); and a failure
never leaves a document pending, nor takes an indexed one out of search (R9)."""
import contextlib
import io
import json
import uuid

import pytest

from repositories import companies, project_docs, sites, users

pytestmark = pytest.mark.integration
pda = pytest.importorskip("project_doc_apply")


class _S3:
    def __init__(self, body=None, raw=None):
        self.body, self.raw = body, raw

    def get_object(self, Bucket, Key):
        data = self.raw if self.raw is not None else json.dumps(self.body).encode()
        return {"Body": io.BytesIO(data)}


@pytest.fixture
def doc(db, monkeypatch):
    tag = uuid.uuid4().hex[:8]
    co = companies.create_company(db, f"ApplyCo-{tag}")
    site = sites.create_site(db, co["id"], f"S-{tag}")
    u = users.upsert_user(db, f"sub-{tag}", f"{tag}@x.nz", company_id=co["id"])
    doc_id = str(uuid.uuid4())
    row = project_docs.insert_document(
        db, doc_id=doc_id, site_id=site["id"], company_id=co["id"], title="Spec",
        filename="Spec.txt", source_key=f"project_doc_sources/{co['id']}/{doc_id}.txt",
        content_type="text/plain", uploaded_by=u["id"])
    monkeypatch.setattr(pda, "get_connection", lambda: contextlib.nullcontext(db))
    key = f"project_doc_vectors/{co['id']}/{doc_id}.json"
    return dict(db=db, co=co, site=site, row=row, id=doc_id, key=key)


def _artifact(d):
    v = [0.0] * 1024
    v[0] = 1.0
    return {"document_id": d["id"], "company_id": str(d["co"]["id"]),
            "site_id": str(d["site"]["id"]), "series_id": str(d["row"]["series_id"]),
            "revision": 1, "status": "indexed", "reason": None, "pages": None, "chars": 20,
            "chunks": [{"ordinal": 0, "heading": None, "page_from": None, "page_to": None,
                        "chunk_text": "Cover is 40 mm.", "embedding": v}]}


def _chunks(d):
    return d["db"].execute("SELECT count(*) FROM project_doc_chunks WHERE document_id=%s",
                           (d["id"],)).fetchone()[0]


def test_a_worker_artifact_is_applied_through_the_route(doc):
    assert pda.apply_key(doc["key"], _S3(_artifact(doc)), "b")["outcome"] == "indexed"
    assert project_docs.get_document(doc["db"], doc["id"])["status"] == "indexed"
    assert _chunks(doc) == 1


def test_an_apply_that_raises_leaves_the_document_error_not_pending(doc, monkeypatch):
    real = project_docs.apply_result

    def half_then_boom(conn, result):
        real(conn, result)                    # writes chunks + status inside the savepoint
        raise RuntimeError("connection lost mid-apply")

    monkeypatch.setattr(pda.project_docs, "apply_result", half_then_boom)
    assert pda.apply_key(doc["key"], _S3(_artifact(doc)), "b")["outcome"] == "error"
    got = project_docs.get_document(doc["db"], doc["id"])
    assert got["status"] == "error" and got["reason"] == project_docs.MSG_INVALID
    assert _chunks(doc) == 0                  # the partial apply rolled back


def test_a_failed_reapply_does_not_take_an_indexed_document_out_of_search(doc, monkeypatch):
    assert pda.apply_key(doc["key"], _S3(_artifact(doc)), "b")["outcome"] == "indexed"

    def boom(conn, result):
        raise RuntimeError("boom")

    monkeypatch.setattr(pda.project_docs, "apply_result", boom)
    assert pda.apply_key(doc["key"], _S3(_artifact(doc)), "b")["outcome"] == "error"
    assert project_docs.get_document(doc["db"], doc["id"])["status"] == "indexed"
    assert _chunks(doc) == 1


def test_an_unreadable_or_mismatched_artifact_marks_the_keys_document_error(doc):
    assert pda.apply_key(doc["key"], _S3(raw=b"{not json"), "b")["outcome"] == \
        "unreadable_artifact"
    assert project_docs.get_document(doc["db"], doc["id"])["status"] == "error"


def test_a_mismatch_marks_only_the_keys_own_document(doc):
    art = _artifact(doc)
    art["company_id"] = str(uuid.uuid4())
    assert pda.apply_key(doc["key"], _S3(art), "b")["outcome"] == "mismatch"
    assert project_docs.get_document(doc["db"], doc["id"])["status"] == "error"
    assert _chunks(doc) == 0


def test_the_error_mark_is_scoped_to_the_keys_company(doc):
    other_key = f"project_doc_vectors/{uuid.uuid4()}/{doc['id']}.json"
    art = _artifact(doc)
    art["company_id"] = other_key.split("/")[1]          # agrees with its (foreign) key
    assert pda.apply_key(other_key, _S3(art), "b")["outcome"] == "mismatch"
    # apply_result refused it (company mismatch) and the error mark did not cross tenants
    assert project_docs.get_document(doc["db"], doc["id"])["status"] == "pending"
