# External Corroboration — Backend Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add `POST /api/ask/corroborate` — a second request that returns what the open web says about the named things in an Ask answer, in a shape the client can render apart from the grounded answer.

**Architecture:** A new route on `lambda_fieldsight_api` bridges to `lambda_ask_agent` by body mode, exactly as `/api/ask` already does. Inside the agent, four steps run against a **dedicated HTTP client** (not `llm_utils`): extract entities and the claim the answer makes about each, filter them through a deterministic privacy gate, run one Anthropic web-search call, then reconcile each entity into a four-state enum. The whole thing is behind `ENABLE_EXTERNAL_CORROBORATION`, default false.

**Tech Stack:** Python 3.12 Lambda, `urllib3`, Anthropic Messages API with the `web_search` server tool, pytest.

**Spec:** `docs/superpowers/specs/2026-08-31-ask-external-corroboration-design.md` — read §3 (the call graph), §4 (what may leave) and §5 before starting. This plan implements §12's B1–B10.

---

## Global Constraints

- **Nothing ships to a customer until §10 decision 1 in the spec has an answer.** The flag defaults `'false'` in both workflows.
- **Only entities leave the account.** Never conversation text, never a substring of a transcript. Queries are assembled from fields.
- **Do not use `llm_utils.call_llm` on this path.** `LLM_HTTP_TIMEOUT` on `AskAgentFunction` is `'45'` and `MAX_ATTEMPTS` is `4`, so one call may block 45 s and a retried one ~187 s, against an `ApiFunction` that dies at 30 s. `call_llm` also takes no timeout parameter and its anthropic branch sends no `tools` and parses only `text` blocks.
- **Total budget ≤ 25 s**, hard internal stop at 24 s. The real ceiling is `ApiFunction`'s `Timeout: 30`, not the agent's own 60.
- **Cap 3 entities**, for the reader.
- **Customer-facing copy is English.** `lambda_ask_agent.py:512` and `:527` already say so.
- **Never `except: return generic_error` without `logger.exception`** (BUG-40).
- **`MSYS_NO_PATHCONV=1`** before any `aws` CLI call carrying a `/`-prefixed argument (BUG-42).
- Existing `/ask` response shape must not change.

---

### Task 1: The dedicated client

**Files:**
- Create: `src/corroboration_client.py`
- Test: `tests/unit/test_corroboration_client.py`

**Interfaces:**
- Consumes: nothing.
- Produces:
  - `class Budget: __init__(self, total_seconds: float)`, `remaining() -> float`, `expired() -> bool`
  - `call_anthropic(prompt: str, *, max_tokens: int, timeout: float, tools: list | None = None) -> tuple[dict | None, str | None]` — returns `(parsed_response_json, None)` or `(None, error_string)`

- [ ] **Step 1: Write the failing tests**

```python
"""Unit: the client the corroborate path uses instead of llm_utils.

llm_utils cannot be used here (spec §3.4). LLM_HTTP_TIMEOUT is 45 on this
function and MAX_ATTEMPTS is 4, so a single call may outlive the 30-second
proxy waiting for it; call_llm takes no timeout parameter to shorten it; and
its anthropic branch sends no `tools` and joins only `text` blocks, discarding
the web_search_result blocks the sources have to come from.
"""
import time
import pytest

cc = pytest.importorskip("corroboration_client")


def test_budget_reports_what_is_left():
    b = cc.Budget(10.0)
    assert 9.0 < b.remaining() <= 10.0
    assert not b.expired()


def test_budget_expires_and_never_returns_negative():
    b = cc.Budget(0.0)
    time.sleep(0.01)
    assert b.expired()
    assert b.remaining() == 0.0


def test_budget_uses_a_monotonic_clock():
    """Wall-clock jumps (NTP, container resume) must not extend or collapse a
    deadline. A step that thinks it has 20 more seconds because the clock moved
    is a step that outlives the proxy."""
    import corroboration_client
    assert "monotonic" in corroboration_client.Budget.__init__.__doc__.lower()


def test_call_passes_the_timeout_through_to_the_request(monkeypatch):
    """The whole reason this module exists: a caller can bound one call."""
    seen = {}

    class FakeResp:
        status = 200
        data = b'{"content":[{"type":"text","text":"hi"}]}'

    class FakePool:
        def request(self, method, url, body=None, headers=None, timeout=None):
            seen["timeout"] = timeout
            seen["url"] = url
            seen["body"] = body
            return FakeResp()

    monkeypatch.setattr(cc.urllib3, "PoolManager", lambda **kw: FakePool())
    monkeypatch.setattr(cc, "ANTHROPIC_API_KEY", "k")
    data, err = cc.call_anthropic("q", max_tokens=100, timeout=7.5)
    assert err is None
    assert seen["timeout"] == 7.5
    assert seen["url"] == "https://api.anthropic.com/v1/messages"


def test_tools_reach_the_payload(monkeypatch):
    """call_llm's anthropic branch cannot send tools at all. This one must."""
    import json as _json
    seen = {}

    class FakeResp:
        status = 200
        data = b'{"content":[]}'

    class FakePool:
        def request(self, method, url, body=None, headers=None, timeout=None):
            seen["payload"] = _json.loads(body)
            return FakeResp()

    monkeypatch.setattr(cc.urllib3, "PoolManager", lambda **kw: FakePool())
    monkeypatch.setattr(cc, "ANTHROPIC_API_KEY", "k")
    cc.call_anthropic("q", max_tokens=100, timeout=5,
                      tools=[{"type": "web_search_20250305", "name": "web_search",
                              "max_uses": 3}])
    assert seen["payload"]["tools"][0]["name"] == "web_search"
    assert seen["payload"]["tools"][0]["max_uses"] == 3


def test_at_most_one_retry_and_only_inside_budget(monkeypatch):
    """llm_utils retries four times. Four 45s attempts is 180 seconds against a
    30-second caller. Here a retry happens once, and only if the budget can
    still pay for it."""
    calls = {"n": 0}

    class FakeResp:
        def __init__(self, status):
            self.status = status
            self.data = b'{"error":{"message":"busy"}}'

    class FakePool:
        def request(self, method, url, body=None, headers=None, timeout=None):
            calls["n"] += 1
            return FakeResp(429)

    monkeypatch.setattr(cc.urllib3, "PoolManager", lambda **kw: FakePool())
    monkeypatch.setattr(cc, "ANTHROPIC_API_KEY", "k")
    monkeypatch.setattr(cc, "RETRY_SLEEP_SECONDS", 0)
    data, err = cc.call_anthropic("q", max_tokens=10, timeout=1)
    assert calls["n"] == 2, "expected exactly one retry"
    assert err is not None


def test_a_missing_key_is_an_error_not_an_exception(monkeypatch):
    monkeypatch.setattr(cc, "ANTHROPIC_API_KEY", "")
    data, err = cc.call_anthropic("q", max_tokens=10, timeout=1)
    assert data is None
    assert "ANTHROPIC_API_KEY" in err
```

