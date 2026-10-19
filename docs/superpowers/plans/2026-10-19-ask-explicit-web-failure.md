# Ask explicit web failure Implementation Plan

> **For agentic workers:** executed natively (two small tasks, one file each).

**Goal:** when the caller explicitly asks for the web and the web step fails, say so instead of "not in your records".

**Spec:** `docs/superpowers/specs/2026-10-19-ask-explicit-web-failure-design.md` (rev 2, Fable-reviewed).

## Global Constraints
- Model-free: no prompt change; the text is fixed (`WEB_UNREACHABLE`, `WEB_TRY_AGAIN`).
- Fires only on `pol == "required"` with a `web_search` envelope whose `meta.status == "failed"`.
- No question or answer text in `ASK_ROUTE`; only `web_failed` = `upstream` | `timeout`.

## Review Focus
1. A records answer must keep its cards (screen) behind the prefix.
2. Voice must stay speakable: prefix + records, then prefix + first two sentences, then the fixed sentences.
3. `_fallback`'s error exit (`+no_synthesis`) must stay an error.
4. `web_late` (no `block`) must fire like a failed block.
5. `auto` / `offer` / `never`, refused and draft-only `unverified` must be unchanged.

### Task 1: backend (`src/ask_orchestrator.py`)
- Constants beside `NO_RECORDS`; `_web_failed_answer(t, text, has_records)`, `_web_failed_extra(t)`.
- Carrier `t.web_failed` set where the round-1 `web_env` is read; `route["web_failed"]`.
- Main path after the offer branch; `_fallback` synthesis and NO_RECORDS exits.
- Tests: `tests/unit/test_ask_orchestrator_web_failure.py` (15 cases, spec §5); 9 fail on the unfixed code.

### Task 2: UI (`fieldsight-ui` `scripts/composites/ask-chat.js`)
- `wantsCorrob` gains `&& !(res.web && typeof res.web.status === 'string')`.
- Test (`node --test`): a result with `web.status` and `from_web: false` requests no corroboration; one without `web` still does.
