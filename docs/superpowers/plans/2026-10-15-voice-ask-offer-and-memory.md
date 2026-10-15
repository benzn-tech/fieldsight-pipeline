# Voice Ask offer + memory Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: superpowers:subagent-driven-development. Steps use checkbox (`- [ ]`) syntax.

**Goal:** On the device two-phase Ask, replace automatic web use with a spoken offer, honour a yes/no reply to it, and forward the last turns as history.

**Architecture:** Model-free reply detection in `voice_web.py`; an `offer_web` flag in `_rag_answer` that returns an offer instead of running the general flow; per-caller `convo.json` and `offer.json` under `ask_turns/<hash>/`, written by the worker, read/consumed by `start`; one TEST-only flag `ASK_VOICE_CONVO`.

**Tech Stack:** Python 3.11 Lambda, boto3 S3, pytest, SAM template.

**Spec:** `docs/superpowers/specs/2026-10-15-voice-ask-offer-and-memory-design.md` (rev 2). It is the authority; exact strings and numbers are there.

## Global Constraints

- Never log question, answer, transcript or `caller_sub` text; logs carry counts and `sub_hash` only.
- Identity only from the authorizer `sub` already in the body as `caller_sub`.
- Flag `ASK_VOICE_CONVO` off ⇒ behaviour byte-for-byte as today (no new S3 calls, no new payload keys).
- Screen Ask and the old `/api/ask/voice` route unchanged.
- S3 absence = NoSuchKey/404/NotFound only; 403 and others are errors (logged at ERROR, then treated as absent for this feature).
- Stage named files only (`git add <file>`), never `-A`/`.`; never `git stash`.
- Unit tests: `python -m pytest -q -p no:cacheprovider tests/unit/<file>`; one pytest process at a time.

## Review Focus

1. "thank you" / "okay" right after an answer must be a new question, not a yes/no (reply_kind → None).
2. A "yes" must search the offered question, never the word "yes" (payload `question`).
3. A cancelled or failed turn must leave no convo/offer write.
4. Flag off must make zero S3 calls on `convo.json`/`offer.json`.
5. AskAgent must be able to GetObject the convo (IAM), proven by template test.

---

### Task 1: `voice_web.reply_kind` and constants

**Files:** Modify `src/voice_web.py`; Test `tests/unit/test_voice_web.py`.

**Produces:** `reply_kind(transcript: str) -> Literal["yes","no"] | None`; `is_bare_search_request(transcript: str) -> bool` (true when `_INTENT` matches and `detect_intent` returns the text unchanged, i.e. the remainder is a fragment); constants `OFFER_NO_RECORDS`, `OFFER_ALSO`, `DECLINED` with the spec §4.1 strings.

- [ ] Write the table test from spec §6 (`reply_kind` rows) and `is_bare_search_request` cases ("Search online." → True, "search it online" → True, "search online for the slump of 25 MPa concrete" → False, "yes" → False).
- [ ] Run, see it fail.
- [ ] Implement per spec §4.1: lowercase, strip punctuation except apostrophes, split; >6 words → None; match bigrams first (`go ahead`, `do it`, `look it up`, `search it`, `search online`, `never mind`), then single words; any word outside core-yes ∪ core-no ∪ filler → None; exactly one of core-yes/core-no present → that kind, else None.
- [ ] Run, pass. Commit `voice_web: reply_kind and offer strings`.

### Task 2: `_rag_answer` `offer_web`

**Files:** Modify `src/lambda_ask_agent.py`; Test `tests/unit/test_ask_voice_web_gates.py` (reuse its `wire`/`ask` helpers).

**Consumes:** Task 1 constants. **Produces:** result key `web_offer: True` on offer answers.

