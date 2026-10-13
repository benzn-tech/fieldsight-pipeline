"""Cut the clips a person will listen to, and leave the dataset in S3 where it belongs.

## Where the dataset lives

**S3 is the system of record; the labelling page is a view of it.** The clips and the
manifest are written under `voiceprint_eval/{run_id}/` in the data bucket, and the labels
come back to the same prefix when the round finishes. A dataset that exists only inside a
web page cannot be re-cut with a different threshold, re-listened to by a second person, or
used at all a month later without somebody picking it back out of the page by hand.

## The trap this script exists to avoid

**Cut from `audio_segments/`, never from `users/{folder}/audio/`.**

After batching, a transcript's timeline belongs to the BATCH wav, not to any single upload.
From 2026-09-10 transcript names look like `..._c0000_bn2_off0.0_to58.0_...` and their item
timestamps run to 58 s, while the device's own `users/.../audio/.../_c0000.wav` is only ever
30 s. Stripping `_bnN` to find "the original" fetches a 30-second file, every turn past 30 s
cuts an empty array, and `MIN_TURN_S` then drops them silently -- the measurement quietly
becomes "the first 30 seconds of each batch". That happened twice in September and the
wrong version looked entirely normal: on 2026-09-10 it used 30 s of a 114 s recording.

The rule in one line: **the timestamps belong to a file; read THAT file.** The VAD step
writes `audio_segments/{folder}/{date}/{transcript stem}.wav` against the transcript's own
timeline, so the stems correspond exactly.

## What a clip is

One speaker turn, >= `MIN_TURN_S`, cut from its own segment wav. Turns come from
`transcript_utils` -- the same function the transcript viewer and the matcher use -- so the
clip a person labels is the same unit `decide_name` will be judged on. Building a second
notion of "a turn" here would mean labelling something the system never sees.

Usage (needs AWS, reads only; writing the dataset is a separate --upload flag):

    python scripts/labelling/cut_turns.py --clips clips.json --out runs/eval1
    python scripts/labelling/cut_turns.py --clips clips.json --out runs/eval1 --upload
"""
from __future__ import annotations

import argparse
import io
import json
import os
import re
import sys
import wave

sys.path.insert(0, os.path.join(
    os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))), "src"))

import transcript_utils  # noqa: E402

MIN_TURN_S = 3.0

#: Clips longer than this are trimmed from their START rather than their end. A person needs
#: enough to recognise a voice and no more, and the opening of a turn is where the speaker
#: change actually happens -- the tail is where an interruption or the next speaker bleeds in.
MAX_CLIP_S = 12.0

#: Where the dataset lives in S3. One prefix per run so a re-cut never overwrites the set a
#: label file refers to; labels land beside the clips they describe.
DATASET_PREFIX = "voiceprint_eval"


def _segment_key(transcript_key: str) -> str:
    """`transcripts/F/D/x.json` -> `audio_segments/F/D/x.wav`.

    Derived by swapping the prefix and the extension, NOT by parsing the stem for a chunk
    number and rebuilding it. The stem carries each chunk's own timestamp and a `_bnN` batch
    marker; every attempt to reconstruct a key from its parts has fetched the wrong file.
    """
    if not transcript_key.startswith("transcripts/"):
        raise ValueError(f"not a transcript key: {transcript_key}")
    return "audio_segments/" + transcript_key[len("transcripts/"):].rsplit(".", 1)[0] + ".wav"


def turns_from_transcript(doc) -> list[dict]:
    """Speaker turns >= MIN_TURN_S, in the same shape the rest of the system uses."""
    results = (doc or {}).get("results") or {}
    segs = results.get("audio_segments")
    if not segs:
        segs = transcript_utils.speaker_turns_from_items(results)
    out = []
    for s in segs or []:
        try:
            start, end = float(s.get("start_time")), float(s.get("end_time"))
        except (TypeError, ValueError):
            continue
        if end - start < MIN_TURN_S:
            continue
        out.append({"start_s": start, "end_s": end,
                    "speaker_label": s.get("speaker_label"),
                    "text": (s.get("transcript") or "").strip()})
    return out


#: Peak level the listening copy is normalised to, as a fraction of full scale. Loud enough
#: to hear on a laptop speaker, short of clipping.
LISTEN_PEAK = 0.7


def normalise_for_listening(frames: bytes) -> bytes:
    """Bring a clip up to a level a person can actually hear.

    **This does not touch the measurement.** Cosine similarity over ECAPA embeddings is
    gain-invariant here, measured: on 2026-09-17 a set of enrolment clips 4-8x quieter than
    the rest produced *identical* scores to four decimal places after normalisation. The
    embeddings are computed from the S3 audio, not from these clips; this copy exists only
    to be listened to.

    **It does change whether a label can be produced at all**, which is why it is not
    optional. Measured on the first real cut, 2026-09-23: peaks across sixteen clips ranged
    from 973 to 27562 out of 32768 -- a 28x spread, the quietest around -30 dBFS. A listener
    given those raw would mark the quiet half "unusable" and the dataset would end up
    describing loud recordings only, with nothing anywhere saying so. That is a sampling
    bias introduced by the tool, and it would look exactly like a property of the material.

    A silent clip is returned unchanged rather than amplified: multiplying nothing by a
    large number is how a microphone fault becomes convincing-sounding noise.
    """
    if not frames:
        return frames
    import array
    a = array.array("h")
    a.frombytes(frames)
    peak = max((abs(v) for v in a), default=0)
    if peak == 0:
        return frames
    gain = (LISTEN_PEAK * 32767.0) / peak
    if gain <= 1.0:
        return frames
    for i, v in enumerate(a):
        a[i] = max(-32768, min(32767, int(v * gain)))
    return a.tobytes()


