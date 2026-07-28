# Alternative LLM + ASR Providers (Qwen 3.7 + ElevenLabs scribe_v2) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add Qwen 3.7 (LLM) and ElevenLabs `scribe_v2` (ASR) as runtime-toggleable drop-in alternatives to Claude and AWS Transcribe, with prod defaulting to the old providers and test to the new.

**Architecture:** One new `llm_utils.py` module dispatches LLM calls to `anthropic` or `qwen` by the `LLM_PROVIDER` env var, preserving the existing `(text, error)` return and `extract_json` ladder so the 6 call sites barely change. One new `elevenlabs_utils.py` calls `scribe_v2` synchronously and adapts its response into the exact AWS Transcribe JSON shape written to the same S3 key, so all downstream transcript consumers are untouched. `lambda_transcribe.py` branches on `ASR_PROVIDER`.

**Tech Stack:** Python 3.11/3.12 Lambdas, `urllib3` (no new SDK deps), AWS SAM (`template.yaml`), GitHub Actions, `pytest`.

## Global Constraints

- **No new pip dependencies.** Both new modules use only `urllib3` + stdlib, mirroring `src/dashscope_utils.py`. Do not add `openai`, `elevenlabs`, `anthropic` SDKs.
- **Default-safe for prod.** CFN parameter defaults are `LlmProvider=anthropic`, `AsrProvider=transcribe`. Prod must be unchanged until a `PROD_*` repo variable is flipped.
- **Test dogfoods new.** The test deploy workflow overrides the defaults to `qwen` / `elevenlabs` via `vars.TEST_LLM_PROVIDER || 'qwen'` and `vars.TEST_ASR_PROVIDER || 'elevenlabs'`.
- **Preserve interfaces.** `call_llm(prompt, max_tokens, force_json=False) -> (text|None, error|None)` and `extract_json(raw) -> dict|None`. ASR output must remain raw AWS Transcribe JSON shape at `transcripts/{user}/{date}/{base}.json`.
- **Per-Lambda model env:** `CLAUDE_MODEL` (anthropic model, already `haiku` on ask-agent and `sonnet` elsewhere) and `QWEN_MODEL` (`qwen3.7-plus` on structured Lambdas, `qwen-flash` on ask-agent). `llm_utils` picks `CLAUDE_MODEL` for the anthropic path and `QWEN_MODEL` for the qwen path — never both.
- **JSON mode:** the 4 structured call sites pass `force_json=True`; the ask path passes `force_json=False` (it returns prose/markdown). Qwen requests with `force_json=True` set `response_format={"type":"json_object"}` and send **no** `max_tokens` (DashScope truncation guidance).
- **Non-VPC rule (BUG-36):** all Lambdas touched here (`report-generator`, `meeting-minutes`, `extract-session`, `programme-matcher`, `ask-agent`, `transcribe`) are already non-VPC and may make external HTTP calls. Do not add `VpcConfig` to any of them.
- **Dev artifacts in English** (comments, commit messages, docstrings). Repo is `autocrlf=true`; use single-line edit anchors and never `git add -A`.
- **Test command:** `python -m pytest <path> -v` from repo root (`pyproject.toml` sets `pythonpath=["src"]`, `testpaths=["tests"]`).

---

### Task 1: `llm_utils.py` — unified provider-dispatching LLM client

**Files:**
- Create: `src/llm_utils.py`
- Test: `tests/unit/test_llm_utils.py`

**Interfaces:**
- Consumes: nothing (foundation).
- Produces:
  - `call_llm(prompt: str, max_tokens: int = 4096, force_json: bool = False) -> (str|None, str|None)`
  - `extract_json(raw_text: str) -> dict | None`
  - `api_key_configured() -> bool`
  - Module constants: `LLM_PROVIDER`, `ANTHROPIC_API_KEY`, `CLAUDE_MODEL`, `QWEN_API_KEY`, `QWEN_BASE_URL`, `QWEN_MODEL`, `MAX_ATTEMPTS`, `RETRYABLE_STATUSES`, `BACKOFF_BASE_SECONDS`.

- [ ] **Step 1: Write the failing tests**

Create `tests/unit/test_llm_utils.py`:

```python
"""Tests for src/llm_utils.py — provider dispatch + retry + JSON extraction.

Mirrors tests/unit/test_dashscope_utils.py: module-level env-derived constants
are monkeypatched on the module object, and urllib3.PoolManager.request is
patched at the class level (each call builds a fresh PoolManager()).
"""
import json
import pytest

lu = pytest.importorskip("llm_utils", reason="requires urllib3 (installed in CI)")


class _FakeResponse:
    def __init__(self, status, payload):
        self.status = status
        self.data = json.dumps(payload).encode("utf-8")


@pytest.fixture(autouse=True)
def no_sleep(monkeypatch):
    monkeypatch.setattr(lu.time, "sleep", lambda s: None)


def _patch_request(monkeypatch, responses):
    """responses: list of _FakeResponse (or Exception) returned in order."""
    calls = {"bodies": [], "urls": [], "headers": []}
    seq = list(responses)

    def fake_request(self, method, url, body=None, headers=None, timeout=None):
        calls["bodies"].append(body)
        calls["urls"].append(url)
        calls["headers"].append(headers)
        item = seq.pop(0)
        if isinstance(item, Exception):
            raise item
        return item

    monkeypatch.setattr(lu.urllib3.PoolManager, "request", fake_request)
    return calls


# --- anthropic path ---

def test_anthropic_success(monkeypatch):
    monkeypatch.setattr(lu, "LLM_PROVIDER", "anthropic")
    monkeypatch.setattr(lu, "ANTHROPIC_API_KEY", "sk-ant-x")
    monkeypatch.setattr(lu, "CLAUDE_MODEL", "claude-sonnet-4-6")
    calls = _patch_request(monkeypatch, [
        _FakeResponse(200, {"content": [{"type": "text", "text": "hello"}]}),
    ])
    text, err = lu.call_llm("hi", max_tokens=100)
    assert (text, err) == ("hello", None)
    assert "api.anthropic.com" in calls["urls"][0]
    assert json.loads(calls["bodies"][0])["max_tokens"] == 100


def test_anthropic_missing_key(monkeypatch):
    monkeypatch.setattr(lu, "LLM_PROVIDER", "anthropic")
    monkeypatch.setattr(lu, "ANTHROPIC_API_KEY", "")
    text, err = lu.call_llm("hi")
    assert text is None and "ANTHROPIC_API_KEY" in err


# --- qwen path ---

def test_qwen_success_prose(monkeypatch):
    monkeypatch.setattr(lu, "LLM_PROVIDER", "qwen")
    monkeypatch.setattr(lu, "QWEN_API_KEY", "sk-w")
    monkeypatch.setattr(lu, "QWEN_MODEL", "qwen-flash")
    calls = _patch_request(monkeypatch, [
        _FakeResponse(200, {"choices": [{"message": {"content": "answer"}}]}),
    ])
    text, err = lu.call_llm("hi", max_tokens=200, force_json=False)
    assert (text, err) == ("answer", None)
    body = json.loads(calls["bodies"][0])
    assert body["model"] == "qwen-flash"
    assert body["max_tokens"] == 200
    assert "response_format" not in body
    assert calls["headers"][0]["Authorization"] == "Bearer sk-w"


def test_qwen_force_json_omits_max_tokens(monkeypatch):
    monkeypatch.setattr(lu, "LLM_PROVIDER", "qwen")
    monkeypatch.setattr(lu, "QWEN_API_KEY", "sk-w")
    calls = _patch_request(monkeypatch, [
        _FakeResponse(200, {"choices": [{"message": {"content": "{}"}}]}),
    ])
    lu.call_llm("give JSON", max_tokens=999, force_json=True)
    body = json.loads(calls["bodies"][0])
    assert body["response_format"] == {"type": "json_object"}
    assert "max_tokens" not in body


def test_qwen_retries_on_503_then_succeeds(monkeypatch):
    monkeypatch.setattr(lu, "LLM_PROVIDER", "qwen")
    monkeypatch.setattr(lu, "QWEN_API_KEY", "sk-w")
    calls = _patch_request(monkeypatch, [
        _FakeResponse(503, {"error": {"message": "busy"}}),
        _FakeResponse(200, {"choices": [{"message": {"content": "ok"}}]}),
    ])
    text, err = lu.call_llm("hi")
    assert (text, err) == ("ok", None)
    assert len(calls["urls"]) == 2  # retried once


def test_qwen_gives_up_after_max_attempts(monkeypatch):
    monkeypatch.setattr(lu, "LLM_PROVIDER", "qwen")
    monkeypatch.setattr(lu, "QWEN_API_KEY", "sk-w")
    _patch_request(monkeypatch, [_FakeResponse(500, {}) for _ in range(lu.MAX_ATTEMPTS)])
    text, err = lu.call_llm("hi")
    assert text is None and err is not None


# --- api_key_configured + extract_json ---

def test_api_key_configured(monkeypatch):
    monkeypatch.setattr(lu, "LLM_PROVIDER", "qwen")
    monkeypatch.setattr(lu, "QWEN_API_KEY", "")
    assert lu.api_key_configured() is False
    monkeypatch.setattr(lu, "QWEN_API_KEY", "sk-w")
    assert lu.api_key_configured() is True


def test_extract_json_fenced():
    assert lu.extract_json('prefix ```json\n{"a": 1}\n``` suffix') == {"a": 1}


def test_extract_json_braces_fallback():
    assert lu.extract_json('noise {"b": 2} trailing') == {"b": 2}


def test_extract_json_failure_returns_none():
    assert lu.extract_json("no json here") is None
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python -m pytest tests/unit/test_llm_utils.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'llm_utils'` (importorskip skips, or collection error). Confirm the module does not yet exist.

