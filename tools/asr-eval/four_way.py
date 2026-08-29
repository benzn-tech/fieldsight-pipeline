# -*- coding: utf-8 -*-
"""4-way: run1 攒批 / run2 整场 / run2' 整场重跑(同配置) / run3 整场+新词.

run2 vs run2' is the NOISE FLOOR — same audio, same settings, two calls. Every other
comparison has to be read against it.
"""
import json,os,glob,re,io,difflib
W=os.environ.get("ASR_EVAL_WORK", os.path.dirname(os.path.abspath(__file__)))
MAN={m["file"]:m for m in json.load(open(os.path.join(W,"manifest.json")))}
def shaped(p):
    return sorted((float(i["start_time"]),float(i["end_time"]),i["alternatives"][0]["content"])
                  for i in json.load(open(p))["results"]["items"] if i.get("type")=="pronunciation")
def batched():
    out=[]
    for p in sorted(glob.glob(os.path.join(W,"batched","*.json"))):
        off=MAN[os.path.basename(p).replace(".json",".wav")]["offset_sec"]
        for i in json.load(open(p))["results"]["items"]:
            if i.get("type")=="pronunciation":
                out.append((float(i["start_time"])+off,float(i["end_time"])+off,i["alternatives"][0]["content"]))
    return sorted(out)
R={"1 攒批":batched(),
   "2 整场":shaped(os.path.join(W,"full_transcribe_shaped.json")),
   "2' 整场重跑":shaped(os.path.join(W,"noise_transcribe_shaped.json")),
   "3 整场+新词":shaped(os.path.join(W,"kt_transcribe_shaped.json"))}
def norm(ws): return [x for x in (re.sub(r"[^\w\u4e00-\u9fff]","",w).lower() for s,e,w in ws) if x]
def cover(ws):
    sp=[]
    for s,e,w in ws:
        if sp and s-sp[-1][1]<2.0: sp[-1][1]=max(sp[-1][1],e)
        else: sp.append([s,e])
    return sum(b-a for a,b in sp), [(a,b) for (_,a),(b,_) in zip(sp[:-1],sp[1:]) if b-a>8]
o=io.open(os.path.join(W,"four_way.txt"),"w",encoding="utf-8")
o.write("== 规模与覆盖 ==\n")
for k,ws in R.items():
    c,g=cover(ws)
    o.write(f"  {k:<12} {len(ws):>5} 词   覆盖 {c:>6.0f}s   >8s 空洞 {len(g)} 个 {[(round(a),round(b)) for a,b in g]}\n")
o.write("\n== 词序相似度（同一口径）==\n")
ks=list(R)
for i in range(len(ks)):
    for j in range(i+1,len(ks)):
        r=difflib.SequenceMatcher(None,norm(R[ks[i]]),norm(R[ks[j]]),autojunk=False).ratio()
        tag=" ← 噪声地板" if (ks[i],ks[j])==("2 整场","2' 整场重跑") else ""
        o.write(f"  {ks[i]:<12} vs {ks[j]:<12} {r*100:>5.1f}%{tag}\n")
o.write("\n== 5 个目标词的精确命中次数 ==\n")
TERMS=["Plaud","DeanDre","Southbase","VisioField","VizField","Lindis"]
o.write(f"{'term':<12}"+"".join(f"{k:>14}" for k in ks)+"\n")
for t in TERMS:
    o.write(f"{t:<12}"+"".join(f"{sum(1 for s,e,w in R[k] if re.sub(r'[^A-Za-z]','',w).lower()==t.lower()):>14}" for k in ks)+"\n")
o.write("\n（Lindis 是控制组：用户点名它错了，但没把它放进 keyterms）\n")
o.close()
print(io.open(os.path.join(W,"four_way.txt"),encoding="utf-8").read())
