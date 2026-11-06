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


def test_a_word_file_with_no_checklist_table_is_unreadable(s3, monkeypatch):
    # Since spec 2026-11-06 R4 it goes to the model reader first; the model finds nothing.
    d = docx.Document(); d.add_paragraph("Just prose")
    buf = BytesIO(); d.save(buf)
    key = f"template_sources/{COMPANY}/{'d' * 32}.docx"
    s3.store[key] = buf.getvalue()
    _request(s3, [key])
    sent = _http(monkeypatch, [_ok('{"sections": []}')])
    worker.lambda_handler(_event(), None)
    res = _result(s3)
    assert res["status"] == "unreadable" and res["reason"] == worker.MSG_NO_ROWS
    assert len(sent) == 1


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


# ---- the model fallback for a Word file no rule reads (spec 2026-11-06 R4) ------------

def _docx_of(*rows, heading=None):
    d = docx.Document()
    if heading:
        d.add_paragraph(heading)
    t = d.add_table(rows=0, cols=len(rows[0]))
    for cells in rows:
        r = t.add_row()
        for i, c in enumerate(cells):
            r.cells[i].text = c
    buf = BytesIO(); d.save(buf)
    return buf.getvalue()


def _docx_no_answer_column():
    return _docx_of(("No.", "Item", "Checked by"), ("1", "Studs at 600 centres", ""),
                    ("2", "Lintels sized to drawings", ""), heading="Wall Framing")


def _docx_source(s3, body):
    key = f"template_sources/{COMPANY}/{'d' * 32}.docx"
    s3.store[key] = body
    _request(s3, [key])


def test_docx_with_no_rule_rows_falls_back_to_model(s3, monkeypatch):
    monkeypatch.delenv("TEMPLATE_DIGITISE_MODEL", raising=False)
    _docx_source(s3, _docx_no_answer_column())
    sent = _http(monkeypatch, [_ok(json.dumps({"sections": [
        {"title": "Wall Framing", "items": ["Studs at 600 centres", "Lintels sized to drawings"]}]}))])
    worker.lambda_handler(_event(), None)
    res = _result(s3)
    assert res["status"] == "done"
    assert res["route"] == "docx_model" and res["model"] == "google/gemini-3.8-flash"
    assert res["counts"] == {"sections": 1, "items": 2}
    content = sent[0]["messages"][0]["content"]
    assert content[0] == {"type": "text", "text": worker.template_vision.TEXT_PROMPT}
    assert "1 | Studs at 600 centres | " in content[1]["text"]


def test_docx_fallback_finds_nothing_is_unreadable_no_rows(s3, monkeypatch):
    _docx_source(s3, _docx_no_answer_column())
    _http(monkeypatch, [_ok('{"sections": [{"title": "Wall Framing", "items": []}]}')])
    worker.lambda_handler(_event(), None)
    res = _result(s3)
    assert res["status"] == "unreadable" and res["reason"] == worker.MSG_NO_ROWS
    assert res["route"] == "docx_model"


def test_docx_fallback_model_error_is_mapped_as_the_vision_branch_does(s3, monkeypatch):
    _docx_source(s3, _docx_no_answer_column())
    _http(monkeypatch, [_R(503, {"error": {"message": "down"}})] * lu.MAX_ATTEMPTS)
    worker.lambda_handler(_event(), None)
    res = _result(s3)
    assert res["status"] == "error" and res["route"] == "docx_model"
    assert res["error"] == worker.template_vision.MSG_UNAVAILABLE


def test_docx_too_long_for_fallback_keeps_old_refusal(s3, monkeypatch):
    _docx_source(s3, _docx_no_answer_column())
    monkeypatch.setattr(template_docx, "render_text",
                        lambda source: "x" * (worker.template_vision.MAX_TEXT_CHARS + 1))
    sent = _http(monkeypatch, [])
    worker.lambda_handler(_event(), None)
    res = _result(s3)
    assert res["status"] == "unreadable" and res["reason"] == worker.MSG_WORD_NO_TABLE
    assert sent == []


def test_docx_rule_read_never_calls_model(s3, monkeypatch):
    _docx_source(s3, _docx_of(("No.", "Item", "Yes / No / N/A", "Comment"),
                              ("1", "Gate locked", "", "")))
    sent = _http(monkeypatch, [])
    worker.lambda_handler(_event(), None)
    res = _result(s3)
    assert res["status"] == "done" and res["route"] == "docx" and res["model"] is None
    assert res["sections"][0]["items"] == [{"text": "Gate locked", "flags": []}]
    assert sent == []


