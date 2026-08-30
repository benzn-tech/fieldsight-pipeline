# -*- coding: utf-8 -*-
"""Where do the 5 target terms actually occur, and what did each run hear there?

Anchors come from run 3 (the keyterms run): every place it emits a target term is a
candidate site. For each site, pull the same time window out of the other two runs and
print what they said. That way a term that run 3 invented shows up as clearly as one it
fixed — an anchor with nothing plausible around it in the other runs is a hallucination
candidate, not a win.
"""
import json, os, sys, glob, re
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "..", "src"))

W = os.environ.get("ASR_EVAL_WORK", os.path.dirname(os.path.abspath(__file__)))
TERMS = ["Plaud", "DeanDre", "Southbase", "VisioField", "VizField"]
CONTROL = ["Lindis"]
MAN = {m["file"]: m for m in json.load(open(os.path.join(W, "manifest.json")))}


def words_of(shaped_path, offsets=None):
    """[(start_sec, end_sec, word)] on the concat clock."""
    out = []
    j = json.load(open(shaped_path))
    for it in j["results"]["items"]:
        if it.get("type") != "pronunciation": continue
        out.append((float(it["start_time"]), float(it["end_time"]),
                    it["alternatives"][0]["content"]))
    return sorted(out)


def batched_words():
    out = []
    for p in sorted(glob.glob(os.path.join(W, "batched", "*.json"))):
        off = MAN[os.path.basename(p).replace(".json", ".wav")]["offset_sec"]
        for it in json.load(open(p))["results"]["items"]:
            if it.get("type") != "pronunciation": continue
            out.append((float(it["start_time"]) + off, float(it["end_time"]) + off,
                        it["alternatives"][0]["content"]))
    return sorted(out)


RUNS = {
    "1 攒批(prod)": batched_words(),
    "2 整场无新词": words_of(os.path.join(W, "full_transcribe_shaped.json")),
    "3 整场+新词": words_of(os.path.join(W, "kt_transcribe_shaped.json")),
}


def hits(ws, term):
    t = term.lower()
    return [(s, e, w) for s, e, w in ws if t in re.sub(r"[^a-z]", "", w.lower())]


def window(ws, a, b):
    return " ".join(w for s, e, w in ws if s >= a and s <= b)


print("=" * 78)
print("A. 每个词在三次运行里各出现几次")
print("=" * 78)
print(f"{'term':<14}" + "".join(f"{k:>16}" for k in RUNS))
for term in TERMS + CONTROL:
    print(f"{term:<14}" + "".join(f"{len(hits(ws, term)):>16}" for ws in RUNS.values()))

print()
print("=" * 78)
print("B. 逐个出现位置：run 3 说的 vs 另外两次在同一时间窗说的")
print("=" * 78)
for term in TERMS + CONTROL:
    sites = hits(RUNS["3 整场+新词"], term)
    if not sites:
        # term never appears in run 3 — look for it anywhere else
        alt = [(k, hits(ws, term)) for k, ws in RUNS.items() if hits(ws, term)]
        print(f"\n### {term}: run 3 里 0 次" + (f"，但 {alt[0][0]} 里有 {len(alt[0][1])} 次" if alt else "，三次都没有"))
        continue
    print(f"\n### {term}  —  run 3 里 {len(sites)} 处")
    for i, (s, e, w) in enumerate(sites, 1):
        mm = f"{int(s)//60:02d}:{int(s)%60:02d}"
        print(f"  [{i}] {mm} (t={s:.1f}s)  run3 写作 “{w}”")
        for k, ws in RUNS.items():
            print(f"        {k:<14} …{window(ws, s - 4, e + 4)}…")

print()
print("=" * 78)
print("C. 三次运行的整体规模")
print("=" * 78)
for k, ws in RUNS.items():
    print(f"  {k:<14} {len(ws):>5} words   末词 {max(e for s,e,w in ws):.0f}s")
