# FieldSight ASR Benchmark 🎙️

A local web app to **benchmark speech-to-text (ASR) providers side by side** on
your real audio — accuracy, speed, and speaker diarization — to decide whether
to replace **AWS Transcribe** in the FieldSight pipeline (e.g. with **Cartesia
Ink**) and how the Chinese engines compare.

Built for live demos: upload audio → every model transcribes in parallel →
compare transcripts, error rates and latency in an LLM-console-style view. Every
run is saved and replayable.

![flow](https://img.shields.io/badge/upload→transcribe(parallel)→score→compare-FF8A4C)

---

## Providers

| Provider | Model | Diarization | Long audio | Notes |
|---|---|:--:|---|---|
| **Cartesia Ink** | `ink-2` (streaming) | — | native (turn detection) | Real-time candidate to replace AWS; `ink-whisper` also selectable (batch) |
| **ElevenLabs Scribe** | `scribe_v2` | ✅ | native (≤10 h) | 90+ langs auto-detect (en+zh in one model), word timestamps; strong AWS alternative |
| **AWS Transcribe** | batch | ✅ | native | Incumbent baseline; async via S3 |
| **Zhipu GLM-ASR** | `glm-asr-2512` | — | auto-chunked (>30s) | Strong Mandarin/dialect CER |
| **Qwen3-ASR** | `qwen3-asr-flash` | — | auto-chunked (>3min) | Alibaba DashScope |
| **Ali Fun-ASR** | `fun-asr` | ✅ | native | Needs a public URL → presigns via your S3 |

Length-limited engines are **automatically chunked and recombined**, so you can
upload a long file and still compare everyone. Cartesia gets the whole file so
you can test its built-in smart VAD.

## Scoring

- **With a reference transcript** → **WER** (English) and **CER** (Chinese)
  computed with `jiwer` (punctuation/case normalized). The headline metric is
  CER for Chinese-dominant audio, WER otherwise.
- **Without a reference** → **Claude acts as an LLM judge**, estimating each
  transcript's accuracy via cross-model consensus (transcripts anonymized to
  reduce brand bias). This is an *estimate*, clearly labelled — not a true WER.
- Always shown: **latency**, **real-time factor (RTF)**, char count, #speakers,
  #chunks.

---

## Quick start

### Option A — GitHub Codespaces (zero local setup) ✅ recommended
1. Push this branch, then **Code ▸ Codespaces ▸ Create codespace**.
   The devcontainer installs Python, **ffmpeg**, and the deps automatically.
2. In the terminal: `streamlit run benchmark/app.py`
3. Open the forwarded **port 8501**. Paste API keys in the sidebar (🔑) — done.

### Option B — Local
```bash
cd benchmark
python -m venv .venv && source .venv/bin/activate     # Windows: .venv\Scripts\activate
pip install -r requirements.txt
# ffmpeg is required (audio normalization + chunking):
#   macOS:  brew install ffmpeg
#   Ubuntu: sudo apt-get install ffmpeg
#   Windows: choco install ffmpeg   (or download from ffmpeg.org and add to PATH)
cp .env.example .env        # then fill in the keys you have
streamlit run app.py
```
Open http://localhost:8501.

> **No Python locally?** Use Codespaces (Option A). Per `CLAUDE.md` BUG-29 the
> Windows dev box has no Python; Codespaces sidesteps that entirely.

---

## API keys

Fill `.env` (copied from `.env.example`) **or** paste keys into the sidebar at
runtime. Any provider left blank shows as ⚪ *not configured* and is skipped —
the app runs fine with just one provider.

> 💰 **Pricing, free tiers, sign-up portals, and which keys need a card /
> real-name:** see [`docs/providers_and_pricing.md`](docs/providers_and_pricing.md).

| Env var | For |
|---|---|
| `ANTHROPIC_API_KEY` | LLM-as-judge (reference-free scoring) |
| `CARTESIA_API_KEY` | Cartesia Ink |
| `ELEVENLABS_API_KEY` | ElevenLabs Scribe |
| `AWS_ACCESS_KEY_ID` / `AWS_SECRET_ACCESS_KEY` / `AWS_TRANSCRIBE_BUCKET` | AWS Transcribe **and** Fun-ASR's presigned URL |
| `ZHIPU_API_KEY` | Zhipu GLM-ASR |
| `DASHSCOPE_API_KEY` | Qwen3-ASR **and** Fun-ASR (one key) |

`AWS_TRANSCRIBE_BUCKET` defaults to the FieldSight data bucket; AWS and
Fun-ASR only light up 🟢 when credentials are actually resolvable (explicit
keys, env, `~/.aws`, profile, or role) — not just because a bucket name exists.

---

## How it works

```
upload ─▶ ffmpeg normalize to 16kHz mono WAV ─▶ run providers in parallel
              │                                      │ (chunk + recombine if length-limited)
              │                                      ▼
              └────────────────────────────▶ score (WER/CER or LLM judge) ─▶ save (SQLite + per-run folder) ─▶ compare UI
```

- `core/audio.py` — ffmpeg duration / normalize / chunk
- `core/metrics.py` — WER / CER / RTF
- `core/judge.py` — Claude LLM judge
- `core/runner.py` — parallel orchestration + chunk merge + scoring
- `core/storage.py` — SQLite persistence (`data/benchmark.db` + `data/runs/<id>/`)
- `providers/*.py` — one self-contained adapter per ASR engine

## Stored runs

Everything is written under `benchmark/data/` (git-ignored):
```
data/benchmark.db                 # runs + per-provider results + scores
data/runs/<run_id>/audio.wav      # normalized audio
data/runs/<run_id>/<provider>.json# raw provider response
```
Reload and re-show any past run from the **History** tab.

## Validate your install (offline, no keys)

```bash
python tests/smoke_test.py
```
Checks audio chunking, WER/CER math, persistence, and the runner end-to-end with
a fake provider — no network or API keys required.

## Replacing AWS Transcribe with Cartesia

See **[`docs/cartesia_integration.md`](docs/cartesia_integration.md)** for where
Cartesia plugs into `src/lambda_transcribe.py`, the trade-offs (no multi-speaker
diarization, streaming-first, dropping the Silero VAD step), and a migration
checklist. Benchmark first, migrate second.
