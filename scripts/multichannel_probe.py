"""Can channel index stand in for speaker identity on a multi-device recording?

Scribe v2 transcribes up to 5 channels independently and tags every word with
`channel_index`. If each device is dominated by its own wearer, that tag IS the
speaker — no embeddings, no clustering, no voiceprints, no consent surface. This
script answers whether that holds, from audio we already have, for free.

    python scripts/multichannel_probe.py \
        --bucket fieldsight-data-509194952652 \
        --date 2026-08-07 \
        --a Ben_UCPK:sid39ad6c92bdbb4034b1de7748c56316dd \
        --b Ben_UCPK2:sid61be49d563524f51b17c54c67733b08c \
        --truth truth.csv          # optional: start,end,who  (seconds into the overlap)

Two things it measures, in order, because the second is meaningless without the
first:

1. **Clock skew.** Devices timestamp chunks from their own clock and share none.
   Measured on 2026-08-07 at 0.75-0.92 s — 7.5-9x the ~100 ms margin that would let you
   align by filename — and it STEPS mid-session rather than drifting, so it must
   be measured per chunk pair, never once per session.

2. **Whether the louder channel is the speaker.** The decisive statistic is the
   level difference's spread and sign, not its mean. A near-constant difference
   means both microphones hear the same room at a fixed ratio and the channel
   index says nothing. A difference that swings and changes sign means different
   people dominate at different moments.

With `--truth`, it scores that directly: for each labelled span, is the louder
channel the one worn by the person who was speaking?
"""
import argparse
import csv
import os
import re
import subprocess
import sys
import tempfile
import wave

import numpy as np

CHUNK_RE = re.compile(r"_(\d{2})-(\d{2})-(\d{2})_sid([0-9a-f]{32})_c(\d{4})\.wav$")

# ----------------------------------------------------------------------------
# Pure functions — the judgement lives here, so it can be tested without S3.
# ----------------------------------------------------------------------------


def parse_chunk(key):
    """(seconds_into_day, chunk_index) from a chunk key, or None.

    The device's own wall clock, which is exactly the thing under suspicion —
    two devices disagree by ~0.9 s, so these values order chunks but must never
    be trusted to align them.
    """
    m = CHUNK_RE.search(key or "")
    if not m:
        return None
    h, mi, s, _sid, idx = m.groups()
    return int(h) * 3600 + int(mi) * 60 + int(s), int(idx)


def pair_chunks(a_chunks, b_chunks, chunk_seconds=30.0):
    """Chunks from two devices that cover a common wall-clock window.

    Returns (a_key, b_key, a_offset, b_offset) where the offsets are seconds to
    skip into each file so both nominally start at the same instant. Nominally:
    the whole point of the skew measurement is that they do not really.
    """
    out = []
    for a_key, a_start in a_chunks:
        for b_key, b_start in b_chunks:
            overlap_start = max(a_start, b_start)
            overlap_end = min(a_start, b_start) + chunk_seconds
            if overlap_end - overlap_start >= 10:
                out.append((a_key, b_key,
                            overlap_start - a_start, overlap_start - b_start))
                break
    return out


def estimate_skew(a, b, sr, max_lag_s=3.0, window_s=6.0, min_peak=0.08):
    """Median lag in ms where `a` matches `b`, over several windows.

    Positive means the same sound lands LATER in `a`.

    Two traps this exists to avoid, both of which produced confident wrong
    numbers before they were caught:

    * take the POSITIVE peak, not the largest absolute one. The same acoustic
      event on two microphones correlates positively; `argmax(|c|)` happily
      returns a negative trough.
    * one window is not a sample. Windows that disagree by hundreds of ms mean
      the estimate is noise, so the spread is returned and the caller must look
      at it.

    Returns (median_ms, spread_ms, n_usable, n_total). median is None when too
    few windows produced a usable peak.
    """
    win = int(window_s * sr)
    step = int(window_s * sr / 2)
    n = min(len(a), len(b))
    ests = []
    total = 0
    for start in range(0, max(n - win, 0) + 1, step):
        x, y = a[start:start + win], b[start:start + win]
        if len(x) < win or len(y) < win:
            continue
        total += 1
        x = (x - x.mean()) / (x.std() + 1e-9)
        y = (y - y.mean()) / (y.std() + 1e-9)
        m = len(x)
        c = np.fft.irfft(np.fft.rfft(x, 2 * m) * np.conj(np.fft.rfft(y, 2 * m)), 2 * m)
        w = int(max_lag_s * sr)
        c = np.concatenate([c[-w:], c[:w + 1]]) / m
        lags = np.arange(-w, w + 1)
        k = int(np.argmax(c))                       # positive peak only
        if c[k] >= min_peak:
            ests.append(lags[k] / sr * 1000.0)
    if len(ests) < 2:
        return None, None, len(ests), total
    return float(np.median(ests)), float(max(ests) - min(ests)), len(ests), total


