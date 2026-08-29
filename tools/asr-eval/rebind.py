# -*- coding: utf-8 -*-
"""Does a voiceprint pass fix the NAMESPACE without needing to relabel every turn?

Two different jobs, deliberately measured apart:
  (A) re-bind  — 28 decisions: is call N's spk_0 the same voice as call M's spk_0?
  (B) relabel  — 354 decisions: which voice is THIS turn?
(A) only ever embeds long, label-homogeneous stretches. (B) has to judge 3-word turns.
"""
import json, os, glob, wave, itertools
import numpy as np, onnxruntime as ort

W = os.environ.get("ASR_EVAL_WORK", os.path.dirname(os.path.abspath(__file__)))
FR = 0.25
sess = ort.InferenceSession(os.path.join(W, "models", "ecapa_tdnn.onnx"),
                            providers=["CPUExecutionProvider"])

def _once(a):
    return np.asarray(sess.run(None, {"wav": np.asarray(a, np.float32)[None, :],
                                      "wav_lens": np.array([1.0], np.float32)})[0]).ravel()

def embed(a, sr, cap_s=45.0):
    cap = int(cap_s * sr)
    if len(a) <= cap:
        return _once(a)
    starts = list(range(0, max(1, len(a) - cap + 1), cap))
    if len(a) - (starts[-1] + cap) >= sr:
        starts.append(starts[-1] + cap)
    vs = []
    for i in starts:
        v = _once(a[i:i + cap]); n = np.linalg.norm(v)
        if n: vs.append(v / n)
    m = np.mean(vs, 0); n = np.linalg.norm(m)
    return m / n if n else m

def cos(a, b):
    na, nb = np.linalg.norm(a), np.linalg.norm(b)
    return 0.0 if na == 0 or nb == 0 else float(np.dot(a, b) / (na * nb))

with wave.open(os.path.join(W, "session_full.wav"), "rb") as w:
    SR = w.getframerate()
    A = np.frombuffer(w.readframes(w.getnframes()), np.int16).astype(np.float32) / 32768.0

man = {m["file"]: m for m in json.load(open(os.path.join(W, "manifest.json")))}
paths = sorted(glob.glob(os.path.join(W, "batched", "*.json")))

# ---- collect per (call, label) turns on the shared concat clock ----
groups = {}          # (call, label) -> list of (start, end)
frames_batched = {}  # frame -> (call, label)
for ci, p in enumerate(paths, 1):
    off = man[os.path.basename(p).replace(".json", ".wav")]["offset_sec"]
    items = [i for i in json.load(open(p))["results"]["items"] if i.get("type") == "pronunciation"]
    cur = None
    for it in items:
        L = it.get("speaker_label")
        if not L: continue
        s, e = float(it["start_time"]) + off, float(it["end_time"]) + off
        for k in range(int(s / FR), max(int(s / FR) + 1, int(e / FR))):
            frames_batched[k] = (ci, L)
        if cur and cur[0] == L and s - cur[2] < 1.5:
            cur[2] = e
        else:
            cur = [L, s, e]
            groups.setdefault((ci, L), []).append(cur)
        groups[(ci, L)][-1][2] = e

# ---- centroid per (call,label) from turns >= 3s ----
MIN = 3.0
cent, cover = {}, {}
for key, turns in sorted(groups.items()):
    tot = sum(t[2] - t[1] for t in turns)
    use = [t for t in turns if t[2] - t[1] >= MIN]
    used = sum(t[2] - t[1] for t in use)
    cover[key] = (round(used, 1), round(tot, 1), len(use), len(turns))
    if not use:
        continue
    vs = []
    for t in use:
        seg = A[int(t[1] * SR):int(t[2] * SR)]
        if len(seg) < SR: continue
        v = embed(seg, SR); n = np.linalg.norm(v)
        if n: vs.append(v / n)
    if vs:
        m = np.mean(vs, 0); cent[key] = m / np.linalg.norm(m)

print("=== coverage: how much of each (call,label) is judgeable at all ===")
tu = tt = 0
for k in sorted(cover):
    u, t, nu, nt = cover[k]
    tu += u; tt += t
    print(f"  call {k[0]:>2} {k[1]}: {u:>6.1f}s of {t:>6.1f}s usable ({u/t*100:>5.1f}%), {nu}/{nt} turns")
print(f"  TOTAL {tu:.1f}s of {tt:.1f}s = {tu/tt*100:.1f}% of batched speech time is >=3s")
print(f"  centroids built: {len(cent)} of {len(groups)} (call,label) pairs\n")

# ---- cluster the 28 centroids ----
keys = sorted(cent)
V = np.stack([cent[k] for k in keys])
S = V @ V.T
print("=== average-linkage over the (call,label) centroids ===")
best = None
for thr in (0.35, 0.40, 0.45, 0.50, 0.55):
    cl = [[i] for i in range(len(keys))]
    while len(cl) > 1:
        b, bi, bj = -2, None, None
        for i in range(len(cl)):
            for j in range(i + 1, len(cl)):
                m = float(np.mean(S[np.ix_(cl[i], cl[j])]))
                if m > b: b, bi, bj = m, i, j
        if b < thr: break
        cl[bi] = cl[bi] + cl[bj]; cl.pop(bj)
    cl.sort(key=lambda c: -len(c))
    print(f"  thr {thr:.2f} -> {len(cl)} groups  " +
          "  ".join("[" + ",".join(f"{keys[i][0]}:{keys[i][1][-1]}" for i in sorted(c)) + "]" for c in cl))
    if thr == 0.45: best = cl

# ---- purity of the re-bound labels, same frame test as before ----
single = {}
for it in json.load(open(os.path.join(W, "full_transcribe_shaped.json")))["results"]["items"]:
    if it.get("type") != "pronunciation": continue
    L = it.get("speaker_label")
    if not L: continue
    s, e = float(it["start_time"]), float(it["end_time"])
    for k in range(int(s / FR), max(int(s / FR) + 1, int(e / FR))):
        single[k] = L

for thr, cl in [("0.45", best)]:
    gid = {}
    for g, c in enumerate(cl):
        for i in c: gid[keys[i]] = f"G{g}"
    print(f"\n=== purity against the single-pass namespace (threshold {thr}) ===")
    for name, mapper in (("raw batched label", lambda kv: kv[1]),
                         ("re-bound label", lambda kv: gid.get(kv, "?"))):
        conf = {}
        for k, kv in frames_batched.items():
            if k not in single: continue
            r = mapper(kv) if name.startswith("re") else kv[1]
            conf.setdefault(r, {}).setdefault(single[k], 0)
            conf[r][single[k]] += 1
        print(f"  {name}:")
        for r in sorted(conf):
            tot = sum(conf[r].values()); top = max(conf[r].values())
            dist = ", ".join(f"{c}:{n}" for c, n in sorted(conf[r].items(), key=lambda x: -x[1])[:3])
            print(f"    {r:<6} n={tot:<5} purity {top/tot*100:>5.1f}%   {dist}")
