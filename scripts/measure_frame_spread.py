"""Measure the frame spread of real enrolment windows on TEST.

Three enrolments have been refused as "not homogeneous", one of them a 14.7 s window inside a
single chunk — the shape enrolment is supposed to accept. The verdict alone cannot tell
"this window really holds two people" from "0.35 was measured on read speech and site audio
does not look like that".

This asks the deployed embedder for the NUMBER rather than the verdict, over a range of
window lengths from one session, so the threshold can be argued about with data.

    MSYS_NO_PATHCONV=1 python scripts/measure_frame_spread.py \
        --user Ben_UCPK2 --date 2026-08-13 --sub <cognito-sub>

It writes NOTHING: every probe is `op: "spread"`, a read-only op that exists precisely
because the two that could otherwise produce this number both have side effects — `enrol`
stores the sample when the window passes, and `match` never computes frames at all.
"""
import argparse
import json
import subprocess
import sys

REGION = "ap-southeast-2"
ORG_API = "fieldsight-test-org-api"
EMBEDDER = "fieldsight-test-speaker-embed"


def _invoke(function, payload):
    with open("_probe.json", "w", encoding="utf-8") as fh:
        json.dump(payload, fh)
    r = subprocess.run(
        ["aws", "lambda", "invoke", "--function-name", function,
         "--cli-binary-format", "raw-in-base64-out", "--payload", "file://_probe.json",
         "_probe_out.json", "--region", REGION],
        capture_output=True, text=True)
    if r.returncode:
        print("INVOKE FAILED:", r.stderr[:400], file=sys.stderr)
        return None
    with open("_probe_out.json", encoding="utf-8") as fh:
        return json.load(fh)


def transcript_turns(sub, user, date):
    out = _invoke(ORG_API, {
        "httpMethod": "GET", "path": "/api/org/transcripts",
        "queryStringParameters": {"date": date, "user": user,
                                  "start": "00:00", "end": "23:59"},
        "requestContext": {"authorizer": {"claims": {"sub": sub}}}})
    if not out or out.get("statusCode") != 200:
        print("could not read transcripts:", str(out)[:200])
        return []
    return json.loads(out["body"], strict=False).get("speaker_segments") or []



def _calibration_limit(rows):
    """The limit to calibrate AGAINST, which is the compiled-in default and never the
    override.

    `op: "spread"` returns both. Reading `limit` — the one in force — would make this tool
    tautological the moment TEST sets VOICEPRINT_MAX_FRAME_SPREAD: run it at 0.7 to answer
    "is 0.35 wrong?" and it answers "0 of 13 windows refused", which is a restatement of the
    setting, not a measurement.
    """
    for r in rows:
        if r.get("default_limit") is not None:
            if r.get("limit") is not None and r["limit"] != r["default_limit"]:
                print(f"  NOTE: the function is running with an OVERRIDDEN limit of "
                      f"{r['limit']}; calibrating against the default "
                      f"{r['default_limit']} regardless.")
            return r["default_limit"]
    return next((r["limit"] for r in rows if r.get("limit") is not None), 0.35)

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--user", required=True)
    ap.add_argument("--date", required=True)
    ap.add_argument("--sub", required=True)
    ap.add_argument("--max", type=int, default=8, help="how many windows to probe")
    args = ap.parse_args()

    segs = transcript_turns(args.sub, args.user, args.date)
    # Under two 5 s frames the answer is None, not a number, and those windows say nothing
    # about the threshold.
    usable = [s for s in segs if (s.get("duration") or 0) >= 10.0]
    usable.sort(key=lambda s: s.get("duration") or 0)
    if not usable:
        longest = max((s.get("duration") or 0) for s in segs) if segs else 0
        print(f"no window of 10 s or more in {args.user}/{args.date} — "
              f"{len(segs)} turns read, longest {longest:.1f}s")
        return

    print(f"{len(usable)} judgeable windows; probing {min(args.max, len(usable))}\n")
    results = []
    for s in usable[:args.max]:
        out = _invoke(EMBEDDER, {
            "op": "spread",
            "user_folder": args.user, "date": args.date,
            "source_filename": s["source_filename"],
            "start_sec": s["chunk_start"],
            "end_sec": s["chunk_start"] + s["duration"]})
        if not out or "spread" not in out:
            print(f"  {s['duration']:6.1f}s  FAILED {str(out)[:140]}")
            continue
        results.append(out)
        print(f"  {out['seconds']:6.1f}s  frames={out['frames']:2d}  "
              f"spread={out['spread']}  {out['verdict']}")

    judged = sorted(r["spread"] for r in results if r["spread"] is not None)
    if judged:
        limit = _calibration_limit(results)
        over = [x for x in judged if x > limit]
        print(f"\n  n={len(judged)}  min={judged[0]:.3f}  "
              f"median={judged[len(judged) // 2]:.3f}  max={judged[-1]:.3f}")
        print(f"  limit={limit} — {len(over)} of {len(judged)} windows refused")
        print("\n  A single-speaker window above the limit is the finding. One is anecdote;")
        print("  a majority is the threshold being wrong for this audio.")


if __name__ == "__main__":
    main()
