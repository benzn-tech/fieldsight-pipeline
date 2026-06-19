"""Benchmark orchestration.

Given an uploaded audio file and a set of providers:
  1. normalize to 16 kHz mono WAV (once),
  2. run every provider in parallel — auto-chunking long audio for providers
     that declare a ``max_audio_seconds`` limit, and timing each independently,
  3. score: WER/CER when a reference transcript is given, otherwise LLM judge,
  4. return results (the app persists + renders them).
"""
from __future__ import annotations

import os
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import replace

from . import audio as A
from . import judge as J
from . import metrics as M
from providers.base import ASRProvider, ASRResult, Segment


def prepare_audio(src_path: str, workdir: str) -> tuple[str, float]:
    wav = os.path.join(workdir, "audio16k.wav")
    A.normalize_to_wav16k(src_path, wav)
    return wav, A.probe_duration_seconds(wav)


def _merge_chunks(results: list[ASRResult], chunks, provider_label, model) -> ASRResult:
    ok_any = any(r.ok for r in results)
    texts, segments, errors, raws = [], [], [], []
    for r, ch in zip(results, chunks):
        if r.ok and r.text:
            texts.append(r.text)
        if r.error:
            errors.append(f"chunk{ch.index}: {r.error}")
        for s in r.segments:
            segments.append(Segment(s.start + ch.offset_s, s.end + ch.offset_s, s.text, s.speaker))
        raws.append(r.raw)
    return ASRResult(
        provider=provider_label, model=model, ok=ok_any,
        text=" ".join(texts).strip(), segments=segments,
        has_diarization=any(r.has_diarization for r in results),
        raw={"chunks": raws},
        error=("; ".join(errors) if errors and not ok_any else ("partial: " + "; ".join(errors) if errors else "")),
    )


def run_provider(provider: ASRProvider, wav_path: str, duration: float,
                 language, diarize, workdir: str) -> ASRResult:
    t0 = time.time()
    needs_chunk = provider.max_audio_seconds and duration > provider.max_audio_seconds
    if needs_chunk:
        cdir = os.path.join(workdir, f"chunks_{provider.key}")
        chunks = A.split_wav(wav_path, provider.max_audio_seconds, cdir)
        results: list[ASRResult] = [None] * len(chunks)  # type: ignore
        with ThreadPoolExecutor(max_workers=min(4, len(chunks) or 1)) as ex:
            futs = {ex.submit(provider.transcribe_file, c.path, language, diarize): i
                    for i, c in enumerate(chunks)}
            for fut in as_completed(futs):
                results[futs[fut]] = fut.result()
        res = _merge_chunks(results, chunks, provider.label, provider.model)
        res.n_chunks = len(chunks)
        res.chunked = True
    else:
        res = provider.transcribe_file(wav_path, language, diarize)
        res.n_chunks = 1
        res.chunked = False

    res.latency_s = time.time() - t0
    res.audio_duration_s = duration
    res.rtf = M.real_time_factor(res.latency_s, duration)
    return res


def run_benchmark(providers: list[ASRProvider], wav_path: str, duration: float,
                  reference_text: str, language, diarize, config: dict,
                  workdir: str, progress_cb=None) -> list[ASRResult]:
    results: dict[str, ASRResult] = {}

    def _track(label):
        if progress_cb:
            progress_cb(label)

    with ThreadPoolExecutor(max_workers=max(1, len(providers))) as ex:
        futs = {ex.submit(run_provider, p, wav_path, duration, language, diarize, workdir): p
                for p in providers}
        for fut in as_completed(futs):
            p = futs[fut]
            try:
                results[p.label] = fut.result()
            except Exception as exc:  # noqa: BLE001
                results[p.label] = ASRResult(provider=p.label, model=p.model, ok=False,
                                             error=f"runner error: {exc}")
            _track(p.label)

    ordered = [results[p.label] for p in providers if p.label in results]

    # --- scoring ---
    if reference_text and reference_text.strip():
        for r in ordered:
            if r.ok:
                r.wer = M.compute_wer(reference_text, r.text)
                r.cer = M.compute_cer(reference_text, r.text)
                name, val = M.primary_metric(reference_text, r.text)
                r.metric_name, r.metric_value = name, val
    elif J.judge_available(config):
        scores = J.score_transcripts(config, {r.provider: r.text for r in ordered if r.ok})
        for r in ordered:
            entry = scores.get(r.provider)
            if entry:
                r.judge_score = entry.get("score")
                r.judge_comment = entry.get("reason", "")
                r.metric_name = "Judge"
                r.metric_value = entry.get("score")
    return ordered
