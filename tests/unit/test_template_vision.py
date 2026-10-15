"""The vision reader's request: the spike's prompt, pages in order, photos upright and small.

Spec 2026-10-16 section 3. HTTP is stubbed at urllib3, never `call_llm_parts` itself.
"""
import base64
import json
from io import BytesIO

import pytest

lu = pytest.importorskip("llm_utils", reason="requires urllib3 (installed in CI)")
import template_types  # noqa: E402
import template_vision  # noqa: E402

PIL = pytest.importorskip("PIL")
from PIL import Image  # noqa: E402


class _R:
    def __init__(self, status, payload):
        self.status = status
        self.data = json.dumps(payload).encode("utf-8")


def _ok(text):
    return _R(200, {"model": "google/gemini-3.8-flash",
                    "choices": [{"message": {"content": text}, "finish_reason": "stop"}],
                    "usage": {"completion_tokens": 5}})


@pytest.fixture(autouse=True)
def openrouter(monkeypatch):
    monkeypatch.setattr(lu, "LLM_PROVIDER", "qwen")
    monkeypatch.setattr(lu, "QWEN_API_KEY", "k")
    monkeypatch.setattr(lu, "QWEN_BASE_URL", "https://openrouter.ai/api/v1")
    monkeypatch.setattr(lu.time, "sleep", lambda s: None)
    monkeypatch.delenv("TEMPLATE_DIGITISE_MODEL", raising=False)


def _stub(monkeypatch, responses):
    sent = []
    seq = list(responses)

    def fake(self, method, url, body=None, headers=None, timeout=None):
        sent.append(json.loads(body))
        return seq.pop(0)

    monkeypatch.setattr(lu.urllib3.PoolManager, "request", fake)
    return sent


def _jpeg(w, h, orientation=None):
    img = Image.new("RGB", (w, h), (200, 200, 200))
    buf = BytesIO()
    if orientation:
        exif = Image.Exif()
        exif[0x0112] = orientation
        img.save(buf, "JPEG", quality=95, exif=exif.tobytes())
    else:
        img.save(buf, "JPEG", quality=95)
    return buf.getvalue()


def _decode(part):
    url = part["image_url"]["url"]
    head, data = url.split(",", 1)
    return head, Image.open(BytesIO(base64.b64decode(data)))


def test_the_prompt_is_the_spikes_verbatim():
    import pathlib
    spike = (pathlib.Path(__file__).resolve().parents[2] / "docs" / "superpowers" / "specs"
             / "2026-10-16-template-digitise-spike" / "run_llm.py").read_text(encoding="utf-8")
    assert template_vision.PROMPT in spike


def test_pages_go_after_the_prompt_in_order():
    pages = [{"body": b"\x89PNG small-1", "content_type": "image/png", "name": "p1.png"},
             {"body": b"\x89PNG small-2", "content_type": "image/png", "name": "p2.png"}]
    parts = template_vision.build_parts(pages)
    assert parts[0] == {"type": "text", "text": template_vision.PROMPT}
    assert [p["type"] for p in parts[1:]] == ["image_url", "image_url"]
    # not an image Pillow can read -> sent as given, with its own type
    assert parts[1]["image_url"]["url"] == (
        "data:image/png;base64," + base64.b64encode(b"\x89PNG small-1").decode())


def test_a_rotated_phone_photo_is_sent_upright_and_small():
    # 3000 x 1000 pixels stored sideways (EXIF 6 = rotate 90 CW): upright it is portrait.
    body = _jpeg(3000, 1000, orientation=6)
    parts = template_vision.build_parts(
        [{"body": body, "content_type": "image/jpeg", "name": "IMG_1.jpg"}])
    head, img = _decode(parts[1])
    assert head == "data:image/jpeg;base64"
    assert max(img.size) == template_vision.IMAGE_EDGE
    assert img.size[1] > img.size[0], "still sideways"


