# -*- coding: utf-8 -*-
"""Where is the 94-word chunk run2 missed and run3 has? Does run1 have it too?"""
import json,os,glob,re,io
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
R={"1 攒批":batched(),"2 整场":shaped(os.path.join(W,"full_transcribe_shaped.json")),
   "3 +新词":shaped(os.path.join(W,"kt_transcribe_shaped.json"))}
o=io.open(os.path.join(W,"bigblock.txt"),"w",encoding="utf-8")
# find "still snow" in run3
anchors=[s for s,e,w in R["3 +新词"] if re.search(r"snow",w,re.I)]
o.write(f"run3 里 'snow' 出现在: {[round(a) for a in anchors]}\n")
for a in anchors[:3]:
    o.write(f"\n-- t={a:.0f}s ({int(a)//60:02d}:{int(a)%60:02d}) ±25s --\n")
    for k,ws in R.items():
        o.write(f"  {k}:\n    {' '.join(w for s,e,w in ws if a-25<=s<=a+25)}\n")
# coverage: how much wall time does each run actually cover
o.write("\n== 每次运行覆盖的时间 ==\n")
for k,ws in R.items():
    spans=[]
    for s,e,w in ws:
        if spans and s-spans[-1][1]<2.0: spans[-1][1]=max(spans[-1][1],e)
        else: spans.append([s,e])
    cov=sum(b-a for a,b in spans)
    gaps=[(a,b) for (_,a),(b,_) in zip(spans[:-1],spans[1:]) if b-a>8]
    o.write(f"  {k}: {len(ws)} 词, 覆盖 {cov:.0f}s, >8s 的空洞 {len(gaps)} 个 "
            f"{[(round(a),round(b)) for a,b in gaps[:8]]}\n")
o.close()
print(io.open(os.path.join(W,"bigblock.txt"),encoding="utf-8").read()[:3500])
