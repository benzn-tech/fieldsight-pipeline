# -*- coding: utf-8 -*-
"""Side-by-side text at fixed windows, EL vs qwen."""
import json,os,glob,io
W=os.environ.get("ASR_EVAL_WORK",os.path.dirname(os.path.abspath(__file__)))
MAN={m["file"]:m for m in json.load(open(os.path.join(W,"manifest.json")))}
def shaped(p):
    return sorted((float(i["start_time"]),float(i["end_time"]),i["alternatives"][0]["content"])
                  for i in json.load(open(p,encoding="utf-8"))["results"]["items"] if i.get("type")=="pronunciation")
def batched():
    out=[]
    for p in sorted(glob.glob(os.path.join(W,"batched","*.json"))):
        off=MAN[os.path.basename(p).replace(".json",".wav")]["offset_sec"]
        for i in json.load(open(p))["results"]["items"]:
            if i.get("type")=="pronunciation":
                out.append((float(i["start_time"])+off,float(i["end_time"])+off,i["alternatives"][0]["content"]))
    return sorted(out)
R={"EL-1 攒批":batched(),"EL-3 +新词":shaped(os.path.join(W,"kt_transcribe_shaped.json")),
   "QW-A":shaped(os.path.join(W,"qwen_A_shaped.json"))}
def win(ws,a,b): return "".join((w if all('\u4e00'<=c<='\u9fff' for c in w) else " "+w+" ") for s,e,w in ws if a<=s<=b)
o=io.open(os.path.join(W,"sample_text.txt"),"w",encoding="utf-8")
for title,a,b in [("Plaud 那段 (00:19-00:32)",19,32),
                  ("Southbase / 3W / Naylor Love (06:40-06:58)",400,418),
                  ("VizField (08:52-09:15)",532,555),
                  ("Lindis Pass (24:10-24:25)",1450,1465),
                  ("纯英文段 (25:30-25:50)",1530,1550)]:
    o.write(f"\n{'='*72}\n{title}\n{'='*72}\n")
    for k,ws in R.items(): o.write(f"  {k:<10} …{win(ws,a,b)}…\n")
o.close(); print(io.open(os.path.join(W,"sample_text.txt"),encoding="utf-8").read())
