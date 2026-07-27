"""FieldSight ASR Benchmark — Streamlit app.

Upload audio, run it through multiple ASR providers in parallel, and compare
transcripts + accuracy + speed side by side (LLM-console style). Every run is
saved locally and replayable from the History tab.

Run:  streamlit run benchmark/app.py
"""
from __future__ import annotations

import os
import sys
import tempfile

# Make sibling packages importable regardless of CWD.
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import pandas as pd
import streamlit as st

from core import audio as A
from core import preprocess as P
from core import storage
from core.config import load_config
from core.runner import prepare_audio, run_benchmark
from providers import PROVIDER_CLASSES, build_providers

st.set_page_config(page_title="FieldSight ASR Benchmark", page_icon="🎙️", layout="wide")
storage.init_db()

COLS_PER_ROW = 3
SIDEBAR_KEYS = [  # (env key, label, is_secret)
    ("ANTHROPIC_API_KEY", "Anthropic key (judge)", True),
    ("CARTESIA_API_KEY", "Cartesia key", True),
    ("ELEVENLABS_API_KEY", "ElevenLabs key", True),
    ("PLAUD_CLIENT_ID", "Plaud Client ID", False),
    ("PLAUD_API_KEY", "Plaud API key", True),
    ("PLAUD_SECRET_KEY", "Plaud secret (upload fallback)", True),
    ("SONIOX_API_KEY", "Soniox key", True),
    ("DOUBAO_API_KEY", "Doubao API key (or use ID+token)", True),
    ("DOUBAO_APP_ID", "Doubao APP ID", False),
    ("DOUBAO_ACCESS_TOKEN", "Doubao Access Token", True),
    ("ZHIPU_API_KEY", "Zhipu / z.ai key", True),
    ("DASHSCOPE_API_KEY", "DashScope key (Qwen+Fun-ASR)", True),
    ("AWS_ACCESS_KEY_ID", "AWS access key id", False),
    ("AWS_SECRET_ACCESS_KEY", "AWS secret key", True),
    ("AWS_TRANSCRIBE_BUCKET", "AWS S3 bucket", False),
]


# --------------------------------------------------------------------------- #
# Sidebar: credentials + run options
# --------------------------------------------------------------------------- #
def sidebar() -> tuple[dict, dict]:
    st.sidebar.title("🎙️ ASR Benchmark")
    st.sidebar.caption("FieldSight · compare ASR providers on real audio")

    overrides = st.session_state.setdefault("key_overrides", {})
    with st.sidebar.expander("🔑 API keys (override .env)", expanded=False):
        st.caption("Paste keys for a live demo. They stay in this session only.")
        for env_key, label, secret in SIDEBAR_KEYS:
            overrides[env_key] = st.text_input(
                label, value=overrides.get(env_key, ""),
                type="password" if secret else "default", key=f"ov_{env_key}",
            )

    cfg = load_config(overrides)

    st.sidebar.subheader("Options")
    lang_choice = st.sidebar.radio(
        "Language hint", ["Auto-detect", "English", "中文"], horizontal=True,
        help="Auto lets each engine decide. Forcing a language can improve accuracy.",
    )
    language = {"Auto-detect": None, "English": "en", "中文": "zh"}[lang_choice]
    # Diarization is always requested from every engine that supports it
    # (AWS · ElevenLabs · Plaud · Fun-ASR). Cartesia/Qwen/Zhipu have no
    # diarization in their APIs and simply ignore the flag.
    st.sidebar.caption("🗣️ Speaker diarization: always on (engines that support it)")
    st.sidebar.caption("🔇 VAD: always on where it exists — Plaud (skip silence) and "
                       "Cartesia ink-2 (built-in). Other engines expose no VAD option.")

    st.sidebar.subheader("Experiment (A/B/C)")
    exp_label = st.sidebar.text_input(
        "Group label", value="",
        placeholder="e.g. A-baseline / B-isolated / C-noVAD",
        help="Free-text tag saved with every run in this batch — use it to compare "
             "groups in History (e.g. raw vs voice-isolated vs VAD-chopped input).",
    )
    preprocess = st.sidebar.radio(
        "Preprocess before every engine",
        ["None (raw audio)", "Voice Isolator (ElevenLabs)"],
        help="B-group: strip background noise with the ElevenLabs Voice Isolator "
             "before ALL engines transcribe. Uses ELEVENLABS_API_KEY.",
    )
    opts = {"language": language, "diarize": True,
            "label": exp_label.strip(),
            "preprocess": "isolate" if preprocess.startswith("Voice") else "none"}
    return cfg, opts


