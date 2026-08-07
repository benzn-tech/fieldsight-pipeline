# Making a claim checkable against the audio it came from — Design

**Date:** 2026-08-08 · **Status:** DESIGN (v3 — two adversarial review rounds folded in)
**Follows:** `specs/2026-08-07-asr-hallucination-and-vad-findings.md` (P1-2 in
`plans/2026-08-08-asr-switch-rollout.md`)
**Repos:** `fieldsight-pipeline`, later `fieldsight-ui`

---

## Why this exists

The ASR investigation ended with a conclusion the engine switch does not address:

> Moving to ElevenLabs **substantially reduces** fabrication but does not end it.
> It is therefore **not a substitute for making transcripts checkable against
> their audio.** Minutes can still contain things nobody said.

Measured on the 12 hardest chunks: ElevenLabs was flagged for invention 4 times,
**2 substantively** — `c0119` produced *"The market's now coming down"* where two
other engines heard *"what do you think is slow down"*; `c0075` introduced
"Laura" and "Spain" on a New Zealand site walkaround.

Today a topic has a `time_range` for display and a `source_s3_key` pointing at
the whole extraction artifact. Nothing records which words produced it, so
tracing one claim means reading a 30-minute transcript and guessing.

## The layer nobody has measured

There are **two** places a claim can be invented, and only one has been examined:

| layer | invents | evidence |
|---|---|---|
| **ASR** | words never spoken | measured — 313 fabricated words on prod, 2 substantive EL cases in 12 |
| **Extraction** | claims the transcript does not support | **never checked, on any recording, ever** |

The second is a summarising LLM writing prose from a long transcript — the exact
operation that produces plausible additions. It has run in production since the
item store shipped and no measurement of it exists.

---

## Design

### 1. The extraction cites its evidence, and the anchor is resolved on the spot

Each topic carries the words it was derived from **and where they are in the
audio**:

```json
"evidence": [
  {
    "at": "14:23:07",
    "quote": "the slab pour is pushed to Thursday",
    "status": "verified",
    "segment_key": "audio_segments/Ben_UCPK/2026-08-07/ben_ucpk_..._off0.0_to30.0_srcwav.wav",
    "offset_sec": 11.4
  }
]
```

`at` and `quote` come from the model. **`segment_key` and `offset_sec` are
resolved by us, at verification time**, and they are the durable part.

The v1 draft stored only `at` and left the audio lookup to the read path "because
`normalize_transcript` already computes absolute time forwards". That was wrong
in three ways at once, and resolving on the spot removes all three:

- **The turns don't know their source file.** `assemble_deduped_turns` flattens
  turns without the segment filename (`lambda_extract_session.py:733-741`;
  `_build_turn` carries no filename). A reverse lookup would be a new module
  re-deriving each segment's interval from its filename — BUG-09's arithmetic,
  which the repo has already got wrong once — and would have to handle the ~2s
  ring-buffer overlap where two chunks cover the same instant.
