"""Which statistic tells one voice from two on real site audio, and where the line goes.

Measured on windows the user identified: 13 recorded alone (TEST, Ben_UCPK2, 2026-08-12) and
16 holding two speakers (TEST, 2026-08-13, both a paired conversation and a rapidly
alternating one).

    statistic       one voice           two voices        best single threshold
    pair_max        0.429/0.576/0.777   0.727/0.932/1.031  t=0.715 -> 3 refused, 0 ACCEPTED
    centroid_max    0.148/0.222/0.350   0.296/0.477/0.559  t=0.286 -> 2 refused, 0 ACCEPTED
    centroid_mean   0.117/0.188/0.267   0.225/0.277/0.396  t=0.217 -> 3 refused, 0 ACCEPTED

The classes very nearly separate, and every error is in the recoverable direction. The
current limit of 0.35 on `pair_max` passes 0 of 13 one-voice windows; the same statistic
admits 11 of 13 with no two-voice window accepted, at roughly twice that number.

**A first run of this said the opposite** — that the classes overlapped so badly no threshold
divided them, and that the statistic therefore had to be replaced. That conclusion came from a
two-voice set built out of PROD 2026-08-11 by pairing turns whose provider labels differed,
and those labels are not reliable enough for it: this pipeline has recorded three people given
two labels, and label counts that were right while the content was scrambled. The set had a
minimum of 0.445 where the better-labelled set has 0.727, and the whole conclusion rested on
that one number. Labelling quality decided the answer, not the statistic.

So the sets used here are the ones whose labels can be checked: a session the user states they
recorded alone, and sessions where two speakers demonstrably alternate.

    MSYS_NO_PATHCONV=1 python scripts/compare_homogeneity_statistics.py --sub <cognito-sub>

Reads only: `op: "spread"` writes nothing and returns scalars — frame embeddings never leave
the function that computes them.
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
# TEST rather than PROD, and 08-13 rather than 08-11, for the reason in the module
# docstring: the PROD set's two-voice labels were not reliable enough to calibrate against,
# and calibrating against them produced the opposite conclusion.
TWO_VOICES = {
    "fn": "fieldsight-test-speaker-embed",
    "api": "fieldsight-test-org-api",
    "user": "Ben_UCPK2", "date": "2026-08-13",
    "what": "two voices (speakers alternate)",
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


def by_direction(one, two, key, threshold):
    """The two error directions, kept apart because their costs are not.

    A window wrongly REFUSED costs a retry. A window wrongly ACCEPTED stores two people as
    one person's voiceprint, and a poisoned profile cannot be cleaned — only the contributing
    sample deleted, after somebody notices, which nothing prompts them to do.

    So a summary that reports "2 of 29 wrong" is not enough to choose a threshold. Which two.
    """
    refused = [r[key] for r in one if r[key] > threshold]
    accepted = [r[key] for r in two if r[key] <= threshold]
    floor = min(r[key] for r in two)
    return {"wrongly_refused": refused, "wrongly_accepted": accepted,
            "zero_accept_threshold": floor,
            "passes_at_zero_accept": sum(1 for r in one if r[key] < floor)}


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
    print("Error directions at the best single threshold — the summary above cannot choose")
    print("one, because a wrong refusal costs a retry and a wrong acceptance cannot be undone.")
    print()
    for key in CANDIDATES:
        a = [r[key] for r in one if key in r]
        b = [r[key] for r in two if key in r]
        if not a or not b:
            continue
        t = min(((sum(1 for x in a if x > c) + sum(1 for x in b if x <= c)), c)
                for c in sorted(set(a + b)))[1]
        d = by_direction(one, two, key, t)
        print(f"  {key:<15} t={t:<7.3f} refused {len(d['wrongly_refused'])}/{len(a)} "
              f"| ACCEPTED {len(d['wrongly_accepted'])}/{len(b)}")
        print(f"  {'':<15} a threshold of {d['zero_accept_threshold']:.3f} accepts NO "
              f"two-voice window and passes {d['passes_at_zero_accept']}/{len(a)} one-voice")
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
