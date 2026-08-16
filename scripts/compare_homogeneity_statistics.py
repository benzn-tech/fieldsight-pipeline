"""Which statistic, if any, tells one voice from two on real site audio.

`frame_spread` — the max over all frame pairs — does not. Measured on 13 windows of one
person (TEST, Ben_UCPK2, 2026-08-12, recorded alone) and 12 windows of two people (PROD,
Ben_UCPK2, 2026-08-11, with Sam Yu): one voice spans 0.429–0.777, two voices span
0.445–1.022. The classes overlap across almost the whole of the first, so no threshold on
that statistic divides them. That is a fact about the statistic, not about the number 0.35.

This runs every candidate over the same two sets and asks one question of each:

    is there a line with one class above it and the other below?

It reports the answer whatever it is. A statistic that fails to separate is a result, not a
setback — the alternative to hearing it is shipping a threshold that looks calibrated.

    MSYS_NO_PATHCONV=1 python scripts/compare_homogeneity_statistics.py --sub <cognito-sub>

Reads only: `op: "spread"` writes nothing, and returns scalars — frame embeddings never
leave the function that computes them.
"""
import argparse
import json
import subprocess
import sys

REGION = "ap-southeast-2"

ONE_VOICE = {
    "fn": "fieldsight-test-speaker-embed",
    "api": "fieldsight-test-org-api",
    "user": "Ben_UCPK2", "date": "2026-08-12",
    "bucket": "fieldsight-data-test-509194952652",
    "what": "one voice (recorded alone)",
}
TWO_VOICES = {
    "fn": "fieldsight-prod-speaker-embed",
    "api": "fieldsight-prod-org-api",
    "user": "Ben_UCPK2", "date": "2026-08-11",
    "what": "two voices (with Sam Yu)",
}

CANDIDATES = ["pair_max", "pair_median", "centroid_max", "centroid_mean", "clusters"]


def _invoke(function, payload):
    with open("_cmp.json", "w", encoding="utf-8") as fh:
        json.dump(payload, fh)
    r = subprocess.run(
        ["aws", "lambda", "invoke", "--function-name", function,
         "--cli-binary-format", "raw-in-base64-out", "--payload", "file://_cmp.json",
         "_cmp_out.json", "--region", REGION], capture_output=True, text=True)
    if r.returncode:
        print("INVOKE FAILED:", r.stderr[:200], file=sys.stderr)
        return None
    with open("_cmp_out.json", encoding="utf-8") as fh:
        return json.load(fh)


def _raw_chunks(bucket, user, date):
    r = subprocess.run(
        ["aws", "s3", "ls", f"s3://{bucket}/users/{user}/audio/{date}/", "--region", REGION],
        capture_output=True, text=True)
    return [line.split()[-1] for line in (r.stdout or "").splitlines()
            if line.strip().endswith(".wav")]


def _two_speaker_windows(env, sub):
    """Windows the transcriber itself says hold more than one speaker."""
    got = _invoke(env["api"], {
        "httpMethod": "GET", "path": "/api/org/transcripts",
        "queryStringParameters": {"date": env["date"], "user": env["user"],
                                  "start": "00:00", "end": "23:59"},
        "requestContext": {"authorizer": {"claims": {"sub": sub}}}})
    if not got or got.get("statusCode") != 200:
        print("could not read transcripts:", str(got)[:160])
        return []
    segs = json.loads(got["body"], strict=False).get("speaker_segments") or []
    by_file = {}
    for s in segs:
        if s.get("speaker_label") and s.get("chunk_start") is not None:
            by_file.setdefault(s["source_filename"], []).append(s)
    out = []
    for fname, turns in by_file.items():
        if len({t["speaker_label"] for t in turns}) < 2:
            continue
        turns.sort(key=lambda s: s["chunk_start"])
        a = turns[0]["chunk_start"]
        z = turns[-1]["chunk_start"] + turns[-1]["duration"]
        if z - a >= 10.0:
            out.append((fname, a, min(z, a + 20.0)))
    return out


def collect(env, windows):
    rows = []
    for fname, a, b in windows:
        got = _invoke(env["fn"], {"op": "spread", "user_folder": env["user"],
                                  "date": env["date"], "source_filename": fname,
                                  "start_sec": a, "end_sec": b})
        cand = (got or {}).get("candidates")
        if cand:
            rows.append(cand)
    return rows


def separation(one, two, key):
    """Is there a line with one class entirely below and the other entirely above?

    Reported as the OVERLAP, because that is the number that decides it. Zero overlap means
    a threshold exists; anything else means every threshold misclassifies something, and the
    two directions are not equally costly — a wrongly accepted window poisons a profile that
    cannot be cleaned.
    """
    a = sorted(r[key] for r in one if key in r)
    b = sorted(r[key] for r in two if key in r)
    if not a or not b:
        return None
    lo, hi = max(a), min(b)          # one-voice ceiling, two-voice floor
    misgrouped = sum(1 for x in a if x >= hi) + sum(1 for x in b if x <= lo)
    return {"one": (a[0], a[len(a) // 2], a[-1]),
            "two": (b[0], b[len(b) // 2], b[-1]),
            "separates": lo < hi,
            "gap": hi - lo,
            "misgrouped": misgrouped, "n": len(a) + len(b)}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--sub", required=True)
    args = ap.parse_args()

    chunks = _raw_chunks(ONE_VOICE["bucket"], ONE_VOICE["user"], ONE_VOICE["date"])
    one_windows = [(c, 2.0, 17.0) for c in chunks]
    print(f"{ONE_VOICE['what']}: {len(one_windows)} windows")
    two_windows = _two_speaker_windows(TWO_VOICES, args.sub)
    print(f"{TWO_VOICES['what']}: {len(two_windows)} windows\n")

    one = collect(ONE_VOICE, one_windows)
    two = collect(TWO_VOICES, two_windows)
    if not one or not two:
        print("not enough data on one side; a comparison needs both")
        return

    print(f"{'statistic':<15}{'one voice (min/med/max)':<26}"
          f"{'two voices (min/med/max)':<26}verdict")
    print("-" * 88)
    winners = []
    for key in CANDIDATES:
        s = separation(one, two, key)
        if s is None:
            continue
        fmt = "%.3f/%.3f/%.3f" if key != "clusters" else "%.0f/%.0f/%.0f"
        verdict = (f"SEPARATES (gap {s['gap']:.3f})" if s["separates"]
                   else f"overlaps — {s['misgrouped']}/{s['n']} on the wrong side")
        print(f"{key:<15}{fmt % s['one']:<26}{fmt % s['two']:<26}{verdict}")
        if s["separates"]:
            winners.append((key, s))

    print()
    if winners:
        for key, s in winners:
            print(f"  {key} separates the two classes: every one-voice window is below "
                  f"{s['two'][0]:.3f} and every two-voice window above {s['one'][2]:.3f}.")
            print(f"  A threshold anywhere in that gap divides them ON THIS DATA — "
                  f"{s['n']} windows, one speaker pair, one device.")
        print("\n  That is a candidate, not a decision. Two speakers and one room is a")
        print("  narrow basis for a guard whose false accept cannot be undone.")
    else:
        print("  No candidate separates the classes.")
        print()
        print("  The conclusion is then about the APPROACH, not the number: frame-to-frame")
        print("  similarity does not carry enough speaker identity on this audio to decide")
        print("  how many people are in a window. Raising the current threshold would buy")
        print("  enrolments at the cost of accepting two-voice windows, and a poisoned")
        print("  profile cannot be cleaned.")


if __name__ == "__main__":
    main()
