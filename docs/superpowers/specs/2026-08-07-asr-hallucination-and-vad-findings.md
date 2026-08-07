# ASR hallucination, VAD gating, and the move to ElevenLabs

**Date:** 2026-08-07 → 08 · **Environment:** prod, on real site-meeting audio
**Source recording:** `Ben_UCPK` 2026-08-07, 135 chunks / 3 sessions, UC PK walkaround

---

## Why this exists

After a real site meeting the field report was: *"prod transcripts are shredded —
a sentence that held together for twenty seconds yesterday is now one or two
words, and the speaker count is wrong too."*

The first diagnosis blamed the ASR model. That was wrong, and the user rejected
it on the correct grounds — *no ASR, however bad, breaks a sentence into single
words*. Chasing it properly produced the findings below. **Two independent
defects were compounding**, and neither was the model.

---

## What was actually wrong

### 1. Transcribe was inventing words on silence — 10.7% of the meeting

The VAD's no-speech path uploaded the whole chunk and let Transcribe try anyway,
on the theory that VAD might have missed speech under noise (`BUG-07`). Measured:

- 48 of 135 chunks reached that fallback (`speech_ratio = 0`)
- ElevenLabs, given the **identical audio**, returned nothing at all for **37** of them
- Transcribe returned **313 words** for those 37 — fluent, ordinary, invented:
  > *"That just for a little bit Do you know how many No I That's the matter"*
- That is **10.7% of everything the meeting transcribed** (313 of 2,929 words)

Those words reached the extraction prompt, the minutes and the action items with
nothing marking them as fabricated. **The fallback did not rescue speech; it
manufactured it.**

**This also explains the wrong speaker count.** On chunk `c0022` Transcribe
produced 7 words *and assigned them to 3 speakers*, on audio two other engines
report as empty. The diarizer was partitioning a hallucination.

### 2. The VAD retry could not fire

The retry threshold was the constant `0.25`, correct only while `VAD_THRESHOLD`
kept its 0.5 default. Both stages were later set to **0.2**, which turned
"retry at 0.25" into a *stricter* test than the first pass — it could never
succeed, and every uncertain chunk fell straight through to the fabricating
fallback. Fixed as a **relation** (`VAD_THRESHOLD / 2`), so future threshold
changes keep the retry below it automatically.

---

## Threshold: measured, not argued

Whether to tighten `VAD_THRESHOLD` from 0.2 to 0.3 was settled by re-running
Silero locally over all 135 chunks and scoring each threshold against ground
truth (ElevenLabs on the 48 zero-speech chunks, plus a targeted run on the
boundary set).

| threshold | chunks dropped | real speech lost | noise dropped | audio kept |
|---|---|---|---|---|
| 0.10 | 26/135 | 3 / 11 | 23 / 37 | 27.0% |
| 0.15 | 39/135 | 8 / 11 | 31 / 37 | 21.7% |
| **0.20 (live)** | 48/135 | 11 / 11 | 37 / 37 | 18.8% |
| 0.30 | 60/135 | **+10 clear conversations** | 37 / 37 | 15.3% |
| 0.50 | 79/135 | more | 37 / 37 | 11.1% |

**Above 0.2 the noise is already entirely gone (37/37), so tightening has no
upside and only costs real speech.** Tightening to 0.3 would additionally
discard 12 chunks, of which ElevenLabs found **clear, substantive conversation
in 10** — including 97-word and 90-word passages with specific component
references.

**Decision: 0.2 stays.** Do not "clean it up" by raising it.

> ⚠️ The direction of the interaction matters. Once silent chunks are *dropped*
> rather than transcribed, the threshold becomes the **only** gate, and a
> mistake changes from *visible garbage* to *invisible loss*. Errs toward
> keeping.

---

## Engine comparison — the 12 hardest chunks

Same audio, same presigns, engine-blind LLM judging with rotated labels.

| chunk | AWS | ElevenLabs | qwen3.0-filetrans | qwen3.0-multimodal | qwen3-flash | fun-asr |
|---|---|---|---|---|---|---|
| **total words** | 285 | **423** | 270 | 301 | 277 | 155 |
| rank points | 26 | **40** | 25 | 29 | — | — |
| avg coherence | 3.3 | **5.8** | 3.0 | 3.8 | — | — |
| flagged invented | 2/12 | 4/12 | 3/12 | 2/12 | — | — |

ElevenLabs took **7 of 12 first places** and roughly double the coherence of the
rest.

### Each engine's signature failure

| engine | how it fails |
|---|---|
| **AWS Transcribe** | invents words on silence, **then diarizes the invention** |
| **qwen family** | emits **Chinese/Korean text on English audio** (`fun-asr` worst; `qwen-ft` on 2 of 12) |
| **ElevenLabs** | **smooths unclear passages into fluent complete sentences** |