# --------------------------------------------------------------------------- #
# Provider selection
# --------------------------------------------------------------------------- #
def provider_picker(cfg: dict):
    providers = build_providers(cfg)
    st.sidebar.subheader("Providers")
    selected = []
    for p in providers:
        configured = p.is_configured()
        default = configured
        label = f"{p.label}"
        badge = "🟢" if configured else "⚪"
        checked = st.sidebar.checkbox(
            f"{badge} {label}", value=default, disabled=not configured,
            key=f"sel_{p.key}",
            help=(p.notes + ("" if configured else "  ·  no key configured")),
        )
        if checked and configured:
            selected.append(p)
    return providers, selected


# --------------------------------------------------------------------------- #
# Rendering helpers
# --------------------------------------------------------------------------- #
def _dur(r: dict) -> float:
    return r.get("audio_duration_s") or r.get("audio_duration") or 0.0


def _fmt(v, suffix="", pct=False, nd=2):
    if v is None or v == "":
        return "—"
    try:
        return (f"{float(v) * 100:.1f}%" if pct else f"{float(v):.{nd}f}{suffix}")
    except Exception:
        return str(v)


def summary_table(results: list[dict], reference: str):
    rows = []
    for r in results:
        acc = "—"
        if reference:
            if r.get("metric_value") is not None:
                acc = _fmt(r.get("metric_value"), pct=True) + f" {r.get('metric_name','')}"
        elif r.get("judge_score") is not None:
            acc = f"{r.get('judge_score')}/100 (judge)"
        rows.append({
            "Provider": r["provider"],
            "Status": "✅" if r.get("ok") else "❌",
            "Latency (s)": _fmt(r.get("latency_s")),
            "RTF": _fmt(r.get("rtf")),
            "Speed×RT": _fmt(1 / r["rtf"], suffix="×") if r.get("rtf") else "—",
            "Accuracy": acc,
            "Chars": r.get("char_count") or len(r.get("text") or ""),
            "Speakers": r.get("n_speakers") or 0,
            "Chunks": r.get("n_chunks") or 1,
        })
    return pd.DataFrame(rows)


def render_charts(results: list[dict], reference: str):
    ok = [r for r in results if r.get("ok")]
    if not ok:
        return
    c1, c2 = st.columns(2)
    with c1:
        st.caption("⏱️ Latency (seconds — lower is better)")
        df = pd.DataFrame({r["provider"]: [r.get("latency_s") or 0] for r in ok}).T
        df.columns = ["latency_s"]
        st.bar_chart(df, horizontal=True)
    with c2:
        if reference and any(r.get("metric_value") is not None for r in ok):
            st.caption("🎯 Error rate (lower is better)")
            df = pd.DataFrame({r["provider"]: [(r.get("metric_value") or 0) * 100]
                               for r in ok if r.get("metric_value") is not None}).T
            df.columns = ["error_%"]
            st.bar_chart(df, horizontal=True)
        elif any(r.get("judge_score") is not None for r in ok):
            st.caption("🧑‍⚖️ LLM judge score (higher is better)")
            df = pd.DataFrame({r["provider"]: [r.get("judge_score") or 0]
                               for r in ok if r.get("judge_score") is not None}).T
            df.columns = ["judge"]
            st.bar_chart(df, horizontal=True)
        else:
            st.caption("⚡ Real-time factor (lower is faster)")
            df = pd.DataFrame({r["provider"]: [r.get("rtf") or 0] for r in ok}).T
            df.columns = ["rtf"]
            st.bar_chart(df, horizontal=True)


_DIAR_CAPABLE = {cls.label: cls.supports_diarization for cls in PROVIDER_CLASSES}


def _transcript_with_speakers(r: dict) -> str:
    segs = r.get("segments") or []
    if r.get("has_diarization") and any(s.get("speaker") for s in segs):
        lines, cur = [], None
        for s in segs:
            spk = s.get("speaker") or "?"
            if spk != cur:
                lines.append(f"\n**{spk}:** {s.get('text','')}")
                cur = spk
            else:
                lines[-1] += " " + s.get("text", "")
        return "".join(lines).strip()
    return r.get("text") or ""


