"""Would a noise gate keep the enrolments worth keeping — and what would it eat?

## The question this answers, and the one it refuses to skip

A noise gate at the enrolment door decides what may become a voiceprint sample. The obvious
way to evaluate one is "does it reject the bad audio", and that is the half that flatters it.

The half that matters is the other one. **This repository has already shipped a gate-shaped
mechanism that silently discarded 15.7% of real speech** — the old VAD configuration — and
nothing anywhere reported it. A noise gate fails the same way: it eats the quiet talker, and
on a site the quiet talker is exactly the person nobody can identify by ear either.

So every candidate metric is reported with **both** numbers:

  * separation — does it tell the clean enrolment audio from the ruined audio at all
  * cost      — of the windows it would reject, how many carry real transcribed speech

The second uses the **transcript as ground truth**: a window the gate would drop is looked up
against the words the ASR produced for that span. Words present means the gate was about to
throw away somebody talking. That truth is independent of every metric being tested, which is
the point — a metric cannot be scored against itself.

## No threshold is proposed here

Only distributions. A metric earns a threshold by separating two classes whose labels came
from somewhere other than the metric; until the separation exists there is nothing to put a
number on, and a number put there anyway is the shape of the several thresholds this project
has had to un-pick.

    python scripts/labelling/gate_probe.py --spec clean=Sam_Yu/2026-08-08 \\
        --spec wind=Ben_UCPK2/2026-09-23 --bucket fieldsight-data-509194952652
"""
from __future__ import annotations

import argparse
import collections
import io
import json
import math
import os
import sys
import wave

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.join(ROOT, "src"))

import transcript_utils  # noqa: E402

#: Candidate metrics. Each takes 16 kHz mono float samples and returns a number where HIGHER
#: means "more likely to be usable speech". None of them is preferred yet; that is the point.
#:
#: Deliberately cheap and dependency-free: a gate that needs a model to decide whether to run
#: a model has moved the problem rather than solved it, and this has to run inside the
#: embedder's Lambda, which already carries onnxruntime and nothing else.


def _frames(x, sr, ms=30):
    n = max(1, int(sr * ms / 1000))
    return [x[i:i + n] for i in range(0, len(x) - n + 1, n)]


def rms_dbfs(x, sr):
    """Loudness. The crudest candidate, and the one with a known confound: the devices record
    systematically quiet (median −36 dBFS), so a level-based gate would reject a whole fleet
    rather than a whole condition."""
    import numpy as np
    r = float(np.sqrt(np.mean(np.square(x)))) if len(x) else 0.0
    return 20.0 * math.log10(max(r, 1e-9))