- [ ] **Step 2: Run to verify they fail**

Run: `python -m pytest tests/unit/test_corroboration_client.py -q`
Expected: collection skips with "could not import 'corroboration_client'", or FAIL once the file exists but is empty.

- [ ] **Step 3: Write the implementation**

```python
"""HTTP client for the corroborate path.

Deliberately NOT llm_utils. Three reasons, all measured (spec §3.4):

  * llm_utils.call_llm takes no timeout parameter. HTTP_TIMEOUT is module-level
    from env, and on AskAgentFunction it is 45 seconds -- longer than the
    ApiFunction that is waiting, which dies at 30. A caller cannot shorten it.
  * MAX_ATTEMPTS is 4, so a retried call is ~187 seconds worst case.
  * Its anthropic branch sends no `tools` and joins only `text` blocks,
    discarding the web_search_result and citation blocks this feature exists
    to read.

Anthropic on every stack, deliberately. TEST runs LlmProvider=qwen, and a
feature whose tests exercise a different model from the one prod runs is a
feature nobody has tested. ANTHROPIC_API_KEY is on this function regardless of
LlmProvider (template.yaml AskAgentFunction Environment).
"""
import json
import logging
import os
import time

import urllib3

logger = logging.getLogger()

ANTHROPIC_API_KEY = os.environ.get("ANTHROPIC_API_KEY", "")
CORROBORATE_MODEL = os.environ.get("CORROBORATE_MODEL", "claude-haiku-4-5-20251001")
RETRY_SLEEP_SECONDS = 1.0
RETRYABLE_STATUSES = (429, 500, 502, 503, 504)

_ANTHROPIC_URL = "https://api.anthropic.com/v1/messages"


class Budget:
    def __init__(self, total_seconds):
        """Time left in the whole corroborate request, on a monotonic clock.

        Monotonic and not wall-clock: an NTP correction or a container resume
        must not hand a step twenty more seconds than the proxy will wait.
        """
        self._deadline = time.monotonic() + float(total_seconds)

    def remaining(self):
        return max(0.0, self._deadline - time.monotonic())

    def expired(self):
        return self.remaining() <= 0.0


def call_anthropic(prompt, *, max_tokens, timeout, tools=None):
    """One POST, at most one retry, hard-bounded by `timeout` per attempt.

    Returns (parsed_json, None) or (None, error_string). Never raises for a
    network or API failure -- the caller degrades to a partial result, and an
    exception here would take the whole answer's enrichment with it.
    """
    if not ANTHROPIC_API_KEY:
        logger.error("corroborate: ANTHROPIC_API_KEY not set")
        return None, "ANTHROPIC_API_KEY not configured"

    payload = {
        "model": CORROBORATE_MODEL,
        "max_tokens": max_tokens,
        "messages": [{"role": "user", "content": prompt}],
    }
    if tools:
        payload["tools"] = tools
    body = json.dumps(payload)
    headers = {
        "Content-Type": "application/json",
        "x-api-key": ANTHROPIC_API_KEY,
        "anthropic-version": "2023-06-01",
    }

    http = urllib3.PoolManager()
    last_error = None
    for attempt in range(2):          # one try, one retry. Not four.
        try:
            resp = http.request("POST", _ANTHROPIC_URL, body=body,
                                headers=headers, timeout=timeout)
        except Exception as e:        # noqa: BLE001 - network errors are retryable
            last_error = str(e)
            logger.warning("corroborate: request failed (%s)", last_error)
            if attempt == 0:
                time.sleep(RETRY_SLEEP_SECONDS)
                continue
            return None, last_error

        if resp.status in RETRYABLE_STATUSES and attempt == 0:
            last_error = "HTTP %d" % resp.status
            time.sleep(RETRY_SLEEP_SECONDS)
            continue

        data = json.loads(resp.data.decode("utf-8"))
        if resp.status == 200:
            return data, None
        msg = (data.get("error") or {}).get("message") or ("HTTP %d" % resp.status)
        logger.error("corroborate: API error %s", msg)
        return None, msg

    return None, last_error
```

