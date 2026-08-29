"""Frame-level speaker-label agreement on ONE common clock (the concat timeline)."""
import json,os,glob,re
W=os.environ.get("ASR_EVAL_WORK", os.path.dirname(os.path.abspath(__file__)))
man={m["file"]:m for m in json.load(open(os.path.join(W,"manifest.json")))}
FR=0.25  # frame seconds

def frames_from_items(items, base_off, tag):
    out={}
    for it in items:
        if it.get("type")!="pronunciation": continue
        sp=it.get("speaker_label")
        if not sp: continue
        s=float(it["start_time"])+base_off; e=float(it["end_time"])+base_off
        for k in range(int(s/FR), max(int(s/FR)+1,int(e/FR))):
            out[k]=f"{tag}{sp}"
    return out

# batched: each transcript json corresponds to a wav in manifest
B={}
for p in sorted(glob.glob(os.path.join(W,"batched","*.json"))):
    wav=os.path.basename(p).replace(".json",".wav")
    off=man[wav]["offset_sec"]
    B.update(frames_from_items(json.load(open(p))["results"]["items"], off, ""))
S=frames_from_items(json.load(open(os.path.join(W,"full_transcribe_shaped.json")))["results"]["items"], 0.0, "")

common=sorted(set(B)&set(S))
print(f"frames: batched={len(B)} single={len(S)} overlapping={len(common)} ({len(common)*FR:.0f}s)")
mat={}
for k in common:
    mat.setdefault(B[k],{}).setdefault(S[k],0); mat[B[k]][S[k]]+=1
cols=sorted({c for r in mat.values() for c in r})
print("\nconfusion  rows=batched label (per-call namespace), cols=single-pass label")
print(f"{'':<8}"+"".join(f"{c:>9}" for c in cols)+"     purity")
for r in sorted(mat):
    tot=sum(mat[r].values()); best=max(mat[r].values())
    print(f"{r:<8}"+"".join(f"{mat[r].get(c,0):>9}" for c in cols)+f"   {best/tot*100:>6.1f}%")

# same, but per batch: does batched spk_0 mean the same person in every call?
print("\nper-call mapping of batched spk_0 / spk_1 onto single-pass speakers:")
for p in sorted(glob.glob(os.path.join(W,"batched","*.json"))):
    wav=os.path.basename(p).replace(".json",".wav"); off=man[wav]["offset_sec"]
    f=frames_from_items(json.load(open(p))["results"]["items"], off, "")
    d={}
    for k,v in f.items():
        if k in S: d.setdefault(v,{}).setdefault(S[k],0); d[v][S[k]]+=1
    lab=[]
    for v in sorted(d):
        top=sorted(d[v].items(),key=lambda x:-x[1])
        tot=sum(d[v].values())
        lab.append(f"{v}->{top[0][0]}({top[0][1]/tot*100:.0f}%)")
    print(f"  {os.path.basename(p)[-30:]:<32} " + "  ".join(lab))
