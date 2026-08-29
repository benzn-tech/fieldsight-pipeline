# -*- coding: utf-8 -*-
"""Score every run's speaker labels against the three confirmed people, and answer the
question that actually matters for the product: can Ben's enrolled voiceprint pick out and
merge Ben's turns?

People references come from ONE place only: the enrolment clips in `enrol/`. Ben has a real
enrolment (b.wav / b_chinese.wav). Benny and Isaac do not, so their references are built
from EL run 2's labels, which the user confirmed by ear. Where a reference is a model
output that a human validated, that is stated rather than hidden.
"""
import json, os, glob, io, wave
import numpy as np, onnxruntime as ort

W = os.environ.get("ASR_EVAL_WORK", os.path.dirname(os.path.abspath(__file__)))
FR = 0.25
MIN_TURN = 3.0
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
def rd(p):
    with wave.open(p, "rb") as w:
        return (np.frombuffer(w.readframes(w.getnframes()), np.int16).astype(np.float32) / 32768.0,
                w.getframerate())

A, SR = rd(os.path.join(W, "session_full.wav"))

def turns(path):
    out = []
    for it in sorted(json.load(open(path, encoding="utf-8"))["results"]["items"],
                     key=lambda x: float(x.get("start_time", 0))):
        if it.get("type") != "pronunciation": continue
        L = it.get("speaker_label") or "-"
        s, e = float(it["start_time"]), float(it["end_time"])
        if out and out[-1][0] == L and s - out[-1][2] < 1.5: out[-1][2] = e
        else: out.append([L, s, e])
    return out

RUNS = {"EL-2 整场": "full_transcribe_shaped.json",
        "EL-2' 重跑": "noise_transcribe_shaped.json",
        "EL-3 +新词": "kt_transcribe_shaped.json",
        "QW-A sc=5": "qwen_A_shaped.json",
        "QW-B sc=5 重跑": "qwen_B_shaped.json",
        "QW-C sc=3": "qwen_C_shaped.json",
        "QW-D 无词典": "qwen_D_shaped.json"}
T = {k: turns(os.path.join(W, v)) for k, v in RUNS.items() if os.path.exists(os.path.join(W, v))}

# ---- references ----
NAME2 = {"spk_0": "Benny", "spk_1": "Ben", "spk_2": "Ben", "spk_3": "Isaac", "spk_4": "Ben"}
ref = {}
for L, s, e in T["EL-2 整场"]:
    if e - s < MIN_TURN: continue
    seg = A[int(s * SR):int(e * SR)]
    if len(seg) < SR: continue
    v = embed(seg, SR); n = np.linalg.norm(v)
    if n: ref.setdefault(NAME2[L], []).append(v / n)
PC = {}
for k, vs in ref.items():
    m = np.mean(vs, 0); PC[k] = m / np.linalg.norm(m)
# Ben ALSO has a real enrolment — keep it separate so the two can be compared
BEN_ENROL = []
for f in ("b.wav", "b_chinese.wav"):
    p = os.path.join(W, "enrol", f)
    if os.path.exists(p):
        a, sr = rd(p); v = embed(a, sr); BEN_ENROL.append(v / np.linalg.norm(v))
BE = None
if BEN_ENROL:
    m = np.mean(BEN_ENROL, 0); BE = m / np.linalg.norm(m)
people = sorted(PC)

o = io.open(os.path.join(W, "spk_all.txt"), "w", encoding="utf-8")
o.write(f"参考声纹（会中，来自 EL-2 标签 + 用户耳朵确认）: "
        f"{ {k: len(v) for k, v in ref.items()} }\n")
if BE is not None:
    o.write(f"Ben 的独立注册音（enrol/b.wav + b_chinese.wav）对会中 Ben 参考的余弦: "
            f"{cos(BE, PC['Ben']):.3f}   对 Benny: {cos(BE, PC['Benny']):.3f}   "
            f"对 Isaac: {cos(BE, PC['Isaac']):.3f}\n")

truth = {}
for it in json.load(open(os.path.join(W, "full_transcribe_shaped.json")))["results"]["items"]:
    if it.get("type") != "pronunciation": continue
    L = it.get("speaker_label")
    if not L: continue
    s, e = float(it["start_time"]), float(it["end_time"])
    for k in range(int(s / FR), max(int(s / FR) + 1, int(e / FR))):
        truth[k] = NAME2[L]

