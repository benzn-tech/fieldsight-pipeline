"""
Lambda: fieldsight-retag — the non-VPC hop of a re-tag run.

NOT IN VPC, and holding no database, deliberately. It needs the model, and an
in-VPC function cannot reach one: the VPC has an S3 gateway endpoint and no NAT
(BUG-36). It reads a `retag_requests/` artifact (written in-VPC by org-api over
the same S3 gateway endpoint), classifies the topics in it, and hands the
answer to the in-VPC writer by direct Lambda invoke -- the same non-VPC ->
in-VPC direction `lambda_programme_matcher` already uses to reach
`lambda_suggestion_writer`, and for the same reason.

WHAT IT SENDS BACK is a list of {topic_id, slugs}. There is no field for a
title, a summary or any other text, so a re-tag cannot alter what was written
however this function behaves. That is the owner's boundary made structural
rather than remembered.

TWO THINGS IT MUST NOT DO:

  * treat a call that returned nothing as a batch of abstentions. This endpoint
    answers 200 with empty content, and half a real corpus genuinely takes no
    tag -- so "the model abstained" and "the call failed" produce the same
    shape and only the stats tell them apart. An unanswered batch RAISES, so
    the S3 event retries it; writing it would mark a broken run as finished.
  * read an invoke's 200 as "written". `invoke` returns 200 with
    `FunctionError` set when the function raised. Fail closed and let the S3
    event retry -- the writer's ON CONFLICT DO NOTHING makes the retry
    idempotent.

Entry point (S3 event):
  {"Records": [{"s3": {"object": {"key": "retag_requests/{run}/{batch}.json"}}}]}

Environment Variables:
    S3_BUCKET           the data bucket the request artifact lives in
    TAG_WRITER_FUNCTION name of the in-VPC Lambda that applies the tags
"""
import json
import logging
import os
from urllib.parse import unquote_plus

import boto3

import llm_utils
import tagging
import taxonomy_base

logger = logging.getLogger()
logger.setLevel(logging.INFO)

S3_BUCKET = os.environ.get("S3_BUCKET", "")
TAG_WRITER_FUNCTION = os.environ.get("TAG_WRITER_FUNCTION", "")

_s3 = None
_lambda = None


def s3():
    global _s3
    if _s3 is None:
        _s3 = boto3.client("s3")
    return _s3


def lambda_client():
    global _lambda
    if _lambda is None:
        _lambda = boto3.client("lambda")
    return _lambda


def _invoke_writer(payload):
    """Hand the answer to the in-VPC writer, and believe it only if it says so.

    A crashed writer comes back as a 200 with `FunctionError` set. Reading the
    200 alone is how a run ends up marked done with nothing in it -- the same
    trap as reading an invoke's status without looking at what came back.
    """
    resp = lambda_client().invoke(
        FunctionName=TAG_WRITER_FUNCTION,
        InvocationType="RequestResponse",
        Payload=json.dumps(payload),
    )
    if resp.get("FunctionError"):
        raise RuntimeError(
            f"tag writer invoke failed: {resp.get('FunctionError')}")
    # RETURNS NOTHING, deliberately, and tests/unit/
    # test_lambda_invoke_results_are_decoded.py is why. `resp` is boto3's
    # ENVELOPE, not the writer's answer -- the answer is a byte stream under
    # resp["Payload"]. Handing the envelope back would let a caller ask for
    # "tags" or "run_id" and get None, with no error anywhere. Nothing here
    # needs the writer's return value: the raise above is the whole contract,
    # and the writer's own log line is where the counts are.


def lambda_handler(event, _context):
    written = 0
    for record in event.get("Records", []):
        key = unquote_plus(record["s3"]["object"]["key"])
        req = json.loads(s3().get_object(Bucket=S3_BUCKET, Key=key)["Body"].read())
        topics = req.get("topics") or []
        if not topics:
            logger.info("retag: %s carries no topics", key)
            continue

        labels, stats = tagging.classify_with_stats(
            topics, taxonomy_base.LEAVES, llm_utils.call_llm)

        if stats.get("unanswered"):
            # NOT written. An unanswered batch and a batch of abstentions have
            # the same shape, and only this number tells them apart -- so the
            # one thing that must never be persisted is an abstention nobody
            # actually made. Raising lets the S3 event retry the whole batch.
            # .get on every field: a KeyError raised while BUILDING an error
            # message replaces the real failure with a different one, and the
            # thing that actually went wrong never reaches the log.
            raise RuntimeError(
                f"retag: {stats.get('unanswered')} of {stats.get('batches')} "
                f"batch(es) in {key} went unanswered -- nothing written, "
                f"the event will retry")

        _invoke_writer({
            "op": "apply_tags",
            "run_id": req["run_id"],
            "company_id": req["company_id"],
            # Every topic asked about is reported, including the ones with no
            # label: absent would mean "we did not get to it" and the run could
            # never tell the two apart.
            "tagged": [{"topic_id": t["id"], "slugs": slugs}
                       for t, slugs in zip(topics, labels)],
        })
        written += len(topics)
        logger.info("retag: %s -> %d topic(s) handed to the writer, %d tagged",
                    key, len(topics), stats.get("tagged", 0))
    return {"topics": written}
