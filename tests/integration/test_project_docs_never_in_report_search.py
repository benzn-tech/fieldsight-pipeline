"""Contamination rule 1, run as SQL: the recordings search (report_chunks) never returns
a document row, by vector or by keyword, even when the document's chunk is the exact
answer and both sit on the same project."""
import uuid

import pytest

from repositories import chunks, companies, project_docs, sites, users

pytestmark = pytest.mark.integration


def _vec(i):
    v = [0.0] * 1024
    v[i] = 1.0
    return v


def test_report_search_never_returns_a_document_chunk(db):
    tag = uuid.uuid4().hex[:8]
    co = companies.create_company(db, f"MixCo-{tag}")
    site = sites.create_site(db, co["id"], f"S-{tag}")
    u = users.upsert_user(db, f"sub-{tag}", f"{tag}@x.nz", company_id=co["id"])
    doc_id = str(uuid.uuid4())
    row = project_docs.insert_document(
        db, doc_id=doc_id, site_id=site["id"], company_id=co["id"], title="Spec",
        filename="Spec.txt", source_key=f"project_doc_sources/{co['id']}/{doc_id}.txt",
        content_type="text/plain", uploaded_by=u["id"])
    applied = project_docs.apply_result(db, {
        "document_id": doc_id, "company_id": str(co["id"]), "status": "indexed",
        "pages": None, "chars": 30, "chunks": [{
            "ordinal": 0, "heading": "Formwork", "page_from": None, "page_to": None,
            "chunk_text": "Formwork tolerance zebraword 5 mm", "embedding": _vec(1)}]})
    rec = chunks.insert_chunk(db, site["id"], "2026-10-14", "topic", "Poured slab B2",
                              _vec(2), user_id=u["id"], source_s3_key="extractions/a.json")
    assert row is not None
    assert applied == "indexed"
    # Positive control: the document chunk IS findable -- through the documents search --
    # so its absence below is the separation, not a document that never got indexed.
    doc_hits = project_docs.search(db, [str(site["id"])], _vec(1), "zebraword or formwork")
    assert any("zebraword" in h["chunk_text"] for h in doc_hits)
    hits = chunks.search_chunks(db, _vec(1), [str(site["id"])], k=20,
                                query_text="zebraword or formwork")
    assert [h["id"] for h in hits] == [rec["id"]]
    assert not any("zebraword" in (h.get("chunk_text") or "") for h in hits)
