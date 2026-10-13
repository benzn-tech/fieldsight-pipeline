# Lambda Timeout Tiers and Report Budget Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Lambda timeouts follow one rule set by how each function is invoked, the on-demand report worker gets the full 900 s Lambda ceiling with its coupled budgets moved in step, and the 500-object window cap becomes an env-configurable 2000 — all guarded by a template invariant test so the documented timeout incidents cannot recur.

**Architecture:** `src/template.yaml` is the single source of every `Timeout:` literal (no workflow overrides them). A new unit test parses the template text (same regex approach as `tests/unit/test_template_workflow_parameter_wiring.py`, no YAML loader — CloudFormation tags break one) and pins invariants rather than numbers: a model HTTP timeout below its function timeout, API-Gateway handlers at or under 30 s, a scheduled function shorter than its schedule interval, sync callees no longer than their callers. Then the report worker and the tier table are changed under that guard.

**Tech Stack:** AWS SAM `src/template.yaml`, Python 3.12, pytest.

**Spec:** No spec document. Owner decisions from the 2026-09-17/18 session: "Timeout: 300 可以调高…统一" and "按你的改法" (tiers: sync behind API Gateway ≤ 30 s; async heavy 900 s; light async 120 s; SessionReportFunction 300→900 with GENERATION_BUDGET_SECONDS 210→600 and LLM_HTTP_TIMEOUT 120→300; MAX_TRANSCRIPT_OBJECTS 500→2000 and env-configurable); "key 轮换放进 plan，但今天不解决". Per-function evidence (triggers, schedules, sync chains, coupled env vars — read before Task 1): `C:/Users/camil/AppData/Local/Temp/claude/C--Users-camil-Dropbox/ae81ae99-4b24-47e8-a14b-4a8f50179b99/scratchpad/plan-research/lambda-timeout-tiers.md`

## Global Constraints

- AWS Lambda maximum Timeout is 900 s. API Gateway (REST and WebSocket) integration timeout is 29 s — a handler above 30 s buys nothing.
- Every `LLM_HTTP_TIMEOUT` must stay strictly below its function's `Timeout`. Three incidents are documented in the template comments (ExtractSession: 664 invocations killed; SessionFinalize: three hard-kills, no confirmation email; MeetingMinutes).
- `FinalizeSweepFunction` runs `rate(1 minute)` with no reserved concurrency. Its Timeout stays **120** — never raised (overlap stacking + the Aurora auto-pause invariant pinned by `tests/unit/test_sweep_cadence_vs_autopause.py`).
- A sync callee's Timeout never exceeds its caller's.
- No workflow passes a Timeout override (verified: only BatchWindowSec/BatchMaxChunks/BatchSealDeadlineSec are overridden), so editing the template literal is sufficient. Do NOT touch `.github/workflows/*`.
- Do not change batching parameters (`BatchWindowSec`, `BatchMaxChunks`, `BatchSealDeadlineSec`) — the owner has not decided that item.
- Development artefacts in English. Commit messages end with:
  ```
  Co-Authored-By: Claude Opus 5 (1M context) <noreply@anthropic.com>
  Claude-Session: https://claude.ai/code/session_01N2ZDWwHWVd7mH4NNKX1kjt
  ```
- Test harness (worktree root `C:/Users/camil/fswork/lambda-timeout-tiers`, Git Bash):
  ```bash
  export UV_LINK_MODE=copy AWS_ACCESS_KEY_ID=testing AWS_SECRET_ACCESS_KEY=testing AWS_DEFAULT_REGION=ap-southeast-2
  uv run --with pytest --with boto3 --with "psycopg[binary]" --with urllib3 --with numpy pytest <paths> -q
  ```
- Windows repo: never `git add -A`; add files by path. Edit the template with single-line anchored replacements, never a whole-file rewrite.

## Rulings made while planning (the owner should see these)