summary = []
for run, tl in T.items():
    o.write(f"\n===== {run} =====\n")
    lab = {}
    for L, s, e in tl:
        if e - s < MIN_TURN: continue
        seg = A[int(s * SR):int(e * SR)]
        if len(seg) < SR: continue
        v = embed(seg, SR); n = np.linalg.norm(v)
        if n: lab.setdefault(L, []).append((v / n, e - s))
    o.write(f"{'标签':<7}{'长turn':>7}{'秒':>7}" + "".join(f"{p:>9}" for p in people)
            + ("   vs Ben注册音" if BE is not None else "") + "   判为\n")
    assign = {}
    for L in sorted(lab):
        vs = [v for v, d in lab[L]]
        m = np.mean(vs, 0); m = m / np.linalg.norm(m)
        row = {p: cos(m, PC[p]) for p in people}
        best = max(row, key=row.get); assign[L] = best
        extra = f"{cos(m, BE):>14.3f}" if BE is not None else ""
        o.write(f"{L:<7}{len(vs):>7}{sum(d for v, d in lab[L]):>7.0f}"
                + "".join(f"{row[p]:>9.3f}" for p in people) + extra + f"   {best}\n")
    byp = {}
    for L, p in assign.items(): byp.setdefault(p, []).append(L)
    o.write("  每人被切成几个标签: " + ", ".join(f"{p}={len(v)} ({'+'.join(sorted(v))})"
                                          for p, v in sorted(byp.items())) + "\n")

    # frame purity of this run's labels, and of the same labels after voiceprint merge
    fr = {}
    for L, s, e in tl:
        for k in range(int(s / FR), max(int(s / FR) + 1, int(e / FR))):
            fr[k] = L
    def purity(mapper):
        conf = {}
        for k, L in fr.items():
            if k not in truth: continue
            r = mapper(L)
            conf.setdefault(r, {}).setdefault(truth[k], 0); conf[r][truth[k]] += 1
        tot = sum(sum(v.values()) for v in conf.values())
        hit = sum(max(v.values()) for v in conf.values())
        return (hit / tot if tot else 0), tot, conf
    p_raw, n_raw, _ = purity(lambda L: L)
    p_mrg, n_mrg, conf = purity(lambda L: assign.get(L, "?"))
    o.write(f"  原始标签纯度 {p_raw*100:.1f}%   声纹合并后 {p_mrg*100:.1f}%   ({n_raw} 帧)\n")
    for r in sorted(conf):
        n = sum(conf[r].values()); top = max(conf[r].values())
        o.write(f"     {r:<7} n={n:<5} {top/n*100:>5.1f}%   "
                + ", ".join(f"{c}:{v}" for c, v in sorted(conf[r].items(), key=lambda x: -x[1])) + "\n")
    # Ben recall/precision using the ENROLMENT clip only (the product question)
    if BE is not None:
        ben_labels = [L for L in lab if cos(np.mean([v for v, d in lab[L]], 0)
                                            / np.linalg.norm(np.mean([v for v, d in lab[L]], 0)), BE) > 0.35]
        tp = fp = fn = 0
        for k, L in fr.items():
            if k not in truth: continue
            pred = L in ben_labels; act = truth[k] == "Ben"
            tp += pred and act; fp += pred and not act; fn += (not pred) and act
        rec = tp / (tp + fn) if tp + fn else 0
        prec = tp / (tp + fp) if tp + fp else 0
        o.write(f"  用 b.mp3 注册音挑 Ben（阈值 0.35）: 标签 {sorted(ben_labels)}  "
                f"召回 {rec*100:.1f}%  精确 {prec*100:.1f}%\n")
        summary.append((run, p_raw, p_mrg, rec, prec, len(lab)))

o.write("\n\n== 汇总 ==\n")
o.write(f"{'run':<16}{'标签数':>7}{'原始纯度':>10}{'声纹合并后':>12}{'Ben召回':>9}{'Ben精确':>9}\n")
for run, pr, pm, rec, prec, nl in summary:
    o.write(f"{run:<16}{nl:>7}{pr*100:>10.1f}{pm*100:>12.1f}{rec*100:>9.1f}{prec*100:>9.1f}\n")
o.close()
print(io.open(os.path.join(W, "spk_all.txt"), encoding="utf-8").read())
