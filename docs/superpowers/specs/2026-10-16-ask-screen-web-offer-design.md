# Ask on the screen: offer the web first, then search (design)

**Date:** 2026-10-16. **Status:** rev 2, after Fable review of rev 1 (APPROVE WITH CHANGES; §9 maps each finding to its fix).
**Owner ruling (2026-10-16):** at first, the screen asks before it searches the web. An automatic web flow is iterated on later.
**Amends:** `2026-10-15-ask-orchestration-v2-design.md` §6.2 (screen row) and §9 (screen pass rules). It answers that spec's open item 3 (§12).

## 1. What changes

Today a general question on the screen, such as "What is the minimum concrete cover for a slab?", runs the general/web flow automatically. This happens on both paths:
- the legacy `_rag_answer` path, which is live on TEST and prod;
- the orchestrator `auto` policy, which is behind `ASK_AGENTIC` and off.

The voice channel already offers instead (#1122). It says "Your site records don't cover that. Want me to search online?" and searches only after a yes.

After this change, a screen client that opts in gets the same offer:
- The answer carries `web_offer: true`.
- The UI shows a **Search the web** button under the answer.
- A click re-asks the same question with `web_intent: true`, and only that turn uses the web.

## 2. Opt-in contract (no backend flag)

The behaviour follows two body fields that the client sends. These are the same names voice uses.

| Field | Sent by | Meaning |
|---|---|---|
| `offer_web: true` | a client that can render the offer button | Offer the web instead of running it unasked. |
| `web_intent: true` | the offer button's re-ask only | The caller accepted. The web is required for this turn. |

- **Why no server flag:** prod UI (`main`) never sends either field. §3 makes every screen change conditional on `offer_web`, so prod screen behaviour is unchanged by construction. TEST behaviour changes only through a UI that sends them. The UI gate is the Amplify env var `FS_ASK_WEB_OFFER` → `window.FS_ENV.askWebOffer` → `FS.api.askWebOffer`, which mirrors `FS_ASK_ASYNC`. It is set to `true` on the dev branch only.
- **Why the fields are safe to trust from the client:** they choose how the caller's own question is answered. They carry no identity and no scope. `caller_sub` still comes only from the authorizer. The worst a forged field can do:
  - `offer_web` gets an offer instead of an automatic answer;
  - `web_intent` spends one web answer (draft + verify) on the caller's own, unscoped question. That is the same cost as the voice yes-path, and the automatic flow already spends it unasked today.
- **Whitelist:** `lambda_fieldsight_api._build_ask_payload` forwards each field only when it is JSON `true`. It is shared by `/api/ask` and `/api/ask/start`. Any other value is dropped, absent stays absent, and nothing is renamed.
- **Worker:** `_SCREEN_DROP` (`ask_voice_turn.py:514`) today strips `web_intent` and `offer_web` before `_rag_answer`, so on the async path the feature would be silently inert. Both are removed from `_SCREEN_DROP`; `caller_sub`, `mode` and `voice_web` stay dropped. The pinning test is a seam test: `start_screen(body)` → stored state → `_work_screen` → the body `_rag_answer` receives carries both fields. It does not hand-build the state dict.

## 3. Rules (both paths)

On the screen, both web inputs exist only when the client opted in:

- `_wi` (explicit web intent) on the screen is `offer_web and not scoped and (web_intent or "search online" typed)`. A screen turn without `offer_web` keeps `_wi = False` on legacy, exactly as today, so a prod user typing "search online" sees no change. This is the same shape as `policy()` (`ask_orchestrator.py:431`): a scoped screen question never has intent.
- `_offer` on the screen is `offer_web and not _wi`.
- `web_intent` beats a time anchor, as on voice today (`_time_anchored = bool(q_from) and not _wi`). An accepted offer on a question with a time word still searches.

| Condition (screen) | Result |
|---|---|
| No `offer_web` (prod UI, old clients) | Unchanged: automatic, as today. Typed "search online" is not honoured on legacy, as today. |
| Identity error (`result.error`) | Unchanged: error or no-records answer, no web, no offer. |
| Scoped (body site/author/topic/date, pinned topic) | Unchanged: never, even with `web_intent` or the phrase. No offer, so no button. `test_web_intent_is_ignored_in_screen_mode_even_when_scoped` stays green. |
| Time-anchored or project question, no intent (phase 0) | Unchanged: records only, no offer. |
| `_wi` (accepted offer, or phrase typed with `offer_web`) | Required: the web runs, as on voice. |
| `_offer`, general, nothing retrieved or the set is distance-gated | **Offer:** `answer = SCREEN_OFFER_NO_RECORDS`, `web_offer: true`, no citations, no synthesis, no web call. Nothing retrieved also spends no classification call. |
| `_offer`, general, records answer part of it | Records answer as today, with no appended text, and `web_offer: true`. |

**Question sent to the web (legacy).** When `_wi` came from a typed phrase, the phrase is removed with `voice_web.detect_intent(question)[1]`. That `web_question` goes only to:
- the two `_general_run(...)` calls (the empty branch and the classified branch);
- `web_answer.compose(...)`.

`build_rag_prompt`, the rewrite and retrieval keep `question` / `asked` as today. An accepted offer re-sends the question as first asked, so there is nothing to strip.

**Fallbacks.** On the orchestrator, `_fallback` (judge timeout, error or guard) returns the no-records answer with no `web_offer`, so no button appears. This is the same as voice. The eval scores such a general turn as a fail, which is the wanted signal.

**Screen offer text.** Voice phrasing is a spoken question. The screen gets its own constant in `voice_web`: `SCREEN_OFFER_NO_RECORDS = "Your site records don't cover that."`
- **Partial answers:** nothing is appended on the partial path. There is no screen counterpart to `OFFER_ALSO`. The append sites (`lambda_ask_agent.py:1933`, `ask_orchestrator.py:1299`) append only on voice, so there is no trailing space.
- **The button is the offer:** the text does not ask a question the user cannot answer by typing "yes".
- **Typed "yes":** a typed "yes" on the screen is a new question, not an acceptance. The screen has no decline/accept matcher, and this spec does not add one.
- **Voice is unchanged:** the voice-only `_speakable_web_answer` and the voice `OFFER_ALSO` are untouched.

**History on the re-ask.** The screen has no server-side conversation memory: `convo.json` is voice-only, and `_work_screen` neither reads nor writes it. The screen's history is what the UI sends in `history`. The re-ask sends:
- the original question text, verbatim;
- the same `history` that the original turn sent.

So the rewrite step sees exactly what it saw the first time. The mechanics are in §5.

## 4. Orchestrator (`ask_orchestrator`, behind `ASK_AGENTIC`)

- **`policy(channel, scoped, time_anchored, web_intent, offer_web=False)`:**
  - In its last row, the screen returns `"offer" if offer_web else "auto"`.
  - `required` and `never` are unchanged.
  - The caller passes `body.get("offer_web") is True`.
  - The existing `pol == "offer" and not body.offer_web → never` line stays, for voice.
- **`wi` on the screen:**
  - With `offer_web`: `voice_web.detect_intent(question)[0] or body.get("web_intent") is True`.
  - Without `offer_web`: `detect_intent(question)[0]`, as today. The orchestrator is off everywhere, so this is not a prod change.

  The scoped rule inside `policy()` already holds.
- **Offer rule:** `_system_prompt` uses `_OFFER_RULE` whenever `pol == "offer"`, on either channel. The final step is gated on `pol == "offer" and fin["offer_web"]`:
  - voice keeps `OFFER_NO_RECORDS` / `OFFER_ALSO`;
  - the screen uses `SCREEN_OFFER_NO_RECORDS` when there are no records, appends nothing otherwise, and sets `web_offer: true`.
- **Pre-check:** it runs only for `auto`, so an offer turn spends no pre-check call.

## 5. UI (fieldsight-ui `dev`)

- **Flag:** the `amplify.yml` printf writes `askWebOffer` from `FS_ASK_WEB_OFFER` into `env.js`; both the format string and the argument list change. `scripts/api/index.js` exposes `FS.api.askWebOffer` next to `askAsync`.
- **Two body whitelists**, and both must carry the fields:
  - `scripts/composites/ask-chat.js` `requestBodyFor` (~585) rebuilds the body field by field;
  - `scripts/api/ask.js` `ask()` rebuilds it again from `opts` (the ui#316 trap).

  `offer_web: true` is sent when `FS.api.askWebOffer` is set. The re-ask sends `offer_web: true` and `web_intent: true`.
- **Message object:** the assistant message built from a result (~1050) gets `webOffer: !!res.web_offer` and `sentHistory`, the exact `history` array the turn sent.
- **The button:** a message with `webOffer` renders **Search the web**. A click:
  1. removes the button;
  2. appends **no** second user bubble; a "Searching the web…" assistant placeholder follows the offer message;
  3. re-sends the original question with `web_intent: true` and `history = offerMsg.sentHistory`, passed through `opts`, not rebuilt from `msgs`.
- **History afterwards:** `historyFromMessages` (~605-620) pairs each user message with the next non-error assistant message. It skips an assistant message marked `webOffer`, so the original question pairs with the web answer that follows. Turn N+2 then sees `(Q, web answer)`, not `(Q, "Your site records don't cover that.")`.
- **Async and sync:** both transports carry the fields. `askAsync` on or off makes no difference.
- **Amplify:** the dev env gets `FS_ASK_WEB_OFFER=true`. `update-branch` replaces the whole set, so all existing dev vars are carried.

## 6. Eval harness (`tools/ask-routing-eval`)

- **`run.py`:**
  - `screen_turn` (~158) sends `offer_web: true` and records it in the results file;
  - `explicit_web` items also send `web_intent: true` on the screen;
  - `turn_cost` (~95) drops the web calls for screen `general` items.
- **`score.py`:**
  - screen `general` (~190) passes on `route.policy == "offer"` with `web_offer`, and fails on `policy == "offer"` with `from_web` (the web ran unasked);
  - screen `mixed` (~196) passes on `refs_r >= 1` with `web_offer`;
  - `explicit_web` is unchanged (`route.web_ran`).
- **Cost:** `run.py --dry-run` prints the new, lower estimate.

## 7. Tests

- **Seam:** `start_screen` → state → `_work_screen` → the `_rag_answer` body carries both fields (§2).
- **API:** `_build_ask_payload` forwards only JSON `true`, and drops `"true"`, `1` and `false`. Pinned for both `/api/ask` and `/api/ask/start`.
- **Legacy screen:** each case below is tested with and without `offer_web`. Without it, the existing screen auto-web tests stay green unchanged; that is the prod-parity proof.
  - typed phrase without `offer_web` → no web;
  - `offer_web` + scoped + `web_intent` → no web, no offer;
  - `offer_web` + time-anchored, no intent → no offer;
  - `offer_web` + time-anchored + `web_intent` → web;
  - `offer_web` + identity error → no offer;
  - `offer_web` + empty set → offer, no classify, no web;
  - `offer_web` + distance-gated set → offer, no synthesis;
  - `offer_web` + partial records → records answer, `web_offer`, no appended text;
  - typed phrase + `offer_web` → `_general_run` gets the stripped question.
- **Orchestrator:** `policy()` rows with `offer_web`; the offer rule is present in a screen offer prompt; screen offer text; no trailing space.
- **UI:** checked by hand on dev:
  - the button appears and works;
  - the re-ask body carries `web_intent` and the original `history` (network panel);
  - the next turn's history skips the offer message.

## 8. Out of scope

- Automatic screen web (the later iteration).
- A typed yes/no matcher on the screen.
- Prod: the UI var is not set on `main`, and the merge is the owner's.
- The device (voice), which is unchanged.

## 9. Rev 1 review (Fable) → rev 2

| Finding | Fix |
|---|---|
| B1 `_SCREEN_DROP` strips both fields | §2: removed from the drop list; seam test |
| M2 scoped gate `(not scoped or _wi)` | §3: screen `_wi` is false when scoped; intent beats a time anchor |
| M3 typed phrase changes prod | §3: on the screen the phrase counts only with `offer_web` |
| M4 which calls get the stripped question | §3: the two `_general_run` calls and `compose` only |
| M5 two UI whitelists, no `web_offer` on the message | §5 |
| M6 history on the re-ask | §5: `sentHistory`, no second bubble, `historyFromMessages` skips the offer |
| M7 `policy()` has no `offer_web` | §4: new parameter |
| m8 fallback has no button | §3: stated; the eval fails it |
| m9 trailing space | §3: no screen append text |
| m10 eval changes | §6 |
| m11 tests | §7 |
