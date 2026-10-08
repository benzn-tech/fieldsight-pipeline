"""The shared client falls back to the next model when the deploy's model is down.

Spec: docs/superpowers/specs/2026-10-07-model-fallback-and-recovery-design.md
(D1-D3). The HTTP layer is stubbed at urllib3.PoolManager.request, as in
test_llm_utils.py; no real call is made.
"""
import json
import logging

import pytest

lu = pytest.importorskip("llm_utils", reason="requires urllib3 (installed in CI)")

MURE = "meta/muse-spark-1.3-contributor"
LUNA = "openai/gpt-6-luna-pro"
MIMO = "xiaomi/mimo-v2.6-pro"


class _R:
    def __init__(self, status, payload):
        self.status = status
        self.data = json.dumps(payload).encode("utf-8")


def _ok(text="{}", model="x"):
    return _R(200, {"model": model, "choices": [{"message": {"content": text},
                                                  "finish_reason": "stop"}],
                    "usage": {"completion_tokens": 5}})


def _err(status, msg="boom"):
    return _R(status, {"error": {"message": msg}})


@pytest.fixture(autouse=True)
def chain(monkeypatch):
    monkeypatch.setattr(lu, "LLM_PROVIDER", "qwen")
    monkeypatch.setattr(lu, "QWEN_API_KEY", "k")
    monkeypatch.setattr(lu, "QWEN_BASE_URL", "https://openrouter.ai/api/v1")
    monkeypatch.setattr(lu, "QWEN_MODEL", MURE)
    monkeypatch.setattr(lu, "QWEN_MODEL_NONTHINKING", "")
    monkeypatch.setattr(lu, "LLM_REASONING_EFFORT", "high")
    monkeypatch.setattr(lu, "REASONING_HEADROOM_TOKENS", 8000)
    monkeypatch.delenv("LLM_FALLBACK_MODELS", raising=False)
    # A bounded call (every 900 s function sets this): the primary gets 2 attempts, then
    # the chain moves on. Tests that need the unbounded shape delete it.
    monkeypatch.setenv("LLM_CHAIN_BUDGET_SECONDS", "840")
    monkeypatch.setattr(lu.time, "sleep", lambda s: None)


def _stub(monkeypatch, responses):
    sent = []
    seq = list(responses)

    def fake(self, method, url, body=None, headers=None, timeout=None):
        sent.append(json.loads(body))
        item = seq.pop(0)
        if isinstance(item, Exception):
            raise item
        return item

    monkeypatch.setattr(lu.urllib3.PoolManager, "request", fake)
    return sent


def test_503_on_primary_luna_answers(monkeypatch):
    sent = _stub(monkeypatch, [_err(503)] * 2 + [_ok("from-luna")])
    text, err = lu.call_llm("p", max_tokens=1000, force_json=True)
    assert (text, err) == ("from-luna", None)
    assert [b["model"] for b in sent] == [MURE, MURE, LUNA]
    # luna keeps today's payload exactly
    assert sent[2]["reasoning"] == {"effort": "high"}
    assert sent[2]["max_tokens"] == 9000
    assert sent[2]["response_format"] == {"type": "json_object"}


def test_luna_429_mimo_answers_with_reasoning_off_and_no_headroom(monkeypatch):
    sent = _stub(monkeypatch, [_err(502)] * 2 + [_err(429), _ok("from-mimo")])
    text, err = lu.call_llm("p", max_tokens=1000, force_json=True)
    assert (text, err) == ("from-mimo", None)
    assert [b["model"] for b in sent] == [MURE, MURE, LUNA, MIMO]
    assert sent[3]["reasoning"] == {"enabled": False}
    assert sent[3]["max_tokens"] == 1000
    assert sent[3]["response_format"] == {"type": "json_object"}


def test_mimo_without_force_json_sends_no_response_format(monkeypatch):
    sent = _stub(monkeypatch, [_err(503)] * 3 + [_ok("t")])
    lu.call_llm("p", max_tokens=500)
    assert "response_format" not in sent[3]