def snr_proxy(x, sr):
    """Speech-to-background, estimated without knowing which frames are speech.

    The loudest decile of frames stands in for speech and the quietest for the floor. On a
    windy recording the two converge — wind fills the gaps — which is the effect being looked
    for. It is a proxy and is named one: a real SNR needs a speech/non-speech label, and the
    only labeller available here is the VAD whose ownfailure this is partly trying to avoid.
    """
    import numpy as np
    fr = _frames(x, sr)
    if len(fr) < 10:
        return 0.0
    e = sorted(float(np.sqrt(np.mean(np.square(f)))) for f in fr)
    lo = e[max(0, len(e) // 10)]
    hi = e[min(len(e) - 1, len(e) - 1 - len(e) // 10)]
    return 20.0 * math.log10(max(hi, 1e-9) / max(lo, 1e-9))


def spectral_flatness(x, sr):
    """How noise-like the spectrum is, inverted so higher is better.

    Speech is peaky — harmonics and formants; wind and hiss are flat. Independent of level,
    which is what makes it interesting beside `rms_dbfs`: it should survive the fleet's
    quietness while still catching the wind.
    """
    import numpy as np
    fr = _frames(x, sr, ms=64)
    if not fr:
        return 0.0
    vals = []
    for f in fr:
        spec = np.abs(np.fft.rfft(f * np.hanning(len(f)))) + 1e-12
        vals.append(float(np.exp(np.mean(np.log(spec))) / np.mean(spec)))
    return -float(np.median(vals))


def voiced_fraction(x, sr):
    """What share of frames sit well above the recording's own floor.

    Level-relative rather than absolute, so a uniformly quiet recording is not punished for
    being quiet — only for having no dynamics, which is what a wind-filled recording looks
    like.
    """
    import numpy as np
    fr = _frames(x, sr)
    if not fr:
        return 0.0
    e = [float(np.sqrt(np.mean(np.square(f)))) for f in fr]
    floor = sorted(e)[max(0, len(e) // 10)]
    return sum(1 for v in e if v > floor * 3.0) / len(e)


def lf_ratio(x, sr):
    """Energy BELOW the speech band against energy inside it, inverted so higher is better.

    The one candidate aimed at the physics rather than at a summary statistic. Wind on a
    microphone is overwhelmingly low-frequency -- diaphragm buffeting, mostly under ~200 Hz --
    while speech carries its intelligible energy from roughly 300 Hz to 3.4 kHz. Every other
    metric here measures the recording as a whole and hopes the difference shows up; this one
    looks where wind actually lives.

    Level-independent by construction, which matters because the fleet records quiet: it is a
    RATIO of two bands in the same window, so a uniformly soft recording is not penalised.
    """
    import numpy as np
    fr = _frames(x, sr, ms=64)
    if not fr:
        return 0.0
    vals = []
    for f in fr:
        spec = np.abs(np.fft.rfft(f * np.hanning(len(f)))) ** 2
        freqs = np.fft.rfftfreq(len(f), 1.0 / sr)
        low = float(spec[(freqs > 20) & (freqs < 200)].sum())
        band = float(spec[(freqs >= 300) & (freqs < 3400)].sum())
        vals.append(low / max(band, 1e-12))
    return -float(np.median(vals))


METRICS = {"rms_dbfs": rms_dbfs, "snr_proxy": snr_proxy,
           "flatness_inv": spectral_flatness, "voiced_fraction": voiced_fraction,
           "lf_ratio_inv": lf_ratio}


def read_wav(raw):
    with wave.open(io.BytesIO(raw), "rb") as w:
        import numpy as np
        n, sr = w.getnframes(), w.getframerate()
        data = np.frombuffer(w.readframes(n), dtype=np.int16).astype(np.float32) / 32768.0
        if w.getnchannels() > 1:
            data = data[::w.getnchannels()]
    return data, sr


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--spec", action="append", required=True,
                    metavar="LABEL=FOLDER/DATE",
                    help="a class of material, e.g. clean=Sam_Yu/2026-08-08")
    ap.add_argument("--bucket", default="fieldsight-data-509194952652")
    ap.add_argument("--window-s", type=float, default=10.0,
                    help="the unit a gate would judge — the enrolment window length")
    ap.add_argument("--max-windows", type=int, default=40)
    args = ap.parse_args(argv)

    import boto3
    import numpy as np
    s3 = boto3.client("s3")

    rows = []
    for spec in args.spec:
        label, _, path = spec.partition("=")
        folder, _, date = path.partition("/")
        keys = sorted(o["Key"] for o in s3.list_objects_v2(
            Bucket=args.bucket,
            Prefix=f"transcripts/{folder}/{date}/").get("Contents", []))
        if not keys:
            print(f"{label}: no transcripts under {folder}/{date} — a missing "
                  f"s3:ListBucket grant returns 403, not 404, and looks exactly like this",
                  file=sys.stderr)
            continue
        taken = 0
        for k in keys:
            if taken >= args.max_windows:
                break
            doc = json.loads(s3.get_object(Bucket=args.bucket, Key=k)["Body"].read())
            turns = transcript_utils.speaker_turns_from_items(doc.get("results", {})) or \
                (doc.get("results", {}) or {}).get("audio_segments") or []
            seg_key = ("audio_segments/" + k[len("transcripts/"):]).rsplit(".", 1)[0] + ".wav"
            try:
                audio, sr = read_wav(s3.get_object(Bucket=args.bucket,
                                                   Key=seg_key)["Body"].read())
            except Exception as exc:
                print(f"  {label}: no segment audio for {k.split('/')[-1]}: {exc}",
                      file=sys.stderr)
                continue
            # Walk the file in gate-sized windows. Every window is measured, whether or not
            # it holds speech — the cost half of this needs the ones that do NOT, too.
            step = int(args.window_s * sr)
            for start in range(0, max(1, len(audio) - step + 1), step):
                if taken >= args.max_windows:
                    break
                clip = audio[start:start + step]
                if len(clip) < step // 2:
                    continue
                a, b = start / sr, (start + len(clip)) / sr
                # GROUND TRUTH, independent of every metric below: did the ASR put words in
                # this span? A gate that drops a window with words in it is eating speech.
                words = sum(1 for t in turns
                            if float(t.get("end_time", 0)) > a
                            and float(t.get("start_time", 0)) < b
                            and (t.get("transcript") or "").strip())
                rows.append({"label": label, "file": k.split("/")[-1], "start": round(a, 1),
                             "has_speech": words > 0,
                             **{m: round(fn(clip, sr), 4) for m, fn in METRICS.items()}})
                taken += 1

    if not rows:
        raise SystemExit("nothing measured")

    labels = sorted({r["label"] for r in rows})
    print(f"\n{len(rows)} windows of {args.window_s:.0f}s across {labels}\n")
    print(f"{'metric':16s} " + " ".join(f"{l+' (n='+str(sum(1 for r in rows if r['label']==l))+')':>22s}"
                                        for l in labels) + "   separation")
    for m in METRICS:
        cells, means = [], {}
        for l in labels:
            v = [r[m] for r in rows if r["label"] == l]
            means[l] = float(np.mean(v))
            cells.append(f"{np.mean(v):>10.2f} ±{np.std(v):<10.2f}")
        ordered = sorted(means.values())
        # Separation in units of the pooled spread: a gap smaller than the noise within each
        # class is not a gap, however large it looks in raw units.
        pooled = float(np.mean([np.std([r[m] for r in rows if r["label"] == l])
                                for l in labels])) or 1e-9
        sep = (ordered[-1] - ordered[0]) / pooled
        print(f"{m:16s} " + " ".join(cells) + f"   {sep:5.2f}x spread")

    print(f"\n--- the cost half: of the windows each metric would drop, how many hold speech ---")
    print(f"{'metric':16s} {'cut at':>10s} {'dropped':>9s} {'of those, WITH SPEECH':>24s}")
    for m in METRICS:
        vals = sorted(r[m] for r in rows)
        for q in (0.1, 0.25):
            cut = vals[int(len(vals) * q)]
            dropped = [r for r in rows if r[m] <= cut]
            eaten = sum(1 for r in dropped if r["has_speech"])
            print(f"{m:16s} {cut:10.2f} {len(dropped):>4d}/{len(rows):<4d} "
                  f"{eaten:>10d} ({eaten/max(1,len(dropped)):.0%})")

    out = "gate_probe.json"
    open(out, "w", encoding="utf-8").write(json.dumps(rows, indent=2))
    print(f"\nwrote {out} — no threshold is proposed; a metric earns one by separating "
          f"classes labelled from somewhere other than itself")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