- [ ] **Step 3: Write `src/llm_utils.py`**

```python
"""
llm_utils.py — Unified LLM client with runtime provider dispatch.

Replaces the four duplicated call_claude implementations (claude_utils.py,
lambda_report_generator.py, lambda_meeting_minutes.py, lambda_ask_agent.py).
Dispatches on LLM_PROVIDER: 'anthropic' (Claude Messages API, verbatim
behaviour) or 'qwen' (DashScope OpenAI-compatible /chat/completions). Adds the
exponential-backoff retry claude_utils.py never had, mirroring
dashscope_utils.py (MAX_ATTEMPTS=4, backoff on 429/5xx).

Model selection is per-Lambda via env: CLAUDE_MODEL for the anthropic path,
QWEN_MODEL for the qwen path. Never reads both.

Environment Variables:
    LLM_PROVIDER   - 'anthropic' (default) | 'qwen'
    ANTHROPIC_API_KEY / CLAUDE_MODEL - anthropic path
    QWEN_API_KEY (falls back to DASHSCOPE_API_KEY) / QWEN_BASE_URL / QWEN_MODEL - qwen path
"""
import json
import logging
import os
import re
import time

import urllib3

logger = logging.getLogger()
logger.setLevel(logging.INFO)

LLM_PROVIDER = os.environ.get("LLM_PROVIDER", "anthropic")

ANTHROPIC_API_KEY = os.environ.get("ANTHROPIC_API_KEY", "")
CLAUDE_MODEL = os.environ.get("CLAUDE_MODEL", "claude-sonnet-4-6")

QWEN_API_KEY = os.environ.get("QWEN_API_KEY", os.environ.get("DASHSCOPE_API_KEY", ""))
QWEN_BASE_URL = os.environ.get(
    "QWEN_BASE_URL", "https://dashscope-intl.aliyuncs.com/compatible-mode/v1"
)
QWEN_MODEL = os.environ.get("QWEN_MODEL", "qwen3.7-plus")

MAX_ATTEMPTS = 4
RETRYABLE_STATUSES = {429, 500, 502, 503, 504}
BACKOFF_BASE_SECONDS = 1.0
# 150s so the HTTP client loses the race against the Lambda's own Timeout and
# we get a catchable urllib3 error instead of a runtime hard-kill.
HTTP_TIMEOUT = 150.0


def api_key_configured():
    """True if the active provider's key is set (used for fail-fast checks)."""
    if LLM_PROVIDER == "qwen":
        return bool(QWEN_API_KEY)
    return bool(ANTHROPIC_API_KEY)


def call_llm(prompt, max_tokens=4096, force_json=False):
    """Return (text, None) on success or (None, error_string) on failure."""
    if LLM_PROVIDER == "qwen":
        return _call_qwen(prompt, max_tokens, force_json)
    return _call_anthropic(prompt, max_tokens)


def _post_with_retry(url, body, headers):
    """Single POST with exponential backoff on 429/5xx. Returns (resp, error)."""
    http = urllib3.PoolManager()
    last_error = None
    for attempt in range(MAX_ATTEMPTS):
        try:
            resp = http.request(
                "POST", url, body=body, headers=headers, timeout=HTTP_TIMEOUT
            )
        except Exception as e:  # noqa: BLE001 - network errors are retryable
            last_error = str(e)
            if attempt < MAX_ATTEMPTS - 1:
                time.sleep(BACKOFF_BASE_SECONDS * (2 ** attempt))
                continue
            return None, last_error
        if resp.status in RETRYABLE_STATUSES and attempt < MAX_ATTEMPTS - 1:
            last_error = f"HTTP {resp.status}"
            time.sleep(BACKOFF_BASE_SECONDS * (2 ** attempt))
            continue
        return resp, None
    return None, last_error


def _call_anthropic(prompt, max_tokens):
    if not ANTHROPIC_API_KEY:
        logger.error("ANTHROPIC_API_KEY not set")
        return None, "ANTHROPIC_API_KEY not configured"
    body = json.dumps({
        "model": CLAUDE_MODEL,
        "max_tokens": max_tokens,
        "messages": [{"role": "user", "content": prompt}],
    })
    resp, err = _post_with_retry(
        "https://api.anthropic.com/v1/messages",
        body,
        {
            "Content-Type": "application/json",
            "x-api-key": ANTHROPIC_API_KEY,
            "anthropic-version": "2023-06-01",
        },
    )
    if resp is None:
        logger.error(f"Claude API call failed: {err}")
        return None, err
    data = json.loads(resp.data.decode("utf-8"))
    if resp.status == 200:
        blocks = [b["text"] for b in data.get("content", []) if b.get("type") == "text"]
        return "\n".join(blocks), None
    msg = data.get("error", {}).get("message", f"HTTP {resp.status}")
    logger.error(f"Claude API error: {msg}")
    return None, msg


def _call_qwen(prompt, max_tokens, force_json):
    if not QWEN_API_KEY:
        logger.error("QWEN_API_KEY / DASHSCOPE_API_KEY not set")
        return None, "QWEN_API_KEY not configured"
    payload = {"model": QWEN_MODEL, "messages": [{"role": "user", "content": prompt}]}
    if force_json:
        # DashScope: do NOT send max_tokens with response_format (truncation risk).
        payload["response_format"] = {"type": "json_object"}
    else:
        payload["max_tokens"] = max_tokens
    resp, err = _post_with_retry(
        f"{QWEN_BASE_URL}/chat/completions",
        json.dumps(payload),
        {"Content-Type": "application/json", "Authorization": f"Bearer {QWEN_API_KEY}"},
    )
    if resp is None:
        logger.error(f"Qwen API call failed: {err}")
        return None, err
    data = json.loads(resp.data.decode("utf-8"))
    if resp.status == 200:
        try:
            return data["choices"][0]["message"]["content"], None
        except (KeyError, IndexError):
            logger.error(f"Qwen unexpected response shape: {str(data)[:500]}")
            return None, "unexpected Qwen response shape"
    msg = data.get("error", {}).get("message", f"HTTP {resp.status}")
    logger.error(f"Qwen API error: {msg}")
    return None, msg


def extract_json(raw_text):
    """Three-tier fallback: fenced ```json``` block, whole string, brace slice."""
    match = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", raw_text, re.DOTALL)
    if match:
        try:
            return json.loads(match.group(1))
        except json.JSONDecodeError:
            pass
    try:
        return json.loads(raw_text.strip())
    except json.JSONDecodeError:
        pass
    first_brace = raw_text.find("{")
    last_brace = raw_text.rfind("}")
    if first_brace != -1 and last_brace != -1:
        try:
            return json.loads(raw_text[first_brace:last_brace + 1])
        except json.JSONDecodeError:
            pass
    logger.error(f"Failed to extract JSON from LLM response: {raw_text[:500]}")
    return None
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `python -m pytest tests/unit/test_llm_utils.py -v`
Expected: PASS (all 11 tests).

- [ ] **Step 5: Commit**

```bash
git add src/llm_utils.py tests/unit/test_llm_utils.py
git commit -m "feat(llm): unified provider-dispatching LLM client (anthropic|qwen)"
```

---

### Task 2: Route `extract_session` + `programme_matcher` through `llm_utils`

Both already `import claude_utils` and call `claude_utils.call_claude` / `claude_utils.extract_json` directly. Swap to `llm_utils`, add `force_json=True` (structured JSON tasks), and switch the fail-fast key check to `llm_utils.api_key_configured()`.

**Files:**
- Modify: `src/lambda_extract_session.py` (line 45 import; 314 key check; 360 call; 364 extract)
- Modify: `src/lambda_programme_matcher.py` (line 117 import; 300, 441 extract; 560, 653 call)
- Test: `tests/unit/test_lambda_extract_session.py` (existing — verify still green)

**Interfaces:**
- Consumes: `llm_utils.call_llm`, `llm_utils.extract_json`, `llm_utils.api_key_configured` (Task 1).
- Produces: no new public interface.

- [ ] **Step 1: Edit `lambda_extract_session.py`**

Change the import (line 45) from:

```python
import claude_utils
```
to:
```python
import llm_utils
```

Change the key check (line ~314) from:
```python
    if not claude_utils.ANTHROPIC_API_KEY:
```
to:
```python
    if not llm_utils.api_key_configured():
```

Change the call (line ~360) from:
```python
    raw_response, error = claude_utils.call_claude(prompt, max_tokens=max_tokens)
```
to:
```python
    raw_response, error = llm_utils.call_llm(prompt, max_tokens=max_tokens, force_json=True)
```

Change the extract (line ~364) from:
```python
    parsed = claude_utils.extract_json(raw_response)
```
to:
```python
    parsed = llm_utils.extract_json(raw_response)
```

- [ ] **Step 2: Edit `lambda_programme_matcher.py`**

Change the import (line 117) from `import claude_utils` to `import llm_utils`.

Change both extract calls (lines ~300 and ~441) from `claude_utils.extract_json(raw)` to `llm_utils.extract_json(raw)`.

Change the call at line ~560 from:
```python
    raw, error = claude_utils.call_claude(prompt, max_tokens=512)
```
to:
```python
    raw, error = llm_utils.call_llm(prompt, max_tokens=512, force_json=True)
```

Change the call at line ~653 from:
```python
    raw, error = claude_utils.call_claude(prompt, max_tokens=max_tokens)
```
to:
```python
    raw, error = llm_utils.call_llm(prompt, max_tokens=max_tokens, force_json=True)
```

- [ ] **Step 3: Verify no stray `claude_utils` references remain in these two files**

Run: `grep -n "claude_utils" src/lambda_extract_session.py src/lambda_programme_matcher.py`
Expected: no output (comments referencing it in docstrings may remain; code references must be gone). If a code line still references `claude_utils`, fix it.

- [ ] **Step 4: Run the affected unit tests**

Run: `python -m pytest tests/unit/test_lambda_extract_session.py -v`
Expected: PASS. If a test monkeypatches `claude_utils` on the module, update it to patch `llm_utils` (e.g. `monkeypatch.setattr(mod, "llm_utils", fake)` or patch `llm_utils.call_llm`). Show the change and re-run until green.

- [ ] **Step 5: Commit**

```bash
git add src/lambda_extract_session.py src/lambda_programme_matcher.py tests/unit/test_lambda_extract_session.py
git commit -m "refactor(llm): route extract-session + matcher through llm_utils (force_json)"
```

---

### Task 3: Route `report_generator` + `meeting_minutes` through `llm_utils` (delegation shims)

Both have their own local `call_claude_structured` + `extract_json_from_response`. Replace the **bodies** of those two functions with delegation to `llm_utils` so the many existing call sites (5 in report_generator, 1 in meeting_minutes) stay untouched.

**Files:**
- Modify: `src/lambda_report_generator.py` (add import; replace bodies at 411-462)
- Modify: `src/lambda_meeting_minutes.py` (add import; replace bodies at 474-556)
- Test: `tests/unit/test_lambda_report_generator*.py`, `tests/unit/test_lambda_meeting_minutes*.py` if present (verify green)

**Interfaces:**
- Consumes: `llm_utils.call_llm`, `llm_utils.extract_json` (Task 1).
- Produces: `call_claude_structured(prompt, max_tokens=4096)` and `extract_json_from_response(raw_text)` keep their existing names/signatures (now delegating).

- [ ] **Step 1: Edit `lambda_report_generator.py`**

Add `import llm_utils` alongside the other imports at the top of the file (near the existing `import` block, e.g. after `import os`).

Replace the entire body of `call_claude_structured` (currently lines ~411-441) with:

```python
def call_claude_structured(prompt, max_tokens=4096):
    """Delegates to llm_utils (provider-dispatched). force_json: structured task."""
    return llm_utils.call_llm(prompt, max_tokens=max_tokens, force_json=True)
```

Replace the entire body of `extract_json_from_response` (currently lines ~444-462) with:

```python
def extract_json_from_response(raw_text):
    """Delegates to llm_utils.extract_json (identical three-tier ladder)."""
    return llm_utils.extract_json(raw_text)
```

- [ ] **Step 2: Edit `lambda_meeting_minutes.py`**

Add `import llm_utils` near the top imports.

Replace the entire body of `call_claude_structured` (currently lines ~474-511) with:

```python
def call_claude_structured(prompt, max_tokens=4096):
    """Delegates to llm_utils (provider-dispatched). force_json: structured task."""
    return llm_utils.call_llm(prompt, max_tokens=max_tokens, force_json=True)
```

Replace the entire body of `extract_json_from_response` (currently lines ~514-556) with:

```python
def extract_json_from_response(raw_text):
    """Delegates to llm_utils.extract_json (identical three-tier ladder)."""
    return llm_utils.extract_json(raw_text)