def test_all_fail_surfaces_the_original_error(monkeypatch):
    # the last model keeps its retry ladder (4 attempts); queue enough 503s
    sent = _stub(monkeypatch, [_err(503, "primary down")] * 2 + [_err(429, "luna busy")]
                 + [_err(500, "mimo down")] * 4)
    text, err = lu.call_llm("p", max_tokens=100)
    assert text is None
    # every model's failure is named, the primary's first
    assert err.startswith("all models failed: ")
    assert err.index(MURE) < err.index(LUNA) < err.index(MIMO)
    assert "primary down" in err and "luna busy" in err and "mimo down" in err
    assert [b["model"] for b in sent][:4] == [MURE, MURE, LUNA, MIMO]


def test_400_does_not_fall_back(monkeypatch):
    sent = _stub(monkeypatch, [_err(400, "bad request")])
    text, err = lu.call_llm("p", max_tokens=100)
    assert (text, err) == (None, "bad request")
    assert len(sent) == 1


def test_plain_404_does_not_fall_back(monkeypatch):
    sent = _stub(monkeypatch, [_err(404, "no such model")])
    assert lu.call_llm("p")[0] is None
    assert len(sent) == 1


def test_empty_content_falls_back(monkeypatch):
    sent = _stub(monkeypatch, [_ok(""), _ok("filled")])
    assert lu.call_llm("p") == ("filled", None)
    assert len(sent) == 2


def test_finish_length_empty_falls_back(monkeypatch):
    empty = _R(200, {"choices": [{"message": {"content": None},
                                  "finish_reason": "length"}], "usage": {}})
    sent = _stub(monkeypatch, [empty, _ok("filled")])
    assert lu.call_llm("p") == ("filled", None)
    assert len(sent) == 2


def test_timeout_falls_back(monkeypatch):
    sent = _stub(monkeypatch, [Exception("Read timed out"), _ok("ok")])
    assert lu.call_llm("p") == ("ok", None)
    assert len(sent) == 2


def test_provider_error_404_falls_back(monkeypatch):
    sent = _stub(monkeypatch, [_err(404, "Provider returned error"), _ok("ok")])
    assert lu.call_llm("p") == ("ok", None)
    assert len(sent) == 2


def test_deadline_exhausted_means_no_next_model(monkeypatch):
    sent = _stub(monkeypatch, [_err(503)])
    t = {"now": 100.0}
    monkeypatch.setattr(lu.time, "monotonic", lambda: t["now"])
    orig = lu.urllib3.PoolManager.request

    def slow(self, *a, **k):
        t["now"] += 9.5     # the primary burns the budget
        return orig(self, *a, **k)

    monkeypatch.setattr(lu.urllib3.PoolManager, "request", slow)
    text, err = lu.call_llm("p", deadline=10)
    assert text is None
    assert len(sent) == 1


def test_deadline_with_time_left_still_falls_back(monkeypatch):
    sent = _stub(monkeypatch, [_err(503), _ok("ok")])
    assert lu.call_llm("p", deadline=20) == ("ok", None)
    assert len(sent) == 2


def test_empty_env_is_the_old_behaviour(monkeypatch):
    monkeypatch.setenv("LLM_FALLBACK_MODELS", "")
    sent = _stub(monkeypatch, [_err(503)] * 4)
    text, err = lu.call_llm("p")
    assert text is None and err == "boom"
    assert len(sent) == 4 and {b["model"] for b in sent} == {MURE}


def test_primary_already_luna_is_not_called_twice(monkeypatch):
    monkeypatch.setattr(lu, "QWEN_MODEL", LUNA)
    sent = _stub(monkeypatch, [_err(503)] * 2 + [_ok("from-mimo")])
    assert lu.call_llm("p") == ("from-mimo", None)
    assert [b["model"] for b in sent] == [LUNA, LUNA, MIMO]


def test_per_call_model_override_is_the_primary(monkeypatch):
    sent = _stub(monkeypatch, [_err(503)] * 2 + [_ok("ok")])
    lu.call_llm("p", model="google/gemini-3.8-flash")
    assert [b["model"] for b in sent][-1] == LUNA


