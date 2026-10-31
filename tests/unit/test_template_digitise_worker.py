"""The worker, driven from an S3 event to the result JSON (spec 2026-10-16 s3).

The model call is stubbed at the HTTP boundary (urllib3), never the whole function, so
the request the worker actually builds is what these tests see.
"""
import json
from io import BytesIO

import pytest

lu = pytest.importorskip("llm_utils", reason="requires urllib3 (installed in CI)")
docx = pytest.importorskip("docx")
import lambda_template_digitise as worker  # noqa: E402
import template_docx  # noqa: E402

COMPANY = "c0000000-0000-0000-0000-000000000001"
RID = "a" * 32
REQ_KEY = f"template_digitise_requests/{COMPANY}/{RID}.json"
RES_KEY = f"template_digitise_results/{COMPANY}/{RID}.json"


class _R:
    def __init__(self, status, payload):
        self.status = status
        self.data = json.dumps(payload).encode("utf-8")


def _ok(text):
    return _R(200, {"model": "google/gemini-3.8-flash",
                    "choices": [{"message": {"content": text}, "finish_reason": "stop"}],
                    "usage": {"completion_tokens": 5}})


class _S3:
    def __init__(self):
        self.store, self.puts, self.gets, self.sizes = {}, {}, [], {}

    def get_object(self, Bucket, Key):
        # S3 always answers with ContentLength; `sizes` lets a test claim an object
        # larger than the bytes it actually stores.
        self.gets.append(Key)
        body = self.store[Key]
        return {"Body": BytesIO(body), "ContentLength": self.sizes.get(Key, len(body))}

    def put_object(self, Bucket, Key, Body, **kw):
        self.puts[Key] = Body
        return {}


@pytest.fixture
def s3(monkeypatch):
    fake = _S3()
    monkeypatch.setattr(worker, "S3_BUCKET", "lake")
    monkeypatch.setattr(worker, "s3", lambda: fake)
    monkeypatch.setattr(lu, "LLM_PROVIDER", "qwen")
    monkeypatch.setattr(lu, "QWEN_API_KEY", "k")
    monkeypatch.setattr(lu, "QWEN_BASE_URL", "https://openrouter.ai/api/v1")
    monkeypatch.setattr(lu.time, "sleep", lambda s: None)
    return fake


def _http(monkeypatch, responses):
    sent, seq = [], list(responses)

    def fake(self, method, url, body=None, headers=None, timeout=None):
        sent.append(json.loads(body))
        return seq.pop(0)

    monkeypatch.setattr(lu.urllib3.PoolManager, "request", fake)
    return sent


def _request(s3, keys, **over):
    req = {"id": RID, "companyId": COMPANY, "userId": "u-1", "sourceKeys": keys,
           "name": "Site inspection", "requestedAt": "2026-10-16T00:00:00+00:00"}
    req.update(over)
    s3.store[REQ_KEY] = json.dumps(req).encode()


def _event(key=REQ_KEY):
    return {"Records": [{"s3": {"bucket": {"name": "lake"}, "object": {"key": key}}}]}


def _result(s3):
    return json.loads(s3.puts[RES_KEY])


def _docx_bytes():
    d = docx.Document()
    t = d.add_table(rows=0, cols=2)
    r = t.add_row(); r.cells[0].text = "General"; r.cells[1].text = "Yes/No/NA"
    r = t.add_row(); r.cells[0].text = "Gate locked"; r.cells[1].text = "Yes No N/A"
    buf = BytesIO(); d.save(buf)
    return buf.getvalue()


def test_THE_photos_become_a_draft_result(s3, monkeypatch):
    keys = [f"template_sources/{COMPANY}/{'b' * 32}.jpg", f"template_sources/{COMPANY}/{'c' * 32}.jpg"]
    for k in keys:
        s3.store[k] = b"\xff\xd8 not really a jpeg"
    _request(s3, keys)
    sent = _http(monkeypatch, [_ok(json.dumps({"sections": [
        {"title": "1. General", "items": ["a) Gate locked", "Signage up"]},
        {"title": "General", "items": ["Lights on"]}]}))])

    worker.lambda_handler(_event(), None)

    res = _result(s3)
    assert res["status"] == "done"
    assert res["route"] == "vision" and res["model"] == "google/gemini-3.8-flash"
    assert res["pages"] == 2
    assert res["counts"] == {"sections": 2, "items": 3}
    assert res["sections"][0]["title"] == "General"
    assert res["sections"][0]["items"][0] == {"text": "Gate locked", "flags": []}
    assert res["sections"][0]["flags"] == ["split_section"]
    assert (res["id"], res["companyId"], res["userId"], res["sourceKeys"]) == (
        RID, COMPANY, "u-1", keys)
    # the request that went out: prompt first, then both pages, in order
    content = sent[0]["messages"][0]["content"]
    assert content[0]["type"] == "text"
    assert [p["type"] for p in content[1:]] == ["image_url", "image_url"]


