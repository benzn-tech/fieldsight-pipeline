# -*- coding: utf-8 -*-
"""qwen-audio-3.0-asr-flash-filetrans on the whole session — async file transcription.

Contract per skills/qwen-asr:
  - endpoint  dashscope-intl (our key is the international one)
  - header    X-DashScope-Async: enable
  - input     file_urls (PLURAL, array) — the singular form reports as MalformedURL
  - diarization_enabled / vocabulary are OFF unless passed
  - the task payload holds a transcription_url, not the transcript
"""
import json, os, sys, time
import urllib3

W = os.environ.get("ASR_EVAL_WORK", os.path.dirname(os.path.abspath(__file__)))
KEY = os.environ["DASHSCOPE_API_KEY"]
BASE = "https://dashscope-intl.aliyuncs.com/api/v1"
MODEL = "qwen-audio-3.0-asr-flash-filetrans"

# Same 5 terms ElevenLabs run 3 got, so the comparison is like-for-like.
# `Lindis Pass` and `Naylor Love` are deliberately withheld from BOTH engines — they are
# user-confirmed ground truth, so they work as a shared accuracy probe.
VOCAB = {"Plaud": 4, "DeanDre": 4, "Southbase": 4, "VisioField": 4, "VizField": 4}

tag = sys.argv[1] if len(sys.argv) > 1 else "a"
speaker_count = int(sys.argv[2]) if len(sys.argv) > 2 else 5
use_vocab = (sys.argv[3] != "novocab") if len(sys.argv) > 3 else True

url = open(os.path.join(W, "audio_url.txt")).read().strip()
http = urllib3.PoolManager()
H = {"Authorization": f"Bearer {KEY}", "Content-Type": "application/json",
     "X-DashScope-Async": "enable"}

params = {"diarization_enabled": True, "speaker_count": speaker_count,
          "language_hints": ["zh", "en"]}
if use_vocab:
    params["vocabulary"] = VOCAB
body = {"model": MODEL, "input": {"file_urls": [url]}, "parameters": params}

print(f"[{tag}] model={MODEL} speaker_count={speaker_count} vocab={len(VOCAB) if use_vocab else 0}",
      flush=True)
t0 = time.time()
r = http.request("POST", f"{BASE}/services/audio/asr/transcription",
                 body=json.dumps(body).encode(), headers=H, timeout=120)
sub = json.loads(r.data.decode())
if r.status != 200 or "task_id" not in sub.get("output", {}):
    print(f"[{tag}] SUBMIT FAILED HTTP {r.status}: {json.dumps(sub)[:600]}"); sys.exit(1)
task = sub["output"]["task_id"]
print(f"[{tag}] task {task} submitted in {time.time()-t0:.1f}s", flush=True)

status, poll = None, None
while True:
    time.sleep(5)
    p = http.request("GET", f"{BASE}/tasks/{task}",
                     headers={"Authorization": f"Bearer {KEY}"}, timeout=60)
    poll = json.loads(p.data.decode())
    status = poll.get("output", {}).get("task_status")
    if status in ("SUCCEEDED", "FAILED", "CANCELED"):
        break
    if time.time() - t0 > 1800:
        print(f"[{tag}] TIMEOUT after 30 min, last status {status}"); sys.exit(1)
elapsed = time.time() - t0
print(f"[{tag}] {status} in {elapsed:.1f}s", flush=True)
json.dump(poll, open(os.path.join(W, f"qwen_{tag}_task.json"), "w"), ensure_ascii=False, indent=1)
if status != "SUCCEEDED":
    print(json.dumps(poll, ensure_ascii=False)[:1200]); sys.exit(1)

out = poll["output"]
res = out.get("results") or [out.get("result")]
saved = []
for i, r1 in enumerate(res):
    turl = (r1 or {}).get("transcription_url")
    if not turl:
        print(f"[{tag}] no transcription_url in result {i}: {json.dumps(r1)[:400]}"); continue
    tr = json.loads(http.request("GET", turl, timeout=120).data.decode("utf-8"))
    p_out = os.path.join(W, f"qwen_{tag}_transcript.json")
    json.dump(tr, open(p_out, "w", encoding="utf-8"), ensure_ascii=False)
    saved.append(p_out)
    t = (tr.get("transcripts") or [{}])[0]
    sents = t.get("sentences") or []
    spk = sorted({str(s.get("speaker_id")) for s in sents if s.get("speaker_id") is not None})
    print(f"[{tag}] text {len(t.get('text',''))} chars, {len(sents)} sentences, "
          f"speaker_ids={spk}", flush=True)
json.dump({"tag": tag, "elapsed_sec": round(elapsed, 2), "model": MODEL,
           "speaker_count": speaker_count, "vocab": use_vocab, "status": status},
          open(os.path.join(W, f"qwen_{tag}_timing.json"), "w"), indent=1)
print(f"[{tag}] done -> {saved}")
