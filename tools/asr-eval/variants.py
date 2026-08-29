# -*- coding: utf-8 -*-
"""Near-miss hunt: did run 3 get CLOSE on the terms it did not nail exactly?"""
import json, os, glob, re, io, sys
W = os.environ.get("ASR_EVAL_WORK", os.path.dirname(os.path.abspath(__file__)))
MAN = {m["file"]: m for m in json.load(open(os.path.join(W, "manifest.json")))}
def shaped(p):
    return sorted((float(i["start_time"]), float(i["end_time"]), i["alternatives"][0]["content"])
                  for i in json.load(open(p))["results"]["items"] if i.get("type")=="pronunciation")
def batched():
    out=[]
    for p in sorted(glob.glob(os.path.join(W,"batched","*.json"))):
        off=MAN[os.path.basename(p).replace(".json",".wav")]["offset_sec"]
        for i in json.load(open(p))["results"]["items"]:
            if i.get("type")=="pronunciation":
                out.append((float(i["start_time"])+off,float(i["end_time"])+off,i["alternatives"][0]["content"]))
    return sorted(out)
R={"1":batched(),"2":shaped(os.path.join(W,"full_transcribe_shaped.json")),
   "3":shaped(os.path.join(W,"kt_transcribe_shaped.json"))}
o=io.open(os.path.join(W,"terms_report.txt"),"w",encoding="utf-8")
PAT={"Plaud":r"pl(a|o)u?d|^pro$|proud",
     "DeanDre":r"de(a|o|e)n?w?[ao]?n?dre|deon|dean|deewan|dre",
     "Southbase":r"s(ou|a)(th|nd|les)base|soundbase|salesbase|southbase",
     "VisioField":r"visio|visual\s?field|vc\s?field|vizio",
     "VizField":r"vizfield|viz\s?field|vc\s?field|visual\s?field",
     "Lindis":r"lind(i|e|a)s?('s)?"}
o.write("== 各词及其近似写法在三次运行里的出现次数 ==\n")
o.write(f"{'term':<12}{'run1 攒批':>12}{'run2 整场':>12}{'run3 +新词':>12}   run3 实际写法\n")
for t,pat in PAT.items():
    rx=re.compile(pat,re.I); cnt={}; forms={}
    for k,ws in R.items():
        h=[w for s,e,w in ws if rx.search(re.sub(r"[^A-Za-z' ]","",w))]
        cnt[k]=len(h)
        if k=="3": forms=sorted(set(w.strip(".,?!\"'") for w in h))
    o.write(f"{t:<12}{cnt['1']:>12}{cnt['2']:>12}{cnt['3']:>12}   {', '.join(forms[:8]) or '—'}\n")
o.write("\n== 精确命中（区分大小写的完整词）==\n")
for t in PAT:
    row={k:sum(1 for s,e,w in ws if re.sub(r"[^A-Za-z]","",w).lower()==t.lower()) for k,ws in R.items()}
    o.write(f"{t:<12} run1={row['1']:<4} run2={row['2']:<4} run3={row['3']:<4}\n")
o.write("\n== 三次运行规模 ==\n")
for k,ws in R.items():
    o.write(f"  run{k}: {len(ws)} words, 末词 {max(e for s,e,w in ws):.0f}s\n")
# global similarity
import difflib
def seq(ws): return [re.sub(r"[^\w\u4e00-\u9fff]","",w).lower() for s,e,w in ws if re.sub(r"[^\w\u4e00-\u9fff]","",w)]
o.write("\n== 词序相似度 ==\n")
for a,b in (("1","2"),("1","3"),("2","3")):
    r=difflib.SequenceMatcher(None,seq(R[a]),seq(R[b]),autojunk=False).ratio()
    o.write(f"  run{a} vs run{b}: {r*100:.1f}%\n")
o.close()
print(io.open(os.path.join(W,"terms_report.txt"),encoding="utf-8").read())
