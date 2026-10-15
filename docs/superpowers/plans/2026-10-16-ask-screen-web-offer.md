# Ask screen web offer — implementation plan

> **For agentic workers:** REQUIRED SUB-SKILL: superpowers:subagent-driven-development. Steps use checkbox (`- [ ]`) syntax.

**Goal:** a screen client that sends `offer_web: true` gets "Your site records don't cover that." plus a **Search the web** button instead of an automatic web answer. The button re-asks with `web_intent: true`.

**Architecture:**
- Backend: one PR to `develop` (fieldsight), tasks 1–4.
- Frontend: one PR to `dev` (fieldsight-ui), task 5.
- There is no backend flag. Prod is unchanged because prod UI never sends the fields.

**Spec:** `docs/superpowers/specs/2026-10-16-ask-screen-web-offer-design.md`, rev 2, on branch `spec/ask-screen-web-offer`. Its §3 table is the authority for every rule.

## Global Constraints

- **Logging:** never log question, answer, history or `caller_sub` text. Use `sub_hash`.
- **Identity:** `caller_sub` only from the authorizer. The new fields are forwarded only when they are JSON `true` (`is True`).
- **Prod parity:** every behaviour change on the screen is conditional on `body.get("offer_web") is True`. The existing screen tests must stay green **unchanged**. If one goes red, the change is wrong, not the test.
- **Voice:** voice behaviour is unchanged. The existing voice tests stay green unchanged.
- **Git:**
  - stage named files only (never `git add -A` or `.`);
  - never `git stash`;
  - commit messages end with the Co-Authored-By / Claude-Session lines given by the controller.
- **Tests:** `python -m pytest -q -p no:cacheprovider <files>` from the worktree root, using Windows-form paths for Python.
- **Language:** all code, comments and commit text in English.

## Review Focus

1. A screen body **without** `offer_web` behaves exactly as before on both paths, including a typed "search online".
2. A scoped screen turn with `offer_web` + `web_intent` never runs the web and never offers.
3. The async path: the fields survive `start_screen` → state → `_work_screen` → `_rag_answer`.
4. No trailing space and no voice phrasing in a screen answer.
5. `web_intent` with a time word on the screen runs the web, because intent beats the time anchor.

---

### Task 1: API whitelist and worker pass-through

**Files:**
- Modify `src/lambda_fieldsight_api.py` (`_build_ask_payload`, ~128).
- Modify `src/ask_voice_turn.py` (`_SCREEN_DROP`, ~514, and its comment).
- Tests: a new file `tests/unit/test_ask_screen_web_offer_wiring.py`.

- [ ] **Tests first:**
  - **Payload:**
    - `_build_ask_payload({"question": "q", "offer_web": True, "web_intent": True}, caller)` carries both keys as `True`.
    - Each of the values `"true"`, `1` and `False` is dropped.
    - Absent stays absent.
    - Drive both `/api/ask` and `/api/ask/start` through the handler with a fake agent invoke, and assert on the forwarded payload.
  - **Seam:**
    1. Call `start_screen(body)`, with a fake S3 and a fake lambda client capturing the stored state.
    2. Feed that stored state to `_work_screen`, with `laa._rag_answer` monkeypatched to capture its body.
    3. Assert that the captured body has `offer_web is True` and `web_intent is True`, and has no `caller_sub` from the request, no `mode` and no `voice_web`.
    4. Do not hand-build the state dict.
  - Run the tests and watch them fail. The payload test is red because the fields are not forwarded; the seam test is red because of `_SCREEN_DROP`.
- [ ] **Implement:**
  - In `_build_ask_payload`: `for field in ("offer_web", "web_intent"): if body.get(field) is True: payload[field] = True`, with a comment pointing at spec 2026-10-16 §2.
  - Set `_SCREEN_DROP = ("caller_sub", "mode", "voice_web")`, and update its comment: the two web fields are the screen opt-in (spec 2026-10-16 §2).
- [ ] Run the new file plus `tests/unit/test_ask_voice_turn*.py` and `tests/unit/test_lambda_fieldsight_api*.py` (whatever exists). All green.
- [ ] Commit `ask screen: forward offer_web/web_intent to the agent`.