def level_difference(a, b, sr, skew_ms, window_s=0.5, floor_rms=20.0):
    """Per-window (t, a_dbfs, b_dbfs, difference) after applying the skew.

    Windows where either device is essentially silent are dropped: they carry no
    information about who was talking, and including them would drag the spread
    toward zero and make a working signal look like a constant offset.
    """
    shift = int(skew_ms / 1000.0 * sr)
    if shift > 0:
        a = a[shift:]
    elif shift < 0:
        b = b[-shift:]
    n = min(len(a), len(b))
    win = int(window_s * sr)
    rows = []
    for i in range(0, n - win, win):
        ra = float(np.sqrt((a[i:i + win] ** 2).mean()))
        rb = float(np.sqrt((b[i:i + win] ** 2).mean()))
        if ra < floor_rms or rb < floor_rms:
            continue
        da = 20 * np.log10(ra / 32768)
        db = 20 * np.log10(rb / 32768)
        rows.append((i / sr, da, db, da - db))
    return rows


def verdict(rows):
    """Does the level difference carry speaker information?

    Mean is deliberately ignored. A device can sit 11 dB below the other all
    session for reasons that have nothing to do with who is speaking — two
    microphone part variants on the same board differ by 15 dB. What matters is
    whether the difference MOVES, and whether it crosses zero: that is what
    "sometimes this device's wearer is the loud one" looks like.
    """
    if len(rows) < 8:
        return {"usable": False, "reason": f"only {len(rows)} windows with signal on both"}
    d = np.array([r[3] for r in rows])
    return {
        "usable": True,
        "windows": len(rows),
        "mean_db": round(float(d.mean()), 1),
        "sd_db": round(float(d.std()), 1),
        "swing_db": round(float(d.max() - d.min()), 1),
        "crosses_zero": bool((d.max() > 0) and (d.min() < 0)),
        "pct_a_louder": round(float((d > 0).mean() * 100), 1),
    }


def score_against_truth(rows, truth, a_name, b_name):
    """For each labelled span, was the louder channel the speaker's own device?

    This is the only part that actually proves anything. Everything above says
    the signal has the right SHAPE; this says it points at the right person.
    """
    hits = misses = 0
    per_span = []
    for start, end, who in truth:
        inside = [r for r in rows if start <= r[0] < end]
        if not inside:
            per_span.append((start, end, who, None, 0))
            continue
        mean_diff = float(np.mean([r[3] for r in inside]))
        louder = a_name if mean_diff > 0 else b_name
        ok = (louder == who)
        hits, misses = hits + ok, misses + (not ok)
        per_span.append((start, end, who, louder, len(inside)))
    return hits, misses, per_span


# ----------------------------------------------------------------------------
# I/O
# ----------------------------------------------------------------------------


def read_wav(path):
    with wave.open(path) as w:
        if w.getnchannels() != 1:
            raise ValueError(f"{path}: expected mono, got {w.getnchannels()}")
        return (np.frombuffer(w.readframes(w.getnframes()), dtype=np.int16).astype(np.float64),
                w.getframerate())


def s3_list(bucket, prefix, region):
    out = subprocess.run(["aws", "s3", "ls", f"s3://{bucket}/{prefix}",
                          "--region", region], capture_output=True, text=True, check=True)
    return [line.split()[-1] for line in out.stdout.splitlines() if line.strip().endswith(".wav")]


def s3_get(bucket, key, dest, region):
    subprocess.run(["aws", "s3", "cp", f"s3://{bucket}/{key}", dest,
                    "--region", region, "--quiet"], check=True)