- [ ] **Step 4: Run to verify they pass**

Run: `python -m pytest tests/unit/test_corroboration_client.py -q`
Expected: 7 passed.

- [ ] **Step 5: Commit**

```bash
git add src/corroboration_client.py tests/unit/test_corroboration_client.py
git commit -m "A client a caller can actually bound"
```

---

### Task 2: ~~The privacy gate~~ — ALREADY SHIPPED, read it before Task 3

**Nothing to build.** A parallel session implemented this and merged it while
this plan was being written: PR #656, `src/corroboration_gate.py` +
`tests/unit/test_corroboration_gate.py`, on `develop` since 2026-08-31.

Read it before starting Task 3 — Task 3 consumes it, and its interface is not
the one an earlier draft of this plan described.

```python
ALLOWED_KINDS = frozenset({"company", "standard", "product", "material",
                           "public_role", "regulator", "authority"})
MAX_ENTITY_CHARS = 60
MAX_ENTITIES = 3

class Rejected:   # .entity  .kind  .reason        (reason is prose, not a code)
class GateResult: # .allowed .rejected .truncated

def screen_entity(entity, kind) -> str | None      # the reason, or None to allow
def screen(entities, max_entities=MAX_ENTITIES) -> GateResult
```

**One design difference from this plan's earlier draft, and the shipped one is
right.** `screen()` takes **only the entities** — it does not take the answer,
and it does not look at the sentence an entity sits in. The earlier draft
dropped `Naylor Love` when the surrounding sentence mentioned a dispute or a
price.

That was wrong, and wrong in the expensive direction: **the sentence is never
sent.** The query is assembled from fields and reaches the search provider as
`Naylor Love` alone, so the surrounding words cannot leak — while dropping the
entity costs a legitimate corroboration on exactly the meetings that matter
most. What does leak is interest in a company, and `corroboration_gate.py`'s
own docstring names that and accepts it rather than pretending otherwise.

Do not add an `answer` parameter back.

**Also already covered there, so do not duplicate it in Task 3:** commercial
terms in Chinese as well as English, NFKC normalisation, the digits-without-
`standard`-kind rule, clause-narrowed standards, person-shaped strings,
case-insensitive de-duplication before the cap, and a non-list input degrading
to "no entities" rather than raising.

### Task 3: Extraction and reconcile

**Files:**
- Create: `src/corroboration.py`
- Test: `tests/unit/test_corroboration.py`

**Interfaces:**
- Consumes: `corroboration_client.Budget`, `corroboration_client.call_anthropic`, `corroboration_gate.gate`
- Produces:
  - `STATES: frozenset` = `{"corroborated", "conflicts", "not_found", "no_checkable_claim"}`
  - `extract_entities(question: str, answer: str, budget) -> list[dict]`
  - `search(entities: list[dict], budget) -> tuple[str, list[dict]]` — returns `(web_text, sources)`
  - `reconcile(entities: list[dict], web_text: str, sources: list[dict], budget) -> list[dict]`
  - `run(question: str, answer: str, total_seconds: float = 24.0) -> dict` — the whole pipeline, returns the response body of spec §5.1

- [ ] **Step 1: Write the failing tests**

