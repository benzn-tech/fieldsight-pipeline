# -*- coding: utf-8 -*-
"""How much of the run2->run3 change is the 5 terms, and how much is collateral?"""
import json,os,re,io,difflib
W=os.environ.get("ASR_EVAL_WORK", os.path.dirname(os.path.abspath(__file__)))
def shaped(p):
    return [i["alternatives"][0]["content"] for i in
            sorted(json.load(open(p))["results"]["items"],key=lambda x:float(x.get("start_time",0)))
            if i.get("type")=="pronunciation"]
def norm(ws): return [re.sub(r"[^\w\u4e00-\u9fff]","",w).lower() for w in ws]
A=norm(shaped(os.path.join(W,"full_transcribe_shaped.json")))
B=norm(shaped(os.path.join(W,"kt_transcribe_shaped.json")))
A=[w for w in A if w]; B=[w for w in B if w]
TERM=re.compile(r"plaud|deondre|deandre|southbase|vizfield|visiofield|pro$|soundbase|salesbase|sales|deewan|vcfield|visualfield|visual|vc",re.I)
sm=difflib.SequenceMatcher(None,A,B,autojunk=False)
tot=0; termish=0; blocks=[]
for tag,i1,i2,j1,j2 in sm.get_opcodes():
    if tag=="equal": continue
    a=" ".join(A[i1:i2]); b=" ".join(B[j1:j2])
    n=max(i2-i1,j2-j1); tot+=n
    if TERM.search(a) or TERM.search(b): termish+=n
    else: blocks.append((n,a,b))
o=io.open(os.path.join(W,"sideeffect.txt"),"w",encoding="utf-8")
o.write(f"run2 {len(A)} 词, run3 {len(B)} 词, 相似度 {sm.ratio()*100:.1f}%\n")
o.write(f"发生改动的词位: {tot}  ({tot/len(A)*100:.1f}% of run2)\n")
o.write(f"  其中与 5 个目标词（或其旧写法）相关: {termish}  ({termish/max(tot,1)*100:.1f}%)\n")
o.write(f"  与目标词无关的连带改动:            {tot-termish}  ({(tot-termish)/max(tot,1)*100:.1f}%)\n")
blocks.sort(key=lambda x:-x[0])
o.write("\n== 最大的 25 处无关连带改动 (run2 → run3) ==\n")
for n,a,b in blocks[:25]:
    o.write(f"  [{n:>2}] “{a[:90]}”  →  “{b[:90]}”\n")
o.close()
print(io.open(os.path.join(W,"sideeffect.txt"),encoding="utf-8").read()[:4000])
