# -*- coding: utf-8 -*-
"""Purity against GROUND TRUTH (user's ears): spk_0=Benny, spk_1/2/4=Ben, spk_3=Isaac.

The merge is now confirmed by a human, so scoring against it is no longer circular.
"""
import json, os, glob
W = os.environ.get("ASR_EVAL_WORK", os.path.dirname(os.path.abspath(__file__)))
FR = 0.25
NAME = {"spk_0": "Benny", "spk_1": "Ben", "spk_2": "Ben", "spk_3": "Isaac", "spk_4": "Ben"}
GROUP = {("1","spk_1"):"G0",("2","spk_1"):"G0",("3","spk_1"):"G0",("4","spk_0"):"G0",
 ("5","spk_1"):"G0",("6","spk_0"):"G0",("7","spk_0"):"G0",("8","spk_0"):"G0",("9","spk_0"):"G0",
 ("10","spk_1"):"G0",("11","spk_0"):"G0",("12","spk_0"):"G0",("13","spk_0"):"G0",("14","spk_0"):"G0",
 ("1","spk_0"):"G1",("2","spk_0"):"G1",("3","spk_0"):"G1",("4","spk_1"):"G1",("5","spk_0"):"G1",
 ("6","spk_1"):"G1",("7","spk_1"):"G1",("8","spk_1"):"G1",("9","spk_1"):"G1",("10","spk_0"):"G1",
 ("11","spk_1"):"G1",("12","spk_1"):"G1",("13","spk_1"):"G2",("14","spk_1"):"G2"}

man = {m["file"]: m for m in json.load(open(os.path.join(W, "manifest.json")))}
frames_b = {}
for ci, p in enumerate(sorted(glob.glob(os.path.join(W, "batched", "*.json"))), 1):
    off = man[os.path.basename(p).replace(".json", ".wav")]["offset_sec"]
    for it in json.load(open(p))["results"]["items"]:
        if it.get("type") != "pronunciation": continue
        L = it.get("speaker_label")
        if not L: continue
        s, e = float(it["start_time"]) + off, float(it["end_time"]) + off
        for k in range(int(s / FR), max(int(s / FR) + 1, int(e / FR))):
            frames_b[k] = (str(ci), L)

truth = {}
for it in json.load(open(os.path.join(W, "full_transcribe_shaped.json")))["results"]["items"]:
    if it.get("type") != "pronunciation": continue
    L = it.get("speaker_label")
    if not L: continue
    s, e = float(it["start_time"]), float(it["end_time"])
    for k in range(int(s / FR), max(int(s / FR) + 1, int(e / FR))):
        truth[k] = NAME[L]

def score(mapper, title):
    conf = {}
    for k, kv in frames_b.items():
        if k not in truth: continue
        r = mapper(kv)
        conf.setdefault(r, {}).setdefault(truth[k], 0)
        conf[r][truth[k]] += 1
    tot = sum(sum(v.values()) for v in conf.values())
    hit = sum(max(v.values()) for v in conf.values())
    print(f"\n{title}   overall {hit/tot*100:.1f}%  ({hit}/{tot} frames)")
    for r in sorted(conf):
        n = sum(conf[r].values()); top = max(conf[r].values())
        best = max(conf[r], key=conf[r].get)
        dist = ", ".join(f"{c}:{v}" for c, v in sorted(conf[r].items(), key=lambda x: -x[1]))
        print(f"   {r:<4} n={n:<5} -> {best:<6} {top/n*100:>5.1f}%   {dist}")
    return hit / tot

a = score(lambda kv: kv[1], "BEFORE — raw batched labels (spk_0 / spk_1)")
b = score(lambda kv: GROUP[kv], "AFTER  — voiceprint-rebound groups (G0 / G1 / G2)")
print(f"\n=== {a*100:.1f}%  ->  {b*100:.1f}%  against the names you gave ===")
