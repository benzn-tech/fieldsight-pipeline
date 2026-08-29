# asr-eval — the scripts behind the 2026-08-29 accuracy measurements

Findings and conclusions: `docs/superpowers/specs/2026-08-29-asr-accuracy-measured-findings.md`.
These are the exact scripts that produced those numbers, kept so the numbers can be
re-checked or re-run on another session. They are **evaluation tools, not pipeline code** —
nothing here is imported by a Lambda.

## Setting up a working directory

Everything reads and writes one directory. Point `ASR_EVAL_WORK` at it (defaults to this
directory, which you do not want — it fills with WAVs and JSON).

```bash
export ASR_EVAL_WORK=/some/scratch/dir
export UV_LINK_MODE=copy          # Dropbox breaks uv hardlinks on this box
mkdir -p "$ASR_EVAL_WORK"/{wav,batched,vadmeta,models}
```

Populate it for one session (`{FOLDER}`, `{DATE}`, `{SID}`):

```bash
B=s3://fieldsight-data-509194952652
# the batched WAVs prod actually sent to ASR, plus any unbatched tail chunk
aws s3 cp $B/audio_segments/{FOLDER}/{DATE}/ "$ASR_EVAL_WORK/wav/" --recursive \
  --exclude "*" --include "*{SID}*_bn*_srcwav.wav"
# prod's own transcripts for the same session — the baseline you compare against
aws s3 cp $B/transcripts/{FOLDER}/{DATE}/ "$ASR_EVAL_WORK/batched/" --recursive \
  --exclude "*" --include "*{SID}*"
# VAD metadata, for the filtering stats
aws s3 cp $B/audio_segments/{FOLDER}/{DATE}/ "$ASR_EVAL_WORK/vadmeta/" --recursive \
  --exclude "*" --include "*{SID}*vad_metadata.json"
# the voiceprint model, for anything speaker-related
aws s3 cp $B/models/ecapa_tdnn.onnx      "$ASR_EVAL_WORK/models/"
aws s3 cp $B/models/ecapa_tdnn.onnx.data "$ASR_EVAL_WORK/models/"
```

**Include the unbatched tail chunk.** A session usually ends with one chunk that never got
batched (`_c00NN_off0.0_to30.0_srcwav.wav` with no matching `_bn`). Leave it out and the
concatenated audio is shorter than what prod transcribed.

Then build the single whole-session file — byte-identical to what prod sent, in order:

```bash
uv run --no-project python concat.py     # -> session_full.wav + manifest.json
```

## Running ASR

```bash
export ELEVENLABS_API_KEY=...
uv run --no-project --with urllib3 python asr_full.py    # baseline whole-session run
uv run --no-project --with urllib3 python asr_noise.py   # SAME config again -> noise floor
uv run --no-project --with urllib3 python asr_kt.py      # + extra keyterms (edit NEW at the top)
```

**Always run `asr_noise.py`.** Without a same-config second call you cannot tell a real
change from run-to-run jitter, and on this provider that jitter is ~5.6 % of the word
sequence. Skipping it nearly produced a wrong published conclusion.

## Running Qwen (the other provider)

Needs a URL the vendor can fetch, so the audio goes to S3 and gets presigned. **Mint the
URL fresh right before each run** — an expired one is reported as `FILE_DOWNLOAD_FAILED`,
which reads like a model problem. Always probe it first; anything but `206` means stop.

```bash
export DASHSCOPE_API_KEY=...
aws s3 cp "$ASR_EVAL_WORK/session_full.wav" s3://fieldsight-data-test-509194952652/asr-eval/x.wav
aws s3 presign s3://fieldsight-data-test-509194952652/asr-eval/x.wav \
  --expires-in 43200 > "$ASR_EVAL_WORK/audio_url.txt"
# probe it: anything but 206 means stop
curl -s -o /dev/null -w '%{http_code}\n' -r 0-99 "$(cat "$ASR_EVAL_WORK/audio_url.txt")"

uv run --no-project --with urllib3 python qwen_filetrans.py A 5 vocab     # tag, speaker_count, vocab|novocab
uv run --no-project --with urllib3 python qwen_filetrans.py B 5 vocab     # same config -> noise floor
uv run --no-project --with urllib3 python qwen_filetrans.py D 5 novocab   # control: what do hotwords buy
uv run --no-project --with dashscope python qwen_vocab.py V 4            # precompiled vocabulary_id
uv run --no-project python qwen_adapt.py A                                # -> the common shape
```

