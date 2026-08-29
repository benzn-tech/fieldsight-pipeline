# -*- coding: utf-8 -*-
"""Create a precompiled hotword list (vocabulary_id) via the official SDK, then transcribe.

The inline `vocabulary` dict produced no measurable effect (run A vs run D sat at 98.8%,
inside the 99.2% same-config noise floor, with 0 hits on all five terms). This is the
documented path instead. Using the SDK rather than guessing REST routes — skills/qwen-asr
records four sessions lost to hand-written requests.

Deletes the list on the way out either way: the quota is finite.
"""
import json, os, sys, time
from urllib import request as urlreq
from http import HTTPStatus

import dashscope
from dashscope.audio.asr import Transcription, VocabularyService

W = os.environ.get("ASR_EVAL_WORK", os.path.dirname(os.path.abspath(__file__)))
dashscope.base_http_api_url = "https://dashscope-intl.aliyuncs.com/api/v1"
dashscope.api_key = os.environ["DASHSCOPE_API_KEY"]

MODEL = "qwen-audio-3.0-asr-flash-filetrans"
tag = sys.argv[1] if len(sys.argv) > 1 else "V"
weight = int(sys.argv[2]) if len(sys.argv) > 2 else 4

# Same five terms ElevenLabs got. `lang` is set per term: the docs warn that once
# language_hints is set, only hotwords whose lang matches will fire.
TERMS = [{"text": t, "weight": weight, "lang": "en"}
         for t in ("Plaud", "DeanDre", "Southbase", "VisioField", "VizField")]

url = open(os.path.join(W, "audio_url.txt")).read().strip()
svc = VocabularyService()
vid = None
try:
    vid = svc.create_vocabulary(prefix="fseval", target_model=MODEL, vocabulary=TERMS)
    q = svc.query_vocabulary(vid)
    print(f"[{tag}] vocabulary_id={vid} status={q.get('status') if isinstance(q, dict) else q}",
          flush=True)

    t0 = time.time()
    task = Transcription.async_call(model=MODEL, file_urls=[url],
                                    vocabulary_id=vid,
                                    diarization_enabled=True, speaker_count=5,
                                    language_hints=["zh", "en"])
    resp = Transcription.wait(task=task.output.task_id)
    el = time.time() - t0
    print(f"[{tag}] {resp.status_code} in {el:.1f}s", flush=True)
    if resp.status_code != HTTPStatus.OK:
        print(resp); sys.exit(1)
    for r in resp.output["results"]:
        if r.get("subtask_status") != "SUCCEEDED":
            print(f"[{tag}] subtask failed: {r}"); continue
        tr = json.loads(urlreq.urlopen(r["transcription_url"]).read().decode("utf8"))
        json.dump(tr, open(os.path.join(W, f"qwen_{tag}_transcript.json"), "w",
                           encoding="utf-8"), ensure_ascii=False)
        t = (tr.get("transcripts") or [{}])[0]
        txt = t.get("text", "")
        hits = {w["text"]: txt.lower().count(w["text"].lower()) for w in TERMS}
        print(f"[{tag}] {len(txt)} chars, {len(t.get('sentences') or [])} sentences, hits={hits}")
    json.dump({"tag": tag, "elapsed_sec": round(el, 2), "vocabulary_id": vid,
               "weight": weight, "terms": TERMS},
              open(os.path.join(W, f"qwen_{tag}_timing.json"), "w"), indent=1)
finally:
    if vid:
        try:
            svc.delete_vocabulary(vid); print(f"[{tag}] deleted {vid}")
        except Exception as e:
            print(f"[{tag}] WARNING could not delete {vid}: {e}")
