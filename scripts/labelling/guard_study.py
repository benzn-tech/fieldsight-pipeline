"""How well does the enrolment homogeneity guard separate one voice from two?

Frames come from the owner-labelled clips (2026-09-28), cut and embedded by the prod code
(`_frames_at`, `embed_audio`, prod ECAPA onnx). Two populations of 10 s frame PAIRS:

  same   -- two ADJACENT 5 s frames of one clip whose voice the owner named
  cross  -- one frame from person X's clip, one from person Y's clip (X != Y): what a
            window straddling a speaker change looks like to the guard

Then whole windows, to compare the current statistic (max over all pairs) with gentler ones:

  genuine  -- each labelled clip with >= 3 frames, as is
  mixed    -- a genuine clip's frames with ONE frame of another person spliced in the middle
            (the realistic contamination: somebody interjects for a few seconds)

Writes guard_frames.npz so later runs need not re-embed.
"""
import itertools, json, os, sys, wave

import numpy as np

S, CLIPS_DIR = sys.argv[1], sys.argv[2]
sys.path.insert(0, "src")
import lambda_speaker_embed as se  # noqa: E402

se.MODEL_LOCAL = os.path.join(S, "ecapa_tdnn.onnx")
labels = json.load(open(os.path.join(S, "labels.json")))
cache = os.path.join(S, "guard_frames.json")


def read_wav(p):
    with wave.open(p) as w:
        sr, ch = w.getframerate(), w.getnchannels()
        a = np.frombuffer(w.readframes(w.getnframes()), dtype=np.int16).astype(np.float32) / 32768.0
    return (a.reshape(-1, ch).mean(axis=1) if ch > 1 else a), sr


if os.path.exists(cache):
    clips = json.load(open(cache))
else:
    clips = []
    for key in sorted(labels):
        if labels[key].startswith("__"):
            continue
        audio, sr = read_wav(os.path.join(CLIPS_DIR, key + ".wav"))
        fr = se._frames_at(audio, sr)
        step = int(se.FRAME_SECONDS * sr)
        embs = [np.asarray(se.embed_audio(f, sr), dtype=np.float64).ravel() for _, f in fr]
        adjacent = [fr[i + 1][0] == fr[i][0] + step for i in range(len(fr) - 1)]
        clips.append({"clip": key, "who": labels[key], "adjacent": adjacent,
                      "embs": [(e / np.linalg.norm(e)).tolist() for e in embs]})
    json.dump(clips, open(cache, "w"))

for c in clips:
    c["E"] = np.array(c["embs"])


def d(a, b):
    return float(1.0 - a @ b)


same = [d(c["E"][i], c["E"][i + 1]) for c in clips for i in range(len(c["E"]) - 1)
        if c["adjacent"][i]]
cross = [d(a["E"][i], b["E"][j]) for a, b in itertools.combinations(clips, 2)
         if a["who"] != b["who"] for i in range(len(a["E"])) for j in range(len(b["E"]))]
same, cross = np.array(same), np.array(cross)


def pct(x, q):
    return round(float(np.percentile(x, q)), 3)


print(f"people: { {w: sum(c['who'] == w for c in clips) for w in sorted({c['who'] for c in clips})} }")
print(f"\nPAIRS  same n={len(same)}  cross n={len(cross)}")
print(f"  same : min {same.min():.3f}  p50 {pct(same, 50)}  p90 {pct(same, 90)}  max {same.max():.3f}")
print(f"  cross: min {cross.min():.3f}  p1 {pct(cross, 1)}  p5 {pct(cross, 5)}  p50 {pct(cross, 50)}")
for t in (0.35, 0.40, 0.45, 0.50, 0.55, 0.60):
    print(f"  t={t:.2f}: same accepted {np.mean(same <= t):.0%}  cross accepted {np.mean(cross <= t):.2%}")

# ---- whole windows -------------------------------------------------------------------
STATS = {
    "max_all (current)": lambda E: max(d(E[i], E[j]) for i, j in itertools.combinations(range(len(E)), 2)),
    "max_adjacent": lambda E: max(d(E[i], E[i + 1]) for i in range(len(E) - 1)),
    "median_all": lambda E: float(np.median([d(E[i], E[j]) for i, j in itertools.combinations(range(len(E)), 2)])),
    "max_to_centroid": lambda E: max(d(e, (lambda m: m / np.linalg.norm(m))(E.mean(axis=0))) for e in E),
    "max_all_drop1": lambda E: min(max([d(F[i], F[j]) for i, j in itertools.combinations(range(len(F)), 2)] or [0])
                                   for F in (np.delete(E, k, axis=0) for k in range(len(E)))),
}
genuine = [c for c in clips if len(c["E"]) >= 3]
mixed = []
for c in genuine:
    for o in clips:
        if o["who"] != c["who"]:
            for f in o["E"]:
                m = len(c["E"]) // 2
                mixed.append(np.vstack([c["E"][:m], f[None, :], c["E"][m:]]))
print(f"\nWINDOWS  genuine n={len(genuine)}  mixed (one foreign frame spliced in) n={len(mixed)}")
for name, fn in STATS.items():
    g = np.array([fn(c["E"]) for c in genuine])
    m = np.array([fn(E) for E in mixed])
    auc = float(np.mean([[1.0 if x < y else 0.5 if x == y else 0.0 for y in m] for x in g]))
    # the threshold that lets no more than 1% of mixed windows through
    t1 = float(np.percentile(m, 1))
    print(f"  {name:<18} genuine p50 {np.median(g):.3f} max {g.max():.3f} | mixed min {m.min():.3f} "
          f"p1 {t1:.3f} | AUC {auc:.3f} | at t=p1(mixed): genuine pass {np.mean(g < t1):.0%}")
