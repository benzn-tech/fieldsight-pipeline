# Alternative LLM + ASR Providers: Qwen 3.7 + ElevenLabs scribe_v2

**Date:** 2026-07-21
**Branch:** `feat/alt-llm-asr-qwen-scribe` (based on `origin/develop` @ `ef5441c`)
**Status:** Design — awaiting review

## 1. Goal & Drivers

Add Qwen 3.7-series (via DashScope) and ElevenLabs `scribe_v2` as drop-in
alternatives to the current Anthropic Claude (LLM) and AWS Transcribe (ASR)
providers across the FieldSight pipeline.

Drivers, in the user's priority order:

1. **Cost reduction** — Qwen / ElevenLabs unit price is lower per transcription
   and per Q&A call.
2. **Reduce Anthropic / AWS dependency** — vendor diversification; the whole
   stack should be able to run off non-US-cloud model providers.

Non-drivers (explicitly out of scope): improving zh/multilingual quality is not
a goal, but existing zh capability **must not regress**.

## 2. Scope

**In scope:**

- Replace **all 6 Claude call sites** with Qwen:
  `lambda_report_generator`, `lambda_meeting_minutes`, `lambda_extract_session`,
  `lambda_programme_matcher`, and both `lambda_ask_agent` paths (legacy S3-file
  and RAG).
- Replace **AWS Transcribe** with ElevenLabs `scribe_v2` in `lambda_transcribe`.
- Both replacements ship behind **runtime provider toggles** defaulting to the
  existing providers, so merging changes nothing until a toggle is flipped.

**Out of scope (unchanged):**

- Embeddings — stays on DashScope `text-embedding-v4`.
- TTS (SP-Ask voice reply) — stays on DashScope Qwen-TTS.
- VAD segmentation (`lambda_vad`) — unchanged; it remains the sole producer of
  per-segment absolute-time offsets via the `_off{X}_to{Y}` filename convention.
- The `transcript_utils` normalization contract and all 5 downstream transcript
  consumers — untouched (see §5).

## 3. Guiding Principles

- **Prod-safe merge, test dogfoods new.** Every new code path is gated by an
  env-var toggle. **Prod defaults to the current provider** (Claude / Transcribe)
  — merging and deploying to prod changes nothing until a prod toggle is
  explicitly flipped. **Test defaults to the new provider** (Qwen / ElevenLabs)
  — so every push to `develop` validates the new path automatically. The two
  stacks are configured independently.
- **Preserve public interfaces.** LLM callers keep their existing call signature
  and JSON-extraction fallback ladder; ASR downstream keeps the exact Transcribe
  JSON contract. No consumer code changes.
- **Instant rollback.** Cutover and rollback are a single env-var change per
  Lambda (seconds), not a redeploy.
- **Mirror existing non-AWS patterns.** Reuse the `dashscope_utils` shape
  (env-var key, `urllib3`, exponential backoff) rather than introducing new SDKs
  or clients.

## 4. LLM Replacement

### 4.1 Current state (blast radius)

All Claude calls are raw `urllib3` POSTs to `https://api.anthropic.com/v1/messages`
— no Bedrock, no SDK, no streaming, no tool use, single user-turn, prompt-forced
JSON parsed by an `extract_json` fallback ladder. There are **4 duplicate copies**
of the `call_claude` logic:

- `src/claude_utils.py` (shared; imported by `extract_session` + `programme_matcher`)
- Local copies in `lambda_report_generator.py`, `lambda_meeting_minutes.py`,
  `lambda_ask_agent.py`

`claude_utils` has **no retry logic** (single attempt), unlike `dashscope_utils`
(4 attempts, exponential backoff on 429/5xx).

### 4.2 Target: unified `src/llm_utils.py`

Introduce one module that all 6 call sites import, replacing the 4 duplicates.
Public interface preserves the existing contract so call sites change only their
import + function name, not their arguments or return handling:

```
LLM_PROVIDER   = os.environ.get('LLM_PROVIDER', 'anthropic')   # anthropic | qwen

call_llm(prompt: str, max_tokens: int = 4096) -> (text: str|None, error: str|None)
extract_json(raw_text: str) -> dict | None      # unchanged 3-tier fallback ladder
```

Internal dispatch on `LLM_PROVIDER`:

- **`anthropic`** — current behaviour verbatim (single POST to Anthropic Messages
  API), now *with* the retry wrapper it previously lacked.
- **`qwen`** — `urllib3` POST to the DashScope OpenAI-compatible endpoint
  `{QWEN_BASE_URL}/chat/completions`, body
  `{"model": QWEN_MODEL, "messages": [{"role":"user","content": prompt}], ...}`.
  Reuse the `dashscope_utils` retry primitives (`MAX_ATTEMPTS=4`, backoff
  `1.0 * 2**attempt`, retryable statuses `{429,500,502,503,504}`).