- **AskAgentFunction stays at Timeout 60 / LLM_HTTP_TIMEOUT 45.** The tier rule would cap it at 30, but it is invoked synchronously by ApiFunction (30 s) behind API Gateway's 29 s wall, so the user-visible ceiling is already 29 s. Lowering it changes nothing a person sees and needs LLM_HTTP_TIMEOUT dropped below 30 on the one path where a person waits for an answer. Cost if wrong: some doomed Ask invocations keep running past the gateway cut-off.
- **Functions already at or under their tier's value are not lowered** (e.g. WebSocket connect 15, authorizer 10). The tier is a ceiling for sync handlers and a floor for async work, not a number to force.
- **DeviceLedgerFunction stays 60** (sync callee of DeviceReportFunction at 120 — raising it to the tier's 120 would sit exactly at the caller's limit).

## File Structure

- Create `tests/unit/test_template_timeout_invariants.py` — parses template function blocks; pins the invariants.
- Modify `src/transcript_window.py:27` — `MAX_TRANSCRIPT_OBJECTS` read from env, default 2000.
- Modify `tests/unit/test_transcript_window.py` — env-read tests.
- Modify `src/lambda_session_report.py` (≈ line 241–249) — budget default 600 and its comment.
- Modify `src/template.yaml` — SessionReportFunction env and Timeout; the tier table in Task 3.

---

### Task 1: Template timeout invariant test

**Files:**
- Create: `tests/unit/test_template_timeout_invariants.py`

**Interfaces:**
- Produces: `_functions() -> dict[str, dict]` inside the test module, each value `{"timeout": int, "llm_http_timeout": int|None, "budget": int|None, "rates_sec": list[int], "api": bool}`. Later tasks run this test file; they do not import from it.

- [ ] **Step 1: Write the test**

Create `tests/unit/test_template_timeout_invariants.py`:

```python
"""Unit: a Lambda timeout is a promise about everything that runs inside it.

This template has shipped the same failure three times -- a model HTTP timeout at or
above its function's Timeout, so the runtime SIGKILLs the function before urllib3 can
raise and no result is written (ExtractSession: 664 invocations in one afternoon;
SessionFinalize: three hard-kills and no confirmation email; MeetingMinutes). The
numbers below are not pinned; the relationships are:

1. No Timeout exceeds the Lambda maximum (900 s).
2. LLM_HTTP_TIMEOUT and GENERATION_BUDGET_SECONDS are strictly below Timeout.
3. A handler behind API Gateway is at or under 30 s: the gateway cuts at 29 s.
4. A function on a rate() schedule is shorter than its interval -- otherwise runs
   stack -- unless listed in OVERLAP_ACCEPTED with its exact value and a reason.
5. A function invoked synchronously by another is no longer than its caller.

Text parsing, not a YAML loader: CloudFormation tags (!Ref, !Sub) break safe_load,
and tests/unit/test_template_workflow_parameter_wiring.py already reads this file
this way.
"""
import os
import re

REPO = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
TEMPLATE = os.path.join(REPO, "src", "template.yaml")

LAMBDA_MAX_SECONDS = 900
API_GATEWAY_HANDLER_MAX_SECONDS = 30

# rate()-scheduled functions allowed to outlive their interval. Pinned to the exact
# value so a raise still fails.
OVERLAP_ACCEPTED = {
    # rate(1 minute), no reserved concurrency. Sessions are CAS-claimed so an overlap
    # cannot double-finalize, but every extra copy holds an Aurora connection and eats
    # account concurrency; raising it breaks the auto-pause cadence invariant
    # (tests/unit/test_sweep_cadence_vs_autopause.py).
    "FinalizeSweepFunction": 120,
}

# (caller, callee) pairs invoked with InvocationType=RequestResponse -- the caller
# waits, so the callee can never usefully outlive it. Evidence per pair is cited in
# the plan research (lambda-timeout-tiers.md, "Sync callee chains").
SYNC_CALLEES = [
    ("AskAgentFunction", "RagSearchFunction"),
    ("WsSendVoiceFunction", "VoiceResolveFunction"),
    ("SpeakerEmbedFunction", "VoiceprintWriterFunction"),
    ("MatcherFunction", "SuggestionWriterFunction"),
    ("DeviceReportFunction", "DeviceLedgerFunction"),
]

_RATE_UNIT_SECONDS = {"minute": 60, "minutes": 60, "hour": 3600, "hours": 3600,
                      "day": 86400, "days": 86400}


def _global_timeout(text):
    g = re.search(r"(?ms)^Globals:\n(.*?)(?=^\S)", text)
    m = re.search(r"(?m)^    Timeout:\s*(\d+)", g.group(1)) if g else None
    return int(m.group(1)) if m else 3


def _functions():
    text = open(TEMPLATE, encoding="utf-8").read()
    default_timeout = _global_timeout(text)
    resources = re.search(r"(?ms)^Resources:\n(.*?)(?=^\S|\Z)", text).group(1)
    blocks = re.split(r"(?m)^  (?=[A-Za-z0-9]+:\s*$)", resources)
    out = {}
    for block in blocks:
        head = re.match(r"([A-Za-z0-9]+):\s*\n(?:\s*#.*\n)*    Type:\s*AWS::Serverless::Function\b",
                        block)
        if not head:
            continue
        timeout = re.search(r"(?m)^      Timeout:\s*(\d+)", block)
        llm = re.search(r"(?m)^\s+LLM_HTTP_TIMEOUT:\s*'?(\d+)'?\s*$", block)
        budget = re.search(r"(?m)^\s+GENERATION_BUDGET_SECONDS:\s*'?(\d+)'?\s*$", block)
        rates = [int(n) * _RATE_UNIT_SECONDS[u] for n, u in re.findall(
            r"(?m)^\s+Schedule:\s*'?rate\((\d+)\s+(minutes?|hours?|days?)\)'?\s*$", block)]
        api = bool(re.search(r"(?m)^\s+Type:\s*(Api|HttpApi)\s*$", block))
        out[head.group(1)] = {
            "timeout": int(timeout.group(1)) if timeout else default_timeout,
            "llm_http_timeout": int(llm.group(1)) if llm else None,
            "budget": int(budget.group(1)) if budget else None,
            "rates_sec": rates,
            "api": api,
        }
    return out


def test_the_parser_sees_the_whole_template():
    # A parser that silently matches nothing passes every invariant below.
    fns = _functions()
    assert len(fns) >= 40, sorted(fns)
    for name in ("SessionReportFunction", "FinalizeSweepFunction", "OrgApiFunction",
                 "AskAgentFunction", "ExtractSessionFunction"):
        assert name in fns
    assert fns["OrgApiFunction"]["api"] is True
    assert fns["FinalizeSweepFunction"]["rates_sec"] == [60]
    assert fns["ExtractSessionFunction"]["llm_http_timeout"] is not None


def test_no_timeout_exceeds_the_lambda_maximum():
    over = {n: f["timeout"] for n, f in _functions().items()
            if f["timeout"] > LAMBDA_MAX_SECONDS}
    assert over == {}


def test_a_model_http_timeout_is_below_its_function_timeout():
    bad = {n: (f["llm_http_timeout"], f["timeout"]) for n, f in _functions().items()
           if f["llm_http_timeout"] is not None and f["llm_http_timeout"] >= f["timeout"]}
    assert bad == {}


def test_a_generation_budget_is_below_its_function_timeout():
    bad = {n: (f["budget"], f["timeout"]) for n, f in _functions().items()
           if f["budget"] is not None and f["budget"] >= f["timeout"]}
    assert bad == {}


def test_an_api_gateway_handler_is_at_or_under_thirty_seconds():
    bad = {n: f["timeout"] for n, f in _functions().items()
           if f["api"] and f["timeout"] > API_GATEWAY_HANDLER_MAX_SECONDS}
    assert bad == {}


def test_a_scheduled_function_is_shorter_than_its_interval():
    fns = _functions()
    bad = {}
    for name, f in fns.items():
        for interval in f["rates_sec"]:
            if name in OVERLAP_ACCEPTED:
                if f["timeout"] != OVERLAP_ACCEPTED[name]:
                    bad[name] = ("accepted value changed", f["timeout"],
                                 OVERLAP_ACCEPTED[name])
            elif f["timeout"] >= interval:
                bad[name] = (f["timeout"], interval)
    assert bad == {}


def test_a_sync_callee_is_no_longer_than_its_caller():
    fns = _functions()
    bad = {}
    for caller, callee in SYNC_CALLEES:
        assert caller in fns and callee in fns, (caller, callee)
        if fns[callee]["timeout"] > fns[caller]["timeout"]:
            bad[(caller, callee)] = (fns[caller]["timeout"], fns[callee]["timeout"])
    assert bad == {}
```

