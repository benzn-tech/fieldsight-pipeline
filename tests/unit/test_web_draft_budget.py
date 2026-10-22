"""The general flow's draft ceiling: wider than 8 s, never at the verify's expense.

Prod 2026-10-22: three of three web turns lost the draft to an 8 s read timeout;
gemini-3.8-flash draft latency measured 2.1-12.8 s (p50 4.0, p90 7.5, n=20 + 8).
The chain gets at most WEB_WAIT_S (25 s), so the draft may stretch to
DRAFT_BUDGET only while VERIFY_FLOOR seconds stay for the web check, and never
gets less than the old 8 s (or whatever is left).

The fake vendor honours `timeout` like the real client: a call slower than its
ceiling comes back timed out after the ceiling, so these tests fail on the old
flat 8 s rule.
"""
import pytest

import corroboration_client as client
import web_answer

QUESTION = "What is the GST rate in New Zealand?"


class Clock:
    def __init__(self):
        self.t = 100.0

    def __call__(self):
        return self.t


def _reply(text="", error=None, timed_out=False):
    return client.Reply(text=text, search_results=[], error=error,
                        timed_out=timed_out, searched=False)


def _run(monkeypatch, budget, draft_secs=1.0, verify_secs=1.0):
    clock = Clock()
    seen = {}

    def fake_call(prompt, *, timeout, caller=None, **kw):
        seen.setdefault(caller, timeout)
        secs = draft_secs if caller == "web_draft" else verify_secs
        clock.t += min(secs, timeout)
        if secs > timeout:
            return _reply(error="read timeout", timed_out=True)
        if caller == "web_draft":
            return _reply(text="draft text")
        return _reply(error="no sources")                 # verify finds nothing

    monkeypatch.setattr(client, "call", fake_call)
    block = web_answer.general_answer(QUESTION, budget, clock=clock)
    return seen, block


def test_full_budget_draft_gets_the_wider_ceiling(monkeypatch):
    seen, _ = _run(monkeypatch, 25.0)
    assert web_answer.DRAFT_BUDGET == 12.0
    assert seen["web_draft"] == pytest.approx(12.0)


def test_a_slow_draft_inside_the_ceiling_is_delivered(monkeypatch):
    # 10.7 s: the slowest of the n=20 prod-config batch. Lost under the old 8 s.
    _, block = _run(monkeypatch, 25.0, draft_secs=10.7)
    assert block["answer"] == "draft text"


def test_a_draft_past_the_ceiling_still_fails(monkeypatch):
    _, block = _run(monkeypatch, 25.0, draft_secs=13.0)
    assert block["answer"] is None


def test_at_the_pinch_the_draft_gets_its_ceiling_and_the_verify_its_floor(monkeypatch):
    seen, _ = _run(monkeypatch, 21.0, draft_secs=12.0)
    assert seen["web_draft"] == pytest.approx(12.0)
    assert seen["web_verify"] == pytest.approx(web_answer.VERIFY_FLOOR)


def test_draft_stops_short_of_eating_the_verify_floor(monkeypatch):
    seen, _ = _run(monkeypatch, 19.0)
    assert seen["web_draft"] == pytest.approx(19.0 - web_answer.VERIFY_FLOOR)


def test_draft_never_gets_less_than_the_old_eight_seconds(monkeypatch):
    seen, _ = _run(monkeypatch, 15.0)
    assert seen["web_draft"] == pytest.approx(8.0)


def test_a_tiny_budget_still_caps_the_draft_at_what_is_left(monkeypatch):
    seen, _ = _run(monkeypatch, 5.0)
    assert seen["web_draft"] == pytest.approx(5.0)
