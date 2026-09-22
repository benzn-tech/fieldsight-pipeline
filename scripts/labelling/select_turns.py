"""Pick the turns a human should listen to, so the owner only ever makes judgements.

Why this exists
---------------
`auto` mode for cross-company speaker matching is blocked on ONE thing: nobody has ever
listened to this audio. The 2026-08-30 cross-session measurement says so in its own words
("Nobody listened to this audio") and argues by contradiction instead, which cannot produce
an ROC. Without labels there is no ROC, and a threshold quoted off unlabelled data is a
threshold fitted to the material that produced it.

The scarce resource is the owner's time, not compute. So everything that can be decided
without ears is decided here, and what reaches a person is a list of clips and a question.

What it does NOT do
-------------------
It does not read audio, and by default it does not touch AWS at all. It consumes a
**manifest** -- the output of one `aws s3 ls`, see `--scan` -- and emits a **fetch list**.
Downloading the clips is a separate, explicit step, so the privileged half of this work is
one command the owner runs rather than something buried inside a selection heuristic.

The M1 sampling rule (docs/superpowers/specs/2026-09-22-voiceprint-line-phase1-investigation.md)
-------------------------------------------------------------------------------------------
  >= 6 sessions, >= 3 distinct speakers-by-proxy, spanning >= 3 weeks, and at least one
  person recorded on two different devices.

The last clause is the one that makes this a cross-COMPANY proxy rather than a cross-session
one: moving employer changes the device, the site and the acoustic environment, and the
09-18 measurement varied none of them deliberately. A sample that cannot separate "different
session" from "different channel" cannot answer the question being asked of it.

Turns shorter than `MIN_TURN_S` are excluded because the system itself refuses to attribute
them (`voiceprint_utils.DEFAULT_MIN_TURN_S`); labelling audio the matcher will never see
would put effort into rows that cannot appear in any ROC.
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from collections import defaultdict
from datetime import date as _date

# Mirrors voiceprint_utils.DEFAULT_MIN_TURN_S. Imported by value rather than by import
# because this script must run with nothing but the standard library on the owner's machine.
MIN_TURN_S = 3.0

#: The M1 quotas. Deliberately module-level constants rather than CLI defaults buried in
#: argparse: they are the claim this sample makes about itself, and a run that silently
#: fell short of one should say which.
MIN_SESSIONS = 6
MIN_SPEAKERS = 3
MIN_SPAN_DAYS = 21
MIN_MULTI_DEVICE_PEOPLE = 1

#: How many turns per (session, speaker) reach a person. Enough that one mis-heard clip does
#: not decide a label, few enough that the whole pack stays inside a sitting.
TURNS_PER_VOICE = 4

_NAME_RE = re.compile(
    r"^(?P<device>[A-Za-z0-9]+)_(?P<date>\d{4}-\d{2}-\d{2})_(?P<time>\d{2}-\d{2}-\d{2})"
    r"(?:_(?P<sid>sid[0-9a-f]{32}))?")


def parse_key(key: str) -> dict | None:
    """`transcripts/{folder}/{date}/{device}_{date}_{time}_sid…_c0000_srcwav.json` → fields.

    Returns None rather than raising for anything that does not match. Legacy RealPTT
    recordings carry no `sid` and cannot be grouped into a session at all, so they are not
    candidates -- the same refusal `turn_name_overlay.session_base` makes, for the same
    reason: a wrong session key reads one meeting's turns as another's.
    """
    parts = key.split("/")
    if len(parts) < 4 or parts[0] != "transcripts":
        return None
    m = _NAME_RE.match(parts[-1])
    if not m or not m.group("sid"):
        return None
    return {"key": key, "folder": parts[1], "date": parts[2],
            "device": m.group("device"), "session": m.group("sid")}


def _span_days(dates) -> int:
    ds = sorted({_date.fromisoformat(d) for d in dates})
    return (ds[-1] - ds[0]).days if len(ds) > 1 else 0


def shortfalls(selected) -> list[str]:
    """Which M1 quotas this sample fails, in the words of the rule it fails.

    Returned rather than raised, and reported rather than silently topped up. A pack that
    quietly fell one session short would still produce an ROC, and the number would look
    exactly like a good one -- this repository's own lesson about budgets computed from
    samples too small to show their own tail.
    """
    out = []
    sessions = {r["session"] for r in selected}
    folders = {r["folder"] for r in selected}
    by_folder = defaultdict(set)
    for r in selected:
        by_folder[r["folder"]].add(r["device"])
    multi = [f for f, ds in by_folder.items() if len(ds) > 1]

    if len(sessions) < MIN_SESSIONS:
        out.append(f"{len(sessions)} sessions, need {MIN_SESSIONS}")
    if len(folders) < MIN_SPEAKERS:
        out.append(f"{len(folders)} people, need {MIN_SPEAKERS}")
    span = _span_days([r["date"] for r in selected])
    if span < MIN_SPAN_DAYS:
        out.append(f"spans {span} days, need {MIN_SPAN_DAYS}")
    if len(multi) < MIN_MULTI_DEVICE_PEOPLE:
        out.append(
            f"{len(multi)} people recorded on two devices, need {MIN_MULTI_DEVICE_PEOPLE} -- "
            f"without one, this measures cross-SESSION and not cross-CHANNEL, and the "
            f"cross-company question stays unanswered")
    return out


def select(rows, per_voice: int = TURNS_PER_VOICE) -> list[dict]:
    """The clips to fetch, and nothing else.

    Ordering is deterministic (date, session, device, key) so two runs over one manifest
    produce the same pack -- a labelling pack whose contents shift between runs cannot be
    compared with the run before it.

    Sessions are taken oldest-first and DEDUPED BY SESSION, so one meeting spread over
    fourteen chunk files contributes one entry rather than fourteen. An early version
    counted files and reported "23 sessions" for four meetings; the quota checks above then
    passed on a sample that could not support them.
    """
    seen = {}
    for r in sorted(rows, key=lambda r: (r["date"], r["session"], r["device"], r["key"])):
        seen.setdefault((r["session"], r["device"]), r)
    out = []
    for r in seen.values():
        out.append({**r, "turns_wanted": per_voice, "min_turn_s": MIN_TURN_S})
    return out


def _scan(bucket: str, prefix: str) -> list[dict]:
    """List transcript keys from S3. The ONE privileged step, and it is opt-in.

    A missing `s3:ListBucket` grant returns **403, not 404** -- recorded eight times in this
    repository, once costing a whole day's data -- so an error here is surfaced loudly rather
    than folded into "no candidates found", which is what an empty list would read as.
    """
    import boto3  # imported here so the default path needs no AWS SDK at all
    s3 = boto3.client("s3")
    out = []
    token = None
    while True:
        kw = {"Bucket": bucket, "Prefix": prefix}
        if token:
            kw["ContinuationToken"] = token
        resp = s3.list_objects_v2(**kw)
        for obj in resp.get("Contents", []):
            row = parse_key(obj["Key"])
            if row:
                out.append(row)
        if not resp.get("IsTruncated"):
            break
        token = resp.get("NextContinuationToken")
    return out


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--manifest", help="JSON list of transcript keys (default: stdin)")
    ap.add_argument("--scan", nargs=2, metavar=("BUCKET", "PREFIX"),
                    help="list keys from S3 instead of reading a manifest (needs AWS)")
    ap.add_argument("--out", help="write the fetch list here (default: stdout)")
    ap.add_argument("--per-voice", type=int, default=TURNS_PER_VOICE)
    args = ap.parse_args(argv)

    if args.scan:
        rows = _scan(args.scan[0], args.scan[1])
    else:
        raw = (open(args.manifest, encoding="utf-8").read() if args.manifest
               else sys.stdin.read())
        keys = json.loads(raw)
        rows = [r for r in (parse_key(k) for k in keys) if r]

    if not rows:
        print("no candidate transcripts found. If this ran with --scan, check the grant "
              "first: a missing s3:ListBucket returns 403, not 404, and an empty result "
              "is what that looks like from here.", file=sys.stderr)
        return 2

    selected = select(rows, per_voice=args.per_voice)
    missing = shortfalls(selected)

    payload = {
        "clips": selected,
        "quotas": {"sessions": MIN_SESSIONS, "people": MIN_SPEAKERS,
                   "span_days": MIN_SPAN_DAYS,
                   "multi_device_people": MIN_MULTI_DEVICE_PEOPLE},
        "shortfalls": missing,
    }
    text = json.dumps(payload, indent=2)
    if args.out:
        open(args.out, "w", encoding="utf-8").write(text)
    else:
        print(text)

    for m in missing:
        print(f"SHORTFALL: {m}", file=sys.stderr)
    # A shortfall is not a crash: the pack is still usable and the operator may know
    # something this script does not. It is an exit code so a script driving this cannot
    # miss it, and a sentence so a person cannot.
    return 1 if missing else 0


if __name__ == "__main__":
    raise SystemExit(main())