- [ ] **Step 2: Run it against the unchanged template**

Run: `uv run --with pytest --with boto3 --with "psycopg[binary]" --with urllib3 --with numpy pytest tests/unit/test_template_timeout_invariants.py -q`
Expected: `7 passed`. If `test_the_parser_sees_the_whole_template` fails, fix the parser (not the assertions) — read the template around the failing resource to see its real indentation. If an invariant test fails on the unchanged template, STOP and report it with the offending function and values: that is a live defect the owner must see, not something to allowlist.

- [ ] **Step 3: Prove each invariant can go red**

One at a time, make the edit, run Step 2's command, confirm the named test FAILS, then revert with `git checkout -- src/template.yaml`:
1. FinalizeSweepFunction `Timeout: 120` → `Timeout: 900` → `test_a_scheduled_function_is_shorter_than_its_interval` and nothing else fails... (`test_no_timeout_exceeds_the_lambda_maximum` passes at 900).
2. SessionReportFunction `LLM_HTTP_TIMEOUT: '120'` → `'300'` (Timeout still 300) → `test_a_model_http_timeout_is_below_its_function_timeout`.
3. OrgApiFunction `Timeout: 30` → `Timeout: 60` → `test_an_api_gateway_handler_is_at_or_under_thirty_seconds`.
4. VoiceResolveFunction `Timeout: 30` → `Timeout: 60` → `test_a_sync_callee_is_no_longer_than_its_caller`.
Record each observed failure line in your report. `git diff --stat src/template.yaml` must be empty afterwards.

- [ ] **Step 4: Commit**

```bash
git add tests/unit/test_template_timeout_invariants.py
git commit -m "Pin what a Lambda timeout promises, not the number

The template has shipped a model HTTP timeout at or above its function's Timeout three
times; each time the runtime killed the function before the client could raise and no
result was written. Pin the relationships instead of the values: HTTP timeout and
generation budget below Timeout, API Gateway handlers at or under 30 s, scheduled runs
shorter than their interval (FinalizeSweep accepted at exactly 120), sync callees no
longer than their callers.

Co-Authored-By: Claude Opus 5 (1M context) <noreply@anthropic.com>
Claude-Session: https://claude.ai/code/session_01N2ZDWwHWVd7mH4NNKX1kjt"
```

---

### Task 2: Report worker — 900 s, coupled budgets, 2000-object cap from env

**Files:**
- Modify: `src/transcript_window.py:22-27`
- Modify: `tests/unit/test_transcript_window.py`
- Modify: `src/lambda_session_report.py` (the `GENERATION_BUDGET_SECONDS` block, ≈ lines 241–249)
- Modify: `src/template.yaml` — SessionReportFunction (`Timeout:` ≈ line 1736; env ≈ lines 1763–1765)

