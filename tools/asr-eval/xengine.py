# -*- coding: utf-8 -*-
"""Cross-engine comparison. EL splits Chinese per CHARACTER, qwen per word — so word
counts are NOT comparable across engines. Compare on characters."""
import json,os,glob,re,io,difflib
W=os.environ.get("ASR_EVAL_WORK",os.path.dirname(os.path.abspath(__file__)))
MAN={m["file"]:m for m in json.load(open(os.path.join(W,"manifest.json")))}
def shaped(p):
    return sorted((float(i["start_time"]),float(i["end_time"]),i["alternatives"][0]["content"])
                  for i in json.load(open(p,encoding="utf-8"))["results"]["items"]
                  if i.get("type")=="pronunciation")
def batched():
    out=[]
    for p in sorted(glob.glob(os.path.join(W,"batched","*.json"))):
        off=MAN[os.path.basename(p).replace(".json",".wav")]["offset_sec"]
        for i in json.load(open(p))["results"]["items"]:
            if i.get("type")=="pronunciation":
                out.append((float(i["start_time"])+off,float(i["end_time"])+off,i["alternatives"][0]["content"]))
    return sorted(out)
R={"EL-1 攒批":batched(),
   "EL-2 整场":shaped(os.path.join(W,"full_transcribe_shaped.json")),
   "EL-2' 重跑":shaped(os.path.join(W,"noise_transcribe_shaped.json")),
   "EL-3 +新词":shaped(os.path.join(W,"kt_transcribe_shaped.json"))}
for t in ("A","B","C","D"):
    p=os.path.join(W,f"qwen_{t}_shaped.json")
    if os.path.exists(p): R[f"QW-{t}"]=shaped(p)

CJK=lambda s: re.findall(r"[\u4e00-\u9fff]",s)
LAT=lambda s: re.findall(r"[A-Za-z]+",s)
def charseq(ws):
    """One comparable sequence: each CJK char is a token, each latin word is a token."""
    out=[]
    for s,e,w in ws:
        for ch in w:
            if '\u4e00'<=ch<='\u9fff': out.append(ch)
        out+= [x.lower() for x in LAT(w)]
    return out
def cover(ws):
    sp=[]
    for s,e,w in ws:
        if sp and s-sp[-1][1]<2.0: sp[-1][1]=max(sp[-1][1],e)
        else: sp.append([s,e])
    return sum(b-a for a,b in sp),[(a,b) for (_,a),(b,_) in zip(sp[:-1],sp[1:]) if b-a>8]

o=io.open(os.path.join(W,"xengine.txt"),"w",encoding="utf-8")
o.write("== 规模（词数跨引擎不可比：EL 中文按字切、qwen 按词切；看字符）==\n")
o.write(f"{'run':<12}{'items':>7}{'汉字':>7}{'英文词':>8}{'可比token':>10}{'覆盖s':>8}{'空洞':>5}{'末词s':>8}\n")
seqs={}
for k,ws in R.items():
    txt="".join(w for s,e,w in ws); seqs[k]=charseq(ws)
    c,g=cover(ws)
    o.write(f"{k:<12}{len(ws):>7}{len(CJK(txt)):>7}{len(LAT(txt)):>8}{len(seqs[k]):>10}"
            f"{c:>8.0f}{len(g):>5}{max(e for s,e,w in ws):>8.0f}\n")
o.write("\n== 字符级序列相似度 ==\n")
ks=list(R)
o.write(f"{'':<12}"+"".join(f"{k:>12}" for k in ks)+"\n")
for a in ks:
    o.write(f"{a:<12}")
    for b in ks:
        r=1.0 if a==b else difflib.SequenceMatcher(None,seqs[a],seqs[b],autojunk=False).ratio()
        o.write(f"{r*100:>12.1f}")
    o.write("\n")
o.write("\n== 目标词与真值词 ==\n")
TERMS=["Plaud","DeanDre","DeonDre","Southbase","VisioField","VizField","Lindis","Naylor"]
o.write(f"{'term':<12}"+"".join(f"{k:>12}" for k in ks)+"\n")
for t in TERMS:
    o.write(f"{t:<12}")
    for k in ks:
        n=sum(1 for s,e,w in R[k] if t.lower() in re.sub(r"[^A-Za-z]","",w).lower())
        o.write(f"{n:>12}")
    o.write("\n")
o.write("\n（Lindis / Naylor = 已核实真值，两个引擎都没加进热词，是共用的准确度探针）\n")
o.close()
print(io.open(os.path.join(W,"xengine.txt"),encoding="utf-8").read())