def test_a_pdf_goes_as_a_file_part():
    body = b"%PDF-1.7 fake"
    parts = template_vision.build_parts(
        [{"body": body, "content_type": "application/pdf", "name": "form.pdf"}])
    assert parts[1] == {"type": "file", "file": {
        "filename": "form.pdf",
        "file_data": "data:application/pdf;base64," + base64.b64encode(body).decode()}}


def test_the_pdf_page_count_reads_page_objects_not_the_page_tree():
    body = b"%PDF " + b"<< /Type /Pages /Count 2 >> " + b"<< /Type /Page >> " * 2
    assert template_vision.pdf_page_count(body) == 2
    assert template_vision.pdf_page_count(b"%PDF compressed") == 0


def test_more_than_fifteen_pages_is_refused_before_any_call():
    pdf = b"%PDF " + b"<</Type/Page>>" * (template_types.MAX_PAGES + 1)
    why = template_vision.check([{"body": pdf, "content_type": "application/pdf",
                                  "name": "big.pdf"}])
    assert why and "15 pages" in why
    images = [{"body": b"x", "content_type": "image/png", "name": "p.png"}] * 16
    assert "15 pages" in template_vision.check(images)


def test_a_refused_pdf_route_says_so(monkeypatch):
    monkeypatch.setattr(template_types, "PDF_ACCEPTED", False)
    why = template_vision.check([{"body": b"%PDF", "content_type": "application/pdf",
                                  "name": "f.pdf"}])
    assert why and "export the pages as images" in why


def test_read_returns_the_models_sections(monkeypatch):
    sent = _stub(monkeypatch, [_ok('{"sections": [{"title": "General", "items": ["A?"]}]}')])
    raw, err = template_vision.read([{"body": b"x", "content_type": "image/png", "name": "p"}])
    assert err is None
    assert raw == [{"title": "General", "items": ["A?"]}]
    assert sent[0]["model"] == template_vision.DEFAULT_MODEL
    assert sent[0]["temperature"] == 0


def test_the_model_can_be_changed_by_env(monkeypatch):
    monkeypatch.setenv("TEMPLATE_DIGITISE_MODEL", "vendor/other")
    sent = _stub(monkeypatch, [_ok('{"sections": []}')])
    template_vision.read([{"body": b"x", "content_type": "image/png", "name": "p"}])
    assert sent[0]["model"] == "vendor/other"


def test_an_answer_that_is_not_the_json_asked_for_is_retried_once(monkeypatch):
    sent = _stub(monkeypatch, [_ok("Sure! here is the form"),
                               _ok('{"sections": [{"title": "T", "items": ["i"]}]}')])
    raw, err = template_vision.read([{"body": b"x", "content_type": "image/png", "name": "p"}])
    assert err is None and raw[0]["title"] == "T"
    assert len(sent) == 2


def test_two_unusable_answers_are_an_error(monkeypatch):
    _stub(monkeypatch, [_ok("nope"), _ok('{"rows": []}')])
    raw, err = template_vision.read([{"body": b"x", "content_type": "image/png", "name": "p"}])
    assert raw is None and err == template_vision.MSG_BAD_ANSWER


def test_a_vendor_outage_is_reported_in_plain_words(monkeypatch):
    _stub(monkeypatch, [_R(503, {"error": {"message": "down"}})] * lu.MAX_ATTEMPTS)
    raw, err = template_vision.read([{"body": b"x", "content_type": "image/png", "name": "p"}])
    assert raw is None and err == template_vision.MSG_UNAVAILABLE


def test_the_source_shapes():
    assert template_types.source_shape_error(["docx"]) is None
    assert template_types.source_shape_error(["pdf"]) is None
    assert template_types.source_shape_error(["jpg", "png", "jpeg"]) is None
    assert template_types.source_shape_error(["docx", "jpg"])
    assert template_types.source_shape_error(["pdf", "pdf"])
    assert template_types.source_shape_error(["jpg"] * 11)
    assert template_types.source_shape_error([])
    assert template_types.source_shape_error(["md"])
    assert template_types.ext_of("Form.PDF") == "pdf"
    assert template_types.ext_of("noext") == ""
