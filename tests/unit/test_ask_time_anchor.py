"""Ask reads the time out of the question and says what it answered from.

TDD for the two layers:

  * layer 2 -- the caller's zone plus a rules-only slot read narrow the search
    to the days the question names. The payload rag-search receives gains
    date_from/date_to, which its SQL has always accepted and Ask has never sent.
  * layer 1 -- the response carries a structured `scope`. It is computed from
    what came back, NOT written by the model: the screen renders it as a pill
    and the voice path as a clause, and a model asked to phrase it would drift
    between the two and would invent the count.

The bar for "unchanged" here is strict on purpose. Ask with no zone, or with no
time word, must send rag-search a payload identical to today's -- the three keys
it has always sent and nothing else. A new key that is sometimes absent is a
contract; a new key that is always present with a null is a change to every
caller, including the voice one.
"""
import io
import json
import os

import pytest

os.environ.setdefault("RAG_SEARCH_FUNCTION", "fieldsight-test-rag-search")

import lambda_ask_agent as laa   # noqa: E402
import llm_utils                 # noqa: E402
import dashscope_utils           # noqa: E402


CHUNK = {
    "id": "c-1", "chunk_text": "Concrete pour moved to Thursday.", "chunk_type": "topic",
    "topic_id": "t-1", "source_s3_key": "reports/2026-08-27/Ben/daily_report.json",
    "metadata": {}, "topic_title": "Programme", "topic_summary": "",
    "report_date": "2026-08-27", "site_id": "s-1", "site_name": "Ellesmere",
    "site_slug": "ellesmere", "distance": 0.1,
}


class FakeLambdaClient:
    """Records every invoke and replays a queue of responses (the widening
    path invokes rag-search twice, so one canned response is not enough)."""

    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = []

    def invoke(self, FunctionName, InvocationType, Payload):
        self.calls.append(json.loads(Payload))
        body = self.responses.pop(0) if self.responses else {"chunks": []}
        return {"Payload": io.BytesIO(json.dumps(body).encode("utf-8"))}


def wire(monkeypatch, responses, answer="Grounded answer [1]."):
    monkeypatch.setattr(dashscope_utils, "embed", lambda texts, dim=None: [[0.1] * 1024])
    client = FakeLambdaClient(responses)
    monkeypatch.setattr(laa, "_get_lambda_client", lambda: client)
    seen = {}

    def fake_llm(prompt, max_tokens=4096, force_json=False):
        seen["prompt"] = prompt
        return (answer, None)

    monkeypatch.setattr(llm_utils, "call_llm", fake_llm)
    return client, seen


def ask(**body):
    body.setdefault("caller_sub", "sub-1")
    resp = laa.lambda_handler(body, None)
    return json.loads(resp["body"])


NOW = "2026-08-30T09:00:00+00:00"     # 21:00 NZST on the 30th -- still the 30th


# --------------------------------------------------------------------------
# layer 2 -- the search is narrowed
# --------------------------------------------------------------------------

def test_a_time_word_and_a_zone_narrow_the_search(monkeypatch):
    client, _ = wire(monkeypatch, [{"chunks": [CHUNK], "scope": {"from": "2026-08-29",
                                                                "to": "2026-08-29",
                                                                "widened": False}}])

    ask(question="昨天发生了什么", tz="Pacific/Auckland", now=NOW)

    payload = client.calls[0]
    assert payload["date_from"] == "2026-08-29"
    assert payload["date_to"] == "2026-08-29"


def test_the_zone_is_the_callers_not_the_servers(monkeypatch):
    """Same instant, two markets, two different 'yesterday'. This is the whole
    reason the client sends a zone id rather than a date."""
    at_1230z = "2026-08-29T12:30:00+00:00"

    client, _ = wire(monkeypatch, [{"chunks": [CHUNK]}])
    ask(question="yesterday", tz="Pacific/Auckland", now=at_1230z)
    assert client.calls[0]["date_from"] == "2026-08-29"      # NZ is on the 30th

    client, _ = wire(monkeypatch, [{"chunks": [CHUNK]}])
    ask(question="yesterday", tz="Australia/Sydney", now=at_1230z)
    assert client.calls[0]["date_from"] == "2026-08-28"      # AU is still on the 29th