def render_cards(results: list[dict], reference: str):
    # best markers
    ok = [r for r in results if r.get("ok")]
    fastest = min(ok, key=lambda r: r.get("latency_s") or 1e9, default=None)
    if reference:
        best_acc = min((r for r in ok if r.get("metric_value") is not None),
                       key=lambda r: r["metric_value"], default=None)
    else:
        best_acc = max((r for r in ok if r.get("judge_score") is not None),
                       key=lambda r: r["judge_score"], default=None)

    for i in range(0, len(results), COLS_PER_ROW):
        chunk = results[i:i + COLS_PER_ROW]
        cols = st.columns(len(chunk))
        for col, r in zip(cols, chunk):
            with col:
                tags = []
                if fastest and r["provider"] == fastest["provider"]:
                    tags.append("⚡ fastest")
                if best_acc and r["provider"] == best_acc["provider"]:
                    tags.append("🏆 most accurate")
                st.markdown(f"#### {r['provider']}")
                st.caption(f"`{r.get('model','')}`  " + ("  ".join(tags)))
                if not r.get("ok"):
                    st.error(r.get("error") or "failed")
                    continue
                m1, m2, m3 = st.columns(3)
                m1.metric("Latency", _fmt(r.get("latency_s"), "s"))
                m2.metric("RTF", _fmt(r.get("rtf")))
                if reference and r.get("metric_value") is not None:
                    m3.metric(r.get("metric_name", "Err"), _fmt(r.get("metric_value"), pct=True))
                elif r.get("judge_score") is not None:
                    m3.metric("Judge", f"{r.get('judge_score')}")
                else:
                    m3.metric("Chars", r.get("char_count") or len(r.get("text") or ""))
                if r.get("chunked"):
                    st.caption(f"🔪 auto-chunked into {r.get('n_chunks')} parts")
                if _DIAR_CAPABLE.get(r["provider"]) and not r.get("has_diarization"):
                    st.caption("🗣️ diarization requested — engine returned no speaker labels "
                               "(check the raw JSON in data/runs/<id>/)")
                if r.get("judge_comment"):
                    st.caption("🧑‍⚖️ " + r["judge_comment"])
                st.text_area(
                    "transcript", _transcript_with_speakers(r),
                    height=240, key=f"ta_{r['provider']}_{id(r)}", label_visibility="collapsed",
                )


def render_results(results: list[dict], reference: str):
    if not results:
        return
    render_charts(results, reference)
    st.dataframe(summary_table(results, reference), width="stretch", hide_index=True)
    st.divider()
    render_cards(results, reference)


# --------------------------------------------------------------------------- #
# Tabs
# --------------------------------------------------------------------------- #
def tab_run(cfg: dict, opts: dict, selected):
    st.subheader("Run a benchmark")
    if not A.ffmpeg_available():
        st.warning("⚠️ ffmpeg/ffprobe not found — audio normalization will fail. "
                   "Install ffmpeg (the devcontainer/packages.txt do this automatically).")

    ups = st.file_uploader(
        "Upload one or MORE audio files (wav / mp3 / m4a / mp4 / ogg …). Long files "
        "are fine — Cartesia uses smart VAD; length-limited engines are auto-chunked.",
        type=["wav", "mp3", "m4a", "mp4", "ogg", "flac", "webm", "aac"],
        accept_multiple_files=True,
    )
    reference = st.text_area(
        "Reference transcript (optional, single-file runs only) — if provided, we "
        "compute WER/CER; otherwise the LLM judge (Qwen3.7-Max by default) scores "
        "the outputs.", height=100,
    )
    if ups and len(ups) == 1:
        st.audio(ups[0].getvalue())
    elif ups:
        st.caption(f"🎧 {len(ups)} files queued: " + " · ".join(u.name for u in ups))
        if reference.strip():
            st.info("Reference is ignored for multi-file batches — the LLM judge "
                    "scores each file instead.")

    cols = st.columns([1, 3])
    run = cols[0].button("▶️ Run benchmark", type="primary",
                         disabled=not (ups and selected))
    if not selected:
        cols[1].info("Select at least one configured provider in the sidebar.")

    if run and ups:
        _execute(cfg, opts, selected, ups, reference)

    # show last results (persist across reruns)
    batch = st.session_state.get("last_batch")
    if batch:
        st.divider()
        render_batch(batch)