```python
"""Unit: the four states, and the two that are easy to lose.

`not_found` must be produced and rendered -- silence reads as "fine".
`no_checkable_claim` exists because most Ask answers assert nothing externally
checkable, and rendering "this company is real" as `corroborated` invites the
reader to hear "the answer is verified". That is the trust inflation the whole
feature was written to avoid.
"""
import json
import pytest

c = pytest.importorskip("corroborate")


def _fake_text(payload):
    return {"content": [{"type": "text", "text": json.dumps(payload)}]}


def test_the_four_states_are_the_only_ones():
    assert c.STATES == {"corroborated", "conflicts", "not_found", "no_checkable_claim"}


def test_extraction_asks_for_the_claim_not_just_the_entity(monkeypatch):
    """reconcile cannot assign `no_checkable_claim` without knowing what the
    answer actually asserted, and asking once is cheaper than inferring twice."""
    seen = {}

    def fake_call(prompt, **kw):
        seen["prompt"] = prompt
        return _fake_text({"entities": [
            {"entity": "Naylor Love", "kind": "company", "claim": "CEO is Rick Herd"}]}), None

    monkeypatch.setattr(c.client, "call_anthropic", fake_call)
    out = c.extract_entities("who is the CEO?", "Naylor Love's CEO is Rick Herd.",
                             c.client.Budget(10))
    assert out[0]["claim"] == "CEO is Rick Herd"
    assert "claim" in seen["prompt"]


def test_the_search_prompt_contains_the_entities_and_not_the_answer(monkeypatch):
    """Spec §4: only entities leave. This is the assertion that pins it."""
    seen = {}

    def fake_call(prompt, **kw):
        seen["prompt"] = prompt
        return {"content": [{"type": "text", "text": "…"}]}, None

    monkeypatch.setattr(c.client, "call_anthropic", fake_call)
    c.search([{"entity": "Naylor Love", "kind": "company", "claim": "CEO is X"}],
             c.client.Budget(10))
    assert "Naylor Love" in seen["prompt"]
    assert "CEO is X" not in seen["prompt"], "the answer's claim must not be sent to search"


def test_a_disagreement_is_conflicts_and_not_a_summary(monkeypatch):
    def fake_call(prompt, **kw):
        return _fake_text({"results": [
            {"entity": "NZS 3604", "state": "conflicts",
             "summary": "The standard states a 10m maximum."}]}), None

    monkeypatch.setattr(c.client, "call_anthropic", fake_call)
    out = c.reconcile([{"entity": "NZS 3604", "kind": "standard",
                        "claim": "covers buildings up to 12m"}],
                      "web text", [], c.client.Budget(10))
    assert out[0]["state"] == "conflicts"


def test_an_entity_with_no_checkable_claim_is_not_corroborated(monkeypatch):
    def fake_call(prompt, **kw):
        return _fake_text({"results": [
            {"entity": "WorkSafe", "state": "no_checkable_claim", "summary": None}]}), None

    monkeypatch.setattr(c.client, "call_anthropic", fake_call)
    out = c.reconcile([{"entity": "WorkSafe", "kind": "authority", "claim": None}],
                      "web text", [], c.client.Budget(10))
    assert out[0]["state"] == "no_checkable_claim"


def test_an_unknown_state_from_the_model_degrades_to_not_found(monkeypatch):
    """A model that invents a fifth state must not reach the client as one.
    Degrading to not_found is the honest direction: we did not establish it."""
    def fake_call(prompt, **kw):
        return _fake_text({"results": [
            {"entity": "A Ltd", "state": "probably_true", "summary": "x"}]}), None

    monkeypatch.setattr(c.client, "call_anthropic", fake_call)
    out = c.reconcile([{"entity": "A Ltd", "kind": "company", "claim": "c"}],
                      "w", [], c.client.Budget(10))
    assert out[0]["state"] == "not_found"


def test_an_entity_the_model_omitted_still_comes_back(monkeypatch):
    """Every entity that survived the gate gets a row. A silently missing one
    reads as "not checked" when the truth is "we lost it"."""
    def fake_call(prompt, **kw):
        return _fake_text({"results": []}), None

    monkeypatch.setattr(c.client, "call_anthropic", fake_call)
    out = c.reconcile([{"entity": "A Ltd", "kind": "company", "claim": "c"}],
                      "w", [], c.client.Budget(10))
    assert [r["entity"] for r in out] == ["A Ltd"]
    assert out[0]["state"] == "not_found"


def test_run_returns_the_response_shape(monkeypatch):
    def fake_extract(q, a, b):
        return [{"entity": "Naylor Love", "kind": "company", "claim": "CEO is Rick Herd"},
                {"entity": "the Downtown claim", "kind": "project", "claim": None}]

    monkeypatch.setattr(c, "extract_entities", fake_extract)
    monkeypatch.setattr(c, "search", lambda ents, b: ("web", [{"url": "https://x/", "title": "X"}]))
    monkeypatch.setattr(c, "reconcile", lambda ents, w, s, b: [
        {"entity": "Naylor Love", "kind": "company", "state": "corroborated",
         "claim": "CEO is Rick Herd", "summary": "…", "sources": [], "retrieved_at": "z"}])

    out = c.run("q", "a")
    assert [r["state"] for r in out["corroborations"]] == ["corroborated"]
    # The real gate runs here -- kind "project" is not in ALLOWED_KINDS, and the
    # reason is the gate's own prose, carried through rather than re-coded.
    assert out["dropped"][0]["entity"] == "the Downtown claim"
    assert "allowlist" in out["dropped"][0]["reason"]
    assert out["truncated"] is False
    assert out["timed_out"] is False


def test_run_reports_a_timeout_without_raising(monkeypatch):
    """An enrichment that fails must never take the answer with it."""
    def boom(q, a, b):
        raise RuntimeError("upstream on fire")

    monkeypatch.setattr(c, "extract_entities", boom)
    out = c.run("q", "a")
    assert out["corroborations"] == []
    assert out["timed_out"] is True


def test_run_short_circuits_when_the_gate_keeps_nothing(monkeypatch):
    """No entities means no search call. Paying for a web search that has
    nothing to look up is the cheapest bug to avoid."""
    called = {"search": 0}
    monkeypatch.setattr(c, "extract_entities",
                        lambda q, a, b: [{"entity": "x", "kind": "project"}])  # gate refuses
    monkeypatch.setattr(c, "search",
                        lambda *a, **k: called.__setitem__("search", 1) or ("", []))
    out = c.run("q", "a")
    assert called["search"] == 0
    assert out["corroborations"] == []
```

- [ ] **Step 2: Run to verify they fail**

Run: `python -m pytest tests/unit/test_corroboration.py -q`
Expected: collection error / FAIL.

- [ ] **Step 3: Write the implementation**

