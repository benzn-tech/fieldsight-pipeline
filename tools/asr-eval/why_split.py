# -*- coding: utf-8 -*-
"""Why did EL split Ben into spk_1 / spk_2 / spk_4? Language? Level? Gap?"""
import json,os,wave
import numpy as np
W=os.environ.get("ASR_EVAL_WORK", os.path.dirname(os.path.abspath(__file__)))
with wave.open(os.path.join(W,"session_full.wav"),"rb") as w:
    SR=w.getframerate(); A=np.frombuffer(w.readframes(w.getnframes()),np.int16).astype(np.float32)/32768.0
T=json.load(open(os.path.join(W,"single_turns.json"),encoding="cp936"))
def dbfs(x):
    r=float(np.sqrt(np.mean(x**2))) if len(x) else 0.0
    return 20*np.log10(r) if r>0 else -99
st={}
for t in T:
    s=st.setdefault(t["spk"],{"cjk":0,"lat":0,"sec":0.0,"lv":[],"first":t["start"],"last":t["end"]})
    for ch in t["words"]:
        if '\u4e00'<=ch<='\u9fff': s["cjk"]+=1
        elif ch.isalpha(): s["lat"]+=1
    d=t["end"]-t["start"]; s["sec"]+=d; s["last"]=t["end"]
    if d>=1.0: s["lv"].append(dbfs(A[int(t["start"]*SR):int(t["end"]*SR)]))
print(f"{'label':<7}{'span':>18}{'sec':>7}{'CJK%':>7}{'median dBFS':>13}")
for k in sorted(st):
    v=st[k]; tot=v["cjk"]+v["lat"]; lv=sorted(v["lv"])
    print(f"{k:<7}{v['first']:>7.0f}-{v['last']:<10.0f}{v['sec']:>7.0f}"
          f"{v['cjk']/max(tot,1)*100:>7.1f}{lv[len(lv)//2]:>13.1f}")
print("\ngaps at the two Ben->Ben boundaries:")
for a,b in (("spk_1","spk_2"),("spk_2","spk_4")):
    print(f"  {a} ends {st[a]['last']:.1f}s  ->  {b} starts {st[b]['first']:.1f}s   gap {st[b]['first']-st[a]['last']:.1f}s")