def test_the_fallback_read_gets_the_workers_deadline(s3, monkeypatch):
    _docx_source(s3, _docx_no_answer_column())
    seen = []

    def spy(text, deadline=None):
        seen.append(deadline)
        return [{"title": "T", "items": ["i"]}], None

    monkeypatch.setattr(worker.template_vision, "read_text", spy)
    worker.lambda_handler(_event(), _Context(900_000))
    assert _result(s3)["status"] == "done"
    assert 900 - worker.WRITE_RESERVE_SECONDS - 5 < seen[0] <= 900 - worker.WRITE_RESERVE_SECONDS


# ---- the upload audit (spec 2026-11-06 A1, A5) ------------------------------------------

AUDIT_KEY = f"template_digitise_audits/{COMPANY}/{RID}.json"


class _SNS:
    def __init__(self, fail=False):
        self.published, self.fail = [], fail

    def publish(self, **kw):
        if self.fail:
            raise AssertionError("published with no topic configured")
        self.published.append(kw)
        return {"MessageId": "m"}


@pytest.fixture
def sns(monkeypatch):
    fake = _SNS()
    monkeypatch.setattr(worker, "sns", lambda: fake)
    return fake


@pytest.fixture
def audit_on(monkeypatch):
    monkeypatch.setenv("TEMPLATE_DIGITISE_AUDIT", "true")
    monkeypatch.setenv("ALERT_TOPIC_ARN", "arn:aws:sns:ap-southeast-2:1:alerts")
    monkeypatch.setenv("STAGE", "test")
    for name in ("TEMPLATE_DIGITISE_MODEL", "TEMPLATE_DIGITISE_AUDIT_MODEL",
                 "TEMPLATE_DIGITISE_AUDIT_ALT_MODEL"):
        monkeypatch.delenv(name, raising=False)


def _audit_answer(verdict="ok", source_items=1, **lists):
    out = {"verdict": verdict, "source_items": source_items, "missing": [], "extra": [],
           "reworded": [], "split_or_merged": [], "summary": "checked"}
    out.update(lists)
    return _ok(json.dumps(out))


def _audit(s3):
    return json.loads(s3.puts[AUDIT_KEY])


def _rule_read_docx():
    # The rule reads "Gate locked"; "Fence intact" holds a written answer, so it is not an
    # item -- but it IS in the source text, so a model naming it as missing is checkable.
    return _docx_of(("No.", "Item", "Yes / No / N/A", "Comment"), ("1", "Gate locked", "", ""),
                    ("2", "Fence intact", "Checked by J. Smith on site, all good", ""))


def test_audit_off_writes_nothing(s3, sns, monkeypatch):
    monkeypatch.delenv("TEMPLATE_DIGITISE_AUDIT", raising=False)
    _docx_source(s3, _rule_read_docx())
    sent = _http(monkeypatch, [])
    assert worker.lambda_handler(_event(), None) == {"ok": True}
    assert _result(s3)["status"] == "done"
    assert not any(k.startswith("template_digitise_audits/") for k in s3.puts)
    assert sent == [] and sns.published == []


def test_audit_ok_stored_not_published(s3, sns, audit_on, monkeypatch):
    _docx_source(s3, _rule_read_docx())
    sent = _http(monkeypatch, [_audit_answer("ok", 1)])
    worker.lambda_handler(_event(), None)
    rec = _audit(s3)
    assert rec["verdict"] == "ok" and rec["model"] == "google/gemini-3.8-flash"
    assert (rec["id"], rec["companyId"], rec["name"], rec["userId"]) == (
        RID, COMPANY, "Site inspection", "u-1")
    assert rec["sourceKeys"] == _result(s3)["sourceKeys"]
    assert (rec["route"], rec["status"], rec["counts"]) == ("docx", "done", {"sections": 1, "items": 1})
    assert isinstance(rec["elapsed_s"], float) and rec["dropped_claims"] == []
    assert sns.published == []
    # one call, the audit's: a rule read sends the Word file's rendering, then the draft
    assert len(sent) == 1 and sent[0]["model"] == "google/gemini-3.8-flash"
    content = sent[0]["messages"][0]["content"]
    assert content[0]["text"] == worker.template_audit.PROMPT
    assert "1 | Gate locked" in content[1]["text"] and "Fence intact" in content[1]["text"]
    assert '"Gate locked"' in content[2]["text"]
    lines = [json.loads(v) for k, v in s3.puts.items() if k.startswith("lake_manifest/")]
    assert any(l["key"] == AUDIT_KEY and l["hint"].get("company_id") == COMPANY for l in lines)