```python
"""The corroborate pipeline: extract -> gate -> search -> reconcile.

Budget (spec §5.3), against ApiFunction's 30s and not this function's 60s:
an agent still working at 35 seconds is returning to a caller that died.

    extract   4s
    gate     <10ms   (pure Python)
    search   12s
    reconcile 6s
             ---
             ~22s inside a 24s hard stop
"""
import datetime
import json
import logging

import corroboration_client as client
import corroboration_gate as gate_mod

logger = logging.getLogger()

STATES = frozenset({"corroborated", "conflicts", "not_found", "no_checkable_claim"})

ENTITY_CAP = 3
STEP_EXTRACT = 4.0
STEP_SEARCH = 12.0
STEP_RECONCILE = 6.0

_EXTRACT_PROMPT = """From the question and the answer below, list the named things an
external source could check, and for each one the CLAIM the answer makes about it.

Return JSON only: {"entities":[{"entity":"...","kind":"...","claim":"..."}]}

`kind` is one of: company, standard, product, public_role, authority, person, project, other.
`claim` is what the ANSWER asserts about that entity, or null if it asserts nothing
an external source could confirm. Do not invent a claim to fill the field.

Question: %s

Answer: %s
"""

_SEARCH_PROMPT = """Look up each of these and summarise what public sources say about it.
Be brief. If you cannot find a reliable source for one, say so for that one.

%s
"""

_RECONCILE_PROMPT = """For each entity below you are given the CLAIM an internal record
makes about it, and what public web sources say. Decide one state per entity:

  corroborated       - the claim is checkable AND the sources agree
  conflicts          - the claim is checkable AND the sources say otherwise
  not_found          - the claim is checkable but no usable source was found
  no_checkable_claim - the record asserts nothing about it a source could confirm

Return JSON only: {"results":[{"entity":"...","state":"...","summary":"..."}]}

Entities and claims:
%s

What the web said:
%s
"""


def _dropped_json(rejected):
    """`Rejected` is an object with prose in `.reason`; the client needs JSON.

    The reason is carried through verbatim rather than mapped to a code. The
    gate authors wrote them to be read in a log line, and a second vocabulary
    here is a second thing to keep in sync.
    """
    return [{"entity": r.entity, "kind": r.kind, "reason": r.reason}
            for r in (rejected or [])]


def _text_of(data):
    if not data:
        return ""
    return "\n".join(b.get("text", "") for b in data.get("content", [])
                     if b.get("type") == "text")


def _json_of(data):
    raw = _text_of(data).strip()
    if raw.startswith("```"):
        raw = raw.split("```")[1]
        if raw.startswith("json"):
            raw = raw[4:]
    try:
        return json.loads(raw)
    except Exception:
        logger.warning("corroborate: model did not return JSON")
        return {}


def _sources_of(data):
    """Pull sources out of the web_search_result blocks.

    llm_utils' anthropic branch joins `text` blocks only and throws these away,
    which is one of the reasons this path does not use it.
    """
    out = []
    for block in (data or {}).get("content", []):
        if block.get("type") == "web_search_tool_result":
            for r in block.get("content", []) or []:
                if r.get("type") == "web_search_result":
                    out.append({"title": r.get("title"), "url": r.get("url"),
                                "published": r.get("page_age")})
    return out


def extract_entities(question, answer, budget):
    data, err = client.call_anthropic(
        _EXTRACT_PROMPT % (question, answer),
        max_tokens=1024, timeout=min(STEP_EXTRACT, budget.remaining()))
    if err:
        logger.warning("corroborate: extraction failed (%s)", err)
        return []
    return (_json_of(data).get("entities") or [])


def search(entities, budget):
    listing = "\n".join("- %s (%s)" % (e["entity"], e.get("kind") or "")
                        for e in entities)
    data, err = client.call_anthropic(
        _SEARCH_PROMPT % listing,
        max_tokens=2048, timeout=min(STEP_SEARCH, budget.remaining()),
        tools=[{"type": "web_search_20250305", "name": "web_search",
                "max_uses": ENTITY_CAP}])
    if err:
        logger.warning("corroborate: search failed (%s)", err)
        return "", []
    return _text_of(data), _sources_of(data)


def reconcile(entities, web_text, sources, budget):
    listing = "\n".join(
        "- %s | claim: %s" % (e["entity"], e.get("claim") or "(none)")
        for e in entities)
    data, err = client.call_anthropic(
        _RECONCILE_PROMPT % (listing, web_text),
        max_tokens=1536, timeout=min(STEP_RECONCILE, budget.remaining()))
    by_entity = {}
    if not err:
        for r in (_json_of(data).get("results") or []):
            by_entity[r.get("entity")] = r

    now = datetime.datetime.now(datetime.timezone.utc).isoformat()
    out = []
    for e in entities:
        r = by_entity.get(e["entity"]) or {}
        state = r.get("state")
        if state not in STATES:
            # An invented fifth state, or an entity the model dropped. Both
            # mean the same thing to a reader: we did not establish it.
            state = "not_found"
        out.append({
            "entity": e["entity"],
            "kind": e.get("kind"),
            "state": state,
            "claim": e.get("claim"),
            "summary": r.get("summary"),
            "sources": sources if state in ("corroborated", "conflicts") else [],
            "retrieved_at": now,
        })
    return out


def run(question, answer, total_seconds=24.0):
    """Never raises. A failed enrichment returns an empty, flagged result --
    the answer above it is already on the reader's screen and must not be
    disturbed by this."""
    budget = client.Budget(total_seconds)
    try:
        raw = extract_entities(question, answer, budget)

        # corroboration_gate.screen takes ONLY the entities -- not the answer.
        # The sentence an entity sits in is never sent anywhere, so screening on
        # it would drop legitimate lookups to defend against a leak that cannot
        # happen. See the module's own docstring and Task 2.
        result = gate_mod.screen(raw, max_entities=ENTITY_CAP)
        dropped = _dropped_json(result.rejected)

        if not result.allowed:
            return {"corroborations": [], "dropped": dropped,
                    "truncated": result.truncated, "timed_out": False}
        if budget.expired():
            return {"corroborations": [], "dropped": dropped,
                    "truncated": result.truncated, "timed_out": True}

        web_text, sources = search(result.allowed, budget)
        results = reconcile(result.allowed, web_text, sources, budget)
        return {
            "corroborations": results,
            "dropped": dropped,
            # The gate knows this deterministically. Do not re-derive it by
            # counting reasons: `truncated` (the cap bit) and `timed_out` (a step
            # ran out) are different facts and must never collapse into one.
            "truncated": result.truncated,
            "timed_out": budget.expired(),
        }
    except Exception:
        logger.exception("corroborate: pipeline failed")
        return {"corroborations": [], "dropped": [],
                "truncated": False, "timed_out": True}
