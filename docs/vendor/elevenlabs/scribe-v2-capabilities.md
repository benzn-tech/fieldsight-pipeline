# ElevenLabs Scribe v2 — what the API offers, and what we actually use

**Pulled 2026-08-09** from the public docs. Kept in the repo so no session has to re-discover
it, and so the gap between "what the provider does" and "what we ask for" stays visible.

Sources:
[capabilities](https://elevenlabs.io/docs/capabilities/speech-to-text) ·
[POST /v1/speech-to-text](https://elevenlabs.io/docs/api-reference/speech-to-text/convert) ·
[Scribe v2 announcement](https://elevenlabs.io/blog/introducing-scribe-v2) ·
[models](https://elevenlabs.io/docs/overview/models)

## Limits worth knowing

| | |
|---|---|
| file size | up to 3 GB (API reference says <5 GB on `file`) |
| duration | up to **10 hours**; multichannel counts the combined duration of all channels |
| diarization | **up to 32 speakers** |
| multichannel | **max 5 channels**, each processed independently |
| keyterms | **1000 terms × 50 chars** (batch); 50 × 20 for realtime |

A 130-minute meeting is nowhere near any of these. **Whole-session transcription is not
blocked by the provider** — see §"What this changes" below.

## Every request parameter, and whether we send it

`src/elevenlabs_utils.py` sends only the first four.

| parameter | what it does | we send |
|---|---|---|
| `model_id` | model | ✅ `scribe_v2` |
| `file` | the audio | ✅ multipart |
| `diarize` | annotate who is talking | ✅ `true` |
| `num_speakers` | **max** speakers, up to 32; **`null` = auto-detect** | ✅ hard-coded **5** |
| `timestamps_granularity` | `word` or `character` | ✅ `word` |
| `keyterms` | bias terms, up to 1000 | ✅ 130 |
| `language_code` | pin the language | ⬜ empty (auto) |
| **`diarization_threshold`** | **0–1, speaker separation sensitivity** | ❌ |
| **`seed`** | **deterministic sampling** | ❌ |
| **`temperature`** | 0.0–2.0 randomness | ❌ |
| **`tag_audio_events`** | tag laughter and other non-speech | ❌ (defaults on) |
| **`source_url`** | **fetch the audio from a URL instead of uploading** | ❌ |
| **`webhook` / `webhook_id` / `webhook_metadata`** | **async delivery** | ❌ |
| `use_multi_channel` | transcribe each channel independently, max 5 | ❌ |
| `multichannel_output_style` | `separate` or `combined` (one list, each word tagged `channel_index`) | ❌ |
| `use_speaker_library` | match against **workspace** speaker profiles | ❌ |
| `detect_speaker_roles` | agent vs customer | ❌ (not our shape) |
| `no_verbatim` | strip filler words (scribe_v2 only) | ❌ |
| `entity_detection` | 65 entity types incl. names, cards, SSN | ❌ |
| `entity_redaction` / `entity_redaction_mode` | redact them | ❌ |
| `additional_formats` | extra export formats | ❌ |
| `file_format` | `pcm_s16le_16` or `other` | ❌ |
| `cloud_storage_url` | deprecated | — |

## What this changes about decisions already taken

### 1. `num_speakers=5` is probably the wrong call

The parameter is a **maximum**, and `null` means auto-detect. We pin 5 on every request.
The 2026-08-04 provider evaluation already measured that **forcing a speaker count makes a
diarizer manufacture speakers and fragment sentences** to reach it. Auto-detect is a free
experiment and the more likely default.

### 2. `diarization_threshold` aims at the exact failure we care about

The whole speaker problem here is that **the two people furthest from a chest-mounted mic
get merged away**. A documented sensitivity dial for speaker separation is the cheapest
thing to try against that, and we have never touched it.

### 3. `seed` and `temperature` bear directly on the reproducibility work — **and `seed`
would break the hallucination test**

Measured on 2026-08-07: the same audio transcribed seven times returned **173–815 words**,
twice silently discarding the first two minutes. Loudness normalisation collapsed that
spread to 1.13× and is now live. `seed` may collapse it further, and `temperature` is the
obvious lever against invented text.

**But:** the T1 adjudication test — run the same clip three times, invented text varies
while real speech reproduces verbatim — **depends on that run-to-run variance existing.**
Pinning `seed` in production would silence the cheapest hallucination detector we have. If
`seed` is adopted, T1 has to move to cross-engine agreement (T2) alone, or run with the seed
deliberately unset. Decide this explicitly; do not let it happen as a side effect.

### 4. `source_url` + `webhook` remove the blocker that shaped the whole-session design

`2026-08-08-speaker-identity-design.md` §4.2 argued that a session-level call was
impractical: ~150 MB of PCM for 78 minutes against a synchronous multipart POST with
`HTTP_TIMEOUT = 280.0`.

**Both halves of that dissolve.** `source_url` means handing ElevenLabs a presigned S3 URL
instead of uploading; `webhook` means the result arrives asynchronously instead of racing a
timeout. The 10-hour limit covers any meeting we will ever record.

What remains true about whole-session: the audio still has to be *built* (chunks overlap by
~2 s, and `DROP_SILENT_CHUNKS` leaves holes), and `scribe_v2` still splits 8min+ audio into
up to 4 parallel internal jobs, so **speaker-id stability across a long file is still
unvalidated**. The transport objection is gone; the acoustic and stitching ones are not.

### 5. `tag_audio_events` may make PR #294 unnecessary at the source

We filter `[background noise]`, `[鼠标点击]` and friends out of the extraction input
downstream. There is a parameter that stops them being emitted at all.

**Do not flip it without measuring.** On the hardest chunks, ElevenLabs emitting
`[background chattering]` was the *honest* behaviour — it said "there is only noise here"
where AWS Transcribe invented sentences. Turning tagging off might return silence, or might
return invention. The filter also costs nothing now that it exists.

### 6. `entity_detection` / `entity_redaction` overlap work we designed ourselves

The life/work separation feature redacts personal content after extraction. The provider can
detect and redact 65 entity types (names, cards, SSNs) **before the text ever reaches us**.
Worth comparing against what we built, especially for the privacy-preserving feedback path.

## Multichannel — useful for multi-device, useless for one device

**One device: not applicable.** Verified 2026-08-08 on a real prod recording:
`channels=1, sample_rate=16000, pcm_s16le`. `lambda_vad` forces `-ac 1` in two places and
raises `ValueError` on anything else. One microphone records everyone; there is no
per-speaker channel to exploit.

**Multi-device: potentially the best answer we have.** Each device is worn by a different
person, so each device's audio is dominated by its wearer — the documented "multi-track
podcast" case. Channel index would *be* the speaker identity: no voiceprints, no clustering,
no thresholds, no biometric data.

Two prerequisites, and both are things we lack:

1. **Cross-device clock alignment.** Devices share no clock (BUG-37). Building an N-channel
   file needs all N aligned to one timeline; a few hundred ms of skew reverses who spoke
   first. **This is measurable for free** — compare the absolute timestamp of the same
   utterance on two devices in an existing group recording.
2. **Channel isolation.** The docs say each channel should contain one speaker. Ours bleed
   heavily: every device hears everyone, just at different levels.

Bleed is also an opportunity: the same utterance appearing on every channel means **the
loudest channel identifies the speaker** — standard multi-microphone attribution, and far
more robust than embeddings on −54 dBFS audio. ElevenLabs will not do that for us (it
transcribes each channel independently), but with `multichannel_output_style=combined` every
word carries `channel_index`, which is exactly the input such a rule needs.

**Ceiling: 5 channels**, so a meeting with more than five devices cannot use this path.

## Speaker Library — exists, enrollment undocumented

`use_speaker_library` is real and documented as matching against **workspace** speaker
profiles. What is *not* in the public docs is how a profile is created.

Probed 2026-08-08 with our production key:

| endpoint | response | reading |
|---|---|---|
| `/v1/voices` | 401 `missing the permission voices_read` | the key is recognised, the scope is denied |
| `/v1/speech-to-text/speakers` | 401 **`Neither authorization header nor xi-api-key received`** | **this endpoint does not accept an API key at all** |
| `/v1/speech-to-text/speaker-library`, `/v1/voice-library`, `/v1/speakers` | 404 | not these paths |

Two different failure modes on the same key is the signal: the speakers endpoint is on a
different auth surface, which is consistent with enrollment being a dashboard action rather
than an API one.

**Also note:** sending `use_speaker_library=true` on a transcription request returns 200 —
but so does sending a completely invented field name (verified). **The API silently accepts
unknown multipart fields**, so a 200 proves nothing about whether a parameter took effect.
Any future parameter test must check the *response*, never the status code.

**Open, and answerable only from the dashboard:** whether our plan includes it, whether
profiles can be scoped per customer or only per workspace, and whether enrollment has any
API path at all.

## Speech Engine is a different product

`speech_engine.create()` / `.get()` / `.update()` plus `onInit` / `onTranscript` / `onClose`
callbacks and `getWebrtcToken()`. It orchestrates **live voice conversation** — microphone →
transcript → LLM → synthesised speech, with turn-taking and interruption handling.

It is **not** a transcription relay, and nothing about it applies to recorded-session
transcription or speaker identity. It *is* the right shape for SP-Ask / Site Voice, which
already has its own design.