@pytest.mark.parametrize("body", [
    {"question": "昨天发生了什么"},                                  # time word, no zone
    {"question": "混凝土的问题", "tz": "Pacific/Auckland"},           # zone, no time word
    {"question": "昨天发生了什么", "tz": "Mars/Olympus"},             # unusable zone
])
def test_without_a_resolvable_range_the_payload_is_byte_identical(monkeypatch, body):
    """No range means no keys -- not null keys. Every existing caller, the voice
    one included, must see the payload it has always seen."""
    client, _ = wire(monkeypatch, [{"chunks": [CHUNK]}])

    ask(now=NOW, **body)

    assert set(client.calls[0]) == {"sub", "query_embedding", "k"}


def test_a_narrowed_search_asks_rag_search_to_widen_when_it_finds_nothing(monkeypatch):
    client, _ = wire(monkeypatch, [{"chunks": [CHUNK]}])
    ask(question="昨天发生了什么", tz="Pacific/Auckland", now=NOW)
    assert client.calls[0]["widen_when_empty"] is True


# --------------------------------------------------------------------------
# layer 1 -- scope comes back, and the model did not write it
# --------------------------------------------------------------------------

def test_scope_reports_the_dates_actually_used(monkeypatch):
    wire(monkeypatch, [{"chunks": [CHUNK], "scope": {"from": "2026-08-27",
                                                     "to": "2026-08-27",
                                                     "widened": True}}])

    out = ask(question="昨天发生了什么", tz="Pacific/Auckland", now=NOW)

    assert out["scope"]["from"] == "2026-08-27"
    assert out["scope"]["widened"] is True
    assert out["scope"]["chunks"] == 1
    assert out["scope"]["dates"] == ["2026-08-27"]


def test_scope_is_computed_not_generated(monkeypatch):
    """The model is handed the excerpts and writes prose. If it claims three
    meetings when one chunk came back, nothing downstream can tell -- so the
    count is never the model's to produce."""
    wire(monkeypatch,
         [{"chunks": [CHUNK], "scope": {"from": "2026-08-27", "to": "2026-08-27", "widened": True}}],
         answer="I looked at 3 meetings across all of August.")

    out = ask(question="昨天发生了什么", tz="Pacific/Auckland", now=NOW)

    assert out["scope"]["chunks"] == 1
    assert out["scope"]["dates"] == ["2026-08-27"]


def test_scope_is_present_even_when_nothing_matched(monkeypatch):
    """`.get("scope")` returning None must never be how a caller learns the
    search was empty -- that reads as 'this build has no scope' instead."""
    wire(monkeypatch, [{"chunks": [], "scope": {"from": "2026-08-29",
                                                "to": "2026-08-29", "widened": False}}])

    out = ask(question="昨天发生了什么", tz="Pacific/Auckland", now=NOW)

    assert out["scope"]["chunks"] == 0
    assert out["scope"]["dates"] == []


def test_scope_is_present_on_an_unfiltered_ask(monkeypatch):
    wire(monkeypatch, [{"chunks": [CHUNK]}])
    out = ask(question="混凝土的问题", tz="Pacific/Auckland", now=NOW)
    assert out["scope"]["from"] is None and out["scope"]["to"] is None
    assert out["scope"]["widened"] is False
    assert out["scope"]["dates"] == ["2026-08-27"]


# --------------------------------------------------------------------------
# the prompt anchor -- the other half of the same bug
# --------------------------------------------------------------------------

def test_the_prompt_tells_the_model_what_day_it_is(monkeypatch):
    """Narrowing the search fixes retrieval. It does not tell the model that
    'yesterday' is the 29th, and the excerpt headers carry dates it cannot
    place without an anchor."""
    _, seen = wire(monkeypatch, [{"chunks": [CHUNK]}])

    ask(question="昨天发生了什么", tz="Pacific/Auckland", now=NOW)

    assert "2026-08-30" in seen["prompt"]


def test_no_anchor_is_claimed_when_the_zone_is_unusable(monkeypatch):
    """Better no date than a server-side one: a date the caller did not mean is
    worse than none, because the model will use it confidently."""
    _, seen = wire(monkeypatch, [{"chunks": [CHUNK]}])

    ask(question="昨天发生了什么", now=NOW)

    assert "Today" not in seen["prompt"]
