# -*- coding: utf-8 -*-
"""Run 3: whole session again, with 5 user-supplied proper nouns added to the keyterms.

Everything else identical to run 2 (same wav, same model, same num_speakers).
`Lindis Pass` is deliberately NOT added — the user named it as a known error but did not
put it in the list, so it stays as a control: if the 5 listed terms come out right and the
unlisted one stays wrong, keyterms are the lever.
"""
import json, os, sys, time
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "..", "src"))
import urllib3
import elevenlabs_utils as EL

W = os.environ.get("ASR_EVAL_WORK", os.path.dirname(os.path.abspath(__file__)))
AUDIO = os.path.join(W, "session_full.wav")
VOCAB = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "..", "config", "custom_vocabulary_construction_nz.txt")
NEW = ["Plaud", "DeanDre", "Southbase", "VisioField", "VizField"]

base = EL.load_keyterms(VOCAB)
keyterms = NEW + [t for t in base if t not in NEW]
audio = open(AUDIO, "rb").read()
fields = EL._build_fields(audio, os.path.basename(AUDIO), 5, keyterms, True)
http = urllib3.PoolManager()
print(f"{len(audio)/1e6:.1f} MB, {len(keyterms)} keyterms ({len(NEW)} new: {', '.join(NEW)})", flush=True)
t0 = time.time()
resp = http.request("POST", EL.ELEVENLABS_STT_URL, fields=fields,
                    headers={"xi-api-key": EL.ELEVENLABS_API_KEY}, timeout=1800.0)
el = time.time() - t0
print(f"HTTP {resp.status} in {el:.1f}s", flush=True)
if resp.status != 200:
    print(resp.data[:1000]); sys.exit(1)
raw = json.loads(resp.data.decode("utf-8"))
json.dump(raw, open(os.path.join(W, "kt_raw.json"), "w"))
json.dump(EL.adapt_to_transcribe_json(raw), open(os.path.join(W, "kt_transcribe_shaped.json"), "w"))
json.dump({"elapsed_sec": round(el, 2), "keyterms": len(keyterms), "new_terms": NEW},
          open(os.path.join(W, "kt_timing.json"), "w"), indent=1)
print("done")