def cut_wav(raw: bytes, start_s: float, end_s: float, normalise: bool = True) -> bytes | None:
    """The bytes of one turn, or None when the window is not inside this file.

    None rather than a silent empty clip: a window outside the file is the signature of the
    batch-timeline defect above, and an empty wav would be labelled "unusable" by a person
    and disappear into the results as ordinary attrition.
    """
    with wave.open(io.BytesIO(raw), "rb") as w:
        rate, width, channels = w.getframerate(), w.getsampwidth(), w.getnchannels()
        total = w.getnframes()
        if end_s - start_s > MAX_CLIP_S:
            end_s = start_s + MAX_CLIP_S
        a, b = int(start_s * rate), int(end_s * rate)
        if a >= total or b <= a:
            return None
        b = min(b, total)
        w.setpos(a)
        frames = w.readframes(b - a)
    if normalise and width == 2 and channels == 1:
        frames = normalise_for_listening(frames)
    buf = io.BytesIO()
    with wave.open(buf, "wb") as o:
        o.setnchannels(channels)
        o.setsampwidth(width)
        o.setframerate(rate)
        o.writeframes(frames)
    return buf.getvalue()


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--clips", required=True, help="output of select_turns.py")
    ap.add_argument("--out", required=True, help="local directory for the cut clips")
    ap.add_argument("--bucket", default="fieldsight-data-509194952652")
    ap.add_argument("--per-session", type=int, default=4)
    ap.add_argument("--upload", action="store_true",
                    help="also write the dataset to s3://{bucket}/%s/{run}/" % DATASET_PREFIX)
    ap.add_argument("--run-id", default=None)
    args = ap.parse_args(argv)

    import boto3
    s3 = boto3.client("s3")
    run_id = args.run_id or __import__("datetime").date.today().isoformat()
    os.makedirs(args.out, exist_ok=True)

    plan = json.load(open(args.clips, encoding="utf-8"))
    manifest, skipped = [], []

    for entry in plan.get("clips") or []:
        tkey = entry["key"]
        try:
            doc = json.loads(s3.get_object(Bucket=args.bucket, Key=tkey)["Body"].read())
        except Exception as exc:
            # Loudly. A missing transcript and a missing GRANT look the same from here --
            # s3:GetObject denied is a 403, and treating it as "no material" is how a
            # permissions problem becomes a quiet shortfall in the dataset.
            skipped.append({"key": tkey, "why": f"transcript unreadable: {exc}"})
            continue
        turns = turns_from_transcript(doc)[: args.per_session]
        if not turns:
            skipped.append({"key": tkey, "why": "no turns over the duration floor"})
            continue
        skey = _segment_key(tkey)
        try:
            raw = s3.get_object(Bucket=args.bucket, Key=skey)["Body"].read()
        except Exception as exc:
            skipped.append({"key": skey, "why": f"segment audio unreadable: {exc}"})
            continue

        for i, t in enumerate(turns):
            data = cut_wav(raw, t["start_s"], t["end_s"])
            if data is None:
                # The batch-timeline defect, named rather than counted. If this fires at all
                # the dataset is being cut from the wrong file and the run should stop.
                skipped.append({"key": skey, "why": f"window {t['start_s']}-{t['end_s']}s "
                                                    f"is outside the segment audio"})
                continue
            name = f"{entry['session']}_{i:02d}.wav"
            open(os.path.join(args.out, name), "wb").write(data)
            manifest.append({
                "clip": name, "folder": entry["folder"], "date": entry["date"],
                "device": entry["device"], "session": entry["session"],
                "transcript_key": tkey, "segment_key": skey,
                "start_s": t["start_s"], "end_s": t["end_s"],
                "speaker_label": t["speaker_label"],
                # The words, so a listener has the context a bare four seconds does not give
                # -- and so a label can be sanity-checked later without re-listening.
                "text": t["text"][:400],
            })

    meta = {"run_id": run_id, "bucket": args.bucket, "clips": manifest,
            "skipped": skipped, "min_turn_s": MIN_TURN_S, "max_clip_s": MAX_CLIP_S}
    open(os.path.join(args.out, "manifest.json"), "w", encoding="utf-8").write(
        json.dumps(meta, indent=2))

    if args.upload:
        base = f"{DATASET_PREFIX}/{run_id}"
        for row in manifest:
            s3.put_object(Bucket=args.bucket, Key=f"{base}/clips/{row['clip']}",
                          Body=open(os.path.join(args.out, row["clip"]), "rb").read(),
                          ContentType="audio/wav")
        s3.put_object(Bucket=args.bucket, Key=f"{base}/manifest.json",
                      Body=json.dumps(meta, indent=2).encode("utf-8"),
                      ContentType="application/json")
        print(f"dataset written to s3://{args.bucket}/{base}/")

    print(f"{len(manifest)} clips, {len(skipped)} skipped -> {args.out}")
    for s in skipped[:10]:
        print(f"  SKIPPED {s['key']}: {s['why']}", file=sys.stderr)
    # Any window falling outside its segment audio means the wrong file is being cut and
    # every number downstream is about the first N seconds of each batch. Not a warning.
    if any("outside the segment audio" in s["why"] for s in skipped):
        print("STOP: at least one window fell outside its segment audio. The dataset is "
              "being cut from the wrong file -- see this module's docstring.", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