**Structured-output tasks use real JSON mode.** For the 4 report/extraction/matcher
call sites, the Qwen request sets `response_format={"type":"json_object"}`. The
prerequisite (the literal word "JSON" must appear in the prompt) is already
satisfied by all 4 existing prompts. Per DashScope guidance, `max_tokens` is
**not** sent together with `response_format` (truncation risk); a max is enforced
instead via `max_completion_tokens` only where a hard bound is needed. The
`extract_json` ladder remains as a defensive net.

The `(text, error)` tuple return and the `extract_json` fallback are unchanged so
downstream defensive parsing (`repositories/findings._clean_enum`, matcher verdict
coercion, etc.) keeps working untouched.

### 4.3 Model tiering

| Call site(s) | Current model | Qwen model | Mode |
|---|---|---|---|
| report-generator, meeting-minutes, extract-session, programme-matcher | `claude-sonnet-4-6` | `qwen3.7-plus` | non-thinking + `json_object` |
| ask-agent (legacy S3 + RAG) | `claude-haiku-4-5` | `qwen-flash` | non-thinking |

Rationale: the ask path is bounded by API Gateway's 29 s hard timeout plus two
extra hops (embed + rag-search invoke); `qwen-flash` is the fast/cheap tier that
best fits both the latency budget and the cost driver. `qwen3.7-plus` handles the
heavier structured-JSON batch tasks. Both are non-thinking for stable structured
output. **Latency of the ask path on `qwen-flash` must be validated on the test
stack** (§8, Phase 2) before any prod cutover; if it breaches budget, fall back to
keeping ask-agent on Claude via its per-Lambda toggle.

### 4.4 Configuration (per-Lambda env vars)

New env vars (CFN parameters, defaults select Anthropic):

- `LLM_PROVIDER` — `anthropic` (default) | `qwen`
- `QWEN_BASE_URL` — DashScope OpenAI-compatible base URL
- `QWEN_API_KEY` — DashScope key (see open item OI-1)
- `QWEN_MODEL` — per-Lambda: `qwen3.7-plus` or `qwen-flash`

Existing `CLAUDE_MODEL` / `HAIKU_MODEL` / `ANTHROPIC_API_KEY` are retained so the
`anthropic` path stays fully functional for rollback.

### 4.5 Legacy hand-deploy constraint

`lambda_ask_agent` has a legacy hand-built deploy target
(`scripts/deploy-lambda-code.sh`) that zips only `lambda_ask_agent.py` +
`transcript_utils.py` and imports `claude_utils`/`dashscope_utils` **lazily** to
avoid breaking it. Introducing `llm_utils` requires:

- Adding `src/llm_utils.py` to that minimal zip's file list.
- Keeping the import lazy (inside functions), matching the existing pattern.

This is a required, explicit step in the plan — missing it breaks the hand-deploy
target.

## 5. ASR Replacement

### 5.1 Current state

`audio_segments/*.wav` `ObjectCreated` → `lambda_transcribe`
(`StartTranscriptionJob`, 60 s timeout, writes DynamoDB ledger row) → AWS
Transcribe (async) writes `transcripts/{user}/{date}/{base}.json` → EventBridge
`Transcribe Job State Change` → `lambda_transcribe_callback` updates ledger status.

The output artifact is raw AWS Transcribe JSON. Every downstream consumer
(report generator, meeting minutes, extract-session, RAG ingest, ask-agent) reads
it through `transcript_utils.normalize_transcript()` and relies only on
`speaker_turns[]` + `full_text`. **Word-level confidence is parsed but never used
downstream.**

### 5.2 Target: ElevenLabs `scribe_v2`, adapted at write time

Gate `lambda_transcribe` on `ASR_PROVIDER`:

- **`transcribe`** (default) — existing async job behaviour, unchanged.
- **`elevenlabs`** — synchronous path:
  1. On the `audio_segments/*.wav` event, download the segment bytes.
  2. `POST https://api.elevenlabs.io/v1/speech-to-text` (`multipart/form-data`,
     header `xi-api-key`), fields: `model_id=scribe_v2`, `file=<segment>`,
     `diarize=true`, `num_speakers` from `MAX_SPEAKERS`, `timestamps_granularity=word`,
     `keyterms=<construction vocab>` (§5.4), language config (§5.5).
  3. **Adapt** the response into AWS Transcribe JSON shape (§5.3) and write it to
     the **same** key `transcripts/{user}/{date}/{base}.json`.
  4. Write the ledger row `status=completed` directly (no async callback).