def test_a_word_file_is_read_without_any_model_call(s3, monkeypatch):
    key = f"template_sources/{COMPANY}/{'d' * 32}.docx"
    s3.store[key] = _docx_bytes()
    _request(s3, [key])
    sent = _http(monkeypatch, [])
    worker.lambda_handler(_event(), None)
    res = _result(s3)
    assert res["status"] == "done" and res["route"] == "docx" and res["model"] is None
    assert res["sections"][0]["items"] == [{"text": "Gate locked", "flags": []}]
    assert sent == []


def test_without_the_docx_layer_a_word_file_is_an_error_not_an_importerror(s3, monkeypatch):
    key = f"template_sources/{COMPANY}/{'d' * 32}.docx"
    s3.store[key] = b"PK"
    _request(s3, [key])
    monkeypatch.setattr(template_docx, "AVAILABLE", False)
    worker.lambda_handler(_event(), None)
    res = _result(s3)
    assert res["status"] == "error" and res["error"] == worker.MSG_WORD_DISABLED


def test_a_word_file_with_no_checklist_table_is_unreadable(s3):
    d = docx.Document(); d.add_paragraph("Just prose")
    buf = BytesIO(); d.save(buf)
    key = f"template_sources/{COMPANY}/{'d' * 32}.docx"
    s3.store[key] = buf.getvalue()
    _request(s3, [key])
    worker.lambda_handler(_event(), None)
    res = _result(s3)
    assert res["status"] == "unreadable" and res["reason"] == worker.MSG_WORD_NO_TABLE


def test_zero_items_from_the_model_is_unreadable(s3, monkeypatch):
    key = f"template_sources/{COMPANY}/{'b' * 32}.png"
    s3.store[key] = b"png"
    _request(s3, [key])
    _http(monkeypatch, [_ok('{"sections": [{"title": "Photo", "items": []}]}')])
    worker.lambda_handler(_event(), None)
    res = _result(s3)
    assert res["status"] == "unreadable" and res["reason"] == worker.MSG_NO_ROWS


def test_a_source_outside_the_requests_company_is_never_read(s3, monkeypatch):
    other = "c0000000-0000-0000-0000-000000000002"
    key = f"template_sources/{other}/{'b' * 32}.png"
    s3.store[key] = b"png"
    _request(s3, [key])
    sent = _http(monkeypatch, [])
    worker.lambda_handler(_event(), None)
    assert key not in s3.gets and sent == []
    assert _result(s3)["status"] == "error"


def test_a_mixed_request_is_refused_before_any_read(s3, monkeypatch):
    keys = [f"template_sources/{COMPANY}/{'d' * 32}.docx", f"template_sources/{COMPANY}/{'b' * 32}.png"]
    _request(s3, keys)
    sent = _http(monkeypatch, [])
    worker.lambda_handler(_event(), None)
    res = _result(s3)
    assert res["status"] == "error" and res["error"] == worker.SHAPE_ERROR
    assert sent == []


def test_a_key_that_is_not_a_request_is_ignored(s3, monkeypatch):
    sent = _http(monkeypatch, [])
    worker.lambda_handler(_event(RES_KEY), None)
    worker.lambda_handler(_event("template_digitise_requests/not-a-company/x.json"), None)
    assert s3.puts == {} and s3.gets == [] and sent == []


def test_an_unexpected_failure_still_writes_an_error_result(s3, monkeypatch):
    key = f"template_sources/{COMPANY}/{'b' * 32}.png"
    _request(s3, [key])                       # the source object is missing -> KeyError
    worker.lambda_handler(_event(), None)
    res = _result(s3)
    assert res["status"] == "error" and res["error"] == worker.MSG_FAILED


def test_the_result_carries_the_owner_hint_for_the_lake_index(s3, monkeypatch):
    key = f"template_sources/{COMPANY}/{'b' * 32}.png"
    s3.store[key] = b"png"
    _request(s3, [key])
    _http(monkeypatch, [_ok('{"sections": [{"title": "T", "items": ["i"]}]}')])
    worker.lambda_handler(_event(), None)
    lines = [json.loads(v) for k, v in s3.puts.items() if k.startswith("lake_manifest/")]
    assert any(l["key"] == RES_KEY and l["hint"].get("company_id") == COMPANY for l in lines)


class _Context:
    def __init__(self, remaining_ms):
        self.remaining_ms = remaining_ms

    def get_remaining_time_in_millis(self):
        return self.remaining_ms


