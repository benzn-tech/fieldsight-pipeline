"""The project-document worker, from a real S3 event through the real handler, with
DashScope stubbed at the HTTP boundary (urllib3.PoolManager). R9: every accepted request
ends in an artifact, so no document is ever stuck pending."""
import io
import json

import pytest

pytest.importorskip("docx")
pytest.importorskip("pypdf")

import dashscope_utils  # noqa: E402
import lambda_project_doc_index as w  # noqa: E402
from tests.unit.doc_fixtures import make_docx, make_pdf  # noqa: E402

CO = "c0000000-0000-0000-0000-000000000001"
SITE = "50000000-0000-0000-0000-000000000001"
DOC = "d0000000-0000-0000-0000-000000000001"
SERIES = "5e000000-0000-0000-0000-000000000001"


class _S3:
    def __init__(self):
        self.objects, self.puts = {}, {}

    def get_object(self, Bucket, Key):
        body = self.objects[Key]
        return {"Body": io.BytesIO(body), "ContentLength": len(body)}

    def put_object(self, Bucket, Key, Body, **kw):
        self.puts[Key] = Body
        return {}


class _Resp:
    def __init__(self, status, payload):
        self.status, self.data = status, json.dumps(payload).encode()


class _Pool:
    batches = []
    fail = False

    def request(self, method, url, body=None, headers=None, timeout=None):
        n = len(json.loads(body)["input"])
        _Pool.batches.append(n)
        if _Pool.fail:
            return _Resp(400, {"message": "bad"})
        return _Resp(200, {"data": [{"index": i, "embedding": [1.0] + [0.0] * 1023}
                                    for i in range(n)]})


class _Ctx:
    def __init__(self, ms=900_000):
        self.ms = ms

    def get_remaining_time_in_millis(self):
        return self.ms


@pytest.fixture
def s3(monkeypatch):
    fake = _S3()
    monkeypatch.setattr(w, "s3", lambda: fake)
    monkeypatch.setattr(w, "S3_BUCKET", "lake")
    monkeypatch.setattr(dashscope_utils, "DASHSCOPE_API_KEY", "k")
    monkeypatch.setattr(dashscope_utils.urllib3, "PoolManager", lambda *a, **k: _Pool())
    monkeypatch.setattr(dashscope_utils.time, "sleep", lambda s: None)
    _Pool.batches, _Pool.fail = [], False
    return fake


def _request(s3, ext, data, **over):
    src = f"project_doc_sources/{CO}/{DOC}.{ext}"
    s3.objects[src] = data
    req = {"id": DOC, "companyId": CO, "siteId": SITE, "seriesId": SERIES, "revision": 1,
           "sourceKey": src, "contentType": "x", "filename": f"Spec.{ext}", "userId": "u1",
           "requestedAt": "2026-10-16T00:00:00+00:00"}
    req.update(over)
    key = f"project_doc_requests/{CO}/{DOC}.json"
    s3.objects[key] = json.dumps(req).encode()
    return key


def _run(key, ctx=None):
    w.lambda_handler({"Records": [{"s3": {"object": {"key": key}}}]}, ctx or _Ctx())


def _artifact(s3):
    return json.loads(s3.puts[f"project_doc_vectors/{CO}/{DOC}.json"])


def test_THE_a_docx_is_indexed_end_to_end(s3):
    def build(d):
        d.add_heading("Concrete", level=1)
        d.add_paragraph("Cover to reinforcement shall be 40 mm in exposure B2.")
    _run(_request(s3, "docx", make_docx(build)))
    art = _artifact(s3)
    assert art["status"] == "indexed" and art["document_id"] == DOC
    assert (art["company_id"], art["site_id"], art["series_id"], art["revision"]) == (CO, SITE, SERIES, 1)
    c = art["chunks"][0]
    assert c["heading"] == "Concrete" and c["chunk_text"].startswith("Cover to reinforcement")
    assert len(c["embedding"]) == 1024 and c["ordinal"] == 0
    # A manifest line rides along with the artifact (lake_put).
    assert any(k.startswith("lake_manifest/") for k in s3.puts)


def test_the_embedding_is_batched_by_ten_and_carries_the_heading(s3, monkeypatch):
    seen, dims = [], []
    real = dashscope_utils.embed

    def embed(texts, dim=None):
        seen.extend(texts)
        dims.append(dim)
        return real(texts, dim=dim)
    monkeypatch.setattr(dashscope_utils, "embed", embed)
    paras = "\n\n".join("# H%d\n\nbody %d" % (i, i) for i in range(23))
    _run(_request(s3, "md", paras.encode()))
    assert _Pool.batches == [10, 10, 3]
    assert dims == [1024, 1024, 1024]          # vector(1024): never the env default
    assert seen[0] == "H0\n\nbody 0"
    assert _artifact(s3)["chunks"][0]["chunk_text"] == "body 0"


def test_a_scanned_pdf_writes_an_unreadable_artifact(s3):
    _run(_request(s3, "pdf", make_pdf([[], []])))
    art = _artifact(s3)
    assert art["status"] == "unreadable" and "scanned" in art["reason"] and art["pages"] == 2
    assert art["chunks"] == [] and _Pool.batches == []