def test_audit_problems_published_to_topic(s3, sns, audit_on, monkeypatch):
    _docx_source(s3, _rule_read_docx())
    _http(monkeypatch, [_audit_answer("problems", 2, missing=[
        {"section": "", "text": "Fence intact"}, {"section": "", "text": "Roof flashing"}])])
    worker.lambda_handler(_event(), None)
    rec = _audit(s3)
    assert rec["verdict"] == "problems"
    assert rec["missing"] == [{"section": "", "text": "Fence intact"}]
    assert [d["claim"]["text"] for d in rec["dropped_claims"]] == ["Roof flashing"]
    assert len(sns.published) == 1
    msg = sns.published[0]
    assert msg["TopicArn"] == "arn:aws:sns:ap-southeast-2:1:alerts"
    assert msg["Subject"].startswith("[FieldSight test] Template upload problems: Site inspection")
    assert len(msg["Subject"]) <= 100
    body = msg["Message"]
    assert "Site inspection" in body and "problems" in body and "Fence intact" in body
    assert COMPANY in body and AUDIT_KEY in body
    assert "Roof flashing" not in body, "a claim the code dropped never reaches the email"


def test_alert_subject_is_ascii_and_at_most_100_chars(s3, sns, audit_on, monkeypatch):
    _docx_source(s3, _rule_read_docx())
    _request(s3, [f"template_sources/{COMPANY}/{'d' * 32}.docx"],
             name="Inspección — 検査 " * 20 + "\nsecond line")
    _http(monkeypatch, [_audit_answer("problems", 2, missing=[{"section": "", "text": "Fence intact"}])])
    worker.lambda_handler(_event(), None)
    subject = sns.published[0]["Subject"]
    assert len(subject) <= 100 and subject.isascii() and "\n" not in subject
    assert subject.startswith("[FieldSight test] Template upload problems: Inspeccion")



def _stats(s3):
    keys = [k for k in s3.puts if k.startswith("template_digitise_audit_stats/")]
    assert len(keys) == 1, keys
    return keys[0], json.loads(s3.puts[keys[0]])


def test_a_problems_audit_keeps_a_stats_line_with_no_customer_text(s3, sns, audit_on, monkeypatch):
    _docx_source(s3, _rule_read_docx())
    _http(monkeypatch, [_audit_answer("problems", 2, missing=[
        {"section": "", "text": "Fence intact"}, {"section": "", "text": "Roof flashing"}])])
    worker.lambda_handler(_event(), None)
    rec = _audit(s3)
    key, line = _stats(s3)
    assert key == f"template_digitise_audit_stats/{rec['auditedAt'][:7]}/{COMPANY}/{RID}.json"
    assert line == {
        "v": 1, "id": RID, "companyId": COMPANY, "auditedAt": rec["auditedAt"],
        "verdict": "problems", "reason": None, "status": "done", "route": "docx",
        "readModel": rec["readModel"], "auditModel": "google/gemini-3.8-flash",
        "fileTypes": ["docx"], "files": 1, "sections": 1, "items": 1, "sourceItems": 2,
        "missing": 1, "extra": 0, "reworded": 0, "splitOrMerged": 0, "droppedClaims": 1,
        "elapsedS": rec["elapsed_s"]}
    text = s3.puts[key]
    for customer in ("Fence intact", "Roof flashing", "Gate locked", "Site inspection", "u-1"):
        assert customer not in text
    lines = [json.loads(v) for k, v in s3.puts.items() if k.startswith("lake_manifest/")]
    assert any(l["key"] == key and l["hint"].get("company_id") == COMPANY for l in lines)


def test_a_skipped_audit_keeps_a_stats_line_too(s3, sns, audit_on, monkeypatch):
    import template_types
    key = f"template_sources/{COMPANY}/{'e' * 32}.pdf"
    s3.store[key] = b"x"
    s3.sizes[key] = template_types.MAX_BYTES + 1
    _request(s3, [key])
    _http(monkeypatch, [])
    worker.lambda_handler(_event(), None)
    _, line = _stats(s3)
    assert (line["verdict"], line["reason"], line["status"]) == ("skipped", "too_large", "error")
    assert (line["fileTypes"], line["files"], line["auditModel"]) == (["pdf"], 1, None)
    assert line["sourceItems"] is None and line["missing"] == 0