def test_the_model_deadline_leaves_the_result_write_its_reserve(s3, monkeypatch):
    key = f"template_sources/{COMPANY}/{'b' * 32}.png"
    s3.store[key] = b"png"
    _request(s3, [key])
    _http(monkeypatch, [_ok('{"sections": [{"title": "T", "items": ["i"]}]}')])
    import template_vision
    seen, real = [], template_vision.read

    def spy(pages, deadline=None):
        seen.append(deadline)
        return real(pages, deadline=deadline)

    monkeypatch.setattr(template_vision, "read", spy)
    worker.lambda_handler(_event(), _Context(900_000))
    assert _result(s3)["status"] == "done"
    assert len(seen) == 1
    assert 900 - worker.WRITE_RESERVE_SECONDS - 5 < seen[0] <= 900 - worker.WRITE_RESERVE_SECONDS


def test_a_request_that_cannot_be_parsed_still_gets_an_error_result(s3):
    s3.store[REQ_KEY] = b"{not json"
    worker.lambda_handler(_event(), None)
    res = _result(s3)
    assert res["status"] == "error" and res["error"] == worker.MSG_FAILED
    assert (res["id"], res["companyId"]) == (RID, COMPANY)


def test_a_request_that_is_not_its_keys_own_is_answered_but_never_echoed(s3, monkeypatch):
    other = "c0000000-0000-0000-0000-000000000002"
    key = f"template_sources/{other}/{'b' * 32}.png"
    _request(s3, [key], companyId=other)
    sent = _http(monkeypatch, [])
    worker.lambda_handler(_event(), None)
    res = _result(s3)
    assert res["status"] == "error" and res["sourceKeys"] == [] and res["companyId"] == COMPANY
    assert key not in s3.gets and sent == []


def test_the_docx_reader_state_is_logged_at_import(caplog):
    import importlib
    import logging
    with caplog.at_level(logging.INFO):
        importlib.reload(worker)
    assert any(r.getMessage() in ("docx reader: enabled", "docx reader: DISABLED")
               for r in caplog.records)


class _Unread(BytesIO):
    """A body that fails the test if anyone reads it."""
    def read(self, *a):
        raise AssertionError("an oversized source was read before its size was checked")


@pytest.mark.parametrize("ext,route", [("jpg", "vision"), ("pdf", "vision"), ("docx", "docx")])
def test_a_source_over_the_limit_is_refused_before_it_is_read(s3, monkeypatch, ext, route):
    import template_types
    key = f"template_sources/{COMPANY}/{'e' * 32}.{ext}"
    s3.store[key] = b"x"
    s3.sizes[key] = template_types.MAX_BYTES + 1
    real_get = s3.get_object

    def get_object(Bucket, Key):
        obj = real_get(Bucket, Key)
        if Key == key:
            obj["Body"] = _Unread(b"x")
        return obj

    monkeypatch.setattr(s3, "get_object", get_object)
    _request(s3, [key])
    sent = _http(monkeypatch, [])
    worker.lambda_handler(_event(), None)
    res = _result(s3)
    assert res["status"] == "error"
    assert res["route"] == route
    assert res["error"] == "This file is larger than 20 MB — upload a smaller file."
    assert sent == [], "no model call for a file that was never read"


def test_a_source_exactly_at_the_limit_is_still_read(s3, monkeypatch):
    import template_types
    key = f"template_sources/{COMPANY}/{'d' * 32}.docx"
    s3.store[key] = _docx_bytes()
    s3.sizes[key] = template_types.MAX_BYTES
    _request(s3, [key])
    worker.lambda_handler(_event(), None)
    assert _result(s3)["status"] == "done"


def test_a_pdf_read_by_the_pdf_model_is_labelled_with_that_model(s3, monkeypatch):
    """With TEMPLATE_DIGITISE_PDF_MODEL set, `model()` names a model that did not read
    the PDF; the result must name the one that did (spec 2026-10-31 B2)."""
    monkeypatch.setenv("TEMPLATE_DIGITISE_MODEL", "deepseek/deepseek-v4.1-flash")
    monkeypatch.setenv("TEMPLATE_DIGITISE_PDF_MODEL", "meta/muse-spark-1.3-contributor")
    key = f"template_sources/{COMPANY}/{'d' * 32}.pdf"
    s3.store[key] = b"%PDF <</Type/Page>>"
    _request(s3, [key])
    sent = _http(monkeypatch, [_ok(json.dumps({"sections": [
        {"title": "General", "items": ["Gate locked"]}]}))])

    worker.lambda_handler(_event(), None)

    assert sent[0]["model"] == "meta/muse-spark-1.3-contributor"
    assert _result(s3)["model"] == "meta/muse-spark-1.3-contributor"