**Interfaces:**
- Consumes: Task 1's test file (must stay green).
- Produces: `transcript_window.MAX_TRANSCRIPT_OBJECTS` — still a module-level `int`, now `int(os.environ.get("MAX_TRANSCRIPT_OBJECTS", "2000"))`.

- [ ] **Step 1: Write the failing tests**

Append to `tests/unit/test_transcript_window.py`:

```python
# ----------------------------------------------------------
# The object cap was a hardcoded 500 that no measurement supports. It is now read
# from the environment (default 2000) so it can move without a code change. Kept
# module-level: the cap tests above read transcript_window.MAX_TRANSCRIPT_OBJECTS
# as the effective limit.
# ----------------------------------------------------------

import importlib


def test_the_object_cap_defaults_to_two_thousand(monkeypatch):
    monkeypatch.delenv("MAX_TRANSCRIPT_OBJECTS", raising=False)
    try:
        assert importlib.reload(transcript_window).MAX_TRANSCRIPT_OBJECTS == 2000
    finally:
        importlib.reload(transcript_window)


def test_the_object_cap_is_read_from_the_environment(monkeypatch):
    monkeypatch.setenv("MAX_TRANSCRIPT_OBJECTS", "7")
    try:
        assert importlib.reload(transcript_window).MAX_TRANSCRIPT_OBJECTS == 7
    finally:
        monkeypatch.delenv("MAX_TRANSCRIPT_OBJECTS")
        importlib.reload(transcript_window)
```

- [ ] **Step 2: Run to verify they fail**

Run: `uv run --with pytest --with boto3 --with "psycopg[binary]" --with urllib3 --with numpy pytest tests/unit/test_transcript_window.py -q -k object_cap`
Expected: both FAIL (500 ≠ 2000; env ignored).

- [ ] **Step 3: Implement the cap**

In `src/transcript_window.py`, add `import os` to the import block (alphabetical, after `import logging`), and replace the comment + constant with:

```python
# Past this many transcript objects, the read phase (S3 list + get + normalize)
# can itself outlast whatever time the caller has left, independent of how the
# model call is bounded -- a request this large is refused before the first
# read, not discovered mid-read. Named so the error can point at it.
#
# It is a pre-check, not the guard: assemble() also checks its deadline between
# objects. The first value, 500, had no measurement behind it and refused an owner's
# ordinary day (1267 objects) in six seconds; the busiest prod day measured has 295.
# 2000 keeps the pre-check for a structurally broken day while the Timeout (900 s)
# and the per-object deadline carry the real bound. Env-read so it moves without a
# code change.
MAX_TRANSCRIPT_OBJECTS = int(os.environ.get("MAX_TRANSCRIPT_OBJECTS", "2000"))
```

- [ ] **Step 4: Run the whole transcript_window test file**

Run: `uv run --with pytest --with boto3 --with "psycopg[binary]" --with urllib3 --with numpy pytest tests/unit/test_transcript_window.py -q`
Expected: all PASS (the existing cap tests build `MAX_TRANSCRIPT_OBJECTS + 1` keys, now 2001 — slower but correct).

- [ ] **Step 5: Move the worker's budgets**

In `src/lambda_session_report.py`, replace the comment block above `GENERATION_BUDGET_SECONDS` and the line itself with:

```python
# The function has Timeout: 900 and llm_utils retries up to four times at
# LLM_HTTP_TIMEOUT (300) each, so an unbounded ladder outlives the function and writes
# no result at all -- the poller then spins forever. Bound it well inside the timeout.
# Used only as a FALLBACK when the invocation carries no `context` (unit tests, or
# any caller that never got one) -- `context.get_remaining_time_in_millis()` is the
# authority whenever it is available (see `_model_budget_seconds`).
GENERATION_BUDGET_SECONDS = float(os.environ.get("GENERATION_BUDGET_SECONDS", "600"))
```