- **`HH:MM:SS` is not a moment.** It has no date and no zone, so a session
  crossing midnight is ambiguous at the boundary (BUG-37's family).
- **Across devices it is not even a single clock.** Phase C (merged at `f8cdf17`)
  merges several recordings of one meeting, and `assemble_group_turns` says it
  outright: *"across devices there is no shared clock … BUG-37 is a shipped
  instance of a device's wall clock being 12 hours out."* The same `14:23:07`
  names different real moments in different devices' audio.

**The anchor is not already in hand — one small change makes it so.** By the
time verification runs, `assemble_deduped_turns` has returned; its
`normalized_list` is out of scope, and a turn dict carries
`speaker/text/start_sec/end_sec/abs_start/abs_end` and **no filename**
(`transcript_utils.py:382-391`). So Phase A stamps `source_filename` onto each
turn inside the assembly loop (`lambda_extract_session.py:734-738`). This is
safe: `_dedup_turn_boundaries` copies turns with `dict(t, ...)` (`:382`), so
extra keys survive.

`offset_sec` is then the matched turn's `start_sec`, which is already the
in-file offset (turn times are relative to segment start,
`transcript_utils.py:283`).

**The anchor is turn-granularity, not sub-turn.** `_build_turn` joins words into
one string (`:384`) and discards per-word timings, so after normalisation there
is no mapping from a character position back to seconds. Under
`TRANSCRIBE_WHOLE_CHUNK` (`template.yaml:830`) a turn can be a whole 30–60s
chunk, so the player may open up to a minute before the quoted words. That is
acceptable — a person listening from a minute early still hears the passage —
and it is stated so nobody implements a false precision. A match spanning a
chunk seam anchors to **the turn containing the match start**. `_dedup_turn_
boundaries` can trim a turn's leading words without adjusting `start_sec`
(`:379-383`), adding up to ~2s of wobble in the same direction.

`transcripts/{…}.json` → `audio_segments/{…}.wav`: same basename, prefix and
extension swapped. **Note the extension is always `.wav` even when the name
contains `srcmp4`** — that token records the *source* format, not the segment's.
A verified quote always has audio underneath, because every emitted unit
(per-segment, whole-chunk, and the no-speech fallback) lands in
`audio_segments/` before a transcript can exist, and `DROP_SILENT_CHUNKS`
discards only chunks that produce no transcript at all. The player still needs a
graceful 404: the bucket is outside the stack (BUG-34), so a lifecycle rule could
expire objects without leaving a trace in the template.

### 2. The citation is verified mechanically, at write time

Immediately after the LLM returns — in `extract_session`, which still holds
`turns` (`:918`, `:941-1014`) — each quote is checked against the transcript.

**No second LLM call, no human, no extra S3 read, no new IAM.** A quote that
does not appear is not a formatting problem; it is the extraction inventing,
caught mechanically.

The matcher is specified here rather than left to the implementer, because the
Phase A number *is* the deliverable and every unspecified detail is noise added
directly to it.

**Candidate text.** All turns whose `[abs_start, abs_end]` intersects
`[at − W, at + W]`, **concatenated** and then normalised. Concatenated, not
tested one at a time: turns are per-segment and never merged across chunks
(`:733-741`), so a sentence split at a chunk seam is two turns and per-turn
containment would fail an honest citation.

**It must be the same turn list the prompt saw.** Both come from the
post-filter, post-dedup `turns` in `extract_session` today — and it matters,
because `filter_device_announcements` deletes turns (`:924`); verifying against
a re-gathered set would manufacture `unverified`s from turns the model never
had. Stated here as an invariant so a later refactor cannot break it quietly.

**Truncated sessions cut both ways.** `render_transcript` elides the middle of a
long transcript (`:461-485`), so the model can only cite what it saw — but the
verifier's candidate text is the **full** turn list, not the rendered prompt.
That is deliberate in both directions: an honest citation near an elision
boundary must not fail, and a quote from an elided turn that nonetheless
verifies is *not* evidence of fabrication (the model cannot have seen it — it is
a coincidental match, and one worth logging while the number is being
established).

**Attaching a date to `at`.** The model returns a bare `HH:MM:SS`; turn
`abs_start` values are full datetimes. The date comes from the session key path,
and for a session crossing midnight `at` resolves to **the occurrence nearest
the session's own time span**. Without this rule BUG-37's family reappears
inside the matcher — the stored anchor is unambiguous, but the matcher still
consumes the model's string.

**Window `W`.** The prompt renders only each turn's start (`:450`), so `at` is a
turn start; under `TRANSCRIBE_WHOLE_CHUNK` (hardcoded `true`,
`template.yaml:830`) a turn can be a whole 30–60s chunk, putting a quote from
late in a turn up to a minute after its cited `at`. **`W` is not chosen by
argument** — Phase A logs `found_offset = |match_time − at|` and picks from the
distribution. Two things about that calibration that are easy to get wrong:

- **It must not contaminate itself.** Calibrating over *every* match inside a
  wide provisional window folds in the spurious long-range matches that a
  smaller `W` exists to reject, dragging the tail outward. Calibration counts
  **exact-containment matches of quotes at or above the specificity floor
  only**, and the distribution is checked for bimodality first: honest offsets
  are bounded by turn length (~60s) plus the model's timestamp sloppiness, while
  spurious ones spread roughly uniformly across the window. If the two modes
  separate, `W` goes in the valley, not at a percentile.
- **A percentile cut is not free.** Setting `W` at p99 reclassifies ~1% of
  honest matches as `unverified` permanently, and that 1% is then baked into the
  headline fabrication number. Whatever cut is chosen, the expected honest-loss
  is subtracted when the number is reported.

**Normalisation.** Casefold, strip punctuation, collapse whitespace. Plus two
cases that would otherwise dominate the count:

- **CJK spacing.** Turn text is space-joined (`transcript_utils.py:383`) while
  `_joins_without_space` (`:538-554`) shows CJK arrives with variable spacing, so
  normalised `"我 现在"` does not contain `"我现在"`. Whitespace is stripped
  entirely inside CJK runs before comparison. On a bilingual product this alone
  could otherwise be most of the unverified count.
- **Regularisation.** `parse_transcribe_json` keeps only pronunciation items
  (`:227`), so turn text has no punctuation. Casefolding covers most of it, but
  contractions ("can't" → "can t" against a model writing "cannot"), digits
  versus words, and dropped fillers still fail exact containment. So containment
  is the first tier only; a fuzzy match is recorded as **`verified_fuzzy`**,
  counted separately and never folded into `verified`.

  The fuzzy comparison is the **best `difflib` ratio over a sliding window of
  the quote's own length** across the candidate text — not similarity against
  the whole window, which for a 10-token quote in a 2,000-token window is near
  zero regardless of honesty. The 0.9 threshold is **provisional and calibrated
  the same way `W` is**: Phase A logs the ratio distribution for quotes that
  failed exact containment, and the cut goes where honest regularisation
  separates from noise. Shipping an argued-for threshold while insisting `W` be
  measured would be the same mistake in a different place.

**Specificity floor.** A quote below the floor is recorded `weak`, not
`verified` — "yes" or "the slab" is contained in almost any transcript, and
without a floor the headline number is biased optimistic by the cheapest
possible citation. `weak` rather than `unverified`, because some genuinely short
quotes are meaningful ("stop the pour").

**The floor is script-aware, and this is not a corner case.** Five
whitespace-delimited tokens would permanently disqualify Chinese: a perfectly
specific quote — 楼板浇筑推迟到周四 — normalises to **one token** under the CJK
whitespace rule above, so every CJK topic would cap at `weak` and the headline
number would be biased in a language-correlated way on a bilingual product. The
floor counts **CJK characters at roughly 2 chars ≈ 1 token**, or equivalently a
per-script minimum; a mixed quote uses the sum.

**Status is per quote.** The topic rollup is a total order, not a description:

1. any `unverified` → topic is `unverified`
2. else any `unchecked` → topic is `unchecked`
3. else the **worst** remaining: `weak` < `verified_fuzzy` < `verified`
4. empty or missing `evidence` list → `absent`

`unchecked` propagates rather than being masked by a sibling: a verifier crash
beside one good citation must not read as a clean topic — that is the exact
signal-corruption the status was introduced to prevent. And the rollup takes the
*worst*, not the best: a topic with one good and one invented citation is not
verified.

| status | meaning |
|---|---|
| `verified` | contained in the windowed transcript text |
| `verified_fuzzy` | ≥0.9 token similarity — probably honest, counted apart |
| `weak` | matched but under the specificity floor |
| `unverified` | not found — **the fabrication signal** |
| `absent` | the model returned no citation |
| `unchecked` | the verifier itself failed |

`unchecked` exists because v1 routed verifier exceptions into `absent`, which
would have silently deflated the "model didn't cite" signal with our own code
bugs — while the spec simultaneously insisted those two were different failures
with different fixes. Logged loudly (BUG-40: never a silent except).

### 3. Only then, the UI

A "where did this come from" control beside each topic: the quoted words, and a
player at `segment_key` + `offset_sec`. `/api/org/media/presigned-url` already
mints a GET for `audio_segments/` keys under the folder ACL
(`lambda_org_api.py:5405`, `:5488-5515`).

---

## What each layer actually catches

| layer | catches | cost | when it runs |
|---|---|---|---|
| quote-in-transcript | the **extraction** inventing | zero, mechanical | every extraction |
| audio at the moment | the **ASR** inventing | a person listening | only when someone checks |

**Neither catches an ASR fabrication the extraction faithfully quotes.** If
Transcribe invents *"That just for a little bit Do you know how many"* and the
extraction quotes it accurately, the citation verifies. Only a human hearing the
silence underneath catches that.

So `verified` must never be surfaced as "confirmed true". It means *the
extraction did not make this up*. The UI wording matters more than usual here.

---

## Storage — this is a migration, not a pass-through

v1 claimed `evidence` would reach `/live-items` with no schema change. **False,
and the reasoning behind it conflated two different things.** The S3 artifact is
additive-tolerant — consumers do use `.get()` — but Aurora is explicit-column
throughout: `upsert_topic`'s INSERT list (`repositories/topics.py:73-79`),
`_TOPIC_COLS`' SELECT list (`:11-13`), the child queries (`:308`, `:316`), and
item-writer's keyword passing (`lambda_item_writer.py:399-424`). An `evidence`
key in the artifact today is **silently dropped at the database boundary**.

The CLAUDE.md precedent about `/live-items` needing no change was about
`findings` being *attached as a child dict by the repository* — the generic
serializer, not the SQL.

So Phase A includes: a migration adding `evidence jsonb` to `topics`,
`upsert_topic` kwargs, `_TOPIC_COLS`, and the item-writer plumbing. **Take the
next free migration number at merge time, not one pinned here** — several
workstreams allocate from the same sequence and the user runs parallel sessions.

**Phase A stores evidence on `topics` only, not on action items.** Action items
are the higher-stakes object and will want it eventually, but each additional
cited object spends output tokens against a ceiling that has not been
established on the production path (below), and doubles the migration.

Action items live *inside* topic objects in `EXTRACTION_SCHEMA` (`:403-410`), so
"topics only" is expressible as an instruction — but a model shown an `evidence`
field may volunteer one inside action items anyway. Aurora drops it (explicit
columns), but it still costs output tokens and would leave evidence-shaped keys
in the S3 artifact that a downstream reader could mistake for verified. **The
verifier strips evidence found anywhere but the topic level**, so the artifact
never carries an unverified citation.

**Evidence is excluded from the embedding text.** `chunking.py` builds RAG text
from extraction topics; quotes copied into embeddings would double-weight the
cited sentences in retrieval, so a cited topic would rank above an uncited one
for reasons unrelated to relevance.

---

## Output budget — the binding constraint, and v1 got it backwards

v1 said "`max_tokens` already scales with input (BUG-16) … truncation is already
visible via `transcript_stats`". Both halves are wrong:

- **`transcript_stats` records *input* truncation only** (`render_transcript`,
  `:452-491`). Output truncation is invisible: it surfaces as unparseable JSON →
  `RuntimeError` (`:945-947`) → S3-event retry → **the full LLM call re-runs and
  hits the same wall.** That is precisely BUG-43's shape — expensive work
  discarded and retried into the same failure — and it would be paid for on
  every retry.
- **The cap is provider-dependent, and the branch that matters is not the one
  the workflow default names.** `deploy-prod.yml:202` falls back to `anthropic`,
  but the fallback is not what runs: `fieldsight-prod-extract-session` has
  `LLM_PROVIDER=qwen`, `QWEN_MODEL=qwen3.7-max` (checked on the deployed
  function, 2026-08-08). **Prod runs qwen.** Reading the workflow default and
  stopping there is how v1 got this wrong twice over.

| branch | what `llm_utils` sends | ceiling |
|---|---|---|
| **qwen (prod)** | thinking: no `max_tokens` (`:144-150`); non-thinking + `force_json`: `response_format`, no `max_tokens` (`:158-160`) | **DashScope's model default — unmeasured** |
| anthropic | `enable_thinking` ignored (`:79-81`) | `min(4096 + n_segments*350, 8000)` (`:935`) |

Realistic addition: 15 topics × 2 quotes × ~40 tokens ≈ **1,200–1,600 output
tokens**, topics only.

**Neither branch is safe on the evidence available, and they need different
work:**

- **anthropic** — raise the ceiling. Safe: `claude-sonnet-4-6` supports far more
  output, `:935` is the only site that consumes that number, and
  `Timeout: 600` / `LLM_HTTP_TIMEOUT: 540` (`template.yaml:1507`, `:1519`) leave
  room for ~10–12K non-streaming.
- **qwen — measure before claiming anything.** "Sends no cap" is not "uncapped":
  DashScope applies a model default when the field is omitted, and **nobody has
  established what qwen3.7-max's is.** If it sits in the low thousands, the
  *actual production path* has exactly the invisible-truncation → retry-storm
  exposure this section exists to close. v1 called qwen "harmless" without
  looking. Phase A measures it — one call with a deliberately long expected
  output — before the prompt change goes anywhere near prod.

Phase A also adds a test for "the output hit the ceiling" distinct from
`transcript_stats`, because today that condition is indistinguishable from a
model returning malformed JSON.

---

## Failure behaviour

The bias: **a citation problem must never cost the user their content.**

| Case | Behaviour |
|---|---|
| Model returns no `evidence` | `absent`. Topic written normally — a missing citation is a quality signal, not a reason to discard extracted work. |
| Quote not found | `unverified`, quote stored **as returned**. Storing the model's actual words is the whole evidence; overwriting them destroys the record of what it claimed. |
| Quote found outside the window | `unverified`. A quote matching somewhere else is a mis-citation; treating it as verified makes the number meaningless. |
| Quote under the specificity floor | `weak`. |
| Verifier raises | `unchecked`, logged loudly, extraction continues. This step must never be able to fail an extraction — a measurement that can destroy what it measures is worse than no measurement. |
| Output hits the token ceiling | Detected explicitly and logged; the extraction fails as it does today, but the log says *why*, instead of "failed to parse JSON". |
| Older artifacts with no `evidence` | Read as `absent`. |

---

## Measuring — what the number is, and what it is not

**Defined over final-tier artifacts only.** Live passes run repeatedly per
session (thinking off, 90s throttle) and each would log its own counts before
being overwritten (`:250`, `:941`, `:987`); aggregating log lines naively would
double-count weak-mode extractions. The metric is *the fraction of **final**-tier
topics whose evidence is `unverified`*.

**A raw number is not yet a decision input.** Before it drives anything, a fixed
sample — 30 unverified quotes — is read by a person and split into matcher
misses versus real invention. That converts "8% unverified" from an
uninterpretable figure into "x% matcher, y% fabrication". The parent
investigation's own lesson applies: a text-only judge scores a fluent invention
well, so the adjudication is human, not another LLM.

**And the Phase B decision is largely independent of it.** With
`PROD_ASR_PROVIDER=elevenlabs` and EL's signature failure being fluent smoothing
of unclear audio, the extraction will faithfully quote smoothed inventions — so a
near-zero extraction-layer number is the *expected* result, and it says nothing
about the ASR layer, which is the measured, known-nonzero one. Only the player
catches that. Phase A is worth doing because the extraction layer has never been
measured at all; it is not a gate on Phase B.

---

## Testing

**Unit** — containment on an honest quote; the same quote with different casing,
punctuation and a contraction; a CJK quote without spaces against space-joined
turn text; a quote spanning a chunk seam (fails per-turn, passes concatenated);
a quote absent from the transcript is `unverified` and **still stored verbatim**;
a quote found outside `W`; a 2-token quote is `weak`; per-quote status with the
topic rollup; a raising verifier yields `unchecked` and never propagates; the
anchor resolves to the right `segment_key` and `offset_sec` for a segment with a
non-zero VAD offset (BUG-09's arithmetic, forwards, where it is already correct).

**Integration** — `evidence` jsonb round-trips through `upsert_topic` and back
out of `list_topics_for_date` against a real database. The unit suite drives
`FakeConn` and proves nothing about the SQL.

**Live on test** — one recording; every final-tier topic carries `evidence`; read
the per-session status counts and the `found_offset` distribution out of the log,
and set `W` from it. Then take one verified quote, resolve it to audio, **and
listen.** That last step is the only test that the chain reaches the sound.

**Not testable in unit form:** whether the extraction is honest. Phase A produces
the measurement; the tests only prove the measurement is being taken.

---

## Rollout

`EmitEvidence` — a CFN Parameter, env wiring on `ExtractSessionFunction`, and a
`--parameter-overrides` line in **both** workflows (four changes, not one; BUG-38
— a CLI override replaces samconfig wholesale). `DeviceAnnouncementPatterns`
(`template.yaml:1533-1543`) is the pattern to copy, including its `'[]'`-means-
default handling.

`true` on test, **`false` on prod** initially — not because the write is risky
but because it changes the extraction prompt, and a prompt change is not
something to discover the morning after.

Rollback is the variable. Artifacts already written keep their `evidence`; the
column stays and is simply not filled.

---

## Out of scope

- Changing the ASR provider, `VAD_THRESHOLD`, or `DROP_SILENT_CHUNKS`
- Evidence on action items (Phase A is topics only — see Storage)
- Speaker identity or diarisation quality
- Any automated *judgement* of whether a verified claim is correct — an LLM
  judging an LLM, and the ASR investigation established that a text-only judge
  scores a fluent invention well, which is how 313 fabricated words survived
- Re-processing historical extractions
