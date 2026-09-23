"""Score a session's turns against the stored voiceprints, outside the pipeline.

## Why outside

Running this through the product triggers the whole chain: a `.wav` under `users/` starts
VAD, VAD's output under `audio_segments/` starts transcription, and a `.json` under
`transcripts/` starts the extraction LLM. Copying a session between buckets to test speaker
matching therefore pays for a re-transcription and a re-extraction of material that was
already transcribed and extracted — and changes the very rows the test is reading.

This reads prod directly and writes nothing there. No copy, no trigger, no vendor cost.

## Why it is also the better measurement

**The pipeline discards the scores.** `decide_name` returns `Decision(status, name, margin,
reason)` — there is no score field — so `_match` computes a cosine against every candidate,
ranks them, and throws the numbers away. `speaker_turn_names.score` is therefore NULL on
every row ever written (measured 2026-09-23: 604 rows, 0 scores), which in turn means
`recompute_company_floor` can never reach its minimum sample count and
`speaker_voiceprint_company_floors` has stayed empty since the table was created.

So the one thing needed to calibrate a rejection floor is the one thing production does not
keep. This script keeps it.

## Fidelity

Embedding goes through `lambda_speaker_embed.embed_audio` — the deployed function itself,
including its 45-second piecewise-average path — not a reimplementation. A number produced
by a different embedding path is not comparable to a production score, and the 2026-08-30
measurement was careful about exactly this.

Turns come from `transcript_utils`, the same builder the viewer and matcher use, so a scored
turn is the unit `decide_name` is judged on.

    python scripts/labelling/score_session.py --session sid15770a28... \\
        --folder Ben_UCPK2 --date 2026-08-13 [--upload]
"""
from __future__ import annotations

import argparse
import collections
import json
import os
import subprocess
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.join(ROOT, "src"))

PROD_BUCKET = "fieldsight-data-509194952652"
CLUSTER = ("arn:aws:rds:ap-southeast-2:509194952652:cluster:"
           "fieldsight-db-test-dbcluster-hywiixu8ihi9")
DATASET_PREFIX = "voiceprint_eval"

#: Mirrors voiceprint_utils.DEFAULT_MIN_TURN_S. Below it the matcher refuses whatever the
#: score says, so scoring one would produce a row that can never appear in a real decision.
MIN_TURN_S = 3.0


def _sql(secret_arn, database, sql):
    out = subprocess.run(
        ["aws", "rds-data", "execute-statement", "--profile", "fieldsight-deployer",
         "--region", "ap-southeast-2", "--resource-arn", CLUSTER, "--secret-arn", secret_arn,
         "--database", database, "--sql", sql, "--output", "json"],
        capture_output=True, text=True)
    if out.returncode != 0:
        raise SystemExit(f"SQL failed: {out.stdout}{out.stderr}")
    return json.loads(out.stdout)["records"]


