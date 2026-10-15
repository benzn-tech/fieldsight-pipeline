# Voice Ask: offer the web, and remember the last turns — design

Date: 2026-10-15. Status: rev 2 (rev 1 reviewed by Fable: REJECT with 2 blockers; all 10 findings folded in, see §8).
Builds on: `2026-10-15-device-ask-two-phase-design.md` (the two-phase routes, `ask_turns/` store, worker).

## 1. Why

Owner device test of 0.7.21 on TEST, 2026-10-15:

- "What did we do yesterday?" went online on its own (fixed in #1120: a voice project question stays on the records).
- The owner wants the device to **say when the records do not have it and ask whether to search online**, and a long-press reply ("yes") to **carry the previous question forward** straight into the web search — the follow-up must inherit context.
- Today the device sends no history on the two-phase route (`history_turns=0` on every TEST turn), so a follow-up such as "and the day before?" is answered without the previous question.

When the web is used without being asked for is out of scope; the owner will discuss a fuller agent workflow later. This design only turns **automatic** web use on voice into an **offer**.

## 2. Behaviour (voice two-phase route only; screen and the old `/api/ask/voice` route unchanged)

| Situation | Today (after #1120) | New |
|---|---|---|
| Caller says "search online …" | web | web (unchanged) |
| Classify = project | records answer | records answer, no offer (§4.3 ruling) |
| Classify = general/mixed (or distance-gated), records not useful | web automatically, ~20 s | **"Your site records don't cover that. Want me to search online?"** (~classify time); offer stored |
| Classify = general/mixed, records useful | records + web composed | records answer + **" Want me to check online as well?"**; offer stored |
| No chunks, not scoped | web automatically | the same no-records sentence; offer stored |
| Scoped (day/site/author/topic), with or without chunks | records / "No relevant records found …" | unchanged, no offer (scoped asks never reach classify or the web) |
| Next press within 120 s is a **yes** | a new question "yes" | `web_intent=True` on the **offered question**, no history; restatement "Searching online for …" |
| Next press within 120 s is a **no** | a new question | turn finishes at start with the spoken answer "Okay."; offer consumed |
| Next press is anything else | new question, no context | new question; offer consumed; **fresh turns forwarded as history** so the existing rewrite resolves "the day before?" |

## 3. Store: two objects, one writer each

Under the same owner prefix, IAM, hashing and 24 h sweep as turn state:

- `ask_turns/<sha16(sub)>/convo.json` — **written only by the worker**, read only by start:
  `{"updated_at": t, "turns": [{"question": "...", "answer": "...", "at": t}]}`. At most 3 turns, newest last. The worker drops turns older than `CONVO_WINDOW_S = 180` on every write, so the object never holds more than 180 s of text; start also ignores stale turns on read. This is storage of the caller's own Q/A (as turn state already is), never logging.
- `ask_turns/<sha16(sub)>/offer.json` — **written only by the worker** (`{"question": "...", "at": t}`) when the answer ended with an offer, **deleted** by the worker when an answer had no offer, and **consumed by start with DeleteObject** (read then delete; no read-modify-write). Honoured for `OFFER_WINDOW_S = 120`.

Rules:
- The worker writes both only on `status=done`, after the turn state and after its final cancel check. A cancelled, stale or failed turn writes nothing — an answer that was not heard is not context.
- Start reads both with 1 s S3 timeouts (same client settings as the result route). 404/NoSuchKey/unparsable → "none" (fail soft: memory is lost, voice is not). **403 or any other error is logged at ERROR with the message `voice convo read denied` / `voice convo read failed: <TypeName>`** (distinct from absence, per the 403-vs-404 lesson) and also treated as none.
- Two presses in a row: the second start consumes (deletes) any offer before the first turn's worker writes; the first worker may then write a new offer, which belongs to the first answer — the one the caller just heard or interrupted. Bounded by 120 s; accepted.
- Logs carry counts only: `history_turns=`, `offer=consumed_yes|consumed_no|cleared|none`, `sub_hash=`. Never question or answer text.

## 4. Changes

### 4.1 `voice_web.py`

- `reply_kind(transcript) -> "yes" | "no" | None`, model-free. Lowercase, strip punctuation, split. At most 6 words. Every word must be from the union of three sets, and:
  - core yes: `yes yeah yep yup sure` and the bigrams `go ahead`, `do it`, `look it up`, `search it`, `search online`;
  - core no: `no nope nah cancel stop` and the bigram `never mind`, `don't`/`dont`;
  - neutral filler: `please thanks thank you ok okay it that go ahead do search online look up for`.
  Exactly one of {core yes present, core no present} → that kind; both or neither (filler only, e.g. "thank you", "okay") → None. So "yes thanks" → yes, "no thanks" → no, "thank you" → None, "yes the crane was moved" → None (non-vocabulary words).
- With an offer live, a transcript that matches the existing `_INTENT` ("search online", "look it up online") and whose stripped remainder is a fragment (`detect_intent` returns the text unchanged) is **yes on the offered question**. A transcript with a real remainder ("search online for the slump of 25 MPa") is an ordinary web-intent question.
- Constants `OFFER_NO_RECORDS = "Your site records don't cover that. Want me to search online?"`, `OFFER_ALSO = "Want me to check online as well?"`, `DECLINED = "Okay."`.

### 4.2 `ask_voice_turn.start`

After STT and `detect_intent`, when `ASK_VOICE_CONVO == "true"`:

1. Consume the offer (get + delete `offer.json`; fresh = `now - at <= 120`). Read `convo.json`; history = its fresh turns.
2. **Yes** (fresh offer, `reply_kind == "yes"` or the §4.1 search-online fragment rule, and `_voice_web_on()`): the worker payload carries `question = offer.question`, `web_intent = True`, **no history**. `transcript` stays the spoken "yes" (the device shows it). Restatement = existing restate on the offered question with `web_intent=True`.
3. **No** (fresh offer, `reply_kind == "no"`): no worker invoke. TTS `DECLINED`; write the turn state `done` with every `_DONE_FIELDS` key: `transcript` (spoken), `answerText: "Okay."`, `basis: null`, `audioBase64`, `audioFormat: "wav"`, `web: false`, plus `created_at`. Return `turn_id`, `web_intent: false`, `restatement: null`. The device's first result poll returns it; if the device's window has already started its bundled filler, the caller hears the filler then "Okay." — accepted, measured on device (§6), no device change. If TTS fails, the state is `error` "Speech synthesis failed" as in the worker.
4. Anything else: the offer is already consumed; proceed as today with history.
5. Worker payload: `history` from convo (a body `history` from a future device still wins), `question` (only on yes), `offer_web = _voice_web_on() and not web_intent`.
6. Flag off: no convo/offer read, no new payload keys — today's path byte-for-byte.

### 4.3 `lambda_ask_agent._rag_answer`

New body flag `offer_web` (honoured only with `mode == "voice"`):

- Empty-chunk branch, not scoped, `offer_web`, `not _wi`: return `{"answer": OFFER_NO_RECORDS, "web_offer": True, "grounded": True, "citations": [], basis, applied_scope}` — no web call, no `on_web_start`.
- Main branch, `offer_web`, `not _wi`, after classify, when `_run_general` would be true:
  - records not useful (including `source == "gated"`) → same `OFFER_NO_RECORDS` return, no synthesis, no web;
  - records useful → `_run_general = False`; the ordinary sequential records synthesis runs; its answer gets `" " + OFFER_ALSO` appended and the result `web_offer: True`.
- Project questions untouched (#1120). **Ruling, owner to confirm:** the web cannot know the caller's site, day or people, so "what did we do yesterday?" gets the records' honest answer and no offer.

### 4.4 Worker

- `rag_body["question"] = event["question"]` when present, else today's derivation from `transcript`. `offer_web` and `history` pass through.
- The audit row records the question actually answered (`event["question"]` on yes, else the transcript) — the audit is a DB row, not a log.
- After `done` and a final cancel check, when `ASK_VOICE_CONVO == "true"`: write `convo.json` (fresh turns + this one, trimmed to 3) and either put `offer.json` (`rag["web_offer"]`) or delete it. Each write failure logs its type name only and never fails the turn.

### 4.5 Template / workflow

- **AskAgent's `ask_turns/*` statement gains `s3:GetObject`** (start runs in AskAgent; it has only Put/Delete today, `template.yaml:2376-2389`). Update `PREFIX_ACTIONS["AskAgentFunction"]` in `tests/unit/test_ask_voice_worker_template.py`. After deploy, `simulate-principal-policy` for the AskAgent role on `s3:GetObject ask_turns/x/convo.json` must say allowed.
- `ASK_VOICE_CONVO` env on AskAgent and the worker from a new parameter `AskVoiceConvo`, wired from `vars.TEST_ASK_VOICE_CONVO` in `deploy.yml`; `AskVoiceConvo` added to `UNWIRED_BY_DESIGN` for prod.

## 5. Timing and cost

- Start: +1 GET +1 DELETE (offer) +1 GET (convo), each bounded at 1 s, typically ~50–100 ms total.
- **Follow-ups within 180 s now pay one rewrite LLM call** (`ASK_CONVERSATION_MEMORY` is on for the worker on TEST; budget `ASK_REWRITE_BUDGET` 4 s). The yes turn forwards no history, so it pays none. First turns pay none.
- A general question that used to go online now answers in ~classify time with the offer; "yes" then costs the web path (~20 s p50), chosen by the caller.
- "No" finishes at start (STT + one TTS, ~2 s).

## 6. Tests

- `reply_kind` table: yes / yeah go ahead / yes please / yes thanks / sure / search it → yes; no / nope / no thanks / never mind / nah cancel → no; thank you / okay / thanks / "" / "yes the crane was moved" / "no the slab was poured" / 7-word yes → None.
- start (flag on): fresh offer + yes → payload `question` = offered, `web_intent` true, no `history`, `transcript` = "yes"; "search online" alone with a fresh offer → same; stale offer + yes → ordinary question; no → `done` with all `_DONE_FIELDS` and `created_at`, no worker invoke, TTS once; offer.json deleted in every case it existed; convo 404 / garbage → ordinary path; convo or offer **403 → ERROR log `voice convo read denied`**, ordinary path; history = fresh turns only; new log fields carry no text; flag off → zero S3 calls on convo/offer keys and no new payload keys.
- `_rag_answer`: offer_web + general not useful → OFFER_NO_RECORDS, `web_offer`, zero general and synthesis calls, no `on_web_start`; gated → same; offer_web + mixed useful → synthesis + OFFER_ALSO, no general call; offer_web + project → records answer, no `web_offer`; offer_web + scoped empty → "No relevant records…"; offer_web + `_wi` → web as today; screen ignores offer_web; **rewrite-call count**: 0 with no history, 1 with fresh history.
- worker: prefers `event["question"]`; audit gets the answered question; done writes convo (trimmed, stale dropped) and offer iff `web_offer`, deletes offer otherwise; cancelled/error writes neither; a write failure still leaves `done`; flag off writes neither.
- Template invariants: AskAgent `PREFIX_ACTIONS` includes GetObject; `AskVoiceConvo` wired from TEST var only and listed in `UNWIRED_BY_DESIGN`.

Live TEST check after deploy (about 6–8 model calls, stated before running): a general question → offer; "yes" → web answer to the offered question; "no" → "Okay."; "What did we do yesterday?" then "and the day before?" → second answer about the day before. Device check by the owner: the "no" path's filler-then-"Okay." sound.

## 7. Out of scope

- When the web is used without an offer (the owner's later agent-workflow discussion).
- Screen Ask, the old voice route, prod rollout (owner).
- Device changes: none.

## 8. Rev 1 review (Fable) → rev 2

1. AskAgent lacks GetObject → §4.5 adds it plus test and simulate step. 2. "yes" would be searched literally → payload `question` (§4.2.2, §4.4). 3. "search online" as a reply → §4.1 fragment rule. 4. thanks in both lists → core/filler sets. 5. rewrite cost → §5, no history on yes, rewrite-count test. 6. RMW race → separate `offer.json`, one writer per object, start consumes by delete. 7. "no" path fields → §4.2.3 lists `_DONE_FIELDS`, filler-then-Okay accepted. 8. scoped with chunks → §2 row. 9. stored text lifetime → worker drops stale turns on write. 10. missing tests → §6.