def test_a_damaged_docx_writes_an_unreadable_artifact(s3):
    """Review Focus 4: never a stuck pending."""
    _run(_request(s3, "docx", b"not a zip"))
    assert _artifact(s3)["status"] == "unreadable"


def test_no_pdf_layer_is_an_error_not_unreadable(s3, monkeypatch):
    import project_doc_text
    monkeypatch.setattr(project_doc_text, "PDF_AVAILABLE", False)
    _run(_request(s3, "pdf", make_pdf([["x" * 400]])))
    art = _artifact(s3)
    assert art["status"] == "error" and art["reason"] == project_doc_text.MSG_NO_PDF


def test_too_many_chunks_is_refused_before_any_embedding(s3, monkeypatch):
    monkeypatch.setattr(w.project_doc_types, "MAX_CHUNKS", 2)
    paras = "\n\n".join("# H%d\n\nbody" % i for i in range(3))
    _run(_request(s3, "md", paras.encode()))
    art = _artifact(s3)
    assert art["status"] == "unreadable" and art["reason"] == w.MSG_TOO_MANY
    assert _Pool.batches == []


def test_running_out_of_time_writes_an_error_instead_of_being_killed(s3, monkeypatch):
    ticks = iter(range(0, 1000, 5))
    monkeypatch.setattr(w, "_clock", lambda: float(next(ticks)))
    paras = "\n\n".join("# H%d\n\nbody" % i for i in range(30))
    # 61 s left: 1 s of budget after the 60 s write reserve; each clock read moves 5 s.
    _run(_request(s3, "md", paras.encode()), ctx=_Ctx(ms=61_000))
    art = _artifact(s3)
    assert art["status"] == "error" and art["reason"] == w.MSG_TOO_SLOW


def test_an_embedding_failure_is_an_error_artifact(s3):
    _Pool.fail = True
    _run(_request(s3, "txt", b"some text"))
    art = _artifact(s3)
    assert art["status"] == "error" and art["reason"] == w.MSG_EMBED


def test_an_unexpected_failure_still_writes_an_error(s3, monkeypatch):
    def boom(*a, **k):
        raise KeyError("x")
    monkeypatch.setattr(w.project_doc_chunks, "chunk", boom)
    _run(_request(s3, "txt", b"some text"))
    assert _artifact(s3)["status"] == "error" and _artifact(s3)["reason"] == w.MSG_FAILED


def test_a_request_that_disagrees_with_its_key_is_refused_with_an_error(s3):
    key = _request(s3, "txt", b"text", companyId="c0000000-0000-0000-0000-000000000009")
    _run(key)
    art = _artifact(s3)
    assert art["status"] == "error" and art["site_id"] is None


def test_a_source_outside_its_own_prefix_is_never_read(s3):
    other = "project_doc_sources/c0000000-0000-0000-0000-000000000009/x.txt"
    s3.objects[other] = b"secret"
    key = _request(s3, "txt", b"text", sourceKey=other)
    _run(key)
    assert _artifact(s3)["status"] == "error" and _Pool.batches == []


def test_a_key_that_is_not_a_request_is_ignored(s3):
    _run("project_doc_requests/nope.json")
    assert s3.puts == {}


def test_too_large_an_object_is_not_read(s3, monkeypatch):
    monkeypatch.setattr(w.project_doc_types, "MAX_BYTES", 3)
    _run(_request(s3, "txt", b"abcdef"))
    assert _artifact(s3)["status"] == "unreadable"


def test_the_size_is_checked_from_content_length_before_the_body_is_read(s3, monkeypatch):
    """extract() re-checks the size too, so only an unread body proves the ContentLength
    guard ran first (a 50 MB read into a Lambda is the cost being avoided)."""
    monkeypatch.setattr(w.project_doc_types, "MAX_BYTES", 3)
    key = _request(s3, "txt", b"abcdef")
    real_get = s3.get_object

    class _Unreadable(io.BytesIO):
        def read(self, *a):
            raise AssertionError("the body of an oversized source was read")

    def get_object(Bucket, Key):
        resp = real_get(Bucket, Key)
        if Key.startswith("project_doc_sources/"):
            resp["Body"] = _Unreadable()
        return resp
    monkeypatch.setattr(s3, "get_object", get_object)
    _run(key)
    art = _artifact(s3)
    assert art["status"] == "unreadable" and art["reason"] != w.MSG_FAILED


def test_nul_bytes_never_reach_the_artifact(s3, monkeypatch):
    """Postgres rejects NUL in text, and pypdf can emit it."""
    import project_doc_text
    blocks = [{"kind": "heading", "text": "Con\x00crete", "level": 1, "page": 1},
              {"kind": "para", "text": "Cover\x00 40 mm", "level": None, "page": 1},
              {"kind": "para", "text": "\x00\x00", "level": None, "page": 1}]
    monkeypatch.setattr(project_doc_text, "extract",
                        lambda data, ext: {"blocks": blocks, "pages": 1, "chars": 20})
    _run(_request(s3, "txt", b"ignored"))
    art = _artifact(s3)
    assert art["status"] == "indexed" and len(art["chunks"]) == 1
    c = art["chunks"][0]
    assert (c["heading"], c["chunk_text"]) == ("Concrete", "Cover 40 mm")
    assert "\u0000" not in s3.puts[w.vectors_key(CO, DOC)]