def test_one_log_line_per_switch_and_no_prompt(monkeypatch, caplog):
    _stub(monkeypatch, [_err(503, "down")] * 2 + [_err(429, "busy"), _ok("ok")])
    with caplog.at_level(logging.INFO):
        lu.call_llm("SECRET PROMPT TEXT", caller="extraction")
    lines = [r.getMessage() for r in caplog.records
             if r.getMessage().startswith("LLM_FALLBACK ")]
    assert len(lines) == 2
    first = json.loads(lines[0].split(" ", 1)[1])
    assert first == {"caller": "extraction", "from": MURE, "to": LUNA,
                     "reason": "down"}
    assert json.loads(lines[1].split(" ", 1)[1])["to"] == MIMO
    assert not any("SECRET PROMPT" in r.getMessage() for r in caplog.records)


def test_dashscope_never_falls_back(monkeypatch):
    monkeypatch.setattr(lu, "QWEN_BASE_URL",
                        "https://dashscope-intl.aliyuncs.com/compatible-mode/v1")
    sent = _stub(monkeypatch, [_err(400, "x")])
    lu.call_llm("p")
    assert len(sent) == 1


def test_provider_returned_error_on_any_status_falls_back(monkeypatch):
    sent = _stub(monkeypatch, [_err(400, "Provider returned error"), _ok("ok")])
    assert lu.call_llm("p") == ("ok", None)
    assert len(sent) == 2


# ---- T1 fix round: findings 1, 2, 4, 5, 9 --------------------------------------


class _Html:
    """A gateway's error page: not JSON."""
    def __init__(self, status, body=b"<html><body>Bad Gateway</body></html>"):
        self.status = status
        self.data = body


@pytest.mark.parametrize("status", [502, 503, 504])
def test_html_5xx_body_moves_on_instead_of_raising(monkeypatch, status):
    sent = _stub(monkeypatch, [_Html(status), _ok("from-luna")])
    assert lu.call_llm("p") == ("from-luna", None)
    assert len(sent) == 2


def test_html_5xx_everywhere_returns_an_error_never_raises(monkeypatch):
    _stub(monkeypatch, [_Html(502)] * 2 + [_Html(502), _Html(502)] + [_Html(502)] * 4)
    text, err = lu.call_llm("p")
    assert text is None and err.startswith("all models failed: ")
    assert f"{MIMO} HTTP 502" in err


def test_html_400_surfaces_and_does_not_move(monkeypatch):
    sent = _stub(monkeypatch, [_Html(400), _ok("never")])
    text, err = lu.call_llm("p")
    assert text is None and err == "HTTP 400" and len(sent) == 1


def test_unparseable_200_is_a_vendor_failure(monkeypatch):
    sent = _stub(monkeypatch, [_Html(200, b"<html>oops"), _ok("from-luna")])
    assert lu.call_llm("p") == ("from-luna", None)
    assert len(sent) == 2


def test_200_with_error_and_no_choices_moves_on(monkeypatch):
    sent = _stub(monkeypatch, [_R(200, {"error": {"message": "upstream died"}}),
                               _ok("from-luna")])
    assert lu.call_llm("p") == ("from-luna", None)
    assert len(sent) == 2


def test_200_with_empty_choices_moves_on(monkeypatch):
    sent = _stub(monkeypatch, [_R(200, {"choices": []}), _ok("from-luna")])
    assert lu.call_llm("p") == ("from-luna", None)


def test_408_moves_on(monkeypatch):
    sent = _stub(monkeypatch, [_err(408, "upstream timeout"), _ok("from-luna")])
    assert lu.call_llm("p") == ("from-luna", None)


def test_all_models_empty_names_each(monkeypatch):
    empty = lambda: _R(200, {"choices": [{"message": {"content": ""},
                                          "finish_reason": "length"}]})
    _stub(monkeypatch, [empty(), empty()] + [empty()] * 4)
    text, err = lu.call_llm("p")
    assert text is None
    assert "empty answer" in err and "finish_reason=length" in err
    assert err.startswith("all models failed: ")