def load_profiles(secret_arn, database, company_like):
    """Every stored vector this company may match against, one row per SAMPLE.

    **The filters are not restated here; they are read out of the shipped function.**

    They were restated once, with a comment claiming they matched. Migration 0064 then added
    a third — quarantined samples must not match — and this copy did not get it, so a
    verification run reported the profile still holding all twelve vectors while production
    was already scoring against eight. The tool disagreed with the thing it was checking, and
    said so in a way that read like the change had failed.

    That is this repository's most-repeated defect and the one this file's own docstrings
    warn about, so the copy is gone: the WHERE clause is lifted from
    `repositories.voiceprints.profiles_for_matching` at run time. It cannot drift, and a
    change to the real query that this cannot parse fails loudly here rather than quietly
    scoring against the wrong set.
    """
    import re
    src = open(os.path.join(ROOT, "src", "repositories", "voiceprints.py"),
               encoding="utf-8").read()
    body = src[src.index("def profiles_for_matching"):]
    body = body[:body.index("\ndef ", 1)]
    joined = " ".join(re.findall(r'"([^"]*)"', body))
    m = re.search(r"(JOIN speaker_voiceprint_samples.*?)ORDER BY", joined, re.S)
    if not m:
        raise SystemExit(
            "could not lift the matching filters out of profiles_for_matching — its shape "
            "changed. Fix this rather than restating the filters: a stale copy scores "
            "against vectors production does not use, and the numbers look entirely normal.")
    where = m.group(1).replace("p.company_id = %s", f"p.company_id::text LIKE '{company_like}%'")
    rows = _sql(secret_arn, database,
                "SELECT p.id::text || '|' || coalesce(p.display_name,'?') || '|' || "
                "       s.embedding::text "
                "FROM speaker_voiceprints p " + where)
    out = []
    for r in rows:
        pid, name, vec = r[0]["stringValue"].split("|", 2)
        out.append({"person_key": pid, "display_name": name,
                    "embedding": [float(x) for x in vec.strip("[]").split(",")]})
    return out


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--session", required=True, help="sid<32 hex>")
    ap.add_argument("--folder", required=True)
    ap.add_argument("--date", required=True)
    ap.add_argument("--bucket", default=PROD_BUCKET, help="where the audio lives (read-only)")
    ap.add_argument("--database", default="fieldsight_test")
    ap.add_argument("--secret-arn", required=True)
    ap.add_argument("--company", default="7a495d8a", help="company id prefix")
    ap.add_argument("--max-turns", type=int, default=120)
    ap.add_argument("--upload", action="store_true",
                    help="write the scores to s3://{bucket}/%s/{date}-{session}/" % DATASET_PREFIX)
    args = ap.parse_args(argv)

    # The embedder reads its model from whatever S3_BUCKET says, so it is set before import.
    os.environ.setdefault("S3_BUCKET", args.bucket)
    os.environ.setdefault("AWS_DEFAULT_REGION", "ap-southeast-2")
    import boto3
    import transcript_utils
    import turn_name_overlay
    import voiceprint_utils as vp
    import lambda_speaker_embed as emb

    profiles = load_profiles(args.secret_arn, args.database, args.company)
    if not profiles:
        # Loudly. "Nobody was recognised" and "there was nobody to recognise" are the same
        # observation downstream, and this script exists to tell them apart.
        raise SystemExit("no consented, non-withdrawn profiles for that company — there is "
                         "nothing to score against")
    people = sorted({p["display_name"] for p in profiles})
    print(f"profiles: {len(profiles)} sample(s) across {people}")

    s3 = boto3.client("s3")
    keys = sorted(o["Key"] for o in s3.list_objects_v2(
        Bucket=args.bucket,
        Prefix=f"transcripts/{args.folder}/{args.date}/").get("Contents", [])
        if args.session in o["Key"])
    if not keys:
        raise SystemExit(f"no transcripts for {args.session} — check the folder and date "
                         f"(a missing s3:ListBucket grant returns 403, not 404, and looks "
                         f"exactly like this)")

    turns = []
    for k in keys:
        doc = json.loads(s3.get_object(Bucket=args.bucket, Key=k)["Body"].read())
        fn = k.split("/")[-1]
        for s in transcript_utils.speaker_turns_from_items(doc.get("results", {})):
            a, b = float(s["start_time"]), float(s["end_time"])
            if b - a < MIN_TURN_S:
                continue
            turns.append({"source_filename": fn, "start_sec": a, "end_sec": b,
                          "speaker_label": s.get("speaker_label"),
                          "text": (s.get("transcript") or "")[:90]})
    # Longest first: if the cap bites, it should keep the turns carrying the most evidence
    # rather than whichever happened to be transcribed first.
    turns.sort(key=lambda t: t["end_sec"] - t["start_sec"], reverse=True)
    turns = turns[: args.max_turns]
    print(f"turns >= {MIN_TURN_S}s: {len(turns)} (capped at {args.max_turns})")

    scored, failed = [], 0
    for i, t in enumerate(turns, 1):
        try:
            _key, clip, sr = emb._window_audio(args.folder, args.date,
                                               t["source_filename"],
                                               t["start_sec"], t["end_sec"])
            v = emb.embed_audio(clip, sr)
        except Exception as exc:
            failed += 1
            print(f"  [{i}/{len(turns)}] unreadable: {exc}", file=sys.stderr)
            continue
        rows = [{"person_key": p["person_key"], "score": vp.cosine(v, p["embedding"])}
                for p in profiles]
        by_person = vp.aggregate_scores(rows)          # one score per PERSON, mean over samples
        named = {}
        for p in profiles:
            named[p["display_name"]] = by_person.get(p["person_key"])
        d = vp.decide_name(by_person, duration_s=t["end_sec"] - t["start_sec"])
        best = max(named.items(), key=lambda kv: kv[1])
        scored.append({
            "turn_ref": turn_name_overlay.turn_ref(t["source_filename"], t["start_sec"]),
            "source_filename": t["source_filename"],
            "speaker_label": t["speaker_label"],
            "duration_s": round(t["end_sec"] - t["start_sec"], 2),
            "scores": {k: round(val, 4) for k, val in named.items()},
            "best": best[0], "best_score": round(best[1], 4),
            "margin": None if d.margin is None else round(d.margin, 4),
            "status": d.status, "reason": d.reason,
            "text": t["text"],
        })
        if i % 10 == 0:
            print(f"  scored {i}/{len(turns)}")

    if not scored:
        raise SystemExit("nothing could be scored")

    # Grouped by (file, label), NOT by label alone. Diarisation labels are scoped to one
    # transcript call, so `spk_0` in two files is two different people — grouping by the
    # bare label mixes them and produces a confident, wrong summary.
    groups = collections.defaultdict(list)
    for r in scored:
        groups[(r["source_filename"], r["speaker_label"])].append(r)

    print(f"\nscored {len(scored)} turns, {failed} unreadable\n")
    print(f"{'file/label':46s} {'n':>3s} " + " ".join(f"{p:>10s}" for p in people) + "   winner")
    summary = []
    for (fn, lab), rows in sorted(groups.items(), key=lambda kv: -len(kv[1])):
        means = {p: sum(r["scores"].get(p, 0.0) for r in rows) / len(rows) for p in people}
        win = max(means.items(), key=lambda kv: kv[1])
        # The gap to the RUNNER-UP, not the winner's score. An absolute score says nothing
        # on its own here — the distributions overlap — and a single enrolled person has no
        # runner-up at all, which is the case `decide_name` refuses outright.
        ordered = sorted(means.values(), reverse=True)
        gap = f"+{ordered[0] - ordered[1]:.3f}" if len(ordered) > 1 else "no runner-up"
        label = f"{fn[-38:]}/{lab}"
        print(f"{label:46s} {len(rows):3d} "
              + " ".join(f"{means[p]:10.3f}" for p in people)
              + f"   {win[0]} ({gap})")
        summary.append({"file": fn, "label": lab, "turns": len(rows),
                        "mean_scores": {p: round(means[p], 4) for p in people},
                        "winner": win[0]})

    payload = {"session": args.session, "folder": args.folder, "date": args.date,
               "bucket": args.bucket, "profiles": [
                   {"person_key": p["person_key"], "display_name": p["display_name"]}
                   for p in profiles],
               "min_turn_s": MIN_TURN_S, "turns": scored, "by_group": summary}
    out_name = f"scores-{args.date}-{args.session}.json"
    open(out_name, "w", encoding="utf-8").write(json.dumps(payload, indent=2))
    print(f"\nwrote {out_name}")

    if args.upload:
        # S3 is the system of record: these scores are what a threshold gets calibrated on,
        # and a number that exists only in a terminal cannot be re-read next month.
        key = f"{DATASET_PREFIX}/{args.date}-{args.session}/{out_name}"
        s3.put_object(Bucket=args.bucket, Key=key,
                      Body=json.dumps(payload, indent=2).encode("utf-8"),
                      ContentType="application/json")
        print(f"uploaded to s3://{args.bucket}/{key}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