`qwen_adapt.py` reshapes Qwen output into the same AWS-Transcribe shape the ElevenLabs
adapter produces, so every analysis script below works on either provider unchanged.

**Read `skills/qwen-asr` before editing any of these.** The field name is `file_urls`
(plural); the wrong one is reported as `MalformedURL`, not as a bad field.

**Always run the `novocab` control.** Hotwords producing zero hits is not evidence they are
inert — it can equally mean the terms are unreachable. Only a baseline separates the two.

## Analysis

| script | answers | needs |
|--------|---------|-------|
| `vadstats.py` | how much did VAD actually discard | `vadmeta/` |
| `four_way.py` | word counts, coverage, holes, similarity incl. the noise floor | all runs |
| `term_hunt.py` | for each target term: where it occurs and what every run heard there | all runs |
| `variants.py` | near-miss spellings, not just exact hits | all runs |
| `sideeffect.py` | how much of a run-to-run change is the terms vs collateral | 2 runs |
| `bigblock.py` | locate a chunk one run dropped and another kept | all runs |
| `confusion.py` | frame-level speaker agreement, batched vs whole-session | batched + 1 run |
| `rebind.py` | **the main result** — re-bind the 28 (call, label) namespaces, purity before/after | batched + 1 run |
| `purity_gt.py` | re-score purity against real names (edit `NAME`) | batched + 1 run |
| `voiceprint.py` | per-label centroids, within/between distributions, clustering sweep | 1 run |
| `spk_score.py` | score every run's labels against confirmed people | all runs |
| `identify.py` | blind ID: does an enrolment clip match a session speaker | `enrol/*.wav` |
| `why_split.py` | why a speaker got split — language, level, or silence | 1 run |
| `make_listen.py` | export per-speaker mp3s so a human can settle who is who | 1 run |
| `extract_run.py` | run the production extraction prompt over a transcript | `ANTHROPIC`/`QWEN` env |
| `xengine.py` | cross-provider scale + similarity, on CHARACTERS not words | all runs |
| `terms_joined.py` | term counts on space-stripped letters — survives a fragmenting engine | all runs |
| `sample_text.py` | the same time windows, provider by provider, for eyeballing | all runs |
| `spk_all.py` | every run's labels vs the confirmed people + Ben-recall via enrolment | all runs |

Most take `numpy`; the speaker ones also take `onnxruntime`:

```bash
uv run --no-project --with numpy --with onnxruntime python rebind.py
```

## Reading the output — four traps that already bit

- **State the denominator.** "36 % of turns are ≥3 s" and "85.8 % of speech seconds are
  ≥3 s" are the same data. The first rules out per-turn relabelling; the second says
  namespace re-binding has plenty to work with. Quoting the wrong one inverts the conclusion.
- **A threshold that only works at one value is overfitting.** `voiceprint.py` and
  `rebind.py` sweep on purpose. Trust a result when a *band* of thresholds agrees
  (0.35–0.45 did here), not when one does.
- **Word counts are not comparable across providers.** ElevenLabs splits Chinese per
  character, Qwen per word — 6853 vs 3708 "words" for near-identical content. Use
  `xengine.py` (characters), and `terms_joined.py` for term hits, because Qwen fragments
  English (`n aylor love`) and a per-word search silently undercounts it.
- **Never score against another model's labels.** Purity against the whole-session run's own
  labels is circular and read 84.9 %; against three human-confirmed names it read 96.3 %.
  Get ground truth from `make_listen.py` and a person.

## Ground truth needs a human

`make_listen.py` writes one mp3 per speaker label (`ASR_EVAL_OUT`, default
`$ASR_EVAL_WORK/listen`) by stitching that label's longest turns. Someone listens and says
which labels are the same person. Every speaker percentage in the findings doc depends on
that step — nothing downstream substitutes for it.
