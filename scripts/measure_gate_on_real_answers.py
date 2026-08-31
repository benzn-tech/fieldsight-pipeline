"""What would actually have left the account, on real recordings.

Spec: docs/superpowers/specs/2026-08-31-ask-external-corroboration-design.md §4.1

Every test of the privacy gate so far used sentences someone wrote for the test.
§4.1 says plainly that the gate's input is model-assigned and that this is the
residual risk -- and a residual risk measured only against invented sentences has
not been measured. This runs step 1 and step 2 against the language the product
actually produces, and prints the exact strings that would have been sent to a
search engine.

## It cannot call out, and that is enforced rather than promised

The search step is replaced with a function that raises. A script that merely
"doesn't call the search step" is one edit away from calling it; this one cannot,
and the run dies loudly if anything tries. That matters because this reads real
customer content, and the whole question being asked is what would escape.

## What it prints, and what it does not

It prints **every entity the gate allowed**, verbatim, because those are exactly
the strings that would reach a third party and there is no way to judge the
decision without seeing them. It prints refusals as a count per reason. It never
prints the source sentence, the topic summary, or any transcript text -- if a
refused string needed its context printed to be understood, the gate refusing it
is doing its job and the context is the thing being protected.

Read-only. Nothing is written to S3 or the database.

Usage:
    AWS_PROFILE=... ANTHROPIC_API_KEY=... \\
        python scripts/measure_gate_on_real_answers.py [--limit N]
"""
import argparse
import collections
import json
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(
    os.path.abspath(__file__))), "src"))

os.environ.setdefault("ENABLE_EXTERNAL_CORROBORATION", "true")

import boto3  # noqa: E402

import corroboration  # noqa: E402
import corroboration_gate as gate  # noqa: E402

BUCKET = os.environ.get("S3_BUCKET", "fieldsight-data-509194952652")
PREFIX = "extractions/"


class SearchWasCalled(RuntimeError):
    pass


def _never(*a, **kw):
    raise SearchWasCalled(
        "the search step ran during a measurement that must not leave the account")


def answer_like(artifact):
    """The prose an Ask answer is built out of, in the register Ask answers use.

    Topic summaries, decisions and actions -- the sentences the model writes about
    the day. Not the transcript: Ask answers from these, and the gate sees what
    the answer says.
    """
    parts = []
    for topic in artifact.get("topics") or []:
        if not isinstance(topic, dict):
            continue
        if topic.get("summary"):
            parts.append(str(topic["summary"]))
        for key in ("decisions", "findings", "action_items"):
            for item in topic.get(key) or []:
                if isinstance(item, str):
                    parts.append(item)
                elif isinstance(item, dict):
                    for f in ("text", "action", "decision", "summary", "detail"):
                        if item.get(f):
                            parts.append(str(item[f]))
                            break
    return "\n".join(parts)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--limit", type=int, default=40)
    args = ap.parse_args()

    if not os.environ.get("ANTHROPIC_API_KEY"):
        sys.exit("ANTHROPIC_API_KEY is not set")

    # Enforced, not promised.
    corroboration._search = _never

    s3 = boto3.client("s3")
    keys = []
    token = None
    while True:
        kw = {"Bucket": BUCKET, "Prefix": PREFIX}
        if token:
            kw["ContinuationToken"] = token
        page = s3.list_objects_v2(**kw)
        keys += [o["Key"] for o in page.get("Contents", [])
                 if o["Key"].endswith(".json")]
        token = page.get("NextContinuationToken")
        if not token:
            break
    keys = sorted(keys)[-args.limit:]
    print(f"{len(keys)} artifacts\n")

    allowed_rows = []
    reasons = collections.Counter()
    per_kind = collections.Counter()
    empty_answers = 0
    no_entities = 0
    truncated = 0

    for key in keys:
        body = s3.get_object(Bucket=BUCKET, Key=key)["Body"].read()
        artifact = json.loads(body)
        answer = answer_like(artifact)
        if not answer.strip():
            empty_answers += 1
            continue

        entities, err = corroboration._extract(
            "what happened on site", answer, corroboration.EXTRACT_BUDGET)
        if err:
            print(f"  extraction failed on {key}: {err}")
            continue
        if not entities:
            no_entities += 1
            continue

        result = gate.screen(entities, max_entities=gate.MAX_ENTITIES)
        if result.truncated:
            truncated += 1
        for r in result.rejected:
            reasons[r.reason] += 1
        for a in result.allowed:
            per_kind[a["kind"]] += 1
            allowed_rows.append((key.split("/")[1], a["kind"], a["entity"]))

    print("=" * 74)
    print(f"artifacts with no prose            {empty_answers}")
    print(f"artifacts where nothing external   {no_entities}")
    print(f"artifacts where the cap bit        {truncated}")
    print()
    print(f"WOULD HAVE LEFT THE ACCOUNT: {len(allowed_rows)} strings, "
          f"{len(set(r[2] for r in allowed_rows))} distinct")
    for kind, n in per_kind.most_common():
        print(f"   {kind:14} {n}")
    print()
    for folder, kind, entity in sorted(set(allowed_rows)):
        print(f"   [{kind:11}] {entity}")

    print()
    print(f"REFUSED BY THE GATE: {sum(reasons.values())}")
    for reason, n in reasons.most_common():
        print(f"   {n:4}  {reason}")


if __name__ == "__main__":
    main()