def _execute(cfg, opts, selected, ups, reference):
    batch_id = storage.new_run_id()
    multi = len(ups) > 1
    batch = []
    for idx, up in enumerate(ups, 1):
        workdir = A.make_workdir()
        src = os.path.join(workdir, up.name)
        with open(src, "wb") as f:
            f.write(up.getvalue())

        tag = f"[{idx}/{len(ups)}] {up.name}"
        status = st.status(f"{tag}: preparing audio…", expanded=True)
        try:
            wav, duration = prepare_audio(src, workdir)
        except Exception as exc:  # noqa: BLE001
            status.update(label=f"{tag}: audio prep failed: {exc}", state="error")
            continue

        if opts.get("preprocess") == "isolate":
            status.write("🔇 Voice Isolator: stripping background noise…")
            try:
                wav = P.voice_isolate(cfg, wav, workdir)
                duration = A.probe_duration_seconds(wav)
            except Exception as exc:  # noqa: BLE001
                status.update(label=f"{tag}: voice isolation failed: {exc}", state="error")
                continue

        ref = reference if not multi else ""
        status.write(f"Normalized to 16 kHz mono · {duration:.1f}s · "
                     f"running {len(selected)} providers in parallel…")
        done = []
        prog = status.progress(0.0)

        def cb(label, _done=done, _prog=prog):
            _done.append(label)
            _prog.progress(len(_done) / len(selected), text=f"finished {label}")

        results = run_benchmark(
            selected, wav, duration, ref, opts["language"], opts["diarize"],
            cfg, workdir, progress_cb=cb,
        )
        status.update(label=f"{tag}: scoring + saving…", state="running")

        run_id = storage.new_run_id()
        storage.save_audio_copy(run_id, wav)
        storage.save_run({
            "run_id": run_id, "audio_filename": up.name, "audio_duration": duration,
            "reference_text": ref, "language_hint": opts["language"],
            "diarize": opts["diarize"], "audio_path": storage.run_dir(run_id) + "/audio.wav",
            "label": opts.get("label") or None,
            "preprocess": opts.get("preprocess") or "none",
            "batch_id": batch_id if multi else None,
        })
        rows = []
        for r in results:
            row = r.to_row()
            row["char_count"] = r.char_count
            storage.save_result(run_id, row, raw=r.raw if isinstance(r.raw, (dict, list)) else None)
            rows.append(row)
        batch.append({"run_id": run_id, "file": up.name, "rows": rows, "reference": ref})
        status.update(label=f"{tag}: done — saved as {run_id}", state="complete")

    st.session_state["last_batch"] = batch
    st.rerun()


def render_batch(batch: list[dict]):
    if not batch:
        return
    if len(batch) == 1:
        st.subheader(f"Results — {batch[0]['run_id']}")
        render_results(batch[0]["rows"], batch[0].get("reference") or "")
        return

    st.subheader(f"Batch results — {len(batch)} files")
    st.caption("Per-provider averages across all files in this batch.")
    agg: dict[str, dict] = {}
    for entry in batch:
        for r in entry["rows"]:
            a = agg.setdefault(r["provider"],
                               {"ok": 0, "n": 0, "lat": [], "rtf": [], "metric": [], "judge": []})
            a["n"] += 1
            if r.get("ok"):
                a["ok"] += 1
                if r.get("latency_s") is not None:
                    a["lat"].append(r["latency_s"])
                if r.get("rtf") is not None:
                    a["rtf"].append(r["rtf"])
                if r.get("metric_value") is not None:
                    a["metric"].append(r["metric_value"])
                if r.get("judge_score") is not None:
                    a["judge"].append(r["judge_score"])

    def _mean(xs):
        return round(sum(xs) / len(xs), 3) if xs else None

    df = pd.DataFrame([{
        "Provider": prov,
        "OK": f"{a['ok']}/{a['n']}",
        "Avg latency (s)": _mean(a["lat"]),
        "Avg RTF": _mean(a["rtf"]),
        "Avg error %": (round(_mean(a["metric"]) * 100, 1) if a["metric"] else None),
        "Avg judge": _mean(a["judge"]),
    } for prov, a in agg.items()])
    st.dataframe(df, width="stretch", hide_index=True)

    for entry in batch:
        with st.expander(f"📄 {entry['file']} — {entry['run_id']}"):
            render_results(entry["rows"], entry.get("reference") or "")