Because the write target and JSON shape are identical, `transcript_utils` and all
5 downstream consumers are **completely untouched**.

**Synchronous, not webhook-async.** VAD already chunks recordings into short
per-utterance segments, so a blocking call per segment is cheap and lets us drop
the entire EventBridge → callback hop for this path. Consequences:

- `lambda_transcribe` timeout raised from 60 s to ~300 s (it now does real work,
  not just fire-and-forget). It is already non-VPC, satisfying the BUG-36 rule
  (external HTTP only from non-VPC Lambdas).
- `lambda_transcribe_callback` and the EventBridge rule remain deployed but are
  simply not exercised by the ElevenLabs path (still needed by the `transcribe`
  path for rollback).

New module `src/elevenlabs_utils.py` mirrors `dashscope_utils` (env-var key,
`urllib3`, `MAX_ATTEMPTS=4` exponential backoff). It exposes the transcription
call **and** the adapter, so the Transcribe-shaped dict is the module's return
value.

### 5.3 Response adapter (compatibility contract)

ElevenLabs `words[]` items carry `start`, `end`, `speaker_id`, `type`
(`word`/`spacing`/`audio_event`), `logprob`. Mapping into Transcribe JSON:

| Transcribe field | Source |
|---|---|
| `results.transcripts[0].transcript` | ElevenLabs top-level `text` |
| `results.items[]` (one per `type=="word"`) | ElevenLabs word |
| `item.type` | `"pronunciation"` |
| `item.start_time` / `item.end_time` | `str(word.start)` / `str(word.end)` |
| `item.speaker_label` | `speaker_id` → `spk_{N}` (stable index mapping) |
| `item.alternatives[0].content` | the word string |
| `item.alternatives[0].confidence` | `"1.0"` placeholder (unused downstream) |

`spacing`/`audio_event` entries are dropped from `items[]` (full text already
comes from top-level `text`; matching current behaviour where punctuation items
are excluded from `words[]`). If ElevenLabs returns no `speaker_id` on any word,
omit `speaker_label` entirely so `transcript_utils` collapses to a single
`unknown` turn — exactly its existing no-diarization behaviour.

**OI-2:** confirm the exact word-string field name in a live `scribe_v2` response
(doc lists `start/end/speaker_id/type/logprob` but not the string field
explicitly; almost certainly `text`). Validated against a real response in
Phase 2.

### 5.4 Custom vocabulary → keyterms

The existing 130-term NZ construction vocabulary
(`config/custom_vocabulary_construction_nz.txt`, tab-separated
`Phrase/SoundsLike/IPA/DisplayAs`) maps to ElevenLabs `keyterms` (limit 1000
terms, ≤50 chars each — comfortably within bounds). Parse the `Phrase` column into
a `keyterms` list. Only `scribe_v2` supports the full 1000-term batch keyterms.

### 5.5 Language handling

Preserve current zh capability. AWS Transcribe currently auto-detects across
`en-NZ,en-AU,en-GB,en-US,zh-CN`. ElevenLabs uses ISO 639-3 codes (`eng`, `cmn`)
and supports auto language detection. Default the ElevenLabs path to **auto
language detection** (omit `language_code`) so both English and Mandarin recordings
transcribe without regression. Config knob `ELEVENLABS_LANGUAGE` can pin a language
if auto-detect proves unreliable in testing.

### 5.6 ASR configuration

New env vars (CFN parameters, defaults select AWS Transcribe):

- `ASR_PROVIDER` — `transcribe` (default) | `elevenlabs`
- `ELEVENLABS_API_KEY` — GitHub secret → CFN `NoEcho` param → Lambda env
  (same mechanism as `DASHSCOPE_API_KEY`)
- `ELEVENLABS_STT_MODEL` — `scribe_v2`
- `ELEVENLABS_LANGUAGE` — empty (auto-detect) by default

## 6. Deployment & Secrets

- `ELEVENLABS_API_KEY` follows the existing DashScope pattern exactly: GitHub repo
  secret → workflow `env:` → `sam deploy --parameter-overrides` → CFN `NoEcho`
  String parameter → Lambda plaintext env var. **Action required from user:** add
  the `ELEVENLABS_API_KEY` secret to the GitHub repo (shared across test/prod).
- Provider selection is driven by GitHub Actions **repo variables** injected into
  `sam deploy --parameter-overrides` (mirroring the existing `PROD_WIRE_LAKE` /
  `PROD_AUTHORITY_FLIP` / `TEST_GRADED_ROLES` pattern), which bake into each
  Lambda's env var:
  - Test: `TEST_LLM_PROVIDER` / `TEST_ASR_PROVIDER` default to **`qwen` /
    `elevenlabs`** (new) — test dogfoods the new path on every `develop` deploy.
  - Prod: `PROD_LLM_PROVIDER` / `PROD_ASR_PROVIDER` default to **`anthropic` /
    `transcribe`** (old) — prod is untouched until explicitly cut over.
