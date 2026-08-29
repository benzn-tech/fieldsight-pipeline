# -*- coding: utf-8 -*-
"""Score every run's speaker labels against the three CONFIRMED people.

Person references are built from run 2's labels, which the user confirmed by ear
(spk_0=Benny, spk_1/2/4=Ben, spk_3=Isaac). Every other run is then scored against
those three voices — including run 2 itself, which is the sanity check.
"""
import json, os, glob, re, io, wave
import numpy as np, onnxruntime as ort

W = os.environ.get("ASR_EVAL_WORK", os.path.dirname(os.path.abspath(__file__)))
FR = 0.25
sess = ort.InferenceSession(os.path.join(W, "models", "ecapa_tdnn.onnx"),
                            providers=["CPUExecutionProvider"])
def _once(a):
    return np.asarray(sess.run(None, {"wav": np.asarray(a, np.float32)[None, :],
                                      "wav_lens": np.array([1.0], np.float32)})[0]).ravel()
def embed(a, sr, cap=45.0):
    c = int(cap * sr)
    if len(a) <= c: return _once(a)
    st = list(range(0, max(1, len(a) - c + 1), c))
    if len(a) - (st[-1] + c) >= sr: st.append(st[-1] + c)
    vs = []
    for i in st:
        v = _once(a[i:i + c]); n = np.linalg.norm(v)
        if n: vs.append(v / n)
    m = np.mean(vs, 0); n = np.linalg.norm(m)
    return m / n if n else m
def cos(a, b):
    na, nb = np.linalg.norm(a), np.linalg.norm(b)
    return 0.0 if na == 0 or nb == 0 else float(np.dot(a, b) / (na * nb))

with wave.open(os.path.join(W, "session_full.wav"), "rb") as w:
    SR = w.getframerate()
    A = np.frombuffer(w.readframes(w.getnframes()), np.int16).astype(np.float32) / 32768.0

def turns(path):
    out = []
    for it in sorted(json.load(open(path))["results"]["items"],
                     key=lambda x: float(x.get("start_time", 0))):
        if it.get("type") != "pronunciation": continue
        L = it.get("speaker_label") or "-"
        s, e = float(it["start_time"]), float(it["end_time"])
        if out and out[-1][0] == L and s - out[-1][2] < 1.5: out[-1][2] = e
        else: out.append([L, s, e])
    return out

RUNS = {"2 整场": "full_transcribe_shaped.json",
        "2' 重跑": "noise_transcribe_shaped.json",
        "3 +新词": "kt_transcribe_shaped.json"}
T = {k: turns(os.path.join(W, v)) for k, v in RUNS.items()}

# --- person references from run 2 (user-confirmed) ---
NAME2 = {"spk_0": "Benny", "spk_1": "Ben", "spk_2": "Ben", "spk_3": "Isaac", "spk_4": "Ben"}
ref = {}
for L, s, e in T["2 整场"]:
    if e - s < 3.0: continue
    seg = A[int(s * SR):int(e * SR)]
    if len(seg) < SR: continue
    v = embed(seg, SR); n = np.linalg.norm(v)
    if n: ref.setdefault(NAME2[L], []).append(v / n)
PC = {}
for k, vs in ref.items():
    m = np.mean(vs, 0); PC[k] = m / np.linalg.norm(m)
people = sorted(PC)
print("参考声纹:", {k: len(v) for k, v in ref.items()}, flush=True)

o = io.open(os.path.join(W, "spk_score.txt"), "w", encoding="utf-8")
for run, tl in T.items():
    o.write(f"\n===== {run} =====\n")
    o.write(f"{'标签':<8}{'长turn':>7}{'秒':>7}" + "".join(f"{p:>9}" for p in people) + "   判为\n")
    lab = {}
    for L, s, e in tl:
        if e - s < 3.0: continue
        seg = A[int(s * SR):int(e * SR)]
        if len(seg) < SR: continue
        v = embed(seg, SR); n = np.linalg.norm(v)
        if n: lab.setdefault(L, []).append((v / n, e - s))
    resets = []
    for L in sorted(lab):
        vs = [v for v, d in lab[L]]
        m = np.mean(vs, 0); m = m / np.linalg.norm(m)
        row = {p: cos(m, PC[p]) for p in people}
        best = max(row, key=row.get)
        resets.append((L, best))
        o.write(f"{L:<8}{len(vs):>7}{sum(d for v,d in lab[L]):>7.0f}"
                + "".join(f"{row[p]:>9.3f}" for p in people) + f"   {best}\n")
    byp = {}
    for L, p in resets: byp.setdefault(p, []).append(L)
    o.write("  每个人被切成几个标签: " + ", ".join(f"{p}={len(v)} ({'+'.join(v)})" for p, v in sorted(byp.items())) + "\n")
o.close()
print(io.open(os.path.join(W, "spk_score.txt"), encoding="utf-8").read())