def test_primary_payload_is_byte_identical_with_the_chain_on_and_off(monkeypatch):
    monkeypatch.setenv("LLM_FALLBACK_MODELS", "")
    off = _stub(monkeypatch, [_ok("a")])
    lu.call_llm("p", max_tokens=1000, force_json=True)
    monkeypatch.delenv("LLM_FALLBACK_MODELS")
    on = _stub(monkeypatch, [_ok("a")])
    lu.call_llm("p", max_tokens=1000, force_json=True)
    assert json.dumps(on[0]) == json.dumps(off[0])


def test_llm_usage_names_the_model_that_served(monkeypatch, caplog):
    _stub(monkeypatch, [_err(503)] * 2 + [_err(503), _ok("from-mimo", model=MIMO)])
    with caplog.at_level(logging.INFO):
        assert lu.call_llm("p", caller="t") == ("from-mimo", None)
    usage = [r.getMessage() for r in caplog.records if "LLM_USAGE" in r.getMessage()]
    assert len(usage) == 1 and f"model={MIMO}" in usage[0]


def test_llm_usage_names_luna_when_luna_serves(monkeypatch, caplog):
    _stub(monkeypatch, [_err(503)] * 2 + [_ok("from-luna", model=LUNA)])
    with caplog.at_level(logging.INFO):
        lu.call_llm("p", caller="t")
    usage = [r.getMessage() for r in caplog.records if "LLM_USAGE" in r.getMessage()]
    assert len(usage) == 1 and f"model={LUNA}" in usage[0]


# ---- the chain-wide time budget (finding 2) -------------------------------------


class _Clock:
    def __init__(self):
        self.now = 1000.0

    def __call__(self):
        return self.now


def _hang_world(monkeypatch, behaviours, budget="840", http_timeout=540.0):
    """Fake clock + fake http. `behaviours` maps model -> "hang" (consume the
    request timeout then raise) or a response. Returns (calls, clock)."""
    clock = _Clock()
    monkeypatch.setattr(lu.time, "monotonic", clock)
    monkeypatch.setattr(lu.time, "sleep", lambda s: setattr(clock, "now", clock.now + s))
    monkeypatch.setattr(lu, "HTTP_TIMEOUT", http_timeout)
    if budget is None:
        monkeypatch.delenv("LLM_CHAIN_BUDGET_SECONDS", raising=False)
    else:
        monkeypatch.setenv("LLM_CHAIN_BUDGET_SECONDS", budget)
    calls = []

    def fake(self, method, url, body=None, headers=None, timeout=None):
        model = json.loads(body)["model"]
        calls.append((model, timeout, clock.now))
        b = behaviours[model]
        if b == "hang":
            clock.now += timeout
            raise TimeoutError("read timed out")
        return b

    monkeypatch.setattr(lu.urllib3.PoolManager, "request", fake)
    return calls, clock


def test_hung_primary_and_hung_luna_still_leave_mimo_time(monkeypatch):
    calls, clock = _hang_world(monkeypatch, {MURE: "hang", LUNA: "hang",
                                             MIMO: _ok("from-mimo")})
    t0 = clock.now
    assert lu.call_llm("p") == ("from-mimo", None)
    assert [c[0] for c in calls] == [MURE, LUNA, MIMO]
    # primary: min(540, 0.6 x 840) = 504 for its whole ladder (a hang is not retried into
    # luna's time); luna: 0.6 x what is left; mimo: what is left after that
    assert calls[0][1] == pytest.approx(504.0)
    assert calls[1][1] == pytest.approx(0.6 * (840.0 - 504.0))
    assert calls[2][1] == pytest.approx(840.0 - 504.0 - calls[1][1])
    assert clock.now - t0 <= 840.0


def test_total_never_exceeds_the_budget_when_everything_hangs(monkeypatch):
    calls, clock = _hang_world(monkeypatch, {MURE: "hang", LUNA: "hang", MIMO: "hang"})
    t0 = clock.now
    text, err = lu.call_llm("p")
    assert text is None and err.startswith("all models failed")
    assert clock.now - t0 <= 840.0
    # the last model's per-attempt timeout is what remains, never more
    assert all(c[1] <= 840.0 - (c[2] - t0) + 1e-6 for c in calls)
    assert calls[-1][0] == MIMO


