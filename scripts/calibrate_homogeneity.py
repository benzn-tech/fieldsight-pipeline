"""Measure the homogeneity statistic against windows whose speaker count is known.

The threshold `DEFAULT_MAX_FRAME_SPREAD = 0.35` was measured once, on read speech, and every
enrolment attempted on real site audio has been refused. Moving it needs a measurement that
includes windows KNOWN TO HOLD TWO PEOPLE — a measurement made only on one-voice windows can
only ever tell us how to accept more, which is the direction that cannot be undone.

The labels come from the transcriber's own diarisation:

    one voice   a span of consecutive turns all carrying the SAME speaker label
    two voices  a span containing turns from at least two DIFFERENT labels

**What this assumes, stated because it is the weak point.** It assumes the transcriber's
speaker labels are right about boundaries. They are not always — this pipeline has recorded
cases of three people getting two labels, and of the label count being right while the
content was scrambled. So a two-voice window here is "two voices according to the provider",
not ground truth. It is still far better than calibrating on one-voice windows alone, and any
threshold drawn from it should be read with that caveat attached.

    MSYS_NO_PATHCONV=1 python scripts/calibrate_homogeneity.py \
        --user Ben_UCPK2 --date 2026-08-13 --start 18:05 --end 18:25 --sub <cognito-sub>

Writes nothing: `op: "spread"` is read-only.
"""
import argparse
import json
import subprocess
import sys

REGION = "ap-southeast-2"
ORG_API = "fieldsight-test-org-api"
EMBEDDER = "fieldsight-test-speaker-embed"
MIN_WINDOW_S = 10.0   # under two 5 s frames the answer is None, not a number


def _invoke(function, payload):
    with open("_cal.json", "w", encoding="utf-8") as fh:
        json.dump(payload, fh)
    r = subprocess.run(
        ["aws", "lambda", "invoke", "--function-name", function,
         "--cli-binary-format", "raw-in-base64-out", "--payload", "file://_cal.json",
         "_cal_out.json", "--region", REGION],
        capture_output=True, text=True)
    if r.returncode:
        print("INVOKE FAILED:", r.stderr[:300], file=sys.stderr)
        return None
    with open("_cal_out.json", encoding="utf-8") as fh:
        return json.load(fh)


def windows_by_speaker_count(segs):
    """Spans of at least MIN_WINDOW_S, labelled by how many speakers the transcriber put in
    them. Grouped per source file, because a span may not cross a transcription call: the
    labels are per call and the audio is per file."""
    by_file = {}
    for s in segs:
        if s.get("chunk_start") is None or not s.get("speaker_label"):
            continue
        by_file.setdefault(s["source_filename"], []).append(s)

    one, two = [], []
    for fname, turns in by_file.items():
        turns.sort(key=lambda s: s["chunk_start"])
        for i in range(len(turns)):
            labels, start = set(), turns[i]["chunk_start"]
            for j in range(i, len(turns)):
                labels.add(turns[j]["speaker_label"])
                end = turns[j]["chunk_start"] + turns[j]["duration"]
                if end - start < MIN_WINDOW_S:
                    continue
                (one if len(labels) == 1 else two).append(
                    {"file": fname, "start": start, "end": end,
                     "labels": len(labels)})
                break          # shortest qualifying span from this start
    return one, two


def probe(user, date, w):
    out = _invoke(EMBEDDER, {"op": "spread", "user_folder": user, "date": date,
                             "source_filename": w["file"],
                             "start_sec": w["start"], "end_sec": w["end"]})
    return out if out and "spread" in out else None


def summarise(name, rows, limit):
    vals = sorted(r["spread"] for r in rows if r.get("spread") is not None)
    unjudged = sum(1 for r in rows if r.get("spread") is None)
    if not vals:
        print(f"  {name}: nothing judgeable ({unjudged} unjudgeable)")
        return vals
    over = [v for v in vals if v > limit]
    print(f"  {name}: n={len(vals)} min={vals[0]:.3f} "
          f"median={vals[len(vals) // 2]:.3f} max={vals[-1]:.3f} "
          f"| above {limit}: {len(over)}/{len(vals)} | unjudgeable {unjudged}")
    return vals


def main():
    ap = argparse.ArgumentParser()
    for a in ("user", "date", "sub"):
        ap.add_argument("--" + a, required=True)
    ap.add_argument("--start", default="00:00")
    ap.add_argument("--end", default="23:59")
    ap.add_argument("--max", type=int, default=10, help="windows per class")
    args = ap.parse_args()

    got = _invoke(ORG_API, {
        "httpMethod": "GET", "path": "/api/org/transcripts",
        "queryStringParameters": {"date": args.date, "user": args.user,
                                  "start": args.start, "end": args.end},
        "requestContext": {"authorizer": {"claims": {"sub": args.sub}}}})
    if not got or got.get("statusCode") != 200:
        print("could not read transcripts:", str(got)[:200])
        return
    segs = json.loads(got["body"], strict=False).get("speaker_segments") or []
    one, two = windows_by_speaker_count(segs)
    print(f"{len(segs)} turns -> {len(one)} one-voice windows, "
          f"{len(two)} two-voice windows\n")
    if not two:
        print("No two-voice window found. Calibrating without them can only say how to")
        print("accept MORE, which is the direction that cannot be undone. Stopping.")
        return

    results = {}
    for name, pool in (("one voice ", one), ("two voices", two)):
        rows = []
        for w in pool[:args.max]:
            got = probe(args.user, args.date, w)
            if got:
                rows.append(got)
                print(f"  [{name}] {got['seconds']:6.1f}s frames={got['frames']:2d} "
                      f"spread={got['spread']} {got['verdict']}")
        results[name] = rows
    print()
    limit = next((r["limit"] for rows in results.values() for r in rows), 0.35)
    a = summarise("one voice ", results["one voice "], limit)
    b = summarise("two voices", results["two voices"], limit)
    print()
    if a and b:
        if max(a) < min(b):
            print(f"  The classes SEPARATE: every one-voice window scores below every")
            print(f"  two-voice one. A threshold between {max(a):.3f} and {min(b):.3f} would")
            print(f"  divide them on this data.")
        else:
            print("  The classes OVERLAP. No threshold on this statistic separates them")
            print("  here, which is an argument about the statistic and not about the")
            print("  number — see the spec's step C.")


if __name__ == "__main__":
    main()