### Task 2: legacy `_rag_answer` screen offer

**Files:**
- Modify `src/voice_web.py`: add `SCREEN_OFFER_NO_RECORDS = "Your site records don't cover that."`.
- Modify `src/lambda_ask_agent.py`, `_rag_answer` only. The places:
  - `_wi` / `_offer` (~1353-1360);
  - `_time_anchored` (~1370);
  - the empty-chunk branch (~1390-1425);
  - the classified branch (~1520-1660);
  - `compose` (~1726);
  - `_append_offer` (~1933).
- Test: a new file `tests/unit/test_ask_screen_web_offer_legacy.py`. Model it on the existing voice offer tests: find them with `grep -ln "offer_web" tests/unit`, and reuse their fakes for rag-search, `web_answer` and the LLM.

**Rules,** spec §3:

```
voice      = body.get("mode") == "voice"
screen_opt = (not voice) and body.get("offer_web") is True
if voice:   _wi = <as today>
else:       _wi = screen_opt and not scoped and (body.get("web_intent") is True
                                               or voice_web.detect_intent(question)[0])
_offer = (voice and bool(body.get("offer_web")) and not _wi) or (screen_opt and not _wi)
```

`scoped` must be computed before `_wi`. Today it is at ~1352, so keep that order.

- **Offer return in the empty branch and the `_useful`-false branch:** the answer is `voice_web.OFFER_NO_RECORDS` on voice and `voice_web.SCREEN_OFFER_NO_RECORDS` on the screen.
- **Partial-records path (`_append_offer`):** on the screen, set `web_offer: True` on the result and append no text. On voice, keep appending `OFFER_ALSO` exactly as today.
- **The web gate in the empty branch:** today it is `(body.get("mode") != "voice" or body.get("voice_web"))`. It is unchanged.
- **`web_question`:** on the screen, when `_wi` came from the typed phrase, `web_question = voice_web.detect_intent(question)[1]`; otherwise it is `question`. Pass `web_question` instead of `question` to the two `_general_run(...)` calls and to `web_answer.compose(...)` only. Voice: check what voice already does before these calls (the worker strips the phrase before the agent) and keep voice byte-identical.

- [ ] **Tests first:** one test per spec §7 "Legacy screen" bullet, nine in all. Also the parity pair:
  - a screen general question with an empty set and no `offer_web` still runs the web (as today);
  - the same question with `offer_web` returns the offer with no web call and no classify call. Count the calls on the fakes.
- [ ] Implement. Run the new file plus **every** existing Ask test module:

  ```
  ls tests/unit | grep -i -E "ask|voice_web|web_answer|rag_answer"
  ```

  Run them all. They must be green with **no edits** to existing tests.
- [ ] Commit `ask screen: offer the web before searching (legacy path)`.

### Task 3: orchestrator screen offer

**Files:** modify `src/ask_orchestrator.py`. Tests: `tests/unit/test_ask_orchestrator_policy.py`, or wherever `policy()` is tested now (grep for `def test_.*policy`), plus the orchestrator answer tests that use a fake `chat`.

- [ ] **Tests first:**
  - `policy("screen", False, False, False, offer_web=True) == "offer"`.
  - `policy("screen", False, False, False) == "auto"` (unchanged).
  - `policy("screen", True, False, True, offer_web=True) == "never"`.
  - `policy("screen", False, True, True, offer_web=True) == "required"`.
  - **Answer, screen with `offer_web`:**
    - the system prompt contains `_OFFER_RULE`'s text;
    - a final with `offer_web` true and no R refs → answer `SCREEN_OFFER_NO_RECORDS`, with `web_offer: True`;
    - with R refs → the answer is unchanged (no trailing space, no appended text), with `web_offer: True`;
    - no pre-check call is made.
  - **Screen without `offer_web`:** unchanged; the existing tests already cover it.