def test_last_model_ladder_stops_when_the_budget_cannot_cover_another_attempt(monkeypatch):
    calls, clock = _hang_world(monkeypatch, {MURE: _err(503), LUNA: _err(503),
                                             MIMO: "hang"}, budget="100",
                               http_timeout=60.0)
    t0 = clock.now
    lu.call_llm("p")
    assert clock.now - t0 <= 100.0
    assert [c[0] for c in calls].count(MIMO) <= 2


def test_caller_deadline_still_wins_when_earlier(monkeypatch):
    calls, clock = _hang_world(monkeypatch, {MURE: "hang", LUNA: "hang", MIMO: "hang"})
    t0 = clock.now
    lu.call_llm("p", deadline=50.0)
    assert clock.now - t0 <= 50.0


def test_no_budget_env_keeps_todays_per_attempt_timeout(monkeypatch):
    calls, clock = _hang_world(monkeypatch, {MURE: "hang", LUNA: _ok("l")}, budget=None)
    assert lu.call_llm("p") == ("l", None)
    assert calls[0][1] == 540.0


# ---- final-review fix round, ruling 3: a healthy muse is not regressed -----------


def test_one_blip_on_the_primary_is_absorbed_and_luna_is_not_called(monkeypatch):
    sent = _stub(monkeypatch, [_err(503), _ok("from-muse")])
    assert lu.call_llm("p") == ("from-muse", None)
    assert [b["model"] for b in sent] == [MURE, MURE]


def test_a_long_healthy_primary_is_not_cut_at_a_third_of_the_budget(monkeypatch):
    """A 32k-token report taking 500 s on a healthy muse used to succeed; the flat
    budget/3 = 280 s cap redid it on luna. The primary's request now gets 0.6 x 840."""
    calls, clock = _hang_world(monkeypatch, {MURE: _ok("from-muse")})
    assert lu.call_llm("p") == ("from-muse", None)
    assert calls[0][1] == pytest.approx(504.0) and calls[0][1] > 500.0


def test_a_hung_primary_is_not_retried_into_luna_and_mimos_time(monkeypatch):
    calls, clock = _hang_world(monkeypatch, {MURE: "hang", LUNA: "hang",
                                             MIMO: _ok("from-mimo")})
    assert lu.call_llm("p") == ("from-mimo", None)
    assert [c[0] for c in calls] == [MURE, LUNA, MIMO]      # one muse attempt, not two


def test_the_per_attempt_timeout_never_exceeds_the_functions_own_http_timeout(monkeypatch):
    calls, clock = _hang_world(monkeypatch, {MURE: "hang", LUNA: _ok("l")},
                               http_timeout=150.0)
    lu.call_llm("p")
    assert calls[0][1] == pytest.approx(150.0)


def test_without_any_bound_the_primary_keeps_todays_full_ladder(monkeypatch):
    monkeypatch.delenv("LLM_CHAIN_BUDGET_SECONDS", raising=False)
    sent = _stub(monkeypatch, [_err(503)] * 3 + [_ok("from-muse")])
    assert lu.call_llm("p") == ("from-muse", None)
    assert [b["model"] for b in sent] == [MURE] * 4


def test_luna_gets_a_share_of_what_is_left_and_mimo_the_rest(monkeypatch):
    calls, clock = _hang_world(monkeypatch, {MURE: _err(503), LUNA: "hang",
                                             MIMO: _ok("from-mimo")})
    assert lu.call_llm("p")[0] == "from-mimo"
    luna = next(c for c in calls if c[0] == LUNA)
    mimo = next(c for c in calls if c[0] == MIMO)
    assert luna[1] == pytest.approx(0.6 * (840.0 - (luna[2] - 1000.0)), rel=0.01)
    assert mimo[1] == pytest.approx(840.0 - (mimo[2] - 1000.0), rel=0.01)
