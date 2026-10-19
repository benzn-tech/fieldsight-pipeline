# Ask: say so when an explicit web search fails (design)

**Date:** 2026-10-19. **Status:** rev 2, after Fable review of rev 1 (APPROVE WITH CHANGES; §6 maps each finding).
**Line:** ASR+LLM优化v3 (orchestrator). **Files:** `src/ask_orchestrator.py` and its tests; fieldsight-ui `scripts/composites/ask-chat.js` (one condition, §3).

## 1. What happened

The open item "one 'Search the web …' voice turn did not route to the web" (voice web eval run 2, 2026-10-14) is not a routing miss. The eval rows show `intent=True` on both web=False turns:

- "Search online: what is the minimum fall for a flat roof membrane in New Zealand?" → spoken "The provided site reports do not contain information on the minimum fall…" (13.9 s);
- "Search the web for the minimum clearance between a timber floor and the ground under NZS 3604" → web=False (12.8 s).

TEST worker logs for 2026-10-14 (79 web-step lines) show the cause is upstream:

| Line | Count |
|---|---|
| `verified against N sources` | 47 |
| `verify attempt 1 failed: HTTP 504: The operation was aborted` | 12 |
| `verify attempt 2 failed: HTTP 504 …` | 7 |
| `draft failed: ReadTimeoutError` | 6 |
| `ask compose failed: ReadTimeoutError` | 5 |

So when the caller explicitly asks for the web and the web step fails, the caller is told their **records** don't cover it. The answer is about the wrong thing, and it is spoken as if the search had happened.

## 2. The same case on the orchestrator (TEST today, prod at the release)

`policy()` returns `required` for an explicit request. When `web_answer.general_answer` returns no answer at all (the draft failed or timed out), `web_tool` returns an envelope with no items and `meta.status = "failed"`, `meta.error = "upstream" | "timeout"`. Then:

- the judge sees `[DATA web_search … — error: upstream]` and no rule tells it what to say, so it can (and on the legacy path did) answer "records don't contain";
- `t.web_env` is `None` (no items), so the result carries no `web` block, and the screen shows no web status line either.

When the draft succeeded but verification failed, the envelope has W1 (status `unverified`) and the screen already says "Model knowledge — not verified online". That case is fine and is not changed.

## 3. Rule

**When it fires.** `pol == "required"` (the caller explicitly asked for the web) and the turn's `web_search` envelope has `meta.status == "failed"`. That covers all three shapes:
- the web step returned no answer (`error: "upstream"`);
- it timed out inside the budget (`error: "timeout"`);
- the web thread was still running when round 1 closed (`web_late`, run_round1 synthesises `{status: "failed", error: "timeout"}` with no `block`).

It does not fire on `refused` (only `question_admission.EMPTY` produces it; `too_long` carries the draft as W1), on a draft-only `unverified` envelope (W1 present), or for any other policy.

**Carrier.** `_answer` sets `t.web_failed = meta["error"]` where it reads the round-1 `web_env`, before `t.web_env` is nulled for an envelope with no items. Both the main path and `_fallback` read `t.web_failed`.

**Fixed text.** `WEB_UNREACHABLE = "I couldn't search the web just now."`, `WEB_TRY_AGAIN = "Please try again in a moment."` `answer_language.policy()` is English-only, so both are English. Separator before a records answer: `"

"` on screen (answers are markdown with bullets), `" "` on voice.

