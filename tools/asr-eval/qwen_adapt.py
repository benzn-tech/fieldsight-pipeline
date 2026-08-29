# -*- coding: utf-8 -*-
"""Reshape a qwen filetrans result into the AWS-Transcribe shape the rest of the tools use.

Same target shape as elevenlabs_utils.adapt_to_transcribe_json, so confusion.py /
purity_gt.py / spk_score.py / term_hunt.py / four_way.py all work on qwen output unchanged.

qwen gives word-level times in MILLISECONDS and speaker_id per SENTENCE, so each word
inherits its sentence's speaker. speaker_id is an int from 0 -> spk_N in first-seen order,
matching how the ElevenLabs adapter numbers speakers.
"""
import json, os, sys

W = os.environ.get("ASR_EVAL_WORK", os.path.dirname(os.path.abspath(__file__)))
tag = sys.argv[1]
src = json.load(open(os.path.join(W, f"qwen_{tag}_transcript.json"), encoding="utf-8"))

props = src.get("properties", {})
items, texts, smap = [], [], {}
n_words_no_time = 0
for tr in src.get("transcripts", []):
    texts.append(tr.get("text", ""))
    for s in tr.get("sentences", []):
        sid = s.get("speaker_id")
        lab = None
        if sid is not None:
            if sid not in smap:
                smap[sid] = f"spk_{len(smap)}"
            lab = smap[sid]
        words = s.get("words") or []
        if not words:
            # a sentence with no word breakdown still carries its own span; keep it as one
            # item rather than dropping the text, and say so rather than silently padding.
            words = [{"begin_time": s.get("begin_time"), "end_time": s.get("end_time"),
                      "text": s.get("text", ""), "punctuation": ""}]
        for w in words:
            b, e = w.get("begin_time"), w.get("end_time")
            if b is None or e is None:
                n_words_no_time += 1
                continue
            content = (w.get("text") or "").strip()
            if not content:
                continue
            it = {"type": "pronunciation",
                  "start_time": str(round(b / 1000.0, 3)),
                  "end_time": str(round(e / 1000.0, 3)),
                  "alternatives": [{"content": content, "confidence": "1.0"}]}
            if lab:
                it["speaker_label"] = lab
            items.append(it)

out = {"results": {"transcripts": [{"transcript": " ".join(t for t in texts if t)}],
                   "items": items}}
p = os.path.join(W, f"qwen_{tag}_shaped.json")
json.dump(out, open(p, "w", encoding="utf-8"), ensure_ascii=False)

dur = props.get("original_duration_in_milliseconds")
content = sum(t.get("content_duration_in_milliseconds") or 0 for t in src.get("transcripts", []))
print(f"tag={tag}  words={len(items)}  speakers={len(smap)} {sorted(smap.items())}")
print(f"  audio {(dur or 0)/1000:.0f}s, qwen-judged speech content {content/1000:.0f}s "
      f"({content/max(dur,1)*100:.1f}% of audio)")
print(f"  sample_rate={props.get('original_sampling_rate')} channels={props.get('channels')} "
      f"format={props.get('audio_format')}")
if n_words_no_time:
    print(f"  WARNING: {n_words_no_time} words had no timestamps and were dropped")
print(f"  -> {p}")