def test_a_failed_stats_write_still_stores_and_publishes(s3, sns, audit_on, monkeypatch, caplog):
    _docx_source(s3, _rule_read_docx())
    _http(monkeypatch, [_audit_answer("problems", 2, missing=[
        {"section": "", "text": "Fence intact"}])])
    put = s3.put_object

    def no_stats(Bucket, Key, Body, **kw):
        if Key.startswith("template_digitise_audit_stats/"):
            raise RuntimeError("denied")
        return put(Bucket=Bucket, Key=Key, Body=Body, **kw)
    monkeypatch.setattr(s3, "put_object", no_stats)
    assert worker.lambda_handler(_event(), None) == {"ok": True}
    assert _audit(s3)["verdict"] == "problems" and len(sns.published) == 1
    assert any(r.getMessage() == f"template audit {RID}: could not store the stats line"
               for r in caplog.records)


def test_audit_problems_without_topic_logs_and_stores(s3, audit_on, monkeypatch, caplog):
    import logging
    monkeypatch.delenv("ALERT_TOPIC_ARN", raising=False)
    monkeypatch.setattr(worker, "sns", lambda: _SNS(fail=True))
    _docx_source(s3, _rule_read_docx())
    _http(monkeypatch, [_audit_answer("problems", 2, missing=[{"section": "", "text": "Fence intact"}])])
    with caplog.at_level(logging.INFO):
        assert worker.lambda_handler(_event(), None) == {"ok": True}
    assert _audit(s3)["verdict"] == "problems"
    assert any("no topic" in r.getMessage() for r in caplog.records)
    assert not any(r.levelno >= logging.ERROR for r in caplog.records)


def test_a_publish_failure_is_logged_and_the_record_kept(s3, audit_on, monkeypatch, caplog):
    class _Down:
        def publish(self, **kw):
            raise RuntimeError("sns down")
    monkeypatch.setattr(worker, "sns", lambda: _Down())
    _docx_source(s3, _rule_read_docx())
    _http(monkeypatch, [_audit_answer("problems", 2, missing=[{"section": "", "text": "Fence intact"}])])
    assert worker.lambda_handler(_event(), None) == {"ok": True}
    assert _audit(s3)["verdict"] == "problems"
    assert any("could not publish" in r.getMessage() for r in caplog.records)


def test_audit_exception_leaves_result_and_returns_ok(s3, sns, audit_on, monkeypatch, caplog):
    from datetime import datetime as real_datetime, timezone

    class _Now:
        @staticmethod
        def now(tz=None):
            return real_datetime(2026, 11, 6, tzinfo=timezone.utc)
    monkeypatch.setattr(worker, "datetime", _Now)
    _docx_source(s3, _rule_read_docx())
    monkeypatch.delenv("TEMPLATE_DIGITISE_AUDIT")
    worker.lambda_handler(_event(), None)
    baseline = s3.puts[RES_KEY]

    monkeypatch.setenv("TEMPLATE_DIGITISE_AUDIT", "true")
    s3.puts.clear()

    def boom(source, result, deadline=None):
        result["sections"].clear()      # even a run that mangles what it was handed
        raise RuntimeError("audit blew up")
    monkeypatch.setattr(worker.template_audit, "run", boom)
    assert worker.lambda_handler(_event(), None) == {"ok": True}
    assert s3.puts[RES_KEY] == baseline
    assert AUDIT_KEY not in s3.puts and sns.published == []
    assert any(r.getMessage() == f"template audit {RID} failed" for r in caplog.records)


def test_unread_source_writes_skipped(s3, sns, audit_on, monkeypatch, caplog):
    import logging
    import template_types
    key = f"template_sources/{COMPANY}/{'e' * 32}.pdf"
    s3.store[key] = b"x"
    s3.sizes[key] = template_types.MAX_BYTES + 1
    _request(s3, [key])
    sent = _http(monkeypatch, [])
    with caplog.at_level(logging.INFO):
        worker.lambda_handler(_event(), None)
    rec = _audit(s3)
    assert rec["verdict"] == "skipped" and rec["reason"] == "too_large"
    assert rec["status"] == "error" and rec["sourceKeys"] == [key]
    assert sent == [] and sns.published == []
    assert any(r.getMessage() == f"template audit {RID}: skipped (too_large)" for r in caplog.records)


