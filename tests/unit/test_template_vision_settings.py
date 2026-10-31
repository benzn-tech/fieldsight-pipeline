"""Per-model settings for the vision read: reasoning, provider pin, a separate PDF model.

Spec 2026-10-31 Part B2. Kept apart from test_template_vision.py because that file needs
Pillow for its image fixtures and none of these do (a 1-byte "image" is passed through
unshrunk), so these run wherever llm_utils imports. HTTP is stubbed at urllib3, as there.
"""
import json
import logging

import pytest

lu = pytest.importorskip("llm_utils", reason="requires urllib3 (installed in CI)")
import template_vision  # noqa: E402


class _R:
    def __init__(self, status, payload):
        self.status = status
        self.data = json.dumps(payload).encode("utf-8")


def _ok(text):
    return _R(200, {"model": "x", "choices": [{"message": {"content": text},
                                               "finish_reason": "stop"}],
                    "usage": {"completion_tokens": 5}})


@pytest.fixture(autouse=True)
def openrouter(monkeypatch):
    monkeypatch.setattr(lu, "LLM_PROVIDER", "qwen")
    monkeypatch.setattr(lu, "QWEN_API_KEY", "k")
    monkeypatch.setattr(lu, "QWEN_BASE_URL", "https://openrouter.ai/api/v1")
    monkeypatch.setattr(lu.time, "sleep", lambda s: None)
    for name in ("TEMPLATE_DIGITISE_MODEL", "TEMPLATE_DIGITISE_REASONING",
                 "TEMPLATE_DIGITISE_PROVIDER", "TEMPLATE_DIGITISE_PDF_MODEL",
                 "TEMPLATE_DIGITISE_FALLBACK_MODELS"):
        monkeypatch.delenv(name, raising=False)


def _stub(monkeypatch, responses):
    sent = []
    seq = list(responses)

    def fake(self, method, url, body=None, headers=None, timeout=None):
        sent.append(json.loads(body))
        return seq.pop(0)

    monkeypatch.setattr(lu.urllib3.PoolManager, "request", fake)
    return sent


# Unset, the request is the one test_read_returns_the_models_sections pins: no
# reasoning, no provider, the image model for every page.

PNG = {"body": b"x", "content_type": "image/png", "name": "p"}
PDF = {"body": b"%PDF <</Type/Page>>", "content_type": "application/pdf", "name": "f.pdf"}
MUSE = "meta/muse-spark-1.3-contributor"
DEEPSEEK = "deepseek/deepseek-v4.1-flash"


def test_unset_settings_send_no_reasoning_and_no_provider(monkeypatch):
    sent = _stub(monkeypatch, [_ok('{"sections": []}')])
    template_vision.read([PNG])
    assert "reasoning" not in sent[0] and "provider" not in sent[0]
    assert sent[0]["max_tokens"] == template_vision.answer_tokens()


def test_reasoning_and_provider_come_from_env(monkeypatch):
    monkeypatch.setenv("TEMPLATE_DIGITISE_MODEL", DEEPSEEK)
    monkeypatch.setenv("TEMPLATE_DIGITISE_REASONING", "off")
    monkeypatch.setenv("TEMPLATE_DIGITISE_PROVIDER", "coreweave/fp8")
    sent = _stub(monkeypatch, [_ok('{"sections": []}')])
    template_vision.read([PNG])
    assert sent[0]["model"] == DEEPSEEK
    assert sent[0]["reasoning"] == {"enabled": False}
    assert sent[0]["provider"] == {"only": ["coreweave/fp8"], "allow_fallbacks": False}
    # off spends no reasoning tokens: no headroom, the budget is the answer's.
    assert sent[0]["max_tokens"] == template_vision.answer_tokens()


def test_an_effort_buys_the_reasoning_headroom(monkeypatch):
    monkeypatch.setenv("TEMPLATE_DIGITISE_MODEL", DEEPSEEK)
    monkeypatch.setenv("TEMPLATE_DIGITISE_REASONING", "low")
    sent = _stub(monkeypatch, [_ok('{"sections": []}')])
    template_vision.read([PNG])
    assert sent[0]["reasoning"] == {"effort": "low"}
    assert sent[0]["max_tokens"] == template_vision.answer_tokens() + lu.REASONING_HEADROOM_TOKENS


def test_muse_with_reasoning_off_omits_the_field_rather_than_400ing(monkeypatch, caplog):
    monkeypatch.setenv("TEMPLATE_DIGITISE_MODEL", MUSE)
    monkeypatch.setenv("TEMPLATE_DIGITISE_REASONING", "off")
    sent = _stub(monkeypatch, [_ok('{"sections": []}')])
    with caplog.at_level(logging.WARNING):
        raw, err = template_vision.read([PNG])
    assert err is None
    assert "reasoning" not in sent[0]
    assert any("TEMPLATE_DIGITISE_REASONING" in r.getMessage() for r in caplog.records)


def test_a_pdf_goes_to_the_pdf_model_when_one_is_named(monkeypatch):
    monkeypatch.setenv("TEMPLATE_DIGITISE_MODEL", DEEPSEEK)
    monkeypatch.setenv("TEMPLATE_DIGITISE_PDF_MODEL", MUSE)
    monkeypatch.setenv("TEMPLATE_DIGITISE_REASONING", "off")
    monkeypatch.setenv("TEMPLATE_DIGITISE_PROVIDER", "coreweave/fp8")
    sent = _stub(monkeypatch, [_ok('{"sections": []}')])
    template_vision.read([PDF])
    assert sent[0]["model"] == MUSE
    assert template_vision.model_for([PDF]) == MUSE
    # The pin names the IMAGE model's endpoint; muse is not served there.
    assert "provider" not in sent[0]
    # And `off` is caught for the model that actually gets it.
    assert "reasoning" not in sent[0]


def test_images_stay_on_the_image_model_when_a_pdf_model_is_named(monkeypatch):
    monkeypatch.setenv("TEMPLATE_DIGITISE_MODEL", DEEPSEEK)
    monkeypatch.setenv("TEMPLATE_DIGITISE_PDF_MODEL", MUSE)
    monkeypatch.setenv("TEMPLATE_DIGITISE_PROVIDER", "coreweave/fp8")
    sent = _stub(monkeypatch, [_ok('{"sections": []}')])
    template_vision.read([PNG])
    assert sent[0]["model"] == DEEPSEEK and template_vision.model_for([PNG]) == DEEPSEEK
    assert sent[0]["provider"]["only"] == ["coreweave/fp8"]


def test_no_pdf_model_means_a_pdf_goes_to_the_image_model(monkeypatch):
    sent = _stub(monkeypatch, [_ok('{"sections": []}')])
    template_vision.read([PDF])
    assert sent[0]["model"] == template_vision.DEFAULT_MODEL
    assert template_vision.model_for([PDF]) == template_vision.DEFAULT_MODEL
