"""retag_request.py — the S3 artifact that carries a re-tag batch across the
VPC wall.

THE WALL IS NOT A PREFERENCE. In-VPC functions reach Aurora and nothing else:
the VPC has an S3 gateway endpoint and no NAT (BUG-36). Non-VPC functions reach
the model and not the database. Re-tagging existing data needs both, so it is
three hops, and the shape is the one the programme matcher already uses rather
than a new one invented here:

    org-api        in-VPC   picks what to re-tag, opens the run, writes this
    RetagFunction  non-VPC  S3-triggered on this prefix; classifies; invokes ->
    item-writer    in-VPC   resolves slugs, applies tags, closes the run

WHAT TRAVELS, AND WHAT DOES NOT. The request carries a topic's id, title and
summary -- what the model has to read to label it. It does NOT carry the action
items, the evidence, the participants or the transcript. An artifact that
carries a session's words a second time is a second copy to keep in step with
the first, and this one crosses a trust boundary on the way.

The ANSWER, going the other way, carries slugs and an id. Nothing else. That is
the owner's boundary made structural: re-tagging cannot alter a topic's body
because no hop in this chain has anywhere to put one.

Mirrors match_request.emit's put_object idiom deliberately, including the
deterministic key: re-driving the same batch overwrites its artifact instead of
piling up duplicates, which is the source-key idempotency every other writer
here already relies on. A uuid or a timestamp in the key would defeat it.
"""
import json

PREFIX = "retag_requests/"


def key_for(run_id, batch):
    return f"{PREFIX}{run_id}/{int(batch):04d}.json"


def emit(s3, bucket, run_id, company_id, topics, batch=0):
    """Write one batch of a re-tag run. Returns the key, or None when there is
    nothing to write -- an empty batch makes no S3 call, so "wrote nothing" and
    "was never asked" stay distinguishable at the call site rather than
    becoming an artifact with an empty list in it."""
    if not topics:
        return None
    key = key_for(run_id, batch)
    s3.put_object(
        Bucket=bucket,
        Key=key,
        Body=json.dumps({
            "run_id": str(run_id),
            "company_id": str(company_id),
            "batch": int(batch),
            # id / title / summary and nothing else -- see the module docstring.
            "topics": [{"id": str(t["id"]), "title": t.get("title") or "",
                        "summary": t.get("summary") or ""}
                       for t in topics],
        }),
        ContentType="application/json",
    )
    return key