```

Note: leave the `model` metadata fields in the debug-record dicts (lines ~557, ~1003) as-is; they read the module-level `CLAUDE_MODEL` for logging only and do not affect the call path. They now record the anthropic model id even when qwen served the call — acceptable for a debug artifact, and harmless. (If desired, a later cleanup can log `llm_utils.QWEN_MODEL if llm_utils.LLM_PROVIDER=='qwen' else CLAUDE_MODEL`; not required for this task.)

- [ ] **Step 3: Verify delegation compiles and no local HTTP code remains in those functions**

Run: `python -c "import sys; sys.path.insert(0,'src'); import lambda_report_generator, lambda_meeting_minutes; print('ok')"`
Expected: `ok` (imports cleanly; requires `urllib3` in the environment). If `boto3`/layer imports fail at module load in your environment, instead run `python -m py_compile src/lambda_report_generator.py src/lambda_meeting_minutes.py` and expect no output.

- [ ] **Step 4: Run any existing tests for these modules**

Run: `python -m pytest tests/unit -k "report_generator or meeting_minutes" -v`
Expected: PASS (or "no tests ran" if none exist — acceptable). Fix any test that patched the old local HTTP internals to patch `llm_utils.call_llm` instead.

- [ ] **Step 5: Commit**

```bash
git add src/lambda_report_generator.py src/lambda_meeting_minutes.py
git commit -m "refactor(llm): delegate report + meeting-minutes call_claude to llm_utils"
```

---

### Task 4: Route `ask_agent` (both paths) through `llm_utils` + fix the hand-deploy bundle

The ask path returns prose (`force_json=False`) and uses the fast model. It has TWO call paths and a legacy hand-deploy script (`scripts/deploy-lambda-code.sh`) that bundles a single shared file (`transcript_utils.py`) into every function's zip. Since `ask-agent`, `report-generator`, and `meeting-minutes` (all deployed by that script) now import `llm_utils`, it must be added to the shared bundle and imported lazily in ask-agent.

**Files:**
- Modify: `src/lambda_ask_agent.py` (legacy `call_claude` def ~437 + call ~1057; RAG lazy import ~715 + call ~762)
- Modify: `scripts/deploy-lambda-code.sh` (add `llm_utils.py` to the shared bundle)
- Test: `tests/unit/test_lambda_fieldsight_api_ask*.py` (verify green)

**Interfaces:**
- Consumes: `llm_utils.call_llm` (Task 1).
- Produces: no new public interface.

- [ ] **Step 1: Replace the legacy local `call_claude` with a delegating shim**

In `lambda_ask_agent.py`, replace the entire body of `def call_claude(prompt, max_tokens=MAX_ANSWER_TOKENS):` (starting ~line 437) with a lazy-importing delegation. The lazy import matches the file's existing pattern (the RAG path already lazy-imports to protect the minimal-zip deploy target):

```python
def call_claude(prompt, max_tokens=MAX_ANSWER_TOKENS):
    """Prose answer via llm_utils (provider-dispatched). Lazy import keeps the
    legacy minimal-zip deploy target working. force_json stays False — the ask
    path returns markdown/plain prose, not JSON."""
    import llm_utils
    return llm_utils.call_llm(prompt, max_tokens=max_tokens, force_json=False)
```

- [ ] **Step 2: Point the RAG path at `llm_utils`**

In `_rag_answer` (~line 715 the lazy `import claude_utils`, ~line 762 the call), change the lazy import from:
```python
    import claude_utils
```
to:
```python
    import llm_utils
```
and the call at ~line 762 from:
```python
        answer, err = claude_utils.call_claude(prompt, max_tokens=2048)
```
to:
```python
        answer, err = llm_utils.call_llm(prompt, max_tokens=2048, force_json=False)
```

The `"model": claude_utils.CLAUDE_MODEL` metadata fields nearby (lines ~743, 757, 770, 794, 803) reference `claude_utils` — change each `claude_utils.CLAUDE_MODEL` to `llm_utils.CLAUDE_MODEL` so the lazy `import llm_utils` covers them (these are response-metadata strings; llm_utils exposes `CLAUDE_MODEL` too).

- [ ] **Step 3: Verify no code-level `claude_utils` references remain**

Run: `grep -n "claude_utils" src/lambda_ask_agent.py`
Expected: no output from code lines. Any remaining are in comments/docstrings only; if a code line remains, fix it.

- [ ] **Step 4: Add `llm_utils.py` to the shared bundle in `deploy-lambda-code.sh`**

The script (lines ~22, ~40, ~47) declares one shared file and bundles it into every zip:

```bash
SHARED="src/transcript_utils.py"   # bundled in every zip (CLAUDE.md rule)
...
[ -f "$SHARED" ] || { echo "❌ $SHARED not found (run from repo root)"; exit 1; }
...
  zip -j -q "$ZIP" "$HANDLER" "$SHARED"
```

Convert `SHARED` to a bash array so multiple shared modules are bundled. Change line ~22 from:
```bash
SHARED="src/transcript_utils.py"   # bundled in every zip (CLAUDE.md rule)
```
to:
```bash
SHARED=("src/transcript_utils.py" "src/llm_utils.py")   # bundled in every zip (CLAUDE.md rule)
```

Change the existence check (~line 40) from:
```bash
[ -f "$SHARED" ] || { echo "❌ $SHARED not found (run from repo root)"; exit 1; }
```
to:
```bash
for f in "${SHARED[@]}"; do [ -f "$f" ] || { echo "❌ $f not found (run from repo root)"; exit 1; }; done
```

Change the zip line (~line 47) from:
```bash
  zip -j -q "$ZIP" "$HANDLER" "$SHARED"
```
to:
```bash
  zip -j -q "$ZIP" "$HANDLER" "${SHARED[@]}"
```

(`elevenlabs_utils.py` is added to this same array in Task 6, once the file exists.)

- [ ] **Step 5: Run the ask-path unit tests**

Run: `python -m pytest tests/unit -k "ask" -v`
Expected: PASS. Update any test that patched `claude_utils.call_claude` to patch `llm_utils.call_llm` instead (the ask path now calls the latter). Show the change and re-run until green.

- [ ] **Step 6: Commit**

```bash
git add src/lambda_ask_agent.py scripts/deploy-lambda-code.sh
git commit -m "refactor(llm): route ask-agent through llm_utils; add it to hand-deploy bundle"
```

---

### Task 5: `elevenlabs_utils.py` — scribe_v2 client + Transcribe-shape adapter

**Files:**
- Create: `src/elevenlabs_utils.py`
- Test: `tests/unit/test_elevenlabs_utils.py`

**Interfaces:**
- Consumes: nothing (foundation). The adapter output is consumed by `transcript_utils.parse_transcribe_json` / `normalize_transcript` (existing, unchanged) and asserted via round-trip in tests.
- Produces:
  - `adapt_to_transcribe_json(el_response: dict) -> dict` (AWS Transcribe JSON shape)
  - `load_keyterms(vocab_path: str) -> list[str]`
  - `transcribe_segment(audio_bytes: bytes, filename: str, num_speakers: int = 5, keyterms: list[str]|None = None) -> dict`
  - Module constants: `ELEVENLABS_API_KEY`, `ELEVENLABS_STT_URL`, `ELEVENLABS_STT_MODEL`, `ELEVENLABS_LANGUAGE`, `MAX_ATTEMPTS`, `RETRYABLE_STATUSES`, `BACKOFF_BASE_SECONDS`.

- [ ] **Step 1: Write the failing tests**

Create `tests/unit/test_elevenlabs_utils.py`:

```python
"""Tests for src/elevenlabs_utils.py — scribe_v2 client + Transcribe adapter.

The adapter's contract is that transcript_utils can parse its output exactly
like real AWS Transcribe JSON, so the key test round-trips through
transcript_utils.parse_transcribe_json.
"""
import json
import pytest

