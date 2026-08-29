import json,os,sys,wave,subprocess
sys.path.insert(0,os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "..", "src"))
import numpy as np
W=os.environ.get("ASR_EVAL_WORK", os.path.dirname(os.path.abspath(__file__)))
OUT=os.environ.get("ASR_EVAL_OUT", os.path.join(W, "listen"))  # where the mp3s for human review land
with wave.open(os.path.join(W,"session_full.wav"),"rb") as w:
    SR=w.getframerate(); A=np.frombuffer(w.readframes(w.getnframes()),dtype=np.int16)
print("loaded",len(A)/SR,"s @",SR)

items=json.load(open(os.path.join(W,"full_transcribe_shaped.json")))["results"]["items"]
turns=[]
for it in items:
    if it.get("type")!="pronunciation": continue
    sp=it.get("speaker_label") or "spk_?"; s=float(it["start_time"]); e=float(it["end_time"])
    w_=it["alternatives"][0]["content"]
    if turns and turns[-1]["spk"]==sp and s-turns[-1]["end"]<1.5:
        turns[-1]["end"]=e; turns[-1]["words"].append(w_)
    else:
        turns.append({"spk":sp,"start":s,"end":e,"words":[w_]})
json.dump([{k:(v if k!="words" else " ".join(v)) for k,v in t.items()} for t in turns],
          open(os.path.join(W,"single_turns.json"),"w"),ensure_ascii=False,indent=1)

def cut(s,e): return A[int(s*SR):int(e*SR)]
def wr(path,arr):
    with wave.open(path,"wb") as o:
        o.setnchannels(1); o.setsampwidth(2); o.setframerate(SR); o.writeframes(arr.tobytes())

gap=np.zeros(int(0.6*SR),dtype=np.int16)
report=[]
for spk in sorted({t["spk"] for t in turns}):
    mine=[t for t in turns if t["spk"]==spk]
    mine.sort(key=lambda t:-(t["end"]-t["start"]))
    picked=[]; total=0
    for t in mine:
        d=t["end"]-t["start"]
        if d<2.0: continue
        picked.append(t); total+=d
        if total>=60: break
    if not picked: continue
    picked.sort(key=lambda t:t["start"])
    buf=[]
    for t in picked:
        buf.append(cut(t["start"],t["end"])); buf.append(gap)
    p=os.path.join(OUT,"speaker-samples",f"{spk}_sample.wav")
    wr(p,np.concatenate(buf))
    report.append({"speaker":spk,"clips":len(picked),"seconds":round(total,1),
        "first_seen":round(min(t['start'] for t in [x for x in turns if x['spk']==spk]),1),
        "last_seen":round(max(t['end'] for t in [x for x in turns if x['spk']==spk]),1),
        "file":os.path.basename(p),
        "sample_text":" / ".join(" ".join(t["words"])[:70] for t in picked[:3])})
json.dump(report,open(os.path.join(OUT,"speaker-samples","index.json"),"w"),ensure_ascii=False,indent=1)
for r in report: print(r["speaker"], r["clips"],"clips",r["seconds"],"s  span",r["first_seen"],"-",r["last_seen"])
