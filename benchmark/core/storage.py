"""Run persistence: every benchmark run (audio + per-provider transcripts +
scores + timings) is written to a local SQLite db and a per-run folder so you
can reload and re-show past results in the demo.

Layout:
    benchmark/data/benchmark.db
    benchmark/data/runs/<run_id>/audio.wav      (normalized copy)
    benchmark/data/runs/<run_id>/<provider>.json (raw provider response)
"""
from __future__ import annotations

import json
import os
import shutil
import sqlite3
import time
import uuid
from datetime import datetime, timezone

_BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DATA_DIR = os.path.join(_BASE, "data")
RUNS_DIR = os.path.join(DATA_DIR, "runs")
DB_PATH = os.path.join(DATA_DIR, "benchmark.db")


def _connect() -> sqlite3.Connection:
    os.makedirs(DATA_DIR, exist_ok=True)
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def init_db() -> None:
    os.makedirs(RUNS_DIR, exist_ok=True)
    with _connect() as conn:
        conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS runs (
                run_id          TEXT PRIMARY KEY,
                created_at      TEXT NOT NULL,
                audio_filename  TEXT,
                audio_path      TEXT,
                audio_duration  REAL,
                reference_text  TEXT,
                language_hint   TEXT,
                diarize         INTEGER,
                notes           TEXT
            );
            CREATE TABLE IF NOT EXISTS results (
                id              INTEGER PRIMARY KEY AUTOINCREMENT,
                run_id          TEXT NOT NULL,
                provider        TEXT NOT NULL,
                model           TEXT,
                ok              INTEGER,
                text            TEXT,
                latency_s       REAL,
                rtf             REAL,
                audio_duration  REAL,
                n_chunks        INTEGER,
                chunked         INTEGER,
                has_diarization INTEGER,
                n_speakers      INTEGER,
                wer             REAL,
                cer             REAL,
                metric_name     TEXT,
                metric_value    REAL,
                judge_score     REAL,
                judge_comment   TEXT,
                error           TEXT,
                segments_json   TEXT,
                created_at      TEXT,
                FOREIGN KEY (run_id) REFERENCES runs(run_id)
            );
            """
        )


def new_run_id() -> str:
    return datetime.now().strftime("%Y%m%d_%H%M%S_") + uuid.uuid4().hex[:6]


def run_dir(run_id: str) -> str:
    d = os.path.join(RUNS_DIR, run_id)
    os.makedirs(d, exist_ok=True)
    return d


def save_audio_copy(run_id: str, src_wav: str) -> str:
    dst = os.path.join(run_dir(run_id), "audio.wav")
    shutil.copyfile(src_wav, dst)
    return dst


def save_run(meta: dict) -> None:
    with _connect() as conn:
        conn.execute(
            """INSERT OR REPLACE INTO runs
               (run_id, created_at, audio_filename, audio_path, audio_duration,
                reference_text, language_hint, diarize, notes)
               VALUES (?,?,?,?,?,?,?,?,?)""",
            (
                meta["run_id"],
                meta.get("created_at") or datetime.now(timezone.utc).isoformat(),
                meta.get("audio_filename"),
                meta.get("audio_path"),
                meta.get("audio_duration"),
                meta.get("reference_text"),
                meta.get("language_hint"),
                int(bool(meta.get("diarize"))),
                meta.get("notes"),
            ),
        )


def save_result(run_id: str, r: dict, raw: dict | None = None) -> None:
    if raw is not None:
        try:
            with open(os.path.join(run_dir(run_id), f"{r['provider']}.json"), "w") as f:
                json.dump(raw, f, ensure_ascii=False, indent=2, default=str)
        except Exception:
            pass
    with _connect() as conn:
        conn.execute(
            """INSERT INTO results
               (run_id, provider, model, ok, text, latency_s, rtf, audio_duration,
                n_chunks, chunked, has_diarization, n_speakers, wer, cer,
                metric_name, metric_value, judge_score, judge_comment, error,
                segments_json, created_at)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (
                run_id, r["provider"], r.get("model"), int(bool(r.get("ok"))),
                r.get("text"), r.get("latency_s"), r.get("rtf"),
                r.get("audio_duration_s"), r.get("n_chunks", 1),
                int(bool(r.get("chunked"))), int(bool(r.get("has_diarization"))),
                r.get("n_speakers"), r.get("wer"), r.get("cer"),
                r.get("metric_name"), r.get("metric_value"),
                r.get("judge_score"), r.get("judge_comment"), r.get("error"),
                json.dumps(r.get("segments", []), ensure_ascii=False, default=str),
                datetime.now(timezone.utc).isoformat(),
            ),
        )


def list_runs(limit: int = 200) -> list[dict]:
    with _connect() as conn:
        rows = conn.execute(
            """SELECT r.*,
                      (SELECT COUNT(*) FROM results x WHERE x.run_id = r.run_id) AS n_results
               FROM runs r ORDER BY r.created_at DESC LIMIT ?""",
            (limit,),
        ).fetchall()
        return [dict(row) for row in rows]


def get_run(run_id: str) -> dict | None:
    with _connect() as conn:
        run = conn.execute("SELECT * FROM runs WHERE run_id = ?", (run_id,)).fetchone()
        if not run:
            return None
        results = conn.execute(
            "SELECT * FROM results WHERE run_id = ? ORDER BY provider", (run_id,)
        ).fetchall()
        out = dict(run)
        out["results"] = []
        for row in results:
            d = dict(row)
            try:
                d["segments"] = json.loads(d.get("segments_json") or "[]")
            except Exception:
                d["segments"] = []
            out["results"].append(d)
        return out


def delete_run(run_id: str) -> None:
    with _connect() as conn:
        conn.execute("DELETE FROM results WHERE run_id = ?", (run_id,))
        conn.execute("DELETE FROM runs WHERE run_id = ?", (run_id,))
    shutil.rmtree(os.path.join(RUNS_DIR, run_id), ignore_errors=True)