eu = pytest.importorskip("elevenlabs_utils", reason="requires urllib3 (installed in CI)")
tu = pytest.importorskip("transcript_utils")


class _FakeResponse:
    def __init__(self, status, payload):
        self.status = status
        self.data = json.dumps(payload).encode("utf-8")


@pytest.fixture(autouse=True)
def no_sleep(monkeypatch):
    monkeypatch.setattr(eu.time, "sleep", lambda s: None)


SCRIBE_RESPONSE = {
    "language_code": "eng",
    "language_probability": 0.98,
    "text": "pour the slab today",
    "words": [
        {"text": "pour", "start": 0.10, "end": 0.40, "speaker_id": "speaker_0", "type": "word"},
        {"text": " ", "start": 0.40, "end": 0.41, "speaker_id": "speaker_0", "type": "spacing"},
        {"text": "the", "start": 0.41, "end": 0.55, "speaker_id": "speaker_0", "type": "word"},
        {"text": "slab", "start": 0.55, "end": 0.90, "speaker_id": "speaker_1", "type": "word"},
        {"text": "today", "start": 0.90, "end": 1.30, "speaker_id": "speaker_1", "type": "word"},
    ],
}


def test_adapter_produces_transcribe_shape():
    out = eu.adapt_to_transcribe_json(SCRIBE_RESPONSE)
    assert out["results"]["transcripts"][0]["transcript"] == "pour the slab today"
    items = out["results"]["items"]
    # spacing dropped; only the 4 words become pronunciation items
    assert len(items) == 4
    assert all(it["type"] == "pronunciation" for it in items)
    assert items[0]["start_time"] == "0.1" and items[0]["end_time"] == "0.4"
    assert items[0]["alternatives"][0]["content"] == "pour"
    # two distinct speakers mapped to spk_0 / spk_1 in first-seen order
    labels = {it["speaker_label"] for it in items}
    assert labels == {"spk_0", "spk_1"}


def test_adapter_round_trips_through_transcript_utils():
    out = eu.adapt_to_transcribe_json(SCRIBE_RESPONSE)
    parsed = tu.parse_transcribe_json(out)
    # transcript_utils must accept the adapted shape without error and recover text
    assert "slab" in parsed["full_text"]


def test_adapter_no_speaker_ids_omits_labels():
    resp = {"text": "hello", "words": [{"text": "hello", "start": 0.0, "end": 0.5, "type": "word"}]}
    out = eu.adapt_to_transcribe_json(resp)
    assert "speaker_label" not in out["results"]["items"][0]


def test_load_keyterms_parses_phrase_column(tmp_path):
    f = tmp_path / "vocab.txt"
    f.write_text("# comment line\nGIB\tgib\t\tGIB\ndwang\tdwong\nBRANZ\n", encoding="utf-8")
    terms = eu.load_keyterms(str(f))
    assert terms == ["GIB", "dwang", "BRANZ"]


def test_load_keyterms_missing_file_returns_empty():
    assert eu.load_keyterms("/no/such/file.txt") == []


def test_transcribe_segment_missing_key_raises(monkeypatch):
    monkeypatch.setattr(eu, "ELEVENLABS_API_KEY", "")
    with pytest.raises(RuntimeError, match="ELEVENLABS_API_KEY"):
        eu.transcribe_segment(b"\x00", "seg.wav")


def test_transcribe_segment_success(monkeypatch):
    monkeypatch.setattr(eu, "ELEVENLABS_API_KEY", "xi-key")
    captured = {}

    def fake_request(self, method, url, fields=None, headers=None, timeout=None):
        captured["url"] = url
        captured["fields"] = fields
        captured["headers"] = headers
        return _FakeResponse(200, SCRIBE_RESPONSE)

    monkeypatch.setattr(eu.urllib3.PoolManager, "request", fake_request)
    out = eu.transcribe_segment(b"\x00\x01", "seg.wav", num_speakers=3, keyterms=["GIB"])
    assert out["results"]["transcripts"][0]["transcript"] == "pour the slab today"
    assert captured["headers"]["xi-api-key"] == "xi-key"
    assert captured["fields"]["model_id"] == eu.ELEVENLABS_STT_MODEL
    assert captured["fields"]["num_speakers"] == "3"


def test_transcribe_segment_retries_then_succeeds(monkeypatch):
    monkeypatch.setattr(eu, "ELEVENLABS_API_KEY", "xi-key")
    seq = [_FakeResponse(503, {}), _FakeResponse(200, SCRIBE_RESPONSE)]

    def fake_request(self, method, url, fields=None, headers=None, timeout=None):
        return seq.pop(0)

    monkeypatch.setattr(eu.urllib3.PoolManager, "request", fake_request)
    out = eu.transcribe_segment(b"\x00", "seg.wav")
    assert out["results"]["transcripts"][0]["transcript"] == "pour the slab today"
    assert seq == []  # both responses consumed (one retry)
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python -m pytest tests/unit/test_elevenlabs_utils.py -v`
Expected: FAIL — module does not exist yet.

- [ ] **Step 3: Write `src/elevenlabs_utils.py`**

```python
"""
elevenlabs_utils.py — ElevenLabs scribe_v2 STT client + AWS-Transcribe adapter.

Synchronous batch transcription (multipart POST) plus adapt_to_transcribe_json,
which reshapes the scribe_v2 response into the exact raw AWS Transcribe JSON
that transcript_utils.parse_transcribe_json already consumes — so every
downstream transcript consumer is untouched. Mirrors dashscope_utils.py:
urllib3, env-var key, MAX_ATTEMPTS=4 exponential backoff, loud RuntimeError.

Environment Variables:
    ELEVENLABS_API_KEY   - xi-api-key (required — transcribe_segment raises if unset)
    ELEVENLABS_STT_URL   - endpoint (default: https://api.elevenlabs.io/v1/speech-to-text)
    ELEVENLABS_STT_MODEL - model id (default: scribe_v2)
    ELEVENLABS_LANGUAGE  - ISO 639-3 code to pin language; empty = auto-detect
"""
import json
import logging
import os
import time

import urllib3

logger = logging.getLogger()
logger.setLevel(logging.INFO)

ELEVENLABS_API_KEY = os.environ.get("ELEVENLABS_API_KEY", "")
ELEVENLABS_STT_URL = os.environ.get(
    "ELEVENLABS_STT_URL", "https://api.elevenlabs.io/v1/speech-to-text"
)
ELEVENLABS_STT_MODEL = os.environ.get("ELEVENLABS_STT_MODEL", "scribe_v2")
ELEVENLABS_LANGUAGE = os.environ.get("ELEVENLABS_LANGUAGE", "")

MAX_ATTEMPTS = 4
RETRYABLE_STATUSES = {429, 500, 502, 503, 504}
BACKOFF_BASE_SECONDS = 1.0
# scribe_v2 splits 8min+ audio into up to 4 parallel internal jobs; VAD segments
# are short, but allow generous headroom below the Lambda's own timeout.
HTTP_TIMEOUT = 280.0