def tab_history():
    st.subheader("Past runs")
    runs = storage.list_runs()
    if not runs:
        st.info("No runs yet. Run a benchmark to populate history.")
        return
    df = pd.DataFrame([{
        "Run": r["run_id"], "When": r["created_at"][:19].replace("T", " "),
        "Audio": r["audio_filename"], "Duration (s)": round(r.get("audio_duration") or 0, 1),
        "Label": r.get("label") or "", "Preprocess": r.get("preprocess") or "",
        "Batch": (r.get("batch_id") or "")[-6:],
        "Providers": r["n_results"], "Has reference": "yes" if r.get("reference_text") else "no",
    } for r in runs])
    st.dataframe(df, width="stretch", hide_index=True)

    pick = st.selectbox("Open a run", [r["run_id"] for r in runs])
    if pick:
        data = storage.get_run(pick)
        if not data:
            return
        c1, c2 = st.columns([4, 1])
        extra = []
        if data.get("label"):
            extra.append(f"label: {data['label']}")
        if data.get("preprocess") and data["preprocess"] != "none":
            extra.append(f"preprocess: {data['preprocess']}")
        c1.caption(f"Audio: {data['audio_filename']} · {round(data.get('audio_duration') or 0,1)}s"
                   + ("" if not extra else " · " + " · ".join(extra)))
        if c2.button("🗑️ Delete run"):
            storage.delete_run(pick)
            st.rerun()
        apath = os.path.join(storage.run_dir(pick), "audio.wav")
        if os.path.exists(apath):
            st.audio(apath)
        if data.get("reference_text"):
            with st.expander("Reference transcript"):
                st.write(data["reference_text"])
        for res in data["results"]:
            res["char_count"] = len(res.get("text") or "")
        render_results(data["results"], data.get("reference_text") or "")


def tab_providers(cfg: dict, providers):
    st.subheader("Providers & capabilities")
    rows = []
    for p in providers:
        rows.append({
            "Provider": p.label,
            "Configured": "🟢" if p.is_configured() else "⚪",
            "Diarization": "✅" if p.supports_diarization else "—",
            "Long audio": "native" if p.max_audio_seconds is None else f"chunk >{int(p.max_audio_seconds)}s",
            "Notes": p.notes,
        })
    st.dataframe(pd.DataFrame(rows), width="stretch", hide_index=True)
    st.divider()
    _integration_notes()


def _integration_notes():
    st.subheader("📎 Cartesia → FieldSight integration notes")
    st.markdown(
        """
**Goal:** evaluate replacing **AWS Transcribe** with **Cartesia Ink** in the
VAD → Transcribe → Report pipeline.

**Where it plugs in (`src/lambda_transcribe.py`):** today this Lambda starts an
AWS Transcribe job on each `audio_segments/` object and writes
`transcripts/{user}/{date}/{file}.json`. To swap engines, replace the
`start_transcription_job` call with a Cartesia `POST /stt` (or streaming WS) and
**emit the same transcript JSON shape** that `transcript_utils.normalize_transcript()`
already expects, so report generation is untouched.

**Watch-outs for the swap (vs. AWS):**
- **No multi-speaker diarization** in Ink — AWS gives `speaker_labels`. If reports
  rely on speakers, keep AWS (or add a separate diarizer) for meetings.
- Ink is **streaming-first** with built-in smart VAD — you may be able to drop the
  separate Silero VAD Lambda for the Cartesia path and feed raw audio directly.
- Re-base timestamps the same way (`base_time + vad_offset + word.start`,
  see BUG-09) using Ink's word timestamps.

**Use this tool first:** benchmark Ink vs AWS vs the Chinese engines on your real
VAD segments (WER/CER + latency) before committing to the migration.
        """
    )


# --------------------------------------------------------------------------- #
def main():
    cfg, opts = sidebar()
    providers, selected = provider_picker(cfg)
    st.title("FieldSight ASR Benchmark")
    st.caption("Cartesia · ElevenLabs Scribe · Plaud · Soniox · AWS Transcribe · "
               "Zhipu GLM-ASR · Qwen3-ASR · Ali Fun-ASR · Doubao Seed-ASR — "
               "accuracy, speed & speakers, side by side.")
    t1, t2, t3 = st.tabs(["🎤 Run", "📊 History", "ℹ️ Providers & Integration"])
    with t1:
        tab_run(cfg, opts, selected)
    with t2:
        tab_history()
    with t3:
        tab_providers(cfg, providers)


if __name__ == "__main__":
    main()