**ElevenLabs is not immune to the defect being fixed — it is milder.** Two of
its four flags are substantive: on `c0119` it produced *"The market's now coming
down"* where two other engines heard *"what do you think is slow down"*; on
`c0075` it introduced "Laura" and "Spain" on a NZ site walkaround.

Its best behaviour is also worth naming: on `c0022` it returned
`[background chattering]` — **honestly reporting that there is only noise**,
where Transcribe fabricated 7 words and 3 speakers.

### Consequence for the switch

Moving to ElevenLabs **substantially reduces** fabrication but does not end it.
It is therefore **not a substitute for making transcripts checkable against
their audio.** Minutes can still contain things nobody said.

---

## DashScope / Qwen access notes (each cost real time)

- The pipeline key is **international**. `dashscope.aliyuncs.com` (Beijing)
  rejects it outright with `Invalid API-key provided`.
- `qwen-audio-3.0-asr-filetrans` **does not exist** — the name needs **both**
  tokens: `qwen-audio-3.0-asr-flash-filetrans`.
- The `-filetrans` family lives on the **async transcription** endpoint;
  `qwen3-asr-flash` (no `-filetrans`) lives on **multimodal-generation** with the
  audio in the message content. Wrong endpoint → misleading errors.
- **`file_urls` accepts exactly one file** for `-filetrans`. Passing 12 returns
  `InvalidParameter.MalformedURL — The file_urls array only allows one file link`
  — a URL-shaped error that is really about array length. (`fun-asr` accepts 12.)
- `qwen3-asr-flash-filetrans` **rejects our presigned URLs** (`A valid file URL is
  required.`) while `qwen-audio-3.0-asr-flash-filetrans` accepts the *identical*
  URLs. Same vendor, different validators.
- `qwen-audio-3.0-asr-flash` returns HTTP 200 with the payload under
  **`output.output.sentence`**, not `output.choices` — parsing it the usual way
  raises `KeyError('choices')` and reads like a failure when the call succeeded.
- `ASR_RESPONSE_HAVE_NO_WORDS` arrives as an **HTTP 400**. It means "no speech",
  not "error" — any integration must treat it as a normal empty result.

---

## Method notes worth reusing

- **Chunk index is not an identity.** Indices restart per session, and this day
  held three sessions, so `c0000..c0003` name three different recordings each.
  The first threshold sweep keyed on index alone and reported *"12 of 11"* real
  speech lost. Key on `(recording timestamp, chunk index)`.
- **`lambda_vad` cannot be imported off Lambda** — it builds an S3 client at
  module scope. Tests read it as source; analysis scripts stub `boto3.client`
  rather than copy the functions out (a copy would drift from deployed code).
- **A second engine is the cheapest ground truth available** for "is there
  speech here". It is not perfect: on `c0132` ElevenLabs and fun-asr heard
  nothing while three other engines agreed on 16 words. **Treat engine silence
  as evidence, not proof** — the "37 fabricated" figure is an upper bound.
- **A text-only LLM judge cannot measure accuracy.** A fluent invention scores
  well; that is precisely how 313 fabricated words survived for weeks. Use it
  for coherence and cross-engine corroboration only.

---

## Changes made

| change | where | status |
|---|---|---|
| VAD retry derived from the configured threshold | `lambda_vad.py` | PR #279 |
| Silent chunks dropped, not sent to Transcribe | `lambda_vad.py` | PR #279 |
| `segments_created` counts instead of hardcoding 1 | `lambda_vad.py` | PR #279 |
| `DropSilentChunks` parameter + both deploy workflows | `template.yaml`, `deploy*.yml` | PR #279 |
| `PROD_ASR_PROVIDER=elevenlabs` | repo variable | **set 2026-08-07** |

A dropped chunk **still writes its sidecar** (`vad_result=no_speech_dropped`,
empty `segments`). A drop that leaves no trace is indistinguishable from a lost
chunk, and that difference is the whole of the next investigation.

---

## Known follow-up, deliberately not bundled

**`session_activity` opens and touches `meeting_session` from `transcripts/`
arrivals.** With silent chunks dropped, a long silence no longer touches the
session, and prod runs `INFER_IDLE_CLOSE=true` — so the sweep could infer an
early close and send the confirmation email mid-meeting.

Measured on this recording: **longest unbroken silence 3.5 minutes against a
15-minute `SESSION_GAP`** — real headroom, but a bodycam left running in a quiet
period is exactly the case that closes it. The touch stream needs a second
source that does not depend on speech (the VAD sidecar, or the upload path).

---

## Also found along the way

- **Our own voice prompt is being transcribed.** Two chunks transcribe to
  "Recording started" — the TTS cue we added is captured by the mic and enters
  the transcript, and therefore the minutes. Needs filtering.
- **The ElevenLabs API key sits in plaintext** in the `ELEVENLABS_API_KEY`
  environment variable of both transcribe Lambdas (GitHub secret → CFN NoEcho →
  Lambda env). Anyone with `lambda:GetFunctionConfiguration` can read it. Not
  introduced here; now load-bearing for prod.