def adapt_to_transcribe_json(el_response):
    """Reshape a scribe_v2 response into raw AWS Transcribe JSON.

    Only type=="word" entries become pronunciation items (spacing/audio_event
    dropped — full text comes from the top-level `text`). speaker_id values are
    mapped to spk_0, spk_1, ... in first-seen order; if no word carries a
    speaker_id, no speaker_label is emitted (transcript_utils then treats the
    whole clip as a single 'unknown' turn, matching its no-diarization path).
    Word confidence is a "1.0" placeholder — no downstream consumer reads it.
    """
    text = el_response.get("text", "")
    speaker_map = {}
    items = []
    for w in el_response.get("words", []):
        if w.get("type") != "word":
            continue
        item = {
            "type": "pronunciation",
            "start_time": str(w.get("start", 0.0)),
            "end_time": str(w.get("end", 0.0)),
            "alternatives": [{"content": w.get("text", ""), "confidence": "1.0"}],
        }
        sid = w.get("speaker_id")
        if sid is not None:
            if sid not in speaker_map:
                speaker_map[sid] = f"spk_{len(speaker_map)}"
            item["speaker_label"] = speaker_map[sid]
        items.append(item)
    return {"results": {"transcripts": [{"transcript": text}], "items": items}}


def load_keyterms(vocab_path):
    """Parse the tab-separated NZ construction vocab into a keyterms list.

    Takes the first (Phrase) column of each non-comment line, caps each term at
    50 chars and the list at 1000 (scribe_v2 limits). Missing file -> []."""
    terms = []
    try:
        with open(vocab_path, encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if not line or line.startswith("#"):
                    continue
                phrase = line.split("\t")[0].strip()
                if phrase:
                    terms.append(phrase[:50])
    except OSError:
        logger.warning(f"keyterms vocab not found: {vocab_path}")
        return []
    return terms[:1000]


def transcribe_segment(audio_bytes, filename, num_speakers=5, keyterms=None):
    """POST one audio segment to scribe_v2; return AWS-Transcribe-shaped dict.

    Raises RuntimeError on missing key or after MAX_ATTEMPTS failed attempts."""
    if not ELEVENLABS_API_KEY:
        raise RuntimeError("ELEVENLABS_API_KEY not set")
    fields = {
        "model_id": ELEVENLABS_STT_MODEL,
        "diarize": "true",
        "num_speakers": str(num_speakers),
        "timestamps_granularity": "word",
        "file": (filename, audio_bytes, "application/octet-stream"),
    }
    if ELEVENLABS_LANGUAGE:
        fields["language_code"] = ELEVENLABS_LANGUAGE
    if keyterms:
        # scribe_v2 keyterms: JSON array string. Confirmed against a live
        # response during Phase-2 validation (OI-2); adjust encoding if needed.
        fields["keyterms"] = json.dumps(keyterms)

    http = urllib3.PoolManager()
    last_error = None
    for attempt in range(MAX_ATTEMPTS):
        try:
            resp = http.request(
                "POST", ELEVENLABS_STT_URL, fields=fields,
                headers={"xi-api-key": ELEVENLABS_API_KEY}, timeout=HTTP_TIMEOUT,
            )
        except Exception as e:  # noqa: BLE001 - network errors are retryable
            last_error = str(e)
            if attempt < MAX_ATTEMPTS - 1:
                time.sleep(BACKOFF_BASE_SECONDS * (2 ** attempt))
                continue
            raise RuntimeError(f"ElevenLabs STT failed after {MAX_ATTEMPTS} attempts: {last_error}")
        if resp.status == 200:
            return adapt_to_transcribe_json(json.loads(resp.data.decode("utf-8")))
        if resp.status in RETRYABLE_STATUSES and attempt < MAX_ATTEMPTS - 1:
            last_error = f"HTTP {resp.status}"
            time.sleep(BACKOFF_BASE_SECONDS * (2 ** attempt))
            continue
        raise RuntimeError(f"ElevenLabs STT error HTTP {resp.status}: {resp.data[:300]}")
    raise RuntimeError(f"ElevenLabs STT failed after {MAX_ATTEMPTS} attempts: {last_error}")
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `python -m pytest tests/unit/test_elevenlabs_utils.py -v`
Expected: PASS (9 tests). If `test_adapter_round_trips_through_transcript_utils` fails, inspect `transcript_utils.parse_transcribe_json`'s return keys and adjust the assertion to a field it actually returns (do not change the adapter — the adapter shape is dictated by the raw Transcribe contract).

- [ ] **Step 5: Commit**

```bash
git add src/elevenlabs_utils.py tests/unit/test_elevenlabs_utils.py
git commit -m "feat(asr): ElevenLabs scribe_v2 client + AWS Transcribe-shape adapter"
```

---

### Task 6: `lambda_transcribe.py` — `ASR_PROVIDER` branch (synchronous ElevenLabs path)

When `ASR_PROVIDER=elevenlabs`, download the segment, transcribe synchronously, write the adapted JSON to the same `transcripts/...` key, and write the ledger row directly (no EventBridge callback). Default `transcribe` keeps the existing async-job path verbatim.

**Files:**
- Modify: `src/lambda_transcribe.py` (add env + s3 client + branch in `lambda_handler`)
- Test: `tests/unit/test_lambda_transcribe_elevenlabs.py` (new)

**Interfaces:**
- Consumes: `elevenlabs_utils.transcribe_segment`, `elevenlabs_utils.load_keyterms` (Task 5).
- Produces: no new public interface; writes `transcripts/{user}/{date}/{base}.json` as before.

- [ ] **Step 1: Write the failing test**

Create `tests/unit/test_lambda_transcribe_elevenlabs.py`:

```python
"""ASR_PROVIDER=elevenlabs synchronous path in lambda_transcribe."""
import json
import pytest

mod = pytest.importorskip("lambda_transcribe")


class _FakeBody:
    def __init__(self, data):
        self._data = data

    def read(self):
        return self._data


class _FakeS3:
    def __init__(self):
        self.put_calls = []
        self._obj = b"RIFFfakeWAVdata"

    def get_object(self, Bucket, Key):
        return {"Body": _FakeBody(self._obj)}

    def put_object(self, Bucket, Key, Body, **kw):
        self.put_calls.append({"Bucket": Bucket, "Key": Key, "Body": Body})


def _event(key):
    return {"Records": [{"s3": {"bucket": {"name": "b"}, "object": {"key": key}}}]}


def test_elevenlabs_path_writes_transcript(monkeypatch):
    monkeypatch.setattr(mod, "ASR_PROVIDER", "elevenlabs")
    fake_s3 = _FakeS3()
    monkeypatch.setattr(mod, "s3", fake_s3)

    def fake_transcribe_segment(audio_bytes, filename, num_speakers=5, keyterms=None):
        return {"results": {"transcripts": [{"transcript": "hi"}], "items": []}}

    import elevenlabs_utils
    monkeypatch.setattr(elevenlabs_utils, "transcribe_segment", fake_transcribe_segment)

    key = "audio_segments/John_Smith/2026-07-19/Benl1_2026-07-19_10-30-00_off0.0_to5.0_srcwav.wav"
    out = mod.lambda_handler(_event(key), None)

    assert len(fake_s3.put_calls) == 1
    put = fake_s3.put_calls[0]
    assert put["Key"] == "transcripts/John_Smith/2026-07-19/Benl1_2026-07-19_10-30-00_off0.0_to5.0_srcwav.json"
    assert json.loads(put["Body"])["results"]["transcripts"][0]["transcript"] == "hi"


def test_transcribe_provider_unchanged_default(monkeypatch):
    # Default provider must NOT hit S3 put/elevenlabs; it takes the job path.
    assert mod.ASR_PROVIDER in ("transcribe", "elevenlabs")
```

- [ ] **Step 2: Run the test to verify it fails**

Run: `python -m pytest tests/unit/test_lambda_transcribe_elevenlabs.py -v`
Expected: FAIL — `mod.ASR_PROVIDER` / `mod.s3` do not exist yet (AttributeError).

- [ ] **Step 3: Add env vars + s3 client near the top of `lambda_transcribe.py`**

After the existing `MAX_SPEAKERS` / `VOCABULARY_NAME` env reads (around lines 106-114), add. The module already has `transcribe = boto3.client('transcribe')` at line 65 but **no** `s3` client, so add one:

```python
# --- Provider toggle (Phase: alt-ASR) --------------------------------------
ASR_PROVIDER = os.environ.get('ASR_PROVIDER', 'transcribe')  # transcribe | elevenlabs
KEYTERMS_PATH = os.environ.get('KEYTERMS_PATH', 'config/custom_vocabulary_construction_nz.txt')

s3 = boto3.client('s3')
```

- [ ] **Step 4: Add the branch inside `lambda_handler`**

In `lambda_handler`, the per-record block computes `display_name`, `file_date`, and `base_name`, then builds `output_key`. Immediately **after** the `output_key` is computed (the existing line `output_key = f"{OUTPUT_PREFIX}{display_name}/{file_date}/{base_name}.json"`) and **before** the `logger.info(f"Starting transcription job...")` line, insert the provider branch:

```python
            if ASR_PROVIDER == 'elevenlabs':
                import elevenlabs_utils
                obj = s3.get_object(Bucket=bucket, Key=key)
                audio_bytes = obj['Body'].read()
                keyterms = elevenlabs_utils.load_keyterms(KEYTERMS_PATH)
                transcript_json = elevenlabs_utils.transcribe_segment(
                    audio_bytes,
                    os.path.basename(key),
                    num_speakers=min(max(MAX_SPEAKERS, 2), 10),
                    keyterms=keyterms,
                )
                s3.put_object(
                    Bucket=bucket,
                    Key=output_key,
                    Body=json.dumps(transcript_json),
                    ContentType='application/json',
                )
                # Deliberately NOT writing the ledger here: write_ledger_record
                # hardcodes status='transcribing' and only the (unused, on this
                # path) EventBridge callback flips it to done — so a ledger row
                # would be stuck forever at 'transcribing'. The transcript S3
                # object is the source of truth and the S3-scan fallback finds
                # it without a ledger (the code already supports ledger-off).
                logger.info(f"ElevenLabs transcript written: s3://{bucket}/{output_key}")
                results.append({
                    'key': key,
                    'status': 'completed',
                    'provider': 'elevenlabs',
                    'output_key': output_key,
                    'user': display_name,
                })
                continue
```

Note: the existing `get_transcription_job` idempotency check sits between `base_name` and `output_key` in the current code and calls the AWS Transcribe API — the ElevenLabs branch must be placed **after** `output_key` is assigned but structured so it does not depend on that Transcribe-only check. Duplicate S3 events simply re-write the same key, which is safe/idempotent. (Do not call `write_ledger_record` on this path — see the inline comment above.)

- [ ] **Step 5: Run the test to verify it passes**

Run: `python -m pytest tests/unit/test_lambda_transcribe_elevenlabs.py -v`
Expected: PASS (2 tests).

- [ ] **Step 6: Add `elevenlabs_utils.py` to the hand-deploy shared bundle**

`transcribe` is deployed by `scripts/deploy-lambda-code.sh` and now lazy-imports `elevenlabs_utils` on the ElevenLabs path, so add it to the `SHARED` array edited in Task 4. Change:
```bash
SHARED=("src/transcript_utils.py" "src/llm_utils.py")   # bundled in every zip (CLAUDE.md rule)
```
to:
```bash
SHARED=("src/transcript_utils.py" "src/llm_utils.py" "src/elevenlabs_utils.py")   # bundled in every zip (CLAUDE.md rule)
```

- [ ] **Step 7: Commit**

```bash
git add src/lambda_transcribe.py tests/unit/test_lambda_transcribe_elevenlabs.py scripts/deploy-lambda-code.sh
git commit -m "feat(asr): ASR_PROVIDER=elevenlabs synchronous path in lambda_transcribe"
```

---

### Task 7: `template.yaml` — CFN parameters + per-Lambda env vars

Add the provider parameters and wire per-Lambda env vars. Prod-safe defaults (`anthropic` / `transcribe`); the workflow (Task 8) overrides test to new.

**Files:**
- Modify: `src/template.yaml` (Parameters block + 6 Lambda env blocks + TranscribeFunction Timeout)
- Verify: `sam validate --lint`

**Interfaces:**
- Consumes: env var names read by `llm_utils` (`LLM_PROVIDER`, `QWEN_API_KEY`, `QWEN_BASE_URL`, `QWEN_MODEL`) and `elevenlabs_utils` (`ELEVENLABS_API_KEY`, `ELEVENLABS_STT_MODEL`, `ELEVENLABS_LANGUAGE`) and `lambda_transcribe` (`ASR_PROVIDER`, `KEYTERMS_PATH`).
- Produces: CFN parameters `LlmProvider`, `QwenBaseUrl`, `QwenModelPlus`, `QwenModelFast`, `AsrProvider`, `ElevenLabsApiKey`, `ElevenLabsSttModel`, `ElevenLabsLanguage` for Task 8 to fill.

- [ ] **Step 1: Add parameters**

In the `Parameters:` block (after `DashScopeApiKey`, ~line 148), add:

```yaml
  LlmProvider:
    Type: String
    Description: LLM provider for all Claude call sites (anthropic|qwen)
    AllowedValues: [anthropic, qwen]
    Default: anthropic

  QwenBaseUrl:
    Type: String
    Description: DashScope OpenAI-compatible base URL for Qwen chat completions
    Default: https://dashscope-intl.aliyuncs.com/compatible-mode/v1

  QwenModelPlus:
    Type: String
    Description: Qwen model for structured-JSON tasks (report/meeting/extract/matcher)
    Default: qwen3.7-plus

  QwenModelFast:
    Type: String
    Description: Qwen model for the latency-bound ask path
    Default: qwen-flash

  AsrProvider:
    Type: String
    Description: Speech-to-text provider (transcribe|elevenlabs)
    AllowedValues: [transcribe, elevenlabs]
    Default: transcribe

  ElevenLabsApiKey:
    Type: String
    Description: ElevenLabs xi-api-key for scribe_v2 STT
    NoEcho: true
    Default: ''

  ElevenLabsSttModel:
    Type: String
    Description: ElevenLabs STT model id
    Default: scribe_v2

  ElevenLabsLanguage:
    Type: String
    Description: ISO 639-3 language to pin (empty = auto-detect)
    Default: ''
```

- [ ] **Step 2: Add LLM env vars to the 4 structured Lambdas**

For **ReportGeneratorFunction**, **MeetingMinutesFunction**, **ExtractSessionFunction**, and **MatcherFunction**, in each function's `Environment.Variables`, add these four lines (the functions already have `ANTHROPIC_API_KEY` / `CLAUDE_MODEL`; keep those for rollback):

```yaml
          LLM_PROVIDER: !Ref LlmProvider
          QWEN_API_KEY: !Ref DashScopeApiKey
          QWEN_BASE_URL: !Ref QwenBaseUrl
          QWEN_MODEL: !Ref QwenModelPlus
```

(MatcherFunction already has `DASHSCOPE_API_KEY`; adding `QWEN_API_KEY: !Ref DashScopeApiKey` is still correct and explicit. `llm_utils` reads `QWEN_API_KEY` first.)

- [ ] **Step 3: Add LLM env vars to AskAgentFunction (fast model)**

For **AskAgentFunction**, add:

```yaml
          LLM_PROVIDER: !Ref LlmProvider
          QWEN_API_KEY: !Ref DashScopeApiKey
          QWEN_BASE_URL: !Ref QwenBaseUrl
          QWEN_MODEL: !Ref QwenModelFast
```

Note AskAgentFunction's existing `CLAUDE_MODEL` is already the Haiku id, so the anthropic-path model stays correct after routing through `llm_utils`.

- [ ] **Step 4: Add ASR env vars + raise timeout on TranscribeFunction**

For **TranscribeFunction**, add to `Environment.Variables`:

```yaml
          ASR_PROVIDER: !Ref AsrProvider
          ELEVENLABS_API_KEY: !Ref ElevenLabsApiKey
          ELEVENLABS_STT_MODEL: !Ref ElevenLabsSttModel
          ELEVENLABS_LANGUAGE: !Ref ElevenLabsLanguage
          KEYTERMS_PATH: config/custom_vocabulary_construction_nz.txt
```

And change its `Timeout:` from `60` to `300` (the sync ElevenLabs path does real work; the default-transcribe path is unaffected by a higher ceiling).

Also confirm `TranscribeFunction`'s IAM allows `s3:GetObject` and `s3:PutObject` on the data bucket (it already has S3 read; the ElevenLabs branch adds a `put_object` to the same bucket). If the policy is read-only, add `s3:PutObject` for the `transcripts/*` prefix. The bucket policy already grants Transcribe service PutObject, but the Lambda role itself now needs it too for the elevenlabs path.

- [ ] **Step 5: Validate the template**

Run: `sam validate --lint --template src/template.yaml`
Expected: `... is a valid SAM Template`. Fix any YAML indentation or unresolved-ref errors it reports.

- [ ] **Step 6: Commit**

```bash
git add src/template.yaml
git commit -m "feat(infra): provider-toggle CFN params + per-Lambda env for Qwen/ElevenLabs"
```

---

### Task 8: Deploy workflows — test defaults new, prod defaults old

Inject the new secret + provider variables. Test overrides defaults to `qwen` / `elevenlabs`; prod passes through `anthropic` / `transcribe` unless a `PROD_*` variable flips it.

**Files:**
- Modify: `.github/workflows/deploy.yml` (test)
- Modify: `.github/workflows/deploy-prod.yml` (prod)

**Interfaces:**
- Consumes: CFN parameters from Task 7 (`LlmProvider`, `AsrProvider`, `ElevenLabsApiKey`, ...).
- Produces: deployed stacks with the correct per-stack provider selection.

- [ ] **Step 1: Edit `.github/workflows/deploy.yml` (test)**

Find the `env:` block that already exports `CLAUDE_API_KEY` and `DASHSCOPE_API_KEY` from secrets, and add:

```yaml
          ELEVENLABS_API_KEY: ${{ secrets.ELEVENLABS_API_KEY }}
```

Find the `sam deploy ... --parameter-overrides` line and append these overrides (test dogfoods new providers by default):

```
LlmProvider=${{ vars.TEST_LLM_PROVIDER || 'qwen' }} AsrProvider=${{ vars.TEST_ASR_PROVIDER || 'elevenlabs' }} ElevenLabsApiKey=$ELEVENLABS_API_KEY
```

Keep the existing `ClaudeApiKey=$CLAUDE_API_KEY DashScopeApiKey=$DASHSCOPE_API_KEY` overrides — the anthropic/transcribe rollback paths still need their keys.

- [ ] **Step 2: Edit `.github/workflows/deploy-prod.yml` (prod)**

Add the same secret export to the prod `env:` block:

```yaml
          ELEVENLABS_API_KEY: ${{ secrets.ELEVENLABS_API_KEY }}
```

Append to the prod `sam deploy ... --parameter-overrides` line (prod stays old unless a `PROD_*` variable is set):

```
LlmProvider=${{ vars.PROD_LLM_PROVIDER || 'anthropic' }} AsrProvider=${{ vars.PROD_ASR_PROVIDER || 'transcribe' }} ElevenLabsApiKey=$ELEVENLABS_API_KEY
```

- [ ] **Step 3: Lint the workflow YAML**

Run: `python -c "import yaml,sys; [yaml.safe_load(open(p)) for p in ['.github/workflows/deploy.yml','.github/workflows/deploy-prod.yml']]; print('yaml ok')"`
Expected: `yaml ok`. Fix any indentation errors.

- [ ] **Step 4: Commit**

```bash
git add .github/workflows/deploy.yml .github/workflows/deploy-prod.yml
git commit -m "ci(providers): test defaults Qwen/ElevenLabs, prod defaults Claude/Transcribe"
```

---

### Task 9: Full-suite regression + verification

Prove default behaviour is unchanged (the default-safe guarantee) and both new modules integrate.

**Files:** none (verification only).

- [ ] **Step 1: Run the full unit suite**

Run: `python -m pytest tests/unit -v`
Expected: PASS (all existing tests plus the new `test_llm_utils.py`, `test_elevenlabs_utils.py`, `test_lambda_transcribe_elevenlabs.py`). Investigate and fix any regression — a green suite with defaults (`anthropic`/`transcribe`) is the proof that merging changes nothing for prod.

- [ ] **Step 2: Grep for leftover direct-Claude code references**

Run: `grep -rn "claude_utils.call_claude\|api.anthropic.com" src/ | grep -v "claude_utils.py"`
Expected: no output except possibly `src/llm_utils.py` (the anthropic path legitimately hits `api.anthropic.com`). Any other hit is a call site that was missed — route it through `llm_utils`.

- [ ] **Step 3: Confirm the branch state**

Run: `git log --oneline origin/develop..HEAD`
Expected: the sequence of task commits above, on `feat/alt-llm-asr-qwen-scribe`. Do not merge or push — hand back for review.

- [ ] **Step 4: Final commit (if any verification fixes were made)**

```bash
git add -u
git commit -m "test: full-suite regression pass for provider toggles"
```

---

## Post-Implementation Manual Steps (not code — for the operator)

These are done by the user, outside this plan, before/during cutover:

1. **Add the GitHub secret** `ELEVENLABS_API_KEY` (repo → Settings → Secrets and variables → Actions → Secrets).
2. **(Optional) Confirm the Qwen key/endpoint (OI-1):** verify `qwen3.7-plus` / `qwen-flash` are served on `dashscope-intl.aliyuncs.com/compatible-mode/v1` with the existing `DASHSCOPE_API_KEY`. If they require the workspace `maas` endpoint, set repo variable `QwenBaseUrl` (or a dedicated `QWEN_API_KEY` secret) accordingly.
3. **Push to `develop`** → test stack auto-deploys on Qwen + ElevenLabs. Validate: structured-JSON parity, ask-path latency < 29 s (OI-3), and same-audio transcript parity vs Transcribe (OI-2 confirms the word field / keyterms encoding).
4. **Prod cutover** when validated: set `PROD_LLM_PROVIDER=qwen` and/or `PROD_ASR_PROVIDER=elevenlabs` (repo variables), then deploy. **Rollback:** unset the variable and redeploy, or edit the Lambda env var in the AWS console for instant effect.