In `src/template.yaml`, SessionReportFunction only (verify each anchor is inside that resource block before editing):
- `      Timeout: 300` → `      Timeout: 900`
- `          # One attempt must fit inside Timeout: 300 with room for the docx render.` → `          # One attempt must fit inside Timeout: 900 with room for the docx render.`
- `          LLM_HTTP_TIMEOUT: '120'` → `          LLM_HTTP_TIMEOUT: '300'`
- `          GENERATION_BUDGET_SECONDS: '210'` → `          GENERATION_BUDGET_SECONDS: '600'`
- Add directly after the `GENERATION_BUDGET_SECONDS` line:
  ```yaml
          # Pre-check on transcript objects in one window (transcript_window.py).
          MAX_TRANSCRIPT_OBJECTS: '2000'
  ```

Then grep the SessionReportFunction block and `src/lambda_session_report.py` for any other prose `300`/`210`/`120` that describes these limits: `git grep -n "Timeout: 300\|210\b" src/lambda_session_report.py` and read lines 1725–1770 of the template. Update any stale prose; report what you changed.

- [ ] **Step 6: Run the worker, window and invariant tests**

Run: `uv run --with pytest --with boto3 --with "psycopg[binary]" --with urllib3 --with numpy pytest tests/unit/test_transcript_window.py tests/unit/test_session_report_generates.py tests/unit/test_template_timeout_invariants.py -q`
Also run every test file whose name contains `session_report`: `ls tests/unit | grep session_report`.
Expected: all PASS.

- [ ] **Step 7: Commit**

```bash
git add src/transcript_window.py tests/unit/test_transcript_window.py src/lambda_session_report.py src/template.yaml
git commit -m "Give the report worker the whole Lambda ceiling

The on-demand report worker ran at Timeout 300 while its sibling generators run at 600
and 900; a three-hour window already took 165 s. It is S3-triggered, so API Gateway's
29 s limit never applied -- 900 s was always reachable. The HTTP timeout (300) and the
fallback generation budget (600) move with it. The 500-object pre-check had no
measurement behind it and refused an ordinary day of 1267 objects; it is now read from
MAX_TRANSCRIPT_OBJECTS, default 2000.

Co-Authored-By: Claude Opus 5 (1M context) <noreply@anthropic.com>
Claude-Session: https://claude.ai/code/session_01N2ZDWwHWVd7mH4NNKX1kjt"
```

---

### Task 3: Retier the remaining functions

**Files:**
- Modify: `src/template.yaml` — `Timeout:` literals listed below, plus any prose comment inside the same resource block that states the old value.

**Interfaces:**
- Consumes: Task 1's invariant test (the guard for every edit here).

The table is the complete change set. Anything not listed is unchanged on purpose (see "Unchanged" below).

| Function | Timeout now → new | Why this tier | Coupled value that must stay below |
|---|---|---|---|
| TranscribeFunction | 300 → 900 | async (S3), ASR on batches | — |
| MeetingMinutesFunction | 600 → 900 | async, model + docx | LLM_HTTP_TIMEOUT 540 |
| ExtractSessionFunction | 600 → 900 | async (S3), model | LLM_HTTP_TIMEOUT 540 |
| RollingSummaryFunction | 300 → 900 | async (EventBridge), model; its own comment says 300 is tight on long days | LLM_HTTP_TIMEOUT 240 |
| SessionFinalizeFunction | 300 → 900 | async (S3), model | LLM_HTTP_TIMEOUT 240 |
| NonWorkExpiryFunction | 300 → 900 | hourly schedule (900 < 3600) | — |
| EmbedReportFunction | 300 → 900 | async (S3), embedding | — |
| KeyframeFunction | 600 → 900 | async (S3); its comment argues for headroom | — |
| SpeakerEmbedFunction | 600 → 900 | async (S3), ONNX; caller of VoiceprintWriter (120) | — |
| MatcherFunction | 600 → 900 | async (S3), model; caller of SuggestionWriter (120) | — |
| TranscribeCallbackFunction | 60 → 120 | light async (EventBridge) | — |
| SessionActivityFunction | 60 → 120 | light async (EventBridge) | — |
| RecordingSegmentsFunction | 60 → 120 | light async; `rate(5 minutes)` (120 < 300) | — |
| VoiceReaperFunction | 60 → 120 | light async; `rate(6 hours)` | — |
| VoiceAuditFunction | 30 → 120 | async-invoked (Event) by AskAgent — not caller-bound | — |

