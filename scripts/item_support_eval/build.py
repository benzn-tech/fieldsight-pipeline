"""Build the item_support session set from TEST finals (pre-registration §2). Read-only.

For every `extractions/{folder}/{date}/{base}.json` in the TEST bucket with tier `final`, a
date folder on or after --since, and at least one verified/verified_fuzzy evidence entry, the
session's turns are rebuilt with the extractor's own `gather_session_segments` +
`assemble_session_turns`, and one file is written to data/sessions/{ref}.json. The split
(seeded sha256, first half = fit) is fixed once the whole set is known and recorded in
data/manifest.json.

    python -m scripts.item_support_eval.build            # --since defaults to 2026-08-01 (A12)

Never writes to S3 or a database. boto3 only (the aws CLI misbehaves inside shell read loops
on Windows).
"""
from __future__ import annotations

import argparse
import copy
import os
from pathlib import Path

import item_support

from scripts.item_support_eval import store

DEFAULT_PROFILE = "fieldsight-deployer"
DEFAULT_REGION = "ap-southeast-2"
TEST_BUCKET = "fieldsight-data-test-509194952652"
DEFAULT_SINCE = "2026-08-01"   # pre-registration A12
_EVIDENCE_OK = ("verified", "verified_fuzzy")


def parse_extraction_key(key, prefix="extractions/"):
    """(folder, date, base) or None."""
    if not key.startswith(prefix) or not key.endswith(".json"):
        return None
    parts = key[len(prefix):-len(".json")].split("/")
    if len(parts) != 3 or not all(parts):
        return None
    return tuple(parts)


def eligible(extraction, date, since=DEFAULT_SINCE):
    """None if eligible, else the reason (a counter name)."""
    if date < since:
        return "before_since"
    if not isinstance(extraction, dict) or extraction.get("tier") != "final":
        return "not_final"
    for topic in extraction.get("topics") or []:
        if not isinstance(topic, dict):
            continue
        for ev in topic.get("evidence") or []:
            if isinstance(ev, dict) and ev.get("status") in _EVIDENCE_OK:
                return None
    return "no_evidence"


def quote_resolution(topics, turns):
    """How many verified quotes still resolve to a rebuilt turn. Turns are rebuilt from today's
    transcripts (and batch maps), which can differ from what the final pass saw; a quote that no
    longer resolves silently shrinks the quote path."""
    verified = resolved = 0
    for topic in topics or []:
        if not isinstance(topic, dict):
            continue
        for ev in topic.get("evidence") or []:
            if isinstance(ev, dict) and ev.get("status") in _EVIDENCE_OK:
                verified += 1
                resolved += bool(item_support._evidence_turns([ev], turns))
    return {"verified_quotes": verified, "resolved_quotes": resolved}


def session_record(folder, date, base, extraction, turns, split):
    key = store.session_key(folder, date, base)
    return {"ref": store.session_ref(key), "folder": folder, "date": date, "base": base,
            "split": split, "topics": extraction.get("topics") or [],
            "turns": [store.encode_turn(t) for t in turns]}


def build(*, since=DEFAULT_SINCE, limit=None, out_dir=store.DATA_DIR,
          profile=DEFAULT_PROFILE, region=DEFAULT_REGION):
    import json

    import boto3

    # lambda_extract_session reads through its own lazily built client with the ambient
    # credential chain; setdefault so an exported profile wins.
    os.environ.setdefault("AWS_PROFILE", profile)
    os.environ.setdefault("AWS_DEFAULT_REGION", region)
    import lambda_extract_session as les

    out_dir = store.require_eval_dir(out_dir)
    s3c = boto3.Session(profile_name=profile, region_name=region).client("s3")
    skipped = {"before_since": 0, "not_final": 0, "no_evidence": 0, "no_turns": 0,
               "unreadable": 0}
    found = []
    for page in s3c.get_paginator("list_objects_v2").paginate(
            Bucket=TEST_BUCKET, Prefix=les.EXTRACTIONS_PREFIX):
        for obj in page.get("Contents", []):
            parsed = parse_extraction_key(obj["Key"], les.EXTRACTIONS_PREFIX)
            if parsed is None:
                continue
            folder, date, base = parsed
            if date < since:
                skipped["before_since"] += 1
                continue
            try:
                body = s3c.get_object(Bucket=TEST_BUCKET, Key=obj["Key"])["Body"].read()
                extraction = json.loads(body.decode("utf-8"))
            except Exception:
                skipped["unreadable"] += 1
                continue
            reason = eligible(extraction, date, since)
            if reason:
                skipped[reason] += 1
                continue
            found.append((folder, date, base, extraction))
    found.sort(key=lambda f: store.session_key(*f[:3]))
    n_eligible = len(found)
    if limit:
        found = found[:limit]

    kept = []
    for folder, date, base, extraction in found:
        keys = les.gather_session_segments(TEST_BUCKET, folder, date, base)
        turns, _files, _ann = les.assemble_session_turns(TEST_BUCKET, keys)
        if not turns:
            skipped["no_turns"] += 1
            continue
        kept.append((folder, date, base, extraction, turns))

    splits = store.split_sessions([store.session_key(*k[:3]) for k in kept])
    manifest = []
    for folder, date, base, extraction, turns in kept:
        key = store.session_key(folder, date, base)
        rec = session_record(folder, date, base, extraction, turns, splits[key])
        store.write_json(Path(out_dir, "sessions", f"{rec['ref']}.json"), rec)
        n_items = sum(1 for _ in store.iter_items(copy.deepcopy(rec["topics"])))
        manifest.append({"ref": rec["ref"], "folder": folder, "date": date, "base": base,
                         "split": splits[key], "n_items": n_items, "n_turns": len(turns),
                         **quote_resolution(extraction.get("topics"), turns)})
    manifest.sort(key=lambda m: m["ref"])
    store.write_json(Path(out_dir, "manifest.json"),
                     {"seed": store.SPLIT_SEED, "since": since, "bucket": TEST_BUCKET,
                      "limit": limit, "eligible": n_eligible, "sessions": manifest, "skipped": skipped})
    return {"eligible": n_eligible, "sessions": len(manifest),
            "fit": sum(1 for m in manifest if m["split"] == "fit"),
            "heldout": sum(1 for m in manifest if m["split"] == "heldout"),
            "items": sum(m["n_items"] for m in manifest),
            "verified_quotes": sum(m["verified_quotes"] for m in manifest),
            "resolved_quotes": sum(m["resolved_quotes"] for m in manifest), "skipped": skipped}


def main(argv=None):
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--since", default=DEFAULT_SINCE)
    p.add_argument("--limit", type=int, default=None, help="Smoke test only: first N sessions.")
    p.add_argument("--out", default=str(store.DATA_DIR))
    p.add_argument("--profile", default=DEFAULT_PROFILE)
    p.add_argument("--region", default=DEFAULT_REGION)
    a = p.parse_args(argv)
    counts = build(since=a.since, limit=a.limit, out_dir=a.out, profile=a.profile,
                   region=a.region)
    print(store.dumps(counts))
    return counts


if __name__ == "__main__":
    main()
