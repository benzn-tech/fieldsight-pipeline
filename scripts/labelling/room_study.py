"""Same-room negatives: Ben and Benny in ONE recording (08-27 11:06, one device, one room).

Frames are pseudo-labelled by their own voice: a frame is Ben's when it is clearly closer
to Ben's profile than to Benny's, and the reverse. Ambiguous frames are dropped. That makes
the negatives conservative in one direction only: the frames kept are the clearest ones,
so the cross distances found here are if anything LARGER than a real interjection's.
"""
import glob, itertools, json, os, sys, wave

import numpy as np

S = sys.argv[1]
sys.path.insert(0, "src")
import lambda_speaker_embed as se  # noqa: E402

se.MODEL_LOCAL = os.path.join(S, "ecapa_tdnn.onnx")


def centroid(path, who):
    vs = [np.array(json.loads(r["emb"])) for r in json.load(open(path)) if r["who"] == who and not r["q"]]
    vs = [v / np.linalg.norm(v) for v in vs]
    m = np.mean(vs, axis=0)
    return m / np.linalg.norm(m)


BEN = centroid(os.path.join(S, "samples_test.json"), "Ben Lin")
BENNY = centroid(os.path.join(S, "samples_prod_now.json"), "Benny Huang")

frames = []   # (file, idx, adjacent_to_prev, emb, who)
for p in sorted(glob.glob(os.path.join(S, "room", "*_bn*_srcwav.wav"))):
    with wave.open(p) as w:
        sr, ch = w.getframerate(), w.getnchannels()
        a = np.frombuffer(w.readframes(w.getnframes()), dtype=np.int16).astype(np.float32) / 32768.0
    a = a.reshape(-1, ch).mean(axis=1) if ch > 1 else a
    step = int(se.FRAME_SECONDS * sr)
    prev = None
    for i, (start, f) in enumerate(se._frames_at(a, sr)):
        e = np.asarray(se.embed_audio(f, sr), dtype=np.float64).ravel()
        e /= np.linalg.norm(e)
        sb, sy = float(e @ BEN), float(e @ BENNY)
        who = "Ben" if sb - sy >= 0.15 and sb >= 0.35 else "Benny" if sy - sb >= 0.15 and sy >= 0.35 else None
        frames.append((os.path.basename(p), start, prev is not None and start == prev + step, e, who))
        prev = start

d = lambda a, b: float(1.0 - a @ b)
lab = [f for f in frames if f[4]]
print(f"frames {len(frames)}; confidently Ben {sum(f[4] == 'Ben' for f in lab)}, Benny {sum(f[4] == 'Benny' for f in lab)}")
cross = np.array([d(a[3], b[3]) for a, b in itertools.combinations(lab, 2) if a[4] != b[4]])
same_adj, straddle = [], []
for x, y in zip(frames, frames[1:]):
    if x[0] != y[0] or not y[2] or not x[4] or not y[4]:
        continue
    (same_adj if x[4] == y[4] else straddle).append(d(x[3], y[3]))
same_adj, straddle = np.array(same_adj), np.array(straddle)
q = lambda x, p: round(float(np.percentile(x, p)), 3) if len(x) else None
print(f"same-room cross pairs n={len(cross)}: min {cross.min():.3f} p1 {q(cross, 1)} p5 {q(cross, 5)} p50 {q(cross, 50)}")
print(f"adjacent same-person pairs n={len(same_adj)}: p50 {q(same_adj, 50)} p90 {q(same_adj, 90)} max {same_adj.max() if len(same_adj) else None}")
print(f"adjacent Ben|Benny boundary pairs n={len(straddle)}: min {straddle.min() if len(straddle) else None} values {sorted(round(v, 3) for v in straddle)[:12]}")
for t in (0.35, 0.40, 0.45, 0.50, 0.55):
    print(f"  t={t:.2f}: same-room cross accepted {np.mean(cross <= t):.2%}  "
          f"boundary accepted {np.mean(straddle <= t) if len(straddle) else float('nan'):.2%}  "
          f"same-person adjacent accepted {np.mean(same_adj <= t):.0%}")
