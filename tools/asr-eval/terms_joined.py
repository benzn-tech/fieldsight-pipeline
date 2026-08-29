# -*- coding: utf-8 -*-
"""Count terms on the JOINED latin text, not per word.

qwen fragments English ("n aylor love", "hab its", "ag gressive"), so a per-word match
undercounts it. Join all latin runs, strip non-letters, then substring-match.
"""
import json,os,glob,re,io
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
R={"EL-1 攒批":batched(),"EL-2 整场":shaped(os.path.join(W,"full_transcribe_shaped.json")),
   "EL-2' 重跑":shaped(os.path.join(W,"noise_transcribe_shaped.json")),
   "EL-3 +新词":shaped(os.path.join(W,"kt_transcribe_shaped.json"))}
for t in ("A","B","C","D"):
    p=os.path.join(W,f"qwen_{t}_shaped.json")
    if os.path.exists(p): R[f"QW-{t}"]=shaped(p)
def squash(ws):
    """all latin letters, no spaces/punct — so fragmented tokens still match"""
    return re.sub(r"[^a-z]","", "".join(w for s,e,w in ws).lower())
S={k:squash(ws) for k,ws in R.items()}
o=io.open(os.path.join(W,"terms_joined.txt"),"w",encoding="utf-8")
o.write("== 在「去掉空格的英文串」里数出现次数（对分词碎片也有效）==\n")
GROUPS=[("热词（两边都只有 EL-3 加了）",["plaud","southbase","vizfield","visiofield","deandre","deondre"]),
        ("真值探针（两个引擎都没加）",["lindispass","naylorlove","naylor"]),
        ("旧的错误写法",["soundbase","salesbase","vcfield","visualfield","lindespass","lindaspass","naola","threew"])]
ks=list(R)
for title,terms in GROUPS:
    o.write(f"\n-- {title} --\n{'term':<14}"+"".join(f"{k:>12}" for k in ks)+"\n")
    for t in terms:
        o.write(f"{t:<14}"+"".join(f"{S[k].count(t):>12}" for k in ks)+"\n")
o.write("\n== 英文总量（去空格后的字母数）==\n")
for k in ks: o.write(f"  {k:<12} {len(S[k]):>7} 字母\n")
o.close(); print(io.open(os.path.join(W,"terms_joined.txt"),encoding="utf-8").read())
