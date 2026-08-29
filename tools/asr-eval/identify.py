# -*- coding: utf-8 -*-
"""Blind test: which of the session's speakers is the person in b.mp3 / b_chinese.mp3?

Joe / Leo / Mike / Zoe are the CONTROL group — four people who are (as far as I know) not
in this meeting. If an enrolment clip scores high on a session speaker, the controls tell
me whether that is identity or just channel.
"""
import json, os, wave, glob
import numpy as np, onnxruntime as ort

W = os.environ.get("ASR_EVAL_WORK", os.path.dirname(os.path.abspath(__file__)))
sess = ort.InferenceSession(os.path.join(W, "models", "ecapa_tdnn.onnx"),
                            providers=["CPUExecutionProvider"])

def _once(a):
    return np.asarray(sess.run(None, {"wav": np.asarray(a, np.float32)[None, :],
                                      "wav_lens": np.array([1.0], np.float32)})[0]).ravel()

def embed(a, sr, cap_s=45.0):
    cap = int(cap_s * sr)
    if len(a) <= cap: return _once(a)
    st = list(range(0, max(1, len(a) - cap + 1), cap))
    if len(a) - (st[-1] + cap) >= sr: st.append(st[-1] + cap)
    vs = []
    for i in st:
        v = _once(a[i:i + cap]); n = np.linalg.norm(v)
        if n: vs.append(v / n)
    m = np.mean(vs, 0); n = np.linalg.norm(m)
    return m / n if n else m

def cos(a, b):
    na, nb = np.linalg.norm(a), np.linalg.norm(b)
    return 0.0 if na == 0 or nb == 0 else float(np.dot(a, b) / (na * nb))

def rd(p):
    with wave.open(p, "rb") as w:
        return (np.frombuffer(w.readframes(w.getnframes()), np.int16).astype(np.float32) / 32768.0,
                w.getframerate())

# ---------- session speaker centroids, rebuilt from the single-pass turns ----------
A, SR = rd(os.path.join(W, "session_full.wav"))
turns = [t for t in json.load(open(os.path.join(W, "single_turns.json"), encoding="cp936"))
         if t["end"] - t["start"] >= 3.0]
per = {}
for t in turns:
    seg = A[int(t["start"] * SR):int(t["end"] * SR)]
    if len(seg) < SR: continue
    v = embed(seg, SR); n = np.linalg.norm(v)
    if n: per.setdefault(t["spk"], []).append(v / n)
cent = {}
for k, vs in per.items():
    m = np.mean(vs, 0); cent[k] = m / np.linalg.norm(m)
# ground truth the user just gave
NAME = {"spk_0": "Benny", "spk_1": "Ben", "spk_2": "Ben", "spk_3": "Isaac", "spk_4": "Ben"}
# the merged view: one centroid per PERSON
byname = {}
for k, vs in per.items(): byname.setdefault(NAME[k], []).extend(vs)
pcent = {}
for k, vs in byname.items():
    m = np.mean(vs, 0); pcent[k] = m / np.linalg.norm(m)

# ---------- enrolment clips ----------
enr = {}
for p in sorted(glob.glob(os.path.join(W, "enrol", "*.wav"))):
    a, sr = rd(p)
    enr[os.path.basename(p)[:-4]] = embed(a, sr)

labels = sorted(cent)
print("=== enrolment clip  vs  each single-pass session label ===")
print(f"{'clip':<12}" + "".join(f"{L:>9}" for L in labels) + "   best")
for k in ["b", "b_chinese", "Joe", "Leo", "Mike", "Zoe"]:
    row = {L: cos(enr[k], cent[L]) for L in labels}
    best = max(row, key=row.get)
    srt = sorted(row.values(), reverse=True)
    print(f"{k:<12}" + "".join(f"{row[L]:>9.3f}" for L in labels) +
          f"   {best} ({NAME[best]}) margin {srt[0]-srt[1]:+.3f}")

pl = sorted(pcent)
print("\n=== same, against the three PEOPLE (spk_1/2/4 merged into Ben) ===")
print(f"{'clip':<12}" + "".join(f"{L:>9}" for L in pl) + "   best   margin")
for k in ["b", "b_chinese", "Joe", "Leo", "Mike", "Zoe"]:
    row = {L: cos(enr[k], pcent[L]) for L in pl}
    best = max(row, key=row.get); srt = sorted(row.values(), reverse=True)
    print(f"{k:<12}" + "".join(f"{row[L]:>9.3f}" for L in pl) +
          f"   {best:<7}{srt[0]-srt[1]:+.3f}")

print("\n=== the two b clips against each other, and against the controls ===")
ks = ["b", "b_chinese", "Joe", "Leo", "Mike", "Zoe"]
print(f"{'':<12}" + "".join(f"{k:>11}" for k in ks))
for a in ks:
    print(f"{a:<12}" + "".join(f"{cos(enr[a], enr[b]):>11.3f}" for b in ks))

json.dump({"vs_label": {k: {L: round(cos(enr[k], cent[L]), 4) for L in labels} for k in enr},
           "vs_person": {k: {L: round(cos(enr[k], pcent[L]), 4) for L in pl} for k in enr},
           "enrol_pairs": {f"{a}|{b}": round(cos(enr[a], enr[b]), 4) for a in ks for b in ks}},
          open(os.path.join(W, "identify_result.json"), "w"), indent=1)
print("\ndone")