**Unchanged:** OrchestratorFunction, DownloaderFunction, VadFunction, ReportGeneratorFunction, IngestFunction (already 900). FinalizeSweepFunction (120, per-minute schedule). OrgApiFunction, ApiFunction, WsConnect/WsDisconnect (15), VoiceWsAuthorizer (10), WsSendVoice, VoiceResolve, RagSearch, VoiceFanout, FargateTrigger (≤ 30). AskAgentFunction (60 — see Rulings). DeviceLedgerFunction (60 — sync callee of a 120 caller). MigrateFunction, OrgSeedFunction, ItemWriterFunction, SuggestionWriterFunction, VoiceprintWriterFunction, DeviceReportFunction, ExtractionBacklogFunction (already 120).

- [ ] **Step 1: Apply the edits**

For each row, find the resource block (`git grep -n "^  <Name>:" src/template.yaml`), then change only the `      Timeout: <old>` line inside that block to the new value. Edit with a unique anchor per block — `Timeout: 300` occurs many times; include enough surrounding context (the preceding `FunctionName:` line) to make each replacement unique. After each edit read the block back and confirm the right resource changed. Within each changed block, search for prose that states the old number as a Timeout (e.g. `Timeout: 300`, `300 s`, `Timeout 600`) and update it.

- [ ] **Step 2: Run the invariant test**

Run: `uv run --with pytest --with boto3 --with "psycopg[binary]" --with urllib3 --with numpy pytest tests/unit/test_template_timeout_invariants.py -q`
Expected: `7 passed`.

- [ ] **Step 3: Verify the change set is exactly the table**

Run: `git diff -U0 src/template.yaml | grep -E "^[-+]\s+Timeout:"`
Expected: exactly 15 `-`/`+` pairs, values matching the table. Paste the output into your report. Any extra Timeout line changed is a defect — revert it.

- [ ] **Step 4: Run the template-related suites**

Run: `uv run --with pytest --with boto3 --with "psycopg[binary]" --with urllib3 --with numpy pytest tests/unit/test_template_workflow_parameter_wiring.py tests/unit/test_sweep_cadence_vs_autopause.py tests/unit/test_template_timeout_invariants.py -q`
Also run every test file matching `ls tests/unit | grep -i template`.
Expected: all PASS.

- [ ] **Step 5: Lint the template if cfn-lint is pinned in CI**

Read `.github/workflows/deploy.yml` for the cfn-lint step and its pinned version. Run the same pinned version locally against `src/template.yaml` (e.g. `uvx cfn-lint==<pinned> src/template.yaml`). Expected: no new errors relative to `origin/develop` (run it on both and compare if the baseline is not clean).

- [ ] **Step 6: Commit**

```bash
git add src/template.yaml
git commit -m "Set Lambda timeouts by how each function is invoked

Async model, docx, ASR and embedding work gets the 900 s ceiling; light async writers
get 120 s; API Gateway handlers stay at or under 30 s. Unchanged on purpose:
FinalizeSweep (per-minute schedule), AskAgent (already bounded by the gateway's 29 s),
and every sync callee that would otherwise reach its caller's limit. Guarded by
test_template_timeout_invariants.

Co-Authored-By: Claude Opus 5 (1M context) <noreply@anthropic.com>
Claude-Session: https://claude.ai/code/session_01N2ZDWwHWVd7mH4NNKX1kjt"
```

---

### Task 4: Full suite, push, PR, TEST verification