- [ ] **Implement:**
  - `policy(channel, scoped, time_anchored, web_intent, offer_web=False)`: the last line becomes `return "offer" if (channel == "voice" or offer_web) else "auto"`.
  - **In `answer`:**
    - `screen_opt = (not voice) and body.get("offer_web") is True`;
    - screen `wi = voice_web.detect_intent(question)[0] or (screen_opt and body.get("web_intent") is True)`;
    - pass `offer_web=screen_opt` to `policy`.

    Keep the `pol == "offer" and not body.get("offer_web") → "never"` line.
  - **`_system_prompt`:** use `_OFFER_RULE` when `pol == "offer"`, on either channel.
  - **The final step (~1295):**
    - when `pol == "offer" and fin["offer_web"]`, set `extra["web_offer"] = True`;
    - voice text is as today;
    - screen text: `SCREEN_OFFER_NO_RECORDS` when `basis == "none" or not out["refs_r"]`, otherwise unchanged.
- [ ] Run the orchestrator test modules (`ls tests/unit | grep orchestrator`) plus `tests/unit/test_ask_orchestrator_tools_contract.py`. All green, with no edits to existing tests except a `policy` call-signature update where one is positional, if any.
- [ ] Commit `ask orchestrator: screen offer policy`.

### Task 4: eval harness

**Files:**
- Modify `tools/ask-routing-eval/run.py` and `tools/ask-routing-eval/score.py`.
- Update `tests/unit/test_ask_routing_eval_score.py` and `tests/unit/test_ask_routing_eval_run.py`.

- [ ] **`run.py`:**
  - `screen_turn` body adds `"offer_web": True`;
  - `explicit_web` items add `"web_intent": True` on the screen;
  - `turn_cost(group, channel)` drops `WEB_CALLS` for screen `general` (update `estimate` to pass the channel);
  - the results file records `offer_web: true` for screen turns.
- [ ] **`score.py`:**
  - screen `general`: pass when `pol == "offer"` and `web_offer`; fail with a "web ran unasked" check when `pol == "offer"` and `from_web`;
  - screen `mixed`: `refs_r >= 1`, plus `web_offer` when `pol == "offer"`, else `refs_w >= 1`;
  - voice is unchanged.
- [ ] Update the tests that encoded the old screen rule. These are intended changes: the spec amends §9, so rewrite each test against the new rule and do not delete it. Add tests for:
  - a screen `general` pass on the offer;
  - a fail on web-ran-unasked;
  - the estimate going down.
- [ ] Commit `ask routing eval: screen offer rules`.

### After tasks 1–4 (controller)

1. Final review on Opus. PR to `develop`; merge once CI is green.
2. Check that the deploy's `headSha` equals the merge commit.
3. A live probe on TEST, 2 model calls, against the screen `/api/ask/start`:
   - a general question with `offer_web` → the offer, no web;
   - the same with `web_intent` → web answer.

   State the call count first.

### Task 5: UI (fieldsight-ui `dev`, worktree `fsui-ask-async`)

**Files:** `amplify.yml`, `scripts/api/index.js`, `scripts/api/ask.js`, `scripts/composites/ask-chat.js`, plus the stylesheet that holds the `fs-ask-*` classes, for the button.

- [ ] Wire the flag: `FS_ASK_WEB_OFFER` → `askWebOffer`. Follow `FS_ASK_ASYNC` / `askAsync` exactly: the printf format string, its argument list, and the `index.js` line.
- [ ] Add `offer_web` and `web_intent` to `requestBodyFor` and to `ask()`'s body builder, both only when true.
- [ ] Message object: add `webOffer: !!res.web_offer` and `sentHistory`.
- [ ] Render the button as spec §5 describes:
  - a click removes the button;
  - no second user bubble is appended;
  - a placeholder is shown while the re-ask runs;
  - the re-ask sends `opts.history = offerMsg.sentHistory`, `web_intent: true` and `offer_web: true`.
- [ ] `historyFromMessages` skips messages with `webOffer`.
- [ ] fieldsight-ui has no CI. Run whatever unit tests the repo has (`ls tests` / `package.json` scripts), and check that the edited files parse with `node --check`.
- [ ] PR to `dev`; merge it.
- [ ] Amplify: read the current dev env, add `FS_ASK_WEB_OFFER=true` with all existing vars, then `start-job RELEASE`.
- [ ] Verify that `env.js` shows `askWebOffer: true`. If Chrome is available, click through once on dev.