- **Operating the toggle:** change the repo variable, then trigger a deploy (push
  to the branch or manually run the workflow). **Emergency rollback:** edit the
  Lambda env var directly in the AWS console for seconds-level effect (note: the
  repo variable remains the source of truth and re-asserts on the next deploy).
- Prod (`fieldsight-prod`, shared Aurora/lake) is actively transcribing daily, so
  it stays on old providers until the prod variable is deliberately flipped.

## 7. Modules & Files Changed

| File | Change |
|---|---|
| `src/llm_utils.py` | **new** — unified provider-dispatching LLM client |
| `src/elevenlabs_utils.py` | **new** — scribe_v2 client + Transcribe-shape adapter |
| `src/lambda_report_generator.py` | route Claude call through `llm_utils` |
| `src/lambda_meeting_minutes.py` | route Claude call through `llm_utils` |
| `src/lambda_extract_session.py` | swap `claude_utils` import → `llm_utils` |
| `src/lambda_programme_matcher.py` | swap `claude_utils` import → `llm_utils` |
| `src/lambda_ask_agent.py` | route both paths through `llm_utils` (lazy import) |
| `src/lambda_transcribe.py` | `ASR_PROVIDER` branch: sync ElevenLabs path |
| `src/template.yaml` | new CFN params + per-Lambda env vars |
| `.github/workflows/deploy.yml`, `deploy-prod.yml` | inject `ELEVENLABS_API_KEY` + provider vars |
| `scripts/deploy-lambda-code.sh` | add `llm_utils.py` to ask-agent minimal zip |
| `src/claude_utils.py` | left in place but unused after call sites move to `llm_utils`; removed in a later cleanup once Qwen is validated |

`transcript_utils.py`, `lambda_transcribe_callback.py`, `lambda_vad.py`,
`lambda_ingest.py`, `lambda_rag_search.py`, `dashscope_utils.py`, and all
downstream consumers: **no change**.

## 8. Rollout Phases (risk isolation)

1. **Implement behind toggles.** Prod defaults old, test defaults new. Unit-test
   both providers. Safe to merge to `develop` — prod behaviour unchanged; test
   begins running the new path on deploy.
2. **Test-stack LLM validation.** With test already on `qwen`, validate
   structured-JSON parity (report/extraction/matcher output shape) and **measure
   ask-path latency** against the 29 s budget.
3. **Test-stack ASR validation.** With test already on `elevenlabs`, run the same
   audio through both providers and diff the normalized transcripts (speaker
   turns, text, absolute times) for parity.
4. **Gated prod cutover.** Flip `PROD_LLM_PROVIDER` / `PROD_ASR_PROVIDER` when
   validated. Rollback = flip back (repo variable + deploy, or env var in console
   for instant effect).

## 9. Open Items (resolve during implementation)

- **OI-1:** Confirm whether `qwen3.7-plus` / `qwen-flash` are served on the
  existing `dashscope-intl.aliyuncs.com/compatible-mode/v1` endpoint (reusing the
  current `DASHSCOPE_API_KEY`) or require the newer workspace endpoint
  `{WorkspaceId}.ap-southeast-1.maas.aliyuncs.com/compatible-mode/v1`. Design keeps
  `QWEN_BASE_URL` + `QWEN_API_KEY` independently configurable to absorb either.
- **OI-2:** Confirm the ElevenLabs `scribe_v2` word-string field name against a
  live response (§5.3).
- **OI-3:** Confirm `qwen-flash` availability + pricing on the target DashScope
  region, and that its ask-path latency fits the 29 s budget (else keep ask-agent
  on Claude).
- **OI-4:** Decide final `max_completion_tokens` bounds per structured call site
  (JSON mode forbids `max_tokens`).

## 10. Testing

- **Unit:** `llm_utils` provider dispatch + retry (mock both providers);
  `extract_json` ladder unchanged; `elevenlabs_utils` adapter maps a canned
  scribe_v2 response into a Transcribe-shaped dict that `transcript_utils`
  parses correctly (round-trip test through `normalize_transcript`).
- **Parity:** on the test stack, same audio through Transcribe vs ElevenLabs →
  compare normalized `speaker_turns` / `full_text` / absolute times.
- **Regression:** with defaults (anthropic/transcribe), all existing tests pass
  unchanged (proves default-safe).
