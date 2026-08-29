"""ECAPA-TDNN voiceprint cosine over this session — an INDEPENDENT check on both runs.

Uses the same ONNX model the prod embedder uses (models/ecapa_tdnn.onnx), the same
45 s cap + normalise-before-average, and voiceprint_utils.cosine.
"""
import json, os, sys, wave, itertools
import numpy as np
import onnxruntime as ort

W = os.environ.get("ASR_EVAL_WORK", os.path.dirname(os.path.abspath(__file__)))
MODEL = os.path.join(W, "models", "ecapa_tdnn.onnx")
MAX_EMBED_SECONDS = 45.0
MIN_TURN_S = 3.0          # voiceprint_utils.DEFAULT_MIN_TURN_S

sess = ort.InferenceSession(MODEL, providers=["CPUExecutionProvider"])

def _embed_once(a):
    out = sess.run(None, {"wav": np.asarray(a, dtype=np.float32)[None, :],
                          "wav_lens": np.array([1.0], dtype=np.float32)})
    return np.asarray(out[0]).ravel()

def embed_audio(audio, sr):
    audio = np.asarray(audio, dtype=np.float32)
    cap = int(MAX_EMBED_SECONDS * sr)
    if len(audio) <= cap:
        return _embed_once(audio)
    starts = list(range(0, max(1, len(audio) - cap + 1), cap))
    tail = starts[-1] + cap
    if len(audio) - tail >= sr:
        starts.append(tail)
    vecs = []
    for i in starts:
        v = _embed_once(audio[i:i + cap]); n = np.linalg.norm(v)
        if n: vecs.append(v / n)
    m = np.mean(vecs, axis=0); n = np.linalg.norm(m)
    return m / n if n else m

def cosine(a, b):
    a = np.asarray(a, np.float64).ravel(); b = np.asarray(b, np.float64).ravel()
    na, nb = np.linalg.norm(a), np.linalg.norm(b)
    return 0.0 if na == 0 or nb == 0 else float(np.dot(a, b) / (na * nb))

with wave.open(os.path.join(W, "session_full.wav"), "rb") as w:
    SR = w.getframerate()
    A = np.frombuffer(w.readframes(w.getnframes()), dtype=np.int16).astype(np.float32) / 32768.0
print(f"audio {len(A)/SR:.0f}s @{SR}", flush=True)

turns = json.load(open(os.path.join(W, "single_turns.json"), encoding="cp936"))
usable = [t for t in turns if t["end"] - t["start"] >= MIN_TURN_S]
print(f"turns {len(turns)}, usable >={MIN_TURN_S}s: {len(usable)}", flush=True)

embs = []
for i, t in enumerate(usable):
    seg = A[int(t["start"]*SR):int(t["end"]*SR)]
    if len(seg) < SR: continue
    embs.append({"spk": t["spk"], "start": t["start"], "end": t["end"],
                 "dur": round(t["end"]-t["start"], 1), "v": embed_audio(seg, SR)})
    if (i+1) % 25 == 0: print(f"  embedded {i+1}/{len(usable)}", flush=True)
print(f"embedded {len(embs)} turns", flush=True)

# ---------- 1. centroid per EL single-pass label ----------
labels = sorted({e["spk"] for e in embs})
cent = {}
for L in labels:
    vs = [e["v"]/np.linalg.norm(e["v"]) for e in embs if e["spk"] == L]
    m = np.mean(vs, axis=0); cent[L] = m/np.linalg.norm(m)
print("\n=== A. cosine between single-pass speaker centroids ===")
print("        " + "".join(f"{L:>9}" for L in labels))
for L in labels:
    print(f"{L:<8}" + "".join(f"{cosine(cent[L],cent[M]):>9.3f}" for M in labels))
sec = {L: round(sum(e['dur'] for e in embs if e['spk']==L),1) for L in labels}
n   = {L: sum(1 for e in embs if e['spk']==L) for L in labels}
print("turns/seconds per label:", {L: (n[L], sec[L]) for L in labels})

# ---------- 2. within vs between distributions ----------
print("\n=== B. turn-to-turn cosine, same EL label vs different EL label ===")
same, diff = [], []
for a, b in itertools.combinations(embs, 2):
    c = cosine(a["v"], b["v"])
    (same if a["spk"] == b["spk"] else diff).append(c)
def q(x, p):
    x = sorted(x); return x[int(p*(len(x)-1))]
print(f"same-label  n={len(same):>5}  median {q(same,.5):.3f}  p10 {q(same,.1):.3f}  p90 {q(same,.9):.3f}")
print(f"diff-label  n={len(diff):>5}  median {q(diff,.5):.3f}  p10 {q(diff,.1):.3f}  p90 {q(diff,.9):.3f}")

# ---------- 3. the specific question: is spk_0 == spk_4?  spk_1 == spk_2? ----------
print("\n=== C. the suspected merges ===")
for a, b in [("spk_0","spk_4"),("spk_1","spk_2"),("spk_0","spk_3"),("spk_1","spk_4"),
             ("spk_0","spk_1"),("spk_3","spk_4"),("spk_0","spk_2"),("spk_2","spk_4")]:
    if a in cent and b in cent:
        print(f"  {a} vs {b}: centroid cosine {cosine(cent[a],cent[b]):+.3f}")

# ---------- 4. agglomerative clustering — how many voices are actually here ----------
print("\n=== D. average-linkage clustering on turn embeddings (EL labels ignored) ===")
V = np.stack([e["v"]/np.linalg.norm(e["v"]) for e in embs])
S = V @ V.T
for thr in (0.30, 0.35, 0.40, 0.45, 0.50, 0.55, 0.60):
    clusters = [[i] for i in range(len(embs))]
    while len(clusters) > 1:
        best, bi, bj = -2, None, None
        for i in range(len(clusters)):
            for j in range(i+1, len(clusters)):
                m = float(np.mean(S[np.ix_(clusters[i], clusters[j])]))
                if m > best: best, bi, bj = m, i, j
        if best < thr: break
        clusters[bi] = clusters[bi] + clusters[bj]; clusters.pop(bj)
    clusters.sort(key=lambda c: -sum(embs[i]["dur"] for i in c))
    desc = []
    for c in clusters:
        lab = {}
        for i in c: lab[embs[i]["spk"]] = lab.get(embs[i]["spk"], 0) + embs[i]["dur"]
        top = sorted(lab.items(), key=lambda x: -x[1])
        desc.append(f"[{sum(embs[i]['dur'] for i in c):.0f}s " +
                    "+".join(f"{k}:{v:.0f}" for k, v in top) + "]")
    print(f"  thr {thr:.2f} -> {len(clusters)} clusters  " + " ".join(desc))

json.dump({"centroid_cosine": {f"{a}|{b}": round(cosine(cent[a],cent[b]),4)
                               for a in labels for b in labels},
           "per_label": {L: {"turns": n[L], "seconds": sec[L]} for L in labels},
           "same_median": round(q(same,.5),4), "diff_median": round(q(diff,.5),4)},
          open(os.path.join(W, "voiceprint_result.json"), "w"), indent=1)
np.save(os.path.join(W, "turn_embeddings.npy"), V)
json.dump([{k: v for k, v in e.items() if k != "v"} for e in embs],
          open(os.path.join(W, "turn_index.json"), "w"), indent=1)
print("\ndone")