def trim(src, dst, skip_s, dur_s):
    # Distinct output name, always: on a case-insensitive filesystem `-i A.wav`
    # writing `a.wav` reads and writes the same file and destroys it. One
    # measurement was computed on a 78-byte truncated file before that was caught.
    subprocess.run(["ffmpeg", "-v", "error", "-y", "-ss", str(skip_s), "-t", str(dur_s),
                    "-i", src, "-ac", "1", "-ar", "16000", dst], check=True)


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--bucket", required=True)
    ap.add_argument("--date", required=True)
    ap.add_argument("--a", required=True, help="folder:sid of device A")
    ap.add_argument("--b", required=True, help="folder:sid of device B")
    ap.add_argument("--region", default="ap-southeast-2")
    ap.add_argument("--pairs", type=int, default=4, help="chunk pairs to sample across the session")
    ap.add_argument("--truth", help="CSV start,end,who — seconds into each pair's overlap")
    args = ap.parse_args()

    a_folder, a_sid = args.a.split(":")
    b_folder, b_sid = args.b.split(":")

    def chunks(folder, sid):
        keys = s3_list(args.bucket, f"users/{folder}/audio/{args.date}/", args.region)
        rows = []
        for k in keys:
            p = parse_chunk(k)
            if p and sid in k:
                rows.append((f"users/{folder}/audio/{args.date}/{k}", p[0]))
        return sorted(rows, key=lambda r: r[1])

    a_chunks, b_chunks = chunks(a_folder, a_sid), chunks(b_folder, b_sid)
    print(f"{a_folder}: {len(a_chunks)} chunks   {b_folder}: {len(b_chunks)} chunks")
    pairs = pair_chunks(a_chunks, b_chunks)
    if not pairs:
        print("no overlapping chunk pairs — are these the same meeting?")
        return 1
    step = max(1, len(pairs) // args.pairs)
    sample = pairs[::step][:args.pairs]
    print(f"{len(pairs)} overlapping pairs; sampling {len(sample)}\n")

    truth = []
    if args.truth:
        with open(args.truth, encoding="utf-8") as fh:
            for row in csv.reader(fh):
                if len(row) >= 3 and not row[0].lstrip().startswith("#"):
                    truth.append((float(row[0]), float(row[1]), row[2].strip()))

    tmp = tempfile.mkdtemp(prefix="mcprobe-")
    all_hits = all_misses = 0
    for i, (ak, bk, aoff, boff) in enumerate(sample):
        s3_get(args.bucket, ak, os.path.join(tmp, f"p{i}_A_raw.wav"), args.region)
        s3_get(args.bucket, bk, os.path.join(tmp, f"p{i}_B_raw.wav"), args.region)
        dur = 30.0 - max(aoff, boff)
        trim(os.path.join(tmp, f"p{i}_A_raw.wav"), os.path.join(tmp, f"p{i}_A_cut.wav"), aoff, dur)
        trim(os.path.join(tmp, f"p{i}_B_raw.wav"), os.path.join(tmp, f"p{i}_B_cut.wav"), boff, dur)
        a, sr = read_wav(os.path.join(tmp, f"p{i}_A_cut.wav"))
        b, _ = read_wav(os.path.join(tmp, f"p{i}_B_cut.wav"))

        skew, spread, used, total = estimate_skew(a, b, sr)
        head = f"pair {i}  {os.path.basename(ak)[-14:-4]} / {os.path.basename(bk)[-14:-4]}"
        if skew is None:
            print(f"{head}\n  skew: unusable ({used}/{total} windows had a peak)\n")
            continue
        flag = "  !! windows disagree" if spread > 100 else ""
        print(f"{head}\n  skew {skew:+.0f} ms  (spread {spread:.0f} ms, {used}/{total} windows){flag}")

        rows = level_difference(a, b, sr, skew)
        v = verdict(rows)
        if not v["usable"]:
            print(f"  level: {v['reason']}\n")
            continue
        print(f"  level A-B: mean {v['mean_db']:+.1f} dB  sd {v['sd_db']}  swing {v['swing_db']} dB  "
              f"crosses zero: {v['crosses_zero']}  A louder in {v['pct_a_louder']}% of windows")
        if truth:
            h, m, spans = score_against_truth(rows, truth, a_folder, b_folder)
            all_hits, all_misses = all_hits + h, all_misses + m
            for s, e, who, louder, n in spans:
                mark = "?" if louder is None else ("ok" if louder == who else "WRONG")
                print(f"    {s:6.1f}-{e:6.1f}s  said by {who:<12} louder {str(louder):<12} {mark} ({n}w)")
        print()

    if truth:
        tot = all_hits + all_misses
        print(f"=== louder channel identified the speaker in {all_hits}/{tot} labelled spans"
              + (f" ({all_hits/tot*100:.0f}%)" if tot else ""))
        print("    below ~90% this cannot carry attribution on its own.")
    else:
        print("No --truth given, so this shows only whether the signal has the right SHAPE.")
        print("Proving it points at the right person needs who-spoke-when inside the overlap.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
