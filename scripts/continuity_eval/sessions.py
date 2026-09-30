"""Session-set builder for the continuity measurement harness (spec §7 "Sessions").

Read-only: every function here either does S3 `list_objects_v2` reads or is pure.
Nothing in this module writes to S3 or any database. Candidate sessions come from
the `extractions/` prefix -- a session with no published extraction yet has nothing
for shape (a)'s "live at 95%, then final" to compare against, and PROD's
hard-negative pairs and `counts.json` both need a real published extraction to read
-- filtered to sessions with `gather_session_segments` returning >= 3 transcript
segments (spec §7 "Sessions": "TEST: s3://.../extractions/** with >= 3 transcript
segments").
"""
from __future__ import annotations

import math

import boto3

import lambda_extract_session as les

DEFAULT_PROFILE = "fieldsight-deployer"
DEFAULT_REGION = "ap-southeast-2"

# Both buckets live in AWS account 509194952652, region ap-southeast-2, per
# continuity-plan-facts.md §9. PROD access here is read-only GETs/Lists only --
# no prod database access, no prod lambda triggered (spec §7 "Prod sessions").
BUCKETS = {
    "test": "fieldsight-data-test-509194952652",
    "prod": "fieldsight-data-509194952652",
}

# Spec §7 "Sessions": candidate sessions need >= 3 transcript segments.
MIN_SEGMENTS = 3


def s3_client(profile=DEFAULT_PROFILE, region=DEFAULT_REGION):
    return boto3.Session(profile_name=profile, region_name=region).client("s3")


def _session_from_extraction_key(key):
    """`extractions/{user_folder}/{date}/{session_base}.json` -> (user_folder, date,
    session_base), or None for anything else under the prefix."""
    prefix = les.EXTRACTIONS_PREFIX
    if not key.startswith(prefix) or not key.endswith(".json"):
        return None
    parts = key[len(prefix):-len(".json")].split("/")
    if len(parts) != 3 or not all(parts):
        return None
    return tuple(parts)


def list_candidate_sessions(env, *, client=None, min_segments=MIN_SEGMENTS, gather=None):
    """Every published session under `env`'s `extractions/` prefix whose OWN
    transcript segments (via `gather_session_segments`, the same grouping
    `extract_session` itself uses) number at least `min_segments`.

    `client` and `gather` are injectable so this can be exercised without a real AWS
    call; both default to real S3 access (`gather` defaults to
    `lambda_extract_session.gather_session_segments`, which reads through the module's
    own lazily-built client, not `client` -- see run.py's AWS_PROFILE note)."""
    bucket = BUCKETS[env]
    s3c = client or s3_client()
    gather_fn = gather or les.gather_session_segments
    seen = set()
    out = []
    paginator = s3c.get_paginator("list_objects_v2")
    for page in paginator.paginate(Bucket=bucket, Prefix=les.EXTRACTIONS_PREFIX):
        for obj in page.get("Contents", []):
            parsed = _session_from_extraction_key(obj["Key"])
            if parsed is None or parsed in seen:
                continue
            seen.add(parsed)
            user_folder, date, session_base = parsed
            n = len(gather_fn(bucket, user_folder, date, session_base))
            if n >= min_segments:
                out.append({"env": env, "user_folder": user_folder, "date": date,
                            "session_base": session_base, "n_segments": n})
    return sorted(out, key=lambda s: (s["user_folder"], s["date"], s["session_base"]))


def prefix_segments(keys, frac):
    """The first `ceil(frac * n)` of `keys`. `keys` is expected in time order --
    `gather_session_segments` already returns its keys `sorted()`, and its own
    docstring is explicit that S3-key sort order is chronological for this
    naming scheme, so no separate time-sort happens here."""
    n = len(keys)
    k = min(n, max(0, math.ceil(frac * n)))
    return keys[:k]


def shapes():
    """Spec §7 "Shapes". Each value is a chain of `(prior_frac, frac)` steps.

    A chain's actual sequence of extraction prefixes is
    `[steps[0][0]] + [frac for _, frac in steps]` (see `fraction_sequence`) -- e.g.
    shape 'b' extracts at 40%, 60%, 80%, 100% (four passes), matching the spec's "40%
    -> 60% -> 80% -> 100%", but only the three LATER passes (each fed the previous
    pass's published items as a continuity prior) are "pass steps" for the spec's
    labelling-volume count ("4 pass steps (1 in shape a, 3 in shape b)"): a chain's
    first pass never has a prior to offer, by construction, so nothing about it can
    be claimed or labelled for continuity.

    (c) is the spec's optional stress case (a 60% prefix straight to final).
    """
    return {
        "a": [(0.95, 1.0)],
        "b": [(0.4, 0.6), (0.6, 0.8), (0.8, 1.0)],
        "c": [(0.6, 1.0)],
    }


def fraction_sequence(shape_steps):
    """The ordered list of prefixes actually extracted for one chain (see `shapes`)."""
    return [shape_steps[0][0]] + [frac for _prev, frac in shape_steps]