```

- [ ] **Step 4: Run to verify they pass**

Run: `python -m pytest tests/unit/test_corroboration.py -q`
Expected: 10 passed.

- [ ] **Step 5: Commit**

```bash
git add src/corroborate.py tests/unit/test_corroboration.py
git commit -m "Four states, and the two that are easy to lose"
```

---

### Task 4: Wire both lambdas

**Files:**
- Modify: `src/lambda_ask_agent.py` (near `:1179`, the existing `body.get('mode') == 'search'` branch)
- Modify: `src/lambda_fieldsight_api.py` (route at `:1451`; new function beside `ask_question` at `:1192`)
- Test: `tests/unit/test_corroborate_route.py`

**Interfaces:**
- Consumes: `corroborate.run`
- Produces: `lambda_fieldsight_api.corroborate_answer(body, caller) -> dict` (an `ok()`/`error()` HTTP response)

- [ ] **Step 1: Write the failing tests**

```python
"""Unit: the seam between the two lambdas.

The first draft of the spec put this route inside lambda_ask_agent's handler.
That function has no path dispatch -- AskAgentFunction has no API Gateway
Events at all and is only ever invoked by ApiFunction. Getting that wrong is
what this file exists to prevent recurring.
"""
import json
import pytest

api = pytest.importorskip("lambda_fieldsight_api")
agent = pytest.importorskip("lambda_ask_agent")


def test_the_proxy_injects_caller_sub_and_the_client_cannot(monkeypatch):
    """Identity comes from the Cognito authorizer, never from the body. A body
    the caller controls is a caller who can ask as somebody else."""
    seen = {}

    class FakeLambda:
        def invoke(self, **kw):
            seen["payload"] = json.loads(kw["Payload"])
            class R:
                def read(self_inner):
                    return json.dumps({"corroborations": []}).encode()
            return {"Payload": R()}

    monkeypatch.setattr(api, "lambda_client", FakeLambda())
    api.corroborate_answer(
        {"question": "q", "answer": "a", "caller_sub": "i-am-somebody-else"},
        {"sub": "real-sub"})
    assert seen["payload"]["caller_sub"] == "real-sub"
    assert seen["payload"]["mode"] == "corroborate"


def test_a_missing_answer_is_rejected_before_any_invoke(monkeypatch):
    called = {"n": 0}

    class FakeLambda:
        def invoke(self, **kw):
            called["n"] += 1

    monkeypatch.setattr(api, "lambda_client", FakeLambda())
    res = api.corroborate_answer({"question": "q"}, {"sub": "s"})
    assert res["statusCode"] == 400
    assert called["n"] == 0


def test_the_agent_dispatches_on_mode_not_on_a_path(monkeypatch):
    """lambda_ask_agent has no routes. Dispatch is on body content, matching
    the existing `mode == 'search'` branch."""
    monkeypatch.setattr(agent, "ENABLE_EXTERNAL_CORROBORATION", True)
    monkeypatch.setattr(agent.corroborate, "run",
                        lambda q, a: {"corroborations": [{"entity": "X"}],
                                      "dropped": [], "truncated": False,
                                      "timed_out": False})
    res = agent.lambda_handler(
        {"question": "q", "answer": "a", "mode": "corroborate",
         "caller_sub": "s"}, None)
    assert json.loads(res["body"])["corroborations"][0]["entity"] == "X"


def test_the_flag_off_returns_an_empty_result_and_never_calls_the_pipeline(monkeypatch):
    called = {"n": 0}
    monkeypatch.setattr(agent, "ENABLE_EXTERNAL_CORROBORATION", False)
    monkeypatch.setattr(agent.corroborate, "run",
                        lambda q, a: called.__setitem__("n", 1) or {})
    res = agent.lambda_handler(
        {"question": "q", "answer": "a", "mode": "corroborate"}, None)
    assert json.loads(res["body"])["corroborations"] == []
    assert called["n"] == 0


def test_the_ask_response_shape_is_unchanged(monkeypatch):
    """A frontend that never calls the new route must see today's behaviour."""
    monkeypatch.setattr(agent, "_rag_answer",
                        lambda body: {"answer": "a", "citations": [], "model": "m",
                                      "grounded": True, "basis": None})
    res = agent.lambda_handler({"question": "q", "caller_sub": "s"}, None)
    body = json.loads(res["body"])
    assert set(body) == {"answer", "citations", "model", "grounded", "basis"}
```

- [ ] **Step 2: Run to verify they fail**

Run: `python -m pytest tests/unit/test_corroborate_route.py -q`
Expected: FAIL — `corroborate_answer` does not exist.

- [ ] **Step 3: Add the branch in `src/lambda_ask_agent.py`**

Near the top, beside the other module-level env reads:

```python
import corroborate

