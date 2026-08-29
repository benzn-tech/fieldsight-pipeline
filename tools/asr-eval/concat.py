import wave, os, json, re, sys
W = os.environ.get("ASR_EVAL_WORK", os.path.dirname(os.path.abspath(__file__)))
wd = os.path.join(W, "wav")
files = sorted(os.listdir(wd))
# chronological: filename embeds HH-MM-SS then chunk index
def key(f):
    m = re.search(r"_(\d{4}-\d{2}-\d{2})_(\d{2}-\d{2}-\d{2})_sid\w+_c(\d{4})", f)
    return (m.group(1), m.group(2), int(m.group(3)))
files.sort(key=key)
out = os.path.join(W, "session_full.wav")
params = None
total = 0
manifest = []
with wave.open(out, "wb") as o:
    for f in files:
        with wave.open(os.path.join(wd, f), "rb") as i:
            p = i.getparams()
            if params is None:
                params = p
                o.setnchannels(p.nchannels); o.setsampwidth(p.sampwidth); o.setframerate(p.framerate)
            else:
                assert (p.nchannels,p.sampwidth,p.framerate)==(params.nchannels,params.sampwidth,params.framerate), f
            n = i.getnframes()
            manifest.append({"file": f, "offset_sec": round(total/p.framerate,3), "dur_sec": round(n/p.framerate,3)})
            total += n
            o.writeframes(i.readframes(n))
print(json.dumps({"channels":params.nchannels,"sampwidth":params.sampwidth,"rate":params.framerate,
                  "total_sec": round(total/params.framerate,2), "n_parts": len(files),
                  "bytes": os.path.getsize(out)}, indent=1))
json.dump(manifest, open(os.path.join(W,"manifest.json"),"w"), indent=1)