**Main path** (after the judge's final answer and the existing guard checks; model-free, not a prompt rule):

| Judge answer cites an R ref (`out["refs_r"]`)? | Text | Citations |
|---|---|---|
| no | `WEB_UNREACHABLE + " " + WEB_TRY_AGAIN` (one line on both channels) | none, `grounded=False` (as the offer row) |
| yes | `WEB_UNREACHABLE + sep + <judge text>` | as the judge cited |

**`_fallback` exits:**
- records synthesis with cards → the "yes" row (prefix + synthesis, cards kept);
- `+no_synthesis` error dict → unchanged (an error must not become an answer);
- `NO_RECORDS` (+ "The latest recorded day before that is …") → the "no" row; the `latest_before` sentence is dropped.

**Voice.** The prefixed text must pass `voice_web.speakable` (URL, bullet and 60-word checks). If it does not: prefix + the first two sentences of the records text (the head rule in `lambda_ask_agent._speakable_web_answer`); if that fails too, the "no" row text.

**Screen.** `extra["web"] = {"status": "unverified", "answer": "", "sources": []}`. The UI already renders it as the warn line "Web check unavailable — no general answer" (`webStatusLine`; the fold shows because `m.web` is set, `ask-chat.js:337-347`).

**UI, one condition.** `wantsCorrob` (`ask-chat.js:1300`) is true for any non-web answer, so it would run external corroboration on "I couldn't search the web just now…". Add `&& !(res.web && typeof res.web.status === 'string')`: a result that already carries a web status has already been to the web.

**Log.** `route["web_failed"] = "upstream" | "timeout"` in `ASK_ROUTE`, beside the existing `web_late`. No question or answer text.

**Excluded.** `not_provisioned` returns `NO_RECORDS` before the judge: the identity error is the real cause, so it is unchanged.

## 4. Out of scope

- The legacy `_rag_answer` voice path. It is replaced by the orchestrator for every channel listed in `ASK_AGENTIC`, which goes to prod at the release.
- The 504 / read-timeout rate itself (about 1 in 4 verify attempts on 2026-10-14). That is the web-step speed and provider item, measured separately once OpenRouter is topped up.
- `pol == "auto"` turns (screen without the offer opt-in): the caller did not ask for the web, so "not in your records" is a fair answer there.
- A scoped screen question with the phrase keeps `never` (scoped-Ask rule), so the rule never fires there. A scoped voice question with the phrase is `required` and is covered, including the guard exit through `_fallback`.

## 5. Tests

- `required` + failed web (`error: upstream`), judge answers with no R ref → the "no" row text, no citations, `grounded=False`, screen result carries `web: {status: "unverified", answer: "", sources: []}`.
- Same, judge answer cites R1 → screen text is `WEB_UNREACHABLE + "

" + judge text`, card for R1 kept; voice uses `" "`.
- `web_late` shape (no `block`) → fires; `ASK_ROUTE` has `web_late` and `web_failed: "timeout"`.
- Voice, records answer that fails `speakable` → prefix + first two sentences; and when those fail too, the "no" row text.
- `_fallback` synthesis exit on a failed-web `required` turn (judge error) → prefix + synthesis, cards kept.
- `_fallback` NO_RECORDS exit → the "no" row text, no `latest_before` sentence.
- `_fallback` `+no_synthesis` → still the error dict.
- `required` + verified, + draft-only `unverified`, + refused → unchanged.
- `offer` / `auto` / `never` + failed web → unchanged. `not_provisioned` → unchanged.
- `ASK_ROUTE` carries `web_failed` and no answer text.
- UI (`node --test`): a result with `web.status` set and `from_web: false` does not request corroboration; one without `web` still does.

## 6. Rev 1 review (Fable) → rev 2

| Finding | Fix |
|---|---|
| 1 no carrier in `_fallback` | §3 Carrier: `t.web_failed` |
| 2 three `_fallback` exits | §3 `_fallback` exits |
| 3 `web_late` shape | §3 When it fires; §5 test; Log |
| 4 voice drop erases records | §3 Voice: two-sentence head first; `speakable` named |
| 5 screen separator | §3 Fixed text |
| 6 corroboration on the fixed text | §3 UI condition; §5 UI test |
| 7 `not_provisioned` | §3 Excluded |
| 8 scoped / guard | §4 |
| 9 refused unreachable | kept as a test |