# Read at module load like the rest of this file's config. The unwired-toggle
# trap in this repo is about the value never REACHING the function, not about
# when it is read -- and the wiring test in Task 5 is what covers that.
ENABLE_EXTERNAL_CORROBORATION = (
    os.environ.get("ENABLE_EXTERNAL_CORROBORATION", "false").strip().lower() == "true"
)
```

Then in `lambda_handler`, immediately before the existing `if body.get('mode') == 'search':` at `:1179`:

```python
        # Second pass (spec §5.1). Dispatched on body content because this
        # lambda has no paths -- ApiFunction routes /api/ask/corroborate here.
        if body.get('mode') == 'corroborate':
            if not ENABLE_EXTERNAL_CORROBORATION:
                # An empty result rather than an error: the flag being off is
                # a configuration state, not a failure, and the client renders
                # nothing for it.
                return ok({"corroborations": [], "dropped": [],
                           "truncated": False, "timed_out": False})
            return ok(corroborate.run(body.get('question', ''),
                                      body.get('answer', '')))
```

- [ ] **Step 4: Add the route and bridge in `src/lambda_fieldsight_api.py`**

Beside `ask_question`:

```python
def corroborate_answer(body, caller):
    """Second pass over an answer /ask already returned.

    Mirrors ask_question deliberately, including its FunctionError guard: an
    unhandled exception inside the agent comes back as a 200 with FunctionError
    set and a stack trace in the payload, and that must never reach a client.

    `caller_sub` is taken from the authorizer and any value in the body is
    ignored -- same rule as ask_question, for the same reason.
    """
    question = (body.get('question') or '').strip()
    answer = (body.get('answer') or '').strip()
    if not question:
        return error('Missing question')
    if not answer:
        return error('Missing answer')

    payload = {
        'question': question,
        'answer': answer,
        'mode': 'corroborate',
        'caller_sub': caller.get('sub', ''),
    }
    try:
        resp = lambda_client.invoke(
            FunctionName=ASK_AGENT_FUNCTION,
            InvocationType='RequestResponse',
            Payload=json.dumps(payload),
        )
        if resp.get('FunctionError'):
            logger.error("Corroborate returned FunctionError: %s", resp.get('FunctionError'))
            return error('Corroborate error', 500)
        return ok(json.loads(resp['Payload'].read().decode('utf-8')))
    except Exception:
        logger.exception("corroborate invoke failed")
        return error('Corroborate error', 500)
```

And at `:1451`, directly under the `/api/ask/voice` line:

```python
        elif path == '/api/ask/corroborate' and method == 'POST': return corroborate_answer(body, caller)
```

- [ ] **Step 5: Run to verify they pass**

Run: `python -m pytest tests/unit/test_corroborate_route.py -q`
Expected: 5 passed.

- [ ] **Step 6: Commit**

```bash
git add src/lambda_ask_agent.py src/lambda_fieldsight_api.py tests/unit/test_corroborate_route.py
git commit -m "Two files, because the route was never where the first draft put it"
```

---

### Task 5: Template and both workflows

**Files:**
- Modify: `src/template.yaml` (Parameters block; `AskAgentFunction` Environment at ~`:1516`)
- Modify: `.github/workflows/deploy.yml` (the `--parameter-overrides` list, ~`:179`)
- Modify: `.github/workflows/deploy-prod.yml` (the same list, ~`:232`)
- Test: `tests/unit/test_template_workflow_parameter_wiring.py` (add a case)

**Interfaces:**
- Consumes: nothing.
- Produces: `ENABLE_EXTERNAL_CORROBORATION` on the deployed `AskAgentFunction`.

⚠️ **Both workflow files are a rebase hot spot.** Parallel sessions add parameters on the same lines — PR #654 hit exactly this and had to be rebased over 104 commits. Rebase onto `origin/develop` immediately before this task, and expect a conflict where you keep **both** sides.

- [ ] **Step 1: Write the failing test**

Append to `tests/unit/test_template_workflow_parameter_wiring.py`:

```python
def test_external_corroboration_reaches_the_ask_agent_from_both_workflows():
    """The repo's generic guard proves a boolean Parameter is passed by both
    workflows. It cannot prove the value reaches a FUNCTION -- and an env
    declared in the template but not threaded through a workflow yields the
    default silently, with no error anywhere (memory:
    fieldsight-unwired-toggle-trap).
    """
    tpl = _read("src/template.yaml")
    assert "EnableExternalCorroboration:" in tpl
    assert tpl.count("ENABLE_EXTERNAL_CORROBORATION: !Ref EnableExternalCorroboration") == 1

    test_wf = _read(".github/workflows/deploy.yml")
    prod_wf = _read(".github/workflows/deploy-prod.yml")
    assert "EnableExternalCorroboration=${{ vars.TEST_ENABLE_EXTERNAL_CORROBORATION || 'false' }}" in test_wf
    assert "EnableExternalCorroboration=${{ vars.PROD_ENABLE_EXTERNAL_CORROBORATION || 'false' }}" in prod_wf
    # A missing repo variable must leave the feature off, not on.
    assert "|| 'true' }}\" \\\n" not in test_wf.split("EnableExternalCorroboration")[1][:80]
```

(If `_read` is not the helper name in that file, use whatever it already uses to load a repo file — do not add a second one.)

- [ ] **Step 2: Run to verify it fails**

Run: `python -m pytest tests/unit/test_template_workflow_parameter_wiring.py -q -k external_corroboration`
Expected: FAIL — `EnableExternalCorroboration:` not in template.

- [ ] **Step 3: Add the Parameter and the env**

In `src/template.yaml` Parameters:

```yaml
  EnableExternalCorroboration:
    Type: String
    Default: 'false'
    AllowedValues: ['true', 'false']
    Description: >-
      Second-pass web corroboration under an Ask answer. Off by default and
      must stay off for any customer until the contractual question in the
      design doc section 10 has an answer -- this sends entity names to a
      third-party search provider.