- [ ] **Step 1: Full unit suite**

Run: `uv run --with pytest --with boto3 --with "psycopg[binary]" --with urllib3 --with numpy pytest tests/unit -q`
Expected: all pass except known pre-existing skips. Record counts.

- [ ] **Step 2: Push and open the PR (controller decision point)**

```bash
git push -u origin chore/lambda-timeout-tiers
gh pr create --base develop --title "Set Lambda timeouts by how each function is invoked" --body-file <body file>
```
Body: the tier rule, the full change table, the Rulings section above verbatim, the invariant test and its four red-proofs, and the Deferred section below. End with:
```
🤖 Generated with [Claude Code](https://claude.com/claude-code)

https://claude.ai/code/session_01N2ZDWwHWVd7mH4NNKX1kjt
```
Merge into `develop` (deploys TEST) is the owner's call.

- [ ] **Step 3: TEST verification after the owner merges and the deploy succeeds**

For every function in Task 2 and Task 3, read the deployed value:
`aws lambda get-function-configuration --function-name fieldsight-test-<suffix> --query "[Timeout,Environment.Variables.LLM_HTTP_TIMEOUT,Environment.Variables.GENERATION_BUDGET_SECONDS,Environment.Variables.MAX_TRANSCRIPT_OBJECTS]" --profile fieldsight-deployer --region ap-southeast-2`
(the suffix is the resource's `FunctionName` pattern in the template). Record a table of expected vs deployed. Print only these four fields — never the whole environment: it holds API keys in plain text.

Then re-run the owner's all-day report on TEST (the request that failed with `window too large: 1267 transcript objects exceeds the 500-object cap`) and record the worker's `REPORT` line duration and whether a document was written.

---

## Deferred (recorded, not done in this plan)

1. **API key rotation and moving secrets out of Lambda environment variables — owner: "放进 plan 里面，但我今天不解决".** On 2026-09-18 a read of `fieldsight-test-session-report`'s configuration printed the OpenRouter (`sk-or-v1-…`) and Anthropic (`sk-ant-api03-…`) keys in plain text; CloudFormation `NoEcho` parameters still materialise as plaintext env. Anyone with `lambda:GetFunctionConfiguration` can read them. Steps when scheduled: (a) owner rotates the OpenRouter, Anthropic, ElevenLabs and DashScope keys at each vendor (a prod write — the owner performs it); (b) store each in Secrets Manager (or SSM SecureString) per stage; (c) add a cached `get_secret(name)` helper read at cold start, and swap every `os.environ["*_API_KEY"]` read to it; (d) grant `secretsmanager:GetSecretValue` per function and add the IAM to the deploy role `github-actions-fieldsight-deploy` for any new resource type; (e) remove the key parameters from `template.yaml` env and from both workflows' `--parameter-overrides`; (f) confirm with `get-function-configuration` that no key remains in any function's environment.
2. **ASR batch window 120 s → 180 s ("6 per batch")** — awaiting the owner's answer on whether this is the batching window (adds ~60 s before transcription starts and presses the stop-email 3-minute budget) or report segment-and-merge generation. `BatchWindowSec` is overridden by both workflows through repo variables `TEST_BATCH_WINDOW_SEC` / `PROD_BATCH_WINDOW_SEC`; the template default alone would not change it.
3. **Legacy gateway audio listing reads only the first page** (`lambda_fieldsight_api.get_audio_segments` calls `list_objects_v2` once, max 1000 keys; prod `Ben_UCPK2/2026-09-10` holds 3319 objects under that prefix). Out of scope here; affects sites whose frontend runs the non-Aurora timeline source.
4. **Missing character cap on the report worker's prompt** (`lambda_session_report` joins every turn into one prompt; `promptChars` is logged but never bounded). Measured: the busiest prod day is ~504,000 characters (≈126–144 K tokens), well inside the model's context, so this is not blocking.
