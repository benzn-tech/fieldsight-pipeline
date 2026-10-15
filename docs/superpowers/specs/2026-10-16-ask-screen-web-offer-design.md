# Ask on the screen: offer the web first, then search (design)

**Date:** 2026-10-16. **Status:** draft rev 1, for Fable review.
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

- **Why no server flag:** prod UI (`main`) never sends either field, so prod screen behaviour is unchanged by construction. TEST behaviour changes only through a UI that sends them. The UI gate is the Amplify env var `FS_ASK_WEB_OFFER` → `window.FS_ENV.askWebOffer` → `FS.api.askWebOffer`, which mirrors `FS_ASK_ASYNC`. It is set to `true` on the dev branch only.
- **Why the fields are safe to trust from the client:** they choose how the caller's own question is answered. They carry no identity and no scope. `caller_sub` still comes only from the authorizer. The worst a forged field can do:
  - `offer_web` gets an offer instead of an automatic answer;
  - `web_intent` spends one web call on the caller's own question. The same caller can already do this today by typing "search online".
- **Whitelist:** `lambda_fieldsight_api._build_ask_payload` forwards each field only when it is JSON `true`. It is shared by `/api/ask` and `/api/ask/start`. Any other value is dropped, absent stays absent, and nothing is renamed.
- **Worker:** `_work_screen` passes the stored request through unchanged, minus `_SCREEN_DROP`. The fields reach `_rag_answer` / `ask_orchestrator.answer` with no worker change. A unit test pins this.

## 3. Rules (both paths)

`_wi` (explicit web intent) and `_offer` are today gated on `mode == "voice"`. On the screen they become:

| Condition | Screen result |
|---|---|
| Identity error | Unchanged (error answer, no web). |
| Scoped (body site/author/topic/date) | Unchanged: never, even with `web_intent`. This is the scoped-Ask rule and `test_web_intent_is_ignored_in_screen_mode_even_when_scoped`. The button is never shown, because a scoped turn never sets `web_offer`. |
| Time-anchored or project question (phase 0) | Unchanged: records only, no offer. |
| `web_intent: true`, unscoped | Required: the web runs, as on voice. The question sent to the web is the question as asked, with any "search online" phrase removed (`voice_web.detect_intent`). |
| "search online" typed in the question, unscoped | Required (unchanged on the orchestrator, newly honoured on legacy). |
| `offer_web: true`, unscoped, general, records do not answer | **Offer:** `answer = SCREEN_OFFER_NO_RECORDS`, `web_offer: true`, no citations, and no web or classification call when nothing was retrieved. |
| `offer_web: true`, unscoped, general, records answer part of it | Records answer, then `SCREEN_OFFER_ALSO` appended, `web_offer: true`. |
| No `offer_web` (prod UI, old clients) | Unchanged: automatic, as today. |

**Screen offer text.** Voice phrasing is a spoken question. The screen gets its own constants in `voice_web`:
- `SCREEN_OFFER_NO_RECORDS = "Your site records don't cover that."`
- `SCREEN_OFFER_ALSO = ""` (nothing is appended).

The button is the offer, so the text does not ask a question the user cannot answer by typing "yes".
- **Typed "yes":** a typed "yes" on the screen is a new question, not an acceptance. The screen has no decline/accept matcher, and this spec does not add one.
- **Partial answers:** the voice-only `_speakable_web_answer` and the voice `OFFER_ALSO` are unchanged.

**History on the re-ask.** The screen has no server-side conversation memory. `convo.json` is voice-only, and `_work_screen` neither reads nor writes it. The screen's history is what the UI sends in `history`. The re-ask sends:
- the original question text, verbatim;
- the same `history` that the original turn sent.

So the rewrite step sees exactly what it saw the first time. The offer turn itself is **not** added to the UI's history: neither the offer answer nor the button click becomes a history turn. The web answer turn is added as a normal turn.

## 4. Orchestrator (`ask_orchestrator`, behind `ASK_AGENTIC`)

- **`policy()`:** the screen returns `offer` when the body carries `offer_web`, and `auto` otherwise. `required` and `never` are unchanged. The `pol == "offer" and not body.offer_web → never` line already exists and covers voice; it now covers the screen too.
- **`wi` on the screen:** `voice_web.detect_intent(question)[0] or body.get("web_intent") is True`.
- **Offer rule:** `_OFFER_RULE` and the final offer append are gated on `pol == "offer"`, not on `channel == "voice"`. The appended text is chosen per channel (voice constants vs screen constants).
- **Pre-check:** it runs only for `auto`, so an offer turn spends no pre-check call.

## 5. UI (fieldsight-ui `dev`)

- **Request body:** `scripts/api/ask.js` sends `offer_web: true` when `FS.api.askWebOffer` is set. The re-ask sends `web_intent: true` and no `offer_web`. The api layer builds the body from named fields, so both must be added there; a field set elsewhere never leaves the browser.
- **The button:** `scripts/composites/ask-chat.js` renders **Search the web** under a message whose result has `web_offer: true`.
  - A click disables the button and sends the re-ask as a new turn, labelled with the original question.
  - The offer message stays in the chat with the button removed.
- **Async and sync:** both transports carry the fields. `askAsync` on or off makes no difference.
- **Amplify:** the dev env gets `FS_ASK_WEB_OFFER=true`, and all existing dev vars are carried.

## 6. Eval harness (`tools/ask-routing-eval`)

- **`run.py`:** sends `offer_web: true` on every screen turn, and records `offer_web` in the results file.
- **`score.py`:**
  - `general` on the screen passes when `web_offer` is true, or when the answer is web-sourced because the question carried intent.
  - `mixed` on the screen passes when it has R citations and `web_offer`.
  - `explicit_web` is unchanged (`route.web_ran`).
  - A screen `general` turn that ran the web unasked **fails**.
- **Cost:** an offer turn spends 1 judge call and no pre-check or web call. The eval estimate falls accordingly, and `run.py --dry-run` prints the new number.

## 7. Tests

- **Legacy:** each row of §3 on the screen, with and without `offer_web`. Without it, the existing screen auto-web tests stay green unchanged; that is the prod-parity proof.
- **Orchestrator:** `policy()` table rows; the offer rule in the prompt for a screen offer turn; screen offer text.
- **API:** the whitelist forwards only JSON `true`, drops `"true"`, `1` and `false`, and pins this for both routes.
- **Worker:** `_work_screen` passes both fields through to `_rag_answer`.

## 8. Out of scope

- Automatic screen web (the later iteration).
- A typed yes/no matcher on the screen.
- Prod: the UI var is not set on `main`, and the merge is the owner's.
- The device (voice), which is unchanged.
