#!/usr/bin/env python3
"""Phase 0 of speaker identity: can two people six metres away be told apart?

Everything measured so far answers a different question — "does the person at 6 m still
resemble himself" — and the answer has been yes (86% held-out, 58 other-speaker turns with
no false positive). The question that decides whether Phase D is worth building is whether
*two* distant people are distinguishable **from each other**, and it cannot be answered on a
two-person recording: the canonical failure is the wearer forming one clean cluster while
every distant speaker collapses into a single "other", which on two people produces exactly
two clusters and looks like success.

Material: `fieldsight-vad-check/2026-08-11-blockV-script/` (script + ground-truth sheet).
Design it implements: `docs/superpowers/specs/2026-08-09-speaker-identity-v2.md` §0.

Two subcommands, because the labelling in the middle is a human step and pretending
otherwise is how ground truth goes wrong:

    segment   split the session on its 4-second separators, write segments.csv and one wav
              per segment so a person can listen and fill in the speaker column
    score     read the labelled segments.csv plus the enrolment reads, and answer the gate

Embeddings are computed locally with SpeechBrain's ECAPA-TDNN (192-d, matching the
`vector(192)` column the design already specifies). Self-hosted rather than a provider API
for a compliance reason, not a technical one: a voiceprint is biometric data under the NZ
Privacy Act, and a workspace-level speaker library at a vendor is a multi-tenant problem we
do not need. The model is downloaded on first use (~80 MB).

Usage
-----
    python scripts/speaker_phase0.py segment --session BlockV.wav --out runs/blockv/
    # fill in the `speaker` and `distance_m` columns of runs/blockv/segments.csv
    python scripts/speaker_phase0.py score --segments runs/blockv/segments.csv \
        --enrol Ben=enrol/ben.wav --enrol David=enrol/david.wav --session BlockV.wav

Run it twice — once on the raw upload under `users/…/audio/`, once on the normalised
`audio_segments/` copy — and compare. §0.3 of the design requires both.
"""
from __future__ import annotations

import argparse
import csv
import math
import os
import sys
import wave
from dataclasses import dataclass, field

import numpy as np

# ============================================================
# Pure arithmetic — the part that decides "separable", and the part worth testing
# ============================================================


@dataclass
class Segment:
    """One stretch of speech between two separators. Times are seconds into the file."""
    index: int
    start_s: float
    end_s: float

    @property
    def duration_s(self) -> float:
        return self.end_s - self.start_s


def split_on_silence(audio: np.ndarray, sr: int, min_silence_s: float = 3.0,
                     frame_s: float = 0.05, rel_floor_db: float = 30.0) -> list[Segment]:
    """Split on the deliberate 4-second separators, not on breaths.

    The threshold is relative to the file's own loud frames, never absolute: this device
    records systematically quiet (measured medians of −36 and −33 dBFS against a normal
    −20…−12), so any fixed dBFS gate is calibrated for a different microphone. `rel_floor_db`
    below the 90th-percentile frame is the floor.

    Returns [] for a file with no speech rather than one segment spanning everything —
    "there is nothing here" and "it is all one turn" must not look the same.
    """
    if audio.size == 0:
        return []
    frame = max(1, int(frame_s * sr))
    n = audio.size // frame
    if n == 0:
        return []
    frames = audio[: n * frame].reshape(n, frame)
    rms = np.sqrt(np.mean(frames.astype(np.float64) ** 2, axis=1))
    peak = float(np.percentile(rms, 90))
    if peak <= 0:
        return []
    floor = peak * (10.0 ** (-rel_floor_db / 20.0))
    loud = rms > floor
    if not loud.any():
        return []

    gap_frames = max(1, int(math.ceil(min_silence_s / frame_s)))
    segments: list[Segment] = []
    start = None
    quiet_run = 0
    for i, is_loud in enumerate(loud):
        if is_loud:
            if start is None:
                start = i
            quiet_run = 0
        else:
            if start is None:
                continue
            quiet_run += 1
            if quiet_run >= gap_frames:
                end = i - quiet_run + 1
                segments.append(Segment(len(segments), start * frame_s, end * frame_s))
                start = None
                quiet_run = 0
    if start is not None:
        segments.append(Segment(len(segments), start * frame_s, n * frame_s))
    return segments


