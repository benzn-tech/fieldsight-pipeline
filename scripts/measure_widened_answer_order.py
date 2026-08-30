#!/usr/bin/env python3
"""Does a widened answer lead with what it FOUND, or with what it did not?

A unit test can pin that the instruction is in the prompt. It cannot pin that
the model follows it -- and the first attempt at this proved the difference:
the instruction was present, the test was green, and both live answers still
opened with "there are no records for yesterday".

So the property is measured against the deployed model, over N runs, because
one run cannot tell an improvement from a coin flip. This repository has
already had a prompt change judged on n=1 and ranked backwards at n=27.

WHAT IS MEASURED. Only the first sentence, and only structurally: does it
assert an absence, or does it say something that happened? That is a binary a
regex can read honestly. It is NOT a judgement of answer quality, and this
script must not be extended into one -- the moment the criterion needs an
opinion, it needs a rubric and a second rater.

    AWS_PROFILE=fieldsight-deployer python scripts/measure_widened_answer_order.py \
        --sub <cognito-sub> --runs 6

Requires a caller_sub whose accessible sites have chunks, and a date range with
none -- "yesterday" on a corpus that stops months ago is exactly that, which is
the state TEST is in.
"""
import argparse
import json
import re
import sys

# An opening that asserts the period is empty. Deliberately narrow: it matches
# the shapes the model actually produced, not every sentence containing "no".
_ABSENCE = re.compile(
    r"\b(there (are|is) no|no records|no information|no data|is empty|"
    r"nothing (recorded|found|in)|does not contain|no relevant)\b"
    r"|没有(任何)?(记录|信息|数据)|无记录",
    re.IGNORECASE,
)


def first_sentence(text):
    """Up to the first terminator, or the first line, whichever comes first.

    Markdown answers open with a paragraph and then a bullet list, so a naive
    split on '.' would run past a heading into the content and score it as a
    pass. The line break is the tighter bound.
    """
    head = (text or "").strip().split("\n", 1)[0]
    m = re.search(r"[.!?。！？]", head)
    return head[: m.end()] if m else head


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--function", default="fieldsight-test-ask-agent")
    ap.add_argument("--region", default="ap-southeast-2")
    ap.add_argument("--sub", required=True, help="a Cognito sub with visible chunks")
    ap.add_argument("--tz", default="Pacific/Auckland")
    ap.add_argument("--runs", type=int, default=6)
    ap.add_argument("--question", default="What happened yesterday?")
    args = ap.parse_args()

    import boto3
    client = boto3.client("lambda", region_name=args.region)

    led_with_content = 0
    skipped = 0
    for i in range(args.runs):
        payload = {"question": args.question, "caller_sub": args.sub,
                   "k": 5, "tz": args.tz}
        raw = client.invoke(FunctionName=args.function, InvocationType="RequestResponse",
                            Payload=json.dumps(payload))["Payload"].read()
        body = json.loads(raw)
        body = json.loads(body["body"]) if "body" in body else body
        basis = body.get("basis") or {}

        if not basis.get("widened"):
            # Not the case under test. Counted and reported rather than quietly
            # dropped: a run of these means the corpus changed and the number
            # below is over a smaller n than it says.
            skipped += 1
            print(f"  {i + 1}. SKIP (not widened; basis={basis})")
            continue

        opener = first_sentence(body.get("answer"))
        leads_with_absence = bool(_ABSENCE.search(opener))
        led_with_content += (not leads_with_absence)
        print(f"  {i + 1}. {'absence' if leads_with_absence else 'CONTENT'}: {opener[:110]}")

    measured = args.runs - skipped
    print()
    if not measured:
        print("no widened runs -- nothing measured. Pick a question whose period is empty.")
        return 2
    print(f"led with content: {led_with_content}/{measured}"
          + (f"  ({skipped} runs skipped)" if skipped else ""))
    return 0 if led_with_content == measured else 1


if __name__ == "__main__":
    sys.exit(main())