```

In `AskAgentFunction`'s `Environment.Variables`:

```yaml
          ENABLE_EXTERNAL_CORROBORATION: !Ref EnableExternalCorroboration
```

- [ ] **Step 4: Add the line to both workflows**

`deploy.yml`, in the `--parameter-overrides` list:

```
              "EnableExternalCorroboration=${{ vars.TEST_ENABLE_EXTERNAL_CORROBORATION || 'false' }}" \
```

`deploy-prod.yml`, same list:

```
              "EnableExternalCorroboration=${{ vars.PROD_ENABLE_EXTERNAL_CORROBORATION || 'false' }}" \
```

- [ ] **Step 5: Run to verify it passes**

Run: `python -m pytest tests/unit/test_template_workflow_parameter_wiring.py -q`
Expected: all pass.

- [ ] **Step 6: The revert-check**

1. Delete the `ENABLE_EXTERNAL_CORROBORATION:` line from `template.yaml`. Run the test. Expect RED. Restore it.
2. Change `deploy.yml`'s fallback from `'false'` to `'true'`. Run the test. Expect RED. Restore it.

Restore by re-editing, **not** by `git checkout <file>` — that reverts the whole file and takes the rest of the task's work with it.

- [ ] **Step 7: Commit**

```bash
git add src/template.yaml .github/workflows/deploy.yml .github/workflows/deploy-prod.yml tests/unit/test_template_workflow_parameter_wiring.py
git commit -m "A switch with all four segments, default off in both"
```

---

### Task 6: Deploy to TEST and measure what the plan assumed

**Files:** none — this task produces a measurement and a PR comment.

- [ ] **Step 1: Full suite**

Run: `python -m pytest tests/unit -q`
Expected: all pass. Record the count.

- [ ] **Step 2: Open the PR to `develop` and wait for CI**

If the PR shows `mergeable=CONFLICTING`, **GitHub will not run `pull_request` workflows at all** and the PR will show zero checks — which reads exactly like "CI hasn't started". Rebase onto `origin/develop` first.

- [ ] **Step 3: After merge, confirm the env actually reached the function**

```bash
export MSYS_NO_PATHCONV=1
aws lambda get-function-configuration \
  --function-name fieldsight-test-ask-agent \
  --profile fieldsight-deployer --region ap-southeast-2 \
  --query 'Environment.Variables.ENABLE_EXTERNAL_CORROBORATION' --output text
```

Expected: `false`. **This is the verification, not the passing test** — the wiring test proves the file says it; this proves the deploy carries it.

- [ ] **Step 4: Measure the search step**

Set `TEST_ENABLE_EXTERNAL_CORROBORATION=true`, redeploy, then:

```bash
export MSYS_NO_PATHCONV=1
aws lambda invoke --function-name fieldsight-test-ask-agent \
  --profile fieldsight-deployer --region ap-southeast-2 \
  --cli-binary-format raw-in-base64-out \
  --payload '{"mode":"corroborate","caller_sub":"f99e04e8-a0c1-7091-3da6-ed96fd63eb08","question":"Who is the CEO of Naylor Love?","answer":"Naylor Love'"'"'s chief executive is Rick Herd."}' \
  "C:/Users/camil/AppData/Local/Temp/corrob.json" --query 'StatusCode' --output text
```

Then read the CloudWatch `Duration` for that invocation.

**Report the number back on the PR.** Spec §9 says plainly that the 12 s allowance for the search step is an estimate and everything else in §3 was read out of a file. If the real figure is 20 s, `STEP_SEARCH` and the whole budget have to change, and that is a design change rather than a tuning knob.

- [ ] **Step 5: Read the JSON, do not assume it**

Print the response body and check each field is present: `corroborations`, `dropped`, `truncated`, `timed_out`, and per item `entity / kind / state / claim / summary / sources / retrieved_at`.

A serializer dropping a field it did not know about is exactly how the 0-day collapse shipped a silent deletion — every repository test was green and only the deployed JSON showed it.

- [ ] **Step 6: Set the flag back off**

```bash
gh variable set TEST_ENABLE_EXTERNAL_CORROBORATION --body "false" --repo benzn-tech/fieldsight-pipeline
```

Leave it off until §10 decision 1 has an answer.

---

## Coverage against the spec

| spec | task |
|---|---|
| §5.1 route + bridge, `{question, answer}` only | 4 |
| §5.2 four states, `conflicts`, `no_checkable_claim` | 3 |
| §5.3 budget, `truncated` vs `timed_out`, cap 3 | 1, 3 |
| §5.4 dedicated client, tools, ≤1 retry | 1 |
| §5.5 the switch, both workflows, revert-check | 5 |
| §5.6 `/ask` shape untouched | 4 (last test) |
| §4 privacy gate | **shipped, PR #656** |
| §4 prompt-contents test (entities only, no answer) | 3 |
| §7 CJK + defeat-it tests | **shipped with the gate** |
| §7 deployed-env check | 6 |
| §9 measure the search latency | 6 |

**Not in this plan, by design:** the frontend (shipping separately on
`feat/ask-corroboration`, PR fieldsight-ui#242) and P1, the extraction-time
path, which is deferred behind spec §10 decisions 1 and 3.