def cosine(a: np.ndarray, b: np.ndarray) -> float:
    """Cosine similarity. Loudness-invariant on purpose: across 0–6 m the level moves by
    ~20 dB, and a score that moved with it would be measuring the microphone."""
    a = np.asarray(a, dtype=np.float64).ravel()
    b = np.asarray(b, dtype=np.float64).ravel()
    na, nb = np.linalg.norm(a), np.linalg.norm(b)
    if na == 0 or nb == 0:
        return 0.0
    return float(np.dot(a, b) / (na * nb))


@dataclass
class Separability:
    separable: bool | None
    overlap: int
    best_accuracy: float | None
    best_threshold: float | None
    fitted_on_the_same_data: bool
    n_same: int
    n_diff: int
    note: str = ""


def separability(same: list[float], diff: list[float], min_n: int = 5) -> Separability:
    """Do the same-person and different-person similarities actually come apart?

    `overlap` counts the pairs that a single threshold cannot get right — the number of
    different-person scores at or above the lowest same-person score, plus the number of
    same-person scores at or below the highest different-person score. Medians are
    deliberately not the verdict: a 12.6 dB median gap with complete range overlap is what
    withdrew Phase A, and it looked convincing until the ranges were plotted.

    `best_accuracy` is the accuracy of the best possible cut **fitted on this same data**, so
    it is an upper bound and is labelled as one. Nothing here is a held-out result.

    With too little data the answer is None, not False. "We could not tell" and "they are not
    separable" are different findings and only one of them stops the project.
    """
    if len(same) < min_n or len(diff) < min_n:
        return Separability(None, 0, None, None, True, len(same), len(diff),
                            note=f"too few pairs to decide: {len(same)} same / "
                                 f"{len(diff)} different, need {min_n} of each")
    lo_same, hi_diff = min(same), max(diff)
    overlap = sum(1 for d in diff if d >= lo_same) + sum(1 for s in same if s <= hi_diff)

    best_acc, best_thr = 0.0, None
    for thr in sorted(set(same) | set(diff)):
        acc = (sum(1 for s in same if s >= thr) + sum(1 for d in diff if d < thr)) \
            / (len(same) + len(diff))
        if acc > best_acc:
            best_acc, best_thr = acc, thr
    return Separability(overlap == 0, overlap, best_acc, best_thr, True,
                        len(same), len(diff),
                        note="best_accuracy is fitted on this data — an upper bound, not a "
                             "held-out result")


@dataclass
class Collapse:
    collapsed: bool | None
    detail: list[str] = field(default_factory=list)
    note: str = ""


def collapse_report(sims: dict[tuple[str, str], list[float]]) -> Collapse:
    """Did the distant speakers merge into one "other"?

    `sims[(spoken_by, profile_of)]` is the list of similarities between that person's turns
    and that profile. Collapse means at least one distant person resembles a *different*
    distant person's profile more than their own — which is precisely the outcome that, with
    only two people in the room, produces two tidy clusters and reads as success.

    Fewer than two distinct people cannot answer the question, and the answer is None. This
    is the whole reason Phase 0 needs ≥3 people with at least two of them distant.
    """
    people = sorted({a for a, _ in sims} | {b for _, b in sims})
    if len(people) < 2:
        return Collapse(None, note="fewer than two speakers — this is exactly the recording "
                                   "that cannot answer the question")
    detail, collapsed = [], False
    for person in people:
        own = sims.get((person, person))
        if not own:
            continue
        own_mean = float(np.mean(own))
        for other in people:
            if other == person:
                continue
            cross = sims.get((person, other))
            if not cross:
                continue
            cross_mean = float(np.mean(cross))
            if cross_mean > own_mean:
                collapsed = True
                detail.append(
                    f"{person} matches {other}'s profile better than their own "
                    f"({cross_mean:.3f} > {own_mean:.3f})")
    return Collapse(collapsed, detail)


# ============================================================
# I/O and the model — imported lazily so the arithmetic above stays testable
# ============================================================


def read_wav_mono(path: str) -> tuple[np.ndarray, int]:
    """16-bit PCM WAV to float32 mono in [-1, 1]. Stereo is averaged, which is wrong for
    per-microphone work and fine here — the identity question is not a channel question."""
    with wave.open(path, "rb") as w:
        sr, ch, width, n = w.getframerate(), w.getnchannels(), w.getsampwidth(), w.getnframes()
        raw = w.readframes(n)
    if width != 2:
        raise SystemExit(f"{path}: expected 16-bit PCM, got {width * 8}-bit")
    data = np.frombuffer(raw, dtype="<i2").astype(np.float32) / 32768.0
    if ch > 1:
        data = data.reshape(-1, ch).mean(axis=1)
    return data, sr