- [ ] Tests from spec §6 `_rag_answer` list (general not useful; gated via `FAR_CHUNK`; mixed useful → synthesis + `" " + OFFER_ALSO`; project; scoped empty; `_wi`; screen ignores). Rewrite-count test: monkeypatch `ask_rewrite.standalone_question` (find its import in `lambda_ask_agent.py`) with a counter; set `ASK_CONVERSATION_MEMORY=true`; assert 0 calls with no history and 1 with one fresh history turn.
- [ ] Implement: `_offer = body.get("mode") == "voice" and bool(body.get("offer_web")) and not _wi`. Empty-chunk branch: before the web block, `if _offer and not scoped and not result.get("error")` return the offer dict (spec §4.3). Main branch: after `_cls`/`_useful`/`_run_general` are computed (after the #1120 project override), `if _offer and _run_general:` if not `_useful` return the offer dict (no synthesis, no `_announce_web_start`); else set `_run_general = False` and a local `_append_offer = True`. Where the sequential records synthesis result becomes the final voice answer, append `" " + voice_web.OFFER_ALSO` and set `web_offer: True` when `_append_offer`. Read the code path after the web-check branch to find the single return that carries the synthesis answer for voice; do not touch screen returns.
- [ ] Run the file plus `tests/unit/test_ask_voice_turn.py`; pass. Commit `ask: offer the web on voice instead of going online`.

### Task 3: store, `start`, worker

**Files:** Modify `src/ask_voice_turn.py`; Test `tests/unit/test_ask_voice_turn.py` (reuse its fake S3 / lambda fakes; read the file's existing fixtures first).

**Consumes:** Tasks 1–2. **Produces:** `CONVO_WINDOW_S=180`, `OFFER_WINDOW_S=120`, `CONVO_MAX_TURNS=3`; helpers `read_convo(s3,bucket,sub,now)->list[dict]`, `consume_offer(s3,bucket,sub,now)->str|None`, `write_convo(s3,bucket,sub,question,answer,now)`, `put_offer(...)`, `delete_offer(...)`; keys `owner_prefix(sub)+"convo.json"` / `"offer.json"`.

- [ ] Tests from spec §6 start and worker lists, including: flag off ⇒ the fake S3 records no call on either key and payload keys equal today's; 403 on convo get ⇒ `caplog` has ERROR `voice convo read denied`; no path writes state `done` with every `_DONE_FIELDS` key (import the tuple from `lambda_fieldsight_api` if importable, else assert the six names) and does not invoke the worker; worker with cancel marker set before the final check writes neither object.
- [ ] Implement per spec §3, §4.2, §4.4. Make sure the existing `sweep_old` does not delete `convo.json`/`offer.json` younger than 24 h (it already keys on LastModified; confirm with a test) and that `TURN_RE`-based routes cannot address these keys (`convo`/`offer` are not 32 hex chars — assert it).
- [ ] The "no" path TTS uses `laa._tts(voice_web.DECLINED)`.
- [ ] Log line: extend the existing `voice start:` info line with `history_turns=%d offer=%s` (values: `consumed_yes|consumed_no|cleared|none`).
- [ ] Run `tests/unit/test_ask_voice_turn.py tests/unit/test_lambda_fieldsight_api_ask_voice_turn.py`; pass. Commit `voice turn: conversation memory and web offer`.

### Task 4: template, workflow, invariants

**Files:** Modify `src/template.yaml`, `.github/workflows/deploy.yml`, `tests/unit/test_ask_voice_worker_template.py`, and whichever test holds `UNWIRED_BY_DESIGN` (grep for it).

- [ ] AskAgent `ask_turns/*` statement: add `s3:GetObject`. `PREFIX_ACTIONS["AskAgentFunction"]` gains it.
- [ ] Parameter `AskVoiceConvo` (String, AllowedValues true/false, Default false) mirroring `AskVoiceRestate`; env `ASK_VOICE_CONVO: !Ref AskVoiceConvo` on AskAgent and the worker; `deploy.yml` passes `AskVoiceConvo=${{ vars.TEST_ASK_VOICE_CONVO || 'false' }}` next to the existing `AskVoiceRestate` override (copy its exact form); add `AskVoiceConvo` to `UNWIRED_BY_DESIGN`.
- [ ] Run `tests/unit/test_ask_voice_worker_template.py` and the wiring/ownership tests (`-k "template or wiring or ownership or unwired"`); pass. Commit `template: AskVoiceConvo flag and AskAgent read of ask_turns`.

### After all tasks (controller)

- Full Ask/voice subset: `python -m pytest -q -p no:cacheprovider tests/unit -k "ask or voice or web or template"`.
- PR to develop, CI green, merge, TEST deploy; set repo variable `TEST_ASK_VOICE_CONVO=true` (needs a redeploy to take effect: rerun the deploy after setting it, or set it before merge).
- `simulate-principal-policy` on the AskAgent role for GetObject on `ask_turns/x/convo.json`.
- Live check per spec §6 (state the call count first).