def test_a_word_text_over_the_cap_is_skipped_not_sent(s3, sns, audit_on, monkeypatch):
    _docx_source(s3, _docx_no_answer_column())
    monkeypatch.setattr(template_docx, "render_text",
                        lambda source: "x" * (worker.template_vision.MAX_TEXT_CHARS + 1))
    sent = _http(monkeypatch, [])
    worker.lambda_handler(_event(), None)
    assert _audit(s3)["verdict"] == "skipped" and _audit(s3)["reason"] == "too_long"
    assert sent == []


def test_audit_skipped_when_under_60s_left(s3, sns, audit_on, monkeypatch, caplog):
    import logging
    _docx_source(s3, _rule_read_docx())
    sent = _http(monkeypatch, [])
    with caplog.at_level(logging.INFO):
        assert worker.lambda_handler(_event(), _Context(70_000)) == {"ok": True}
    assert _result(s3)["status"] == "done"
    assert sent == []
    assert any("audit skipped: no time" in r.getMessage() for r in caplog.records)
    assert _audit(s3)["verdict"] == "skipped" and _audit(s3)["reason"] == "no_time"


def test_the_audit_gets_what_is_left_minus_the_reserve(s3, sns, audit_on, monkeypatch):
    _docx_source(s3, _rule_read_docx())
    seen = []

    def spy(source, result, deadline=None):
        seen.append(deadline)
        return dict(worker.template_audit._unsure("x"), model="m", elapsed_s=0.0)
    monkeypatch.setattr(worker.template_audit, "run", spy)
    worker.lambda_handler(_event(), _Context(900_000))
    assert 900 - worker.WRITE_RESERVE_SECONDS - 5 < seen[0] <= 900 - worker.WRITE_RESERVE_SECONDS
    assert _audit(s3)["verdict"] == "unsure" and sns.published == []


def test_audit_log_line_shape(s3, sns, audit_on, monkeypatch, caplog):
    import logging
    _docx_source(s3, _rule_read_docx())
    _http(monkeypatch, [_audit_answer("ok", 1)])
    with caplog.at_level(logging.INFO):
        worker.lambda_handler(_event(), None)
    assert (f"template audit {RID}: ok route=docx items=1 source_items=1 "
            "missing=0 extra=0 reworded=0") in [r.getMessage() for r in caplog.records]


def test_a_photo_read_is_audited_by_the_other_model(s3, sns, audit_on, monkeypatch):
    keys = [f"template_sources/{COMPANY}/{'b' * 32}.jpg"]
    s3.store[keys[0]] = b"\xff\xd8 not really a jpeg"
    _request(s3, keys)
    sent = _http(monkeypatch, [
        _ok(json.dumps({"sections": [{"title": "General", "items": ["Gate locked"]}]})),
        _audit_answer("ok", 1)])
    worker.lambda_handler(_event(), None)
    assert _result(s3)["model"] == "google/gemini-3.8-flash"
    # gemini read it, so gemini may not audit it: the ALT model, reasoning low
    assert sent[1]["model"] == "meta/muse-spark-1.3-contributor"
    assert sent[1]["reasoning"] == {"effort": "low"}
    content = sent[1]["messages"][0]["content"]
    assert [p["type"] for p in content] == ["text", "image_url", "text"]
    rec = _audit(s3)
    assert rec["verdict"] == "ok" and rec["model"] == "meta/muse-spark-1.3-contributor"
    assert rec["route"] == "vision"


def test_a_refused_docx_with_a_checklist_is_missed(s3, sns, audit_on, monkeypatch):
    # The reader model also found nothing; the audit names a row that is in the file.
    monkeypatch.setenv("TEMPLATE_DIGITISE_MODEL", "meta/muse-spark-1.3-contributor")
    _docx_source(s3, _docx_no_answer_column())
    sent = _http(monkeypatch, [_ok('{"sections": []}'), _audit_answer("missed", 2, missing=[
        {"section": "Wall Framing", "text": "Studs at 600 centres"}])])
    worker.lambda_handler(_event(), None)
    assert _result(s3)["status"] == "unreadable"
    assert sent[1]["model"] == "google/gemini-3.8-flash"
    rec = _audit(s3)
    assert rec["verdict"] == "missed" and rec["route"] == "docx_model"
    assert len(sns.published) == 1
    assert "Template upload missed" in sns.published[0]["Subject"]
