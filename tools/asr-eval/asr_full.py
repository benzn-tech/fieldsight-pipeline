"""One-shot whole-session ElevenLabs scribe_v2 run, timed."""
import json, os, sys, time
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "..", "src"))
os.environ.setdefault("ELEVENLABS_STT_MODEL", "scribe_v2")
import urllib3
import elevenlabs_utils as EL

W = os.environ.get("ASR_EVAL_WORK", os.path.dirname(os.path.abspath(__file__)))
AUDIO = os.path.join(W, "session_full.wav")
VOCAB = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "..", "config", "custom_vocabulary_construction_nz.txt")

keyterms = EL.load_keyterms(VOCAB)
audio = open(AUDIO, "rb").read()
fields = EL._build_fields(audio, os.path.basename(AUDIO), 5, keyterms, True)
http = urllib3.PoolManager()
print(f"upload {len(audio)/1e6:.1f} MB, {len(keyterms)} keyterms, num_speakers=5", flush=True)
t0 = time.time()
resp = http.request("POST", EL.ELEVENLABS_STT_URL, fields=fields,
                    headers={"xi-api-key": EL.ELEVENLABS_API_KEY}, timeout=1800.0)
elapsed = time.time() - t0
print(f"HTTP {resp.status} in {elapsed:.1f}s", flush=True)
if resp.status != 200:
    print(resp.data[:1000]); sys.exit(1)
raw = json.loads(resp.data.decode("utf-8"))
json.dump(raw, open(os.path.join(W, "full_raw.json"), "w"))
json.dump(EL.adapt_to_transcribe_json(raw), open(os.path.join(W, "full_transcribe_shaped.json"), "w"))
json.dump({"elapsed_sec": round(elapsed, 2), "http": resp.status,
           "audio_bytes": len(audio), "keyterms": len(keyterms)},
          open(os.path.join(W, "full_timing.json"), "w"), indent=1)
print("done")
