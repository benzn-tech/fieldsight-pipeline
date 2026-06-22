# Replacing AWS Transcribe with Cartesia Ink — integration notes

> Status: **research / pre-migration.** Benchmark first with this tool, then decide.

## TL;DR / recommendation

- **Cartesia Ink** (`ink-whisper` / Ink-2) is a strong, *much cheaper*
  (~$0.13/hr vs AWS ~$1.44/hr standard), real-time, smart-VAD STT — but it is
  **English-first** and has **no multi-speaker diarization**.
- FieldSight audio is **bilingual (NZ English + Mandarin workers)** and some
  outputs (meeting minutes) rely on **speaker labels**. So a blanket swap is
  risky.
- **Likely end state = hybrid routing**, decided per recording:
  - **Cartesia** for English, single-speaker PTT / site walks (fast + cheap, no
    diarization needed).
  - **AWS Transcribe** (or another diarizing engine) for **meetings** that need
    `speaker_labels`.
  - A **Chinese engine** (GLM-ASR / Fun-ASR) for **Mandarin** audio Cartesia
    can't handle — exactly what the benchmark compares.
- The cleanest integration keeps the rest of the pipeline untouched by making
  any new engine **emit the AWS-Transcribe JSON shape** the pipeline already
  parses.

---

## Where transcription lives today

`src/lambda_transcribe.py` (v1.3):
1. S3 `ObjectCreated` on `audio_segments/*.wav` (Silero VAD output) triggers it.
2. It calls `transcribe.start_transcription_job(...)` with `IdentifyLanguage` +
   `LanguageOptions` and `ShowSpeakerLabels` (diarization).
3. AWS writes the result to **`transcripts/{user}/{date}/{base_name}.json`**.
4. A callback Lambda + DynamoDB ledger track `transcribing → pending → reported`.
5. `src/transcript_utils.normalize_transcript()` parses that JSON and feeds the
   report/minutes generators.

**The contract that matters** — `normalize_transcript()` reads the **AWS shape**:
```jsonc
{
  "results": {
    "transcripts": [{ "transcript": "full text ..." }],
    "items": [
      { "type": "pronunciation", "start_time": "0.07", "end_time": "0.31",
        "alternatives": [{ "content": "hello" }] },
      ...
    ],
    "speaker_labels": { "segments": [ ... ] }   // diarization (optional)
  }
}
```
Absolute time per word = `base_time(filename) + vad_offset(filename) + item.start_time`
(see **BUG-09**). So a replacement engine only needs to populate
`results.transcripts[0].transcript` and `results.items[].start_time/end_time/
alternatives[0].content` — the existing timestamp math then "just works".

---

## What Cartesia Ink gives you

| | Cartesia Ink |
|---|---|
| Batch API | `POST https://api.cartesia.ai/stt` (multipart: `file`, `model=ink-whisper`, `timestamp_granularities[]=word`) → `{ text, duration, language, words:[{word,start,end}] }` |
| Streaming API | WebSocket, real-time partials with built-in **smart VAD / endpointing** |
| Word timestamps | ✅ (`words[].start/end`, seconds) |
| Diarization | ❌ (turn detection, not multi-speaker labelling) |
| Languages | **English-first** (Ink-2 English only; more "coming") |
| Price | ~**$0.13/hr** streaming (vs AWS standard ~$1.44/hr) |
| Compliance | SOC 2 Type II, HIPAA, PCI |

---

## Integration options

### A. Drop-in batch replacement (lowest blast radius) — recommended first step
In `lambda_transcribe.py`, for the Cartesia path, replace
`start_transcription_job` with: download the `audio_segments` WAV → `POST /stt`
with word timestamps → **transform to AWS shape** → write the same
`transcripts/{user}/{date}/{base}.json`. Everything downstream
(callback/report/minutes/`transcript_utils`) is unchanged.

```python
# sketch — cartesia words[] -> AWS Transcribe items[] shape
def cartesia_to_aws_shape(resp: dict) -> dict:
    items = []
    for w in resp.get("words", []):
        items.append({
            "type": "pronunciation",
            "start_time": f'{w["start"]:.3f}',
            "end_time": f'{w["end"]:.3f}',
            "alternatives": [{"confidence": "1.0", "content": w["word"]}],
        })
    return {"results": {
        "transcripts": [{"transcript": resp.get("text", "")}],
        "items": items,
        # no speaker_labels — Ink doesn't diarize
    }}
```
Because Cartesia is synchronous, you can **bypass the async job + DynamoDB
ledger + callback Lambda** for this path (simpler), or keep the ledger by
writing the record straight to `reported`/`pending` after upload.

### B. Streaming + drop Silero VAD (best for real-time / live demo)
Ink's smart VAD means you may **feed raw audio directly** and skip the separate
`fieldsight-vad` Lambda for the Cartesia path. More invasive (new streaming
client, partial-result handling) but unlocks live transcription and removes a
whole pipeline stage. Re-base timestamps the same way using Ink word times.

### C. Hybrid routing (likely production end state)
Pick the engine per recording:
- `attendees`/meeting context **or** `MaxSpeakers>1` needed → **AWS** (diarization).
- English + single speaker → **Cartesia** (cheap/fast).
- Detected/declared Mandarin → **GLM-ASR / Fun-ASR**.

Keep the AWS-shape JSON contract for all of them so reports don't care which
engine ran.

---

## Trade-offs vs AWS Transcribe

| Concern | AWS Transcribe | Cartesia Ink | Impact for FieldSight |
|---|---|---|---|
| Speaker diarization | ✅ `speaker_labels` | ❌ | **Meeting minutes need it** → keep AWS/other for meetings |
| Chinese / Mandarin | ✅ `zh-CN` | ❌ English-first | Mandarin workers → must use a Chinese engine |
| Custom vocabulary | ✅ (NZ construction terms, `LanguageIdSettings`) | check availability | Recognition of site/proper nouns |
| Latency | async batch (queue → minutes) | fast / streaming | Big win for live demo & UX |
| Cost | ~$1.44/hr | ~$0.13/hr | ~10× cheaper |
| Separate VAD step | required (Silero) | built-in smart VAD | Could remove a Lambda |
| Output shape | native (pipeline built on it) | must transform | One small adapter fn |

---

## Migration checklist

- [ ] **Benchmark** Ink vs AWS vs GLM-ASR/Fun-ASR/Qwen/iFlytek on real
      `audio_segments/` WAVs — WER/CER + latency (this tool).
- [ ] Confirm Cartesia accuracy on **NZ accents** + **construction proper nouns**
      vs the AWS custom vocabulary.
- [ ] Decide diarization policy for **meetings** (keep AWS / add a diarizer).
- [ ] Decide **Mandarin** engine (GLM-ASR vs Fun-ASR) and language routing.
- [ ] Implement `cartesia_to_aws_shape()` + a Cartesia path in
      `lambda_transcribe.py` behind an env flag (e.g. `STT_ENGINE=cartesia`).
- [ ] Verify `transcript_utils.normalize_transcript()` output is identical on a
      sample (timestamps, full text).
- [ ] Decide whether to drop Silero VAD for the Cartesia path (option B).
- [ ] Cost/latency review at production volume.

## Open questions for the team
- Do daily **site reports** ever need speaker separation, or only meetings?
- What share of audio is Mandarin vs English (drives routing effort)?
- Is Cartesia's English-only limit acceptable short-term if Mandarin routes to a
  Chinese engine?