def write_wav(path: str, audio: np.ndarray, sr: int) -> None:
    clipped = np.clip(audio, -1.0, 1.0)
    with wave.open(path, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(sr)
        w.writeframes((clipped * 32767.0).astype("<i2").tobytes())


class Embedder:
    """ECAPA-TDNN, 192-d, run locally. Imported here so the rest of this file needs only
    numpy — the tests exercise the arithmetic, not an 80 MB download."""

    def __init__(self, cache_dir: str = ".models/ecapa"):
        try:
            import torch                                    # noqa: F401
            from speechbrain.inference.speaker import EncoderClassifier
        except ImportError as e:                            # pragma: no cover - env dependent
            raise SystemExit(
                "the embedder needs torch + speechbrain. Run this script as:\n"
                "  uv run --with torch --with speechbrain --with numpy "
                "scripts/speaker_phase0.py …\n"
                f"(import failed: {e})")
        self._torch = __import__("torch")
        # COPY, not the default symlink: creating a symlink on Windows needs a privilege
        # ordinary accounts do not have, and the failure is an OSError 1314 from deep inside
        # the fetch — nothing about it says "this is a Windows permissions default".
        kwargs = {"source": "speechbrain/spkrec-ecapa-voxceleb", "savedir": cache_dir}
        try:
            from speechbrain.utils.fetching import LocalStrategy
            kwargs["local_strategy"] = LocalStrategy.COPY
        except ImportError:                                 # pragma: no cover - old versions
            pass
        self.model = EncoderClassifier.from_hparams(**kwargs)

    def embed(self, audio: np.ndarray, sr: int) -> np.ndarray:
        if sr != 16000:                                     # pragma: no cover - env dependent
            raise SystemExit(f"expected 16 kHz audio, got {sr}. Resample first — the model "
                             f"was trained at 16 kHz and silently degrades otherwise.")
        wav = self._torch.from_numpy(np.ascontiguousarray(audio)).unsqueeze(0)
        with self._torch.no_grad():
            return self.model.encode_batch(wav).squeeze().cpu().numpy()


# ============================================================
# Subcommands
# ============================================================

SEGMENTS_HEADER = ["index", "start_s", "end_s", "duration_s", "speaker", "distance_m", "note"]


def cmd_segment(args) -> int:
    audio, sr = read_wav_mono(args.session)
    segs = split_on_silence(audio, sr, min_silence_s=args.min_silence)
    os.makedirs(args.out, exist_ok=True)
    csv_path = os.path.join(args.out, "segments.csv")
    with open(csv_path, "w", newline="", encoding="utf-8") as f:
        writer = csv.writer(f)
        writer.writerow(SEGMENTS_HEADER)
        for s in segs:
            clip = audio[int(s.start_s * sr): int(s.end_s * sr)]
            write_wav(os.path.join(args.out, f"seg{s.index:03d}.wav"), clip, sr)
            writer.writerow([s.index, f"{s.start_s:.2f}", f"{s.end_s:.2f}",
                             f"{s.duration_s:.2f}", "", "", ""])
    total = sum(s.duration_s for s in segs)
    print(f"{len(segs)} segments, {total:.0f}s of speech in a {audio.size / sr:.0f}s file")
    print(f"wrote {csv_path} and seg000.wav … seg{max(0, len(segs) - 1):03d}.wav")
    print("\nFill in `speaker` and `distance_m` from the ground-truth sheet, then run "
          "`score`.\nIf the segment count does not match the script's段落 count, say so "
          "rather than guessing which is which — a mislabelled run looks entirely "
          "reasonable and is the most expensive way to be wrong.")
    return 0


def _load_segments(path: str) -> list[dict]:
    with open(path, newline="", encoding="utf-8") as f:
        rows = [r for r in csv.DictReader(f) if (r.get("speaker") or "").strip()]
    if not rows:
        raise SystemExit(f"{path}: no rows have a speaker filled in — nothing to score")
    return rows


def cmd_score(args) -> int:
    enrol_paths = {}
    for pair in args.enrol:
        if "=" not in pair:
            raise SystemExit(f"--enrol expects NAME=path.wav, got {pair!r}")
        name, path = pair.split("=", 1)
        enrol_paths[name.strip()] = path.strip()

    rows = _load_segments(args.segments)
    audio, sr = read_wav_mono(args.session)
    emb = Embedder()

    profiles = {}
    for name, path in enrol_paths.items():
        a, s = read_wav_mono(path)
        profiles[name] = emb.embed(a, s)
        print(f"enrolled {name} from {os.path.basename(path)} ({a.size / s:.1f}s)")

    unknown = {r["speaker"] for r in rows} - set(profiles)
    if unknown:
        print(f"\n⚠ turns labelled with no enrolment profile: {sorted(unknown)} — they are "
              f"scored against every profile but can never be right. This is usually a "
              f"missing enrolment read, not a result.")

    sims: dict[tuple[str, str], list[float]] = {}
    per_distance: dict[str, list[tuple[str, str, float]]] = {}
    for r in rows:
        spoken_by = r["speaker"].strip()
        clip = audio[int(float(r["start_s"]) * sr): int(float(r["end_s"]) * sr)]
        if clip.size < sr * args.min_turn_s:
            continue
        v = emb.embed(clip, sr)
        best, best_score = None, -1.0
        for name, prof in profiles.items():
            score = cosine(v, prof)
            sims.setdefault((spoken_by, name), []).append(score)
            if score > best_score:
                best, best_score = name, score
        per_distance.setdefault((r.get("distance_m") or "?").strip(), []).append(
            (spoken_by, best, best_score))

    same = [s for (a, b), v in sims.items() if a == b for s in v]
    diff = [s for (a, b), v in sims.items() if a != b for s in v]
    verdict = separability(same, diff, min_n=args.min_n)
    collapse = collapse_report(sims)

    print("\n=== identification accuracy by distance ===")
    for dist in sorted(per_distance):
        rowset = per_distance[dist]
        right = sum(1 for truth, pred, _ in rowset if truth == pred)
        print(f"  {dist:>4} m   {right}/{len(rowset)} turns identified correctly")

    print("\n=== separability ===")
    print(f"  same-person   n={verdict.n_same}"
          + (f"  median {np.median(same):.3f}  range {min(same):.3f}…{max(same):.3f}"
             if same else ""))
    print(f"  cross-person  n={verdict.n_diff}"
          + (f"  median {np.median(diff):.3f}  range {min(diff):.3f}…{max(diff):.3f}"
             if diff else ""))
    print(f"  overlapping pairs: {verdict.overlap}")
    if verdict.best_accuracy is not None:
        print(f"  best fitted cut: {verdict.best_threshold:.3f} → "
              f"{verdict.best_accuracy:.0%}  ← UPPER BOUND, fitted on this same data")
    print(f"  verdict: {verdict.separable}  {verdict.note}")

    print("\n=== did the distant speakers collapse into one? ===")
    if collapse.collapsed is None:
        print(f"  cannot tell — {collapse.note}")
    elif collapse.collapsed:
        print("  YES — this is the failure Phase 0 exists to catch:")
        for line in collapse.detail:
            print(f"    {line}")
    else:
        print("  no — every speaker matches their own profile best")

    print("\n=== the gate (design §0.3) ===")
    print("  wearer separates            → Phase B viable")
    print("  near speakers separate      → Phase C viable")
    print("  DISTANT speakers separate   → Phase D viable; otherwise stop after C")
    print("\nRun this again on the other copy of the audio (raw users/…/audio/ vs "
          "normalised audio_segments/) before concluding anything — §0.3 requires both, and "
          "they have disagreed before.")
    return 0


def main(argv=None) -> int:
    p = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    sub = p.add_subparsers(dest="cmd", required=True)

    seg = sub.add_parser("segment", help="split the session on its 4-second separators")
    seg.add_argument("--session", required=True)
    seg.add_argument("--out", required=True)
    seg.add_argument("--min-silence", type=float, default=3.0,
                     help="seconds of quiet that count as a separator (script says 4)")
    seg.set_defaults(func=cmd_segment)

    sc = sub.add_parser("score", help="answer the Phase 0 gate from a labelled segments.csv")
    sc.add_argument("--segments", required=True)
    sc.add_argument("--session", required=True)
    sc.add_argument("--enrol", action="append", required=True, metavar="NAME=path.wav")
    sc.add_argument("--min-turn-s", type=float, default=1.0,
                    help="turns shorter than this are skipped, not scored badly")
    sc.add_argument("--min-n", type=int, default=5)
    sc.set_defaults(func=cmd_score)

    args = p.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":                                  # pragma: no cover
    sys.exit(main())
