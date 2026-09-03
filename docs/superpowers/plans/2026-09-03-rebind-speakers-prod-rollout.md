# Speaker Re-bind — Production Rollout Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Turn on the already-built anonymous speaker re-bind in production, having first proved the deployed chain produces the right answer on one session whose correct answer is already known.

**Architecture:** Nothing needs building. `lambda_item_writer` enqueues `voiceprint_requests/{company_id}/{session_base}/rebind.json` on the final extraction pass; the S3 notification wakes the non-VPC `lambda_speaker_embed._rebind`, which embeds each `(source_filename, speaker_label)` pair with ECAPA and clusters them; the in-VPC `lambda_voiceprint_writer` op `rebind` replaces the session's rows in `speaker_label_groups`; `lambda_org_api` decorates each transcript segment with `speaker_group`, failing open. Every link is deployed on prod as of 2026-09-01. The only thing switched off is the producer's flag.

**Tech Stack:** Python 3.12 Lambda, ECAPA-TDNN ONNX (`models/ecapa_tdnn.onnx`, 192-d), Aurora PostgreSQL, S3 request artifacts, SAM + GitHub Actions.

**Spec:** `docs/superpowers/specs/2026-08-30-anonymous-speaker-rebind.md` (the feature's own spec — read it first), with `docs/superpowers/specs/2026-08-29-asr-accuracy-measured-findings.md` §2 for the measurement that justifies it and `docs/superpowers/specs/2026-08-09-speaker-identity-v2.md` for the guards.

## Before you start: branch from current `main`

This plan was written on `plan/rebind-speakers-prod-rollout`, cut from `origin/main` at
`449b998`. **Fetch and re-check `main` before doing anything.** The first draft of this
plan was a *build* plan, because the branch it was written on was 737 commits behind and
the entire feature already existed on `main`. Every file, symbol, line number and
deployment fact below was re-verified against `main` on 2026-09-03 — if `main` has moved,
re-verify rather than trusting these numbers.

```bash
git fetch origin
git rev-list --left-right --count HEAD...origin/main   # want 0 on the right
```

## Global Constraints

- **Anonymous only.** The output is letters within one session (`A`, `B`, `C`), never names. No vector is stored. That is what keeps it clear of the consent requirement the *named* library carries — do not let a task in this plan write a name or persist an embedding. (Do not repeat the older claim that consent is what emptied the named library: the spec's own correction of 2026-08-29 traces that to `lambda_voiceprint_writer` failing to import numpy since 2026-08-14 and to the guard being handed whole-chunk windows — PRs #595, #601–#603.)
- **Do not add a minimum-evidence guard.** Seeing a group merged on a single 3.6 s turn is the expected shape, not a defect: on the first real multi-speaker session *both* cross-call merges contained a one-turn member, and the standalone group was the one with the most evidence. A minimum-evidence rule would have refused both merges and left the session exactly as unusable as before (spec, "Measured after shipping"). The similarity floor is the guard that does the work; nothing in this plan loosens or supplements it.
- **Fails open, everywhere.** A session with no groups must read *exactly* as it reads today. Any new failure path logs and returns; none may take an item write, an extraction, or a transcript read down with it.
- **Prod deploys are approval-gated** (`production` GitHub environment). A human approves; the plan cannot self-approve.
- **`export MSYS_NO_PATHCONV=1`** before any AWS CLI call carrying a `/`-prefixed argument (`/aws/lambda/...`). Without it Git Bash rewrites the argument and the failure reads like a routing bug (CLAUDE.md BUG-42).
- **Never `git add -A`.** Stage explicit paths (CLAUDE.md, Windows CRLF section).
- **Test harness:** `export UV_LINK_MODE=copy` and the dummy AWS env before pytest, per the `fieldsight-backend` skill.
- **Known ground truth for the verification session** — `Ben_UCPK2` / `2026-08-27` / `sid93396a6ac8434fdf908c25a50cc7e167`: three people, confirmed by ear. `spk_0`=Benny, `spk_1`/`spk_2`/`spk_4`=Ben, `spk_3`=Isaac. Batched labels are 55.4 % pure; the re-bind measured 96.3 %.

---

## Current state — read this before starting

| link | where | state |
|---|---|---|
| producer | `lambda_item_writer._request_rebind` | built, gated on `REBIND_SPEAKERS` |
| S3 notification | prod bucket, `voiceprint_requests/` → `fieldsight-prod-speaker-embed` | **wired** (verified 2026-09-03) |
| embedder | `lambda_speaker_embed._rebind`, non-VPC, 1769 MB / 600 s, ECAPA layer | deployed 2026-09-01 |
| writer | `lambda_voiceprint_writer` op `rebind`, in-VPC → `speaker_label_groups` | deployed 2026-09-01 |
| reader | `lambda_org_api` sets `seg["speaker_group"]` | deployed |
| **TEST flag** | `TEST_REBIND_SPEAKERS` | **true** |
| **PROD flag** | `PROD_REBIND_SPEAKERS` | **false** ← the only thing off |

**Production has never executed this path — zero log lines, ever.** TEST has, six times on 2026-08-29: two single-group sessions (`4 pairs -> 1`, `3 pairs -> 1`, both genuinely one speaker), **two successful multi-speaker runs (`5 pairs -> 3 groups`)**, and the one large session it was handed — ours, 28 pairs across 14 calls — failed with `NoSuchKey`, because the TEST bucket holds neither the raw chunks nor the batch maps for it. **That failure was missing test data, not a code defect** (verified 2026-09-03: 0 matching objects in TEST, 58 in prod).

So the feature works, on a 5-pair session, on TEST. What has never happened is a run on
prod at all, or a run at this session's scale anywhere. Task 1 closes both gaps at once,
against the only session whose right answer a human has confirmed.

**Re-bind will not repair history.** `speaker_turns` is written into the extraction artifact only on the `final` pass and only since that field was added. The 2026-08-27 artifact is `tier: final` with `speaker_turns: []`, because it predates the field. **Only sessions finalized after the flag flip will be re-bound.** If historical sessions matter, that is a separate backfill, out of scope here.

---

### Task 1: Prove the deployed prod chain on the known-answer session

No product code changes. This builds the request artifact the producer would have built, drops it in prod, and checks the answer against three names a human confirmed.

**Files:**
- Create: `scripts/rebind_verify.py`
- Test: none — this task's test is the live run in Step 4

**Interfaces:**
- Consumes: nothing from earlier tasks
- Produces: `scripts/rebind_verify.py` exposing `build_turns(bucket, user_folder, date, session_base) -> list[dict]`, each dict `{"source_filename": str, "speaker_label": str, "start_sec": float, "end_sec": float}` — Task 2 imports this function

- [ ] **Step 1: Write the verification script**

```python
# scripts/rebind_verify.py
"""Ask the DEPLOYED re-bind chain for one session's speaker groups, and say whether
the answer matches what a human confirmed.

The producer (`lambda_item_writer._request_rebind`) only fires on a final extraction
pass, and only for sessions finalized after the field existed. This builds the same
request from the transcripts directly, so a session can be checked without waiting for
one to be recorded.

    export MSYS_NO_PATHCONV=1
    uv run --with boto3 python scripts/rebind_verify.py \
        --bucket fieldsight-data-509194952652 \
        --user-folder Ben_UCPK2 --date 2026-08-27 \
        --session-base sid93396a6ac8434fdf908c25a50cc7e167 \
        --company-id <uuid> --put

Without `--put` it prints the request and writes nothing — always run that first.
"""
import argparse
import json
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "src"))

import boto3  # noqa: E402
from transcript_utils import normalize_transcript  # noqa: E402


def build_turns(bucket, user_folder, date, session_base):
    """The `speaker_turns` payload item_writer would have sent, built from transcripts.

    Same shape and same vocabulary as `lambda_extract_session`'s artifact: the
    transcript's own `speaker_label`, and times relative to that transcript file — NOT
    absolute. `_window_audio` re-derives the audio coordinates itself.
    """
    s3 = boto3.client("s3")
    prefix = f"transcripts/{user_folder}/{date}/"
    turns = []
    paginator = s3.get_paginator("list_objects_v2")
    for page in paginator.paginate(Bucket=bucket, Prefix=prefix):
        for obj in page.get("Contents", []):
            key = obj["Key"]
            filename = key.rsplit("/", 1)[-1]
            if session_base not in filename or not filename.endswith(".json"):
                continue
            data = json.loads(s3.get_object(Bucket=bucket, Key=key)["Body"].read())
            normalized = normalize_transcript(data, filename)
            if not normalized:
                print(f"  skipped unnormalizable {filename}", file=sys.stderr)
                continue
            for t in normalized.get("speaker_turns") or []:
                if not t.get("speaker"):
                    continue
                turns.append({"source_filename": filename,
                              "speaker_label": t.get("speaker"),
                              "start_sec": t.get("start_sec"),
                              "end_sec": t.get("end_sec")})
    return turns


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("--bucket", required=True)
    ap.add_argument("--user-folder", required=True)
    ap.add_argument("--date", required=True)
    ap.add_argument("--session-base", required=True)
    ap.add_argument("--company-id", required=True)
    ap.add_argument("--put", action="store_true",
                    help="actually write the request; omit for a dry run")
    a = ap.parse_args(argv)

    turns = build_turns(a.bucket, a.user_folder, a.date, a.session_base)
    pairs = {(t["source_filename"], t["speaker_label"]) for t in turns}
    calls = {src for src, _ in pairs}
    print(f"{len(turns)} turns, {len(pairs)} (call,label) pairs across {len(calls)} calls")
    if len(pairs) < 2 or len(calls) < 2:
        print("the producer would skip this session — nothing to group")
        return 1

    req = {"op": "rebind", "company_id": a.company_id,
           "session_base": a.session_base, "user_folder": a.user_folder,
           "date": a.date, "turns": turns}
    key = f"voiceprint_requests/{a.company_id}/{a.session_base}/rebind.json"
    if not a.put:
        print(f"DRY RUN — would put s3://{a.bucket}/{key}")
        return 0
    boto3.client("s3").put_object(Bucket=a.bucket, Key=key,
                                  Body=json.dumps(req).encode("utf-8"),
                                  ContentType="application/json")
    print(f"put s3://{a.bucket}/{key}")
    print("now: aws logs tail /aws/lambda/fieldsight-prod-speaker-embed --since 5m --follow")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
```

- [ ] **Step 2: Find the company_id for `Ben_UCPK2`**

```bash
export MSYS_NO_PATHCONV=1
CL=arn:aws:rds:ap-southeast-2:509194952652:cluster:fieldsight-db-test-dbcluster-hywiixu8ihi9
SEC=$(aws secretsmanager list-secrets --query \
  "SecretList[?contains(Name,'rds!cluster')].ARN" --output text | head -1)
aws rds-data execute-statement --resource-arn "$CL" --secret-arn "$SEC" \
  --database fieldsight --sql \
  "SELECT DISTINCT c.id, c.name FROM companies c
     JOIN users u ON u.company_id = c.id
    WHERE u.folder_name = 'Ben_UCPK2'"
```

Expected: exactly one row. If it returns more than one, stop — the request key is
company-scoped and picking the wrong one files the groups under the wrong tenant.

- [ ] **Step 3: Dry run**

```bash
export MSYS_NO_PATHCONV=1 UV_LINK_MODE=copy
uv run --with boto3 python scripts/rebind_verify.py \
  --bucket fieldsight-data-509194952652 --user-folder Ben_UCPK2 \
  --date 2026-08-27 --session-base sid93396a6ac8434fdf908c25a50cc7e167 \
  --company-id "$COMPANY_ID"
```

Expected: `28 (call,label) pairs across 14 calls` and `DRY RUN`. **If the pair count is
not 28 across 14 calls, stop and find out why** — that number is what the measurement in
the spec was computed on, and a different one means this is not the same input.

- [ ] **Step 4: Fire it, and watch**

```bash
uv run --with boto3 python scripts/rebind_verify.py \
  --bucket fieldsight-data-509194952652 --user-folder Ben_UCPK2 \
  --date 2026-08-27 --session-base sid93396a6ac8434fdf908c25a50cc7e167 \
  --company-id "$COMPANY_ID" --put
aws logs tail /aws/lambda/fieldsight-prod-speaker-embed --since 10m --follow
```

Expected in the log, within ~2 minutes:

```
rebind: 28 (call,label) pairs -> 3 groups at similarity 0.45
rebind stored: 28
```

**Three groups is the pass condition.** One group means it under-split; five or more
means it over-split. Either way, do not proceed to Task 4 — capture the log and the
`speaker_label_groups` rows and treat it as a finding, because the same code produced
three groups on this exact audio offline.

Also expected: **zero** `could not read` lines. Those were the TEST-bucket failures and
must not appear against prod, where all 58 chunks and all 13 batch maps exist.

- [ ] **Step 5: Check the rows landed and match the confirmed people**

```bash
aws rds-data execute-statement --resource-arn "$CL" --secret-arn "$SEC" \
  --database fieldsight --sql \
  "SELECT speaker_group, count(*), array_agg(DISTINCT speaker_label)
     FROM speaker_label_groups
    WHERE session_base = 'sid93396a6ac8434fdf908c25a50cc7e167'
    GROUP BY speaker_group ORDER BY 2 DESC"
```

Expected: three groups. Cross-check against the spec's measured mapping — the largest
group should collect calls 1/2/3/5/10's `spk_1` together with calls 4/6/7/8/9/11/12/13/14's
`spk_0` (that is Ben), and the second-largest the mirror image (Benny). A third small
group of two pairs from calls 13/14 is Isaac.

- [ ] **Step 6: Commit the script**

```bash
git add scripts/rebind_verify.py
git commit -m "tool(rebind): ask the deployed chain for one session's groups"
```

---

### Task 2: Pin the measured expectation as a test

The spec records 28 pairs → 3 groups. Right now that lives only in prose, and the code
that produces it has no test asserting the shape of a multi-call session.

**Files:**
- Create: `tests/unit/test_rebind_request_shape.py`
- Test: itself

**Interfaces:**
- Consumes: `build_turns` from `scripts/rebind_verify.py` (Task 1)
- Produces: nothing later tasks depend on

- [ ] **Step 1: Write the failing test**

```python
# tests/unit/test_rebind_request_shape.py
"""The producer's contract: what a batched multi-speaker session must look like.

Not a test of the clustering — that needs audio and a GPU-less minute of ONNX. This
pins the shape of the request, which is where the two real failures lived: a payload
whose turns carry no `source_filename` groups nothing, and one whose labels all come
from a single call is correctly skipped.
"""
import json
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                "..", "..", "scripts"))
import rebind_verify  # noqa: E402


class _FakeS3:
    def __init__(self, objects):
        self._objects = objects

    def get_paginator(self, _name):
        outer = self

        class _P:
            def paginate(self, Bucket=None, Prefix=None):
                yield {"Contents": [{"Key": Prefix + k} for k in outer._objects]}
        return _P()

    def get_object(self, Bucket=None, Key=None):
        body = json.dumps(self._objects[Key.rsplit("/", 1)[-1]]).encode()

        class _B:
            def read(self_inner):
                return body
        return {"Body": _B()}


def _transcript(labels):
    """A minimal AWS-Transcribe-shaped doc with one word per label."""
    return {"results": {"transcripts": [{"transcript": " ".join(labels)}],
                        "items": [{"type": "pronunciation",
                                   "start_time": str(i * 4.0),
                                   "end_time": str(i * 4.0 + 3.5),
                                   "speaker_label": lab,
                                   "alternatives": [{"content": f"w{i}",
                                                     "confidence": "1.0"}]}
                                  for i, lab in enumerate(labels)]}}


def test_two_calls_two_labels_yields_four_pairs(monkeypatch):
    sid = "sid" + "a" * 32
    objects = {
        f"ben_2026-08-27_11-06-35_{sid}_c0000_bn3_off0.0_to86.0_srcwav.json":
            _transcript(["spk_0", "spk_1"]),
        f"ben_2026-08-27_11-08-03_{sid}_c0003_bn4_off0.0_to114.0_srcwav.json":
            _transcript(["spk_0", "spk_1"]),
    }
    monkeypatch.setattr(rebind_verify.boto3, "client", lambda _s: _FakeS3(objects))
    turns = rebind_verify.build_turns("b", "Ben_UCPK2", "2026-08-27", sid)

    pairs = {(t["source_filename"], t["speaker_label"]) for t in turns}
    assert len(pairs) == 4, f"expected 4 (call,label) pairs, got {sorted(pairs)}"
    assert len({src for src, _ in pairs}) == 2
    assert all(t["source_filename"] and t["speaker_label"] for t in turns)
    assert all(isinstance(t["start_sec"], (int, float)) for t in turns)


def test_turns_from_another_session_are_not_collected(monkeypatch):
    """One day holds several sessions; grouping across them would be wrong."""
    mine, other = "sid" + "a" * 32, "sid" + "b" * 32
    objects = {
        f"ben_2026-08-27_11-06-35_{mine}_c0000_bn3_off0.0_to86.0_srcwav.json":
            _transcript(["spk_0", "spk_1"]),
        f"ben_2026-08-27_14-00-00_{other}_c0000_bn3_off0.0_to86.0_srcwav.json":
            _transcript(["spk_0", "spk_1"]),
    }
    monkeypatch.setattr(rebind_verify.boto3, "client", lambda _s: _FakeS3(objects))
    turns = rebind_verify.build_turns("b", "Ben_UCPK2", "2026-08-27", mine)
    assert {t["source_filename"] for t in turns} == {
        f"ben_2026-08-27_11-06-35_{mine}_c0000_bn3_off0.0_to86.0_srcwav.json"}
```

- [ ] **Step 2: Run it and watch it fail**

```bash
export UV_LINK_MODE=copy AWS_ACCESS_KEY_ID=testing AWS_SECRET_ACCESS_KEY=testing \
       AWS_DEFAULT_REGION=ap-southeast-2
uv run --with pytest --with boto3 --with numpy pytest \
  tests/unit/test_rebind_request_shape.py -v
```

Expected before Task 1 is merged: `ModuleNotFoundError: No module named 'rebind_verify'`.
After Task 1: both tests should pass immediately — the script was written to satisfy
them. **If they pass, delete a line from `build_turns` (the `session_base not in
filename` guard) and re-run to confirm the second test goes red.** A test that cannot
fail is not evidence (CLAUDE.md, `ci-green-over-a-dead-path`).

- [ ] **Step 3: Restore the deleted line and re-run**

```bash
git checkout scripts/rebind_verify.py
uv run --with pytest --with boto3 --with numpy pytest \
  tests/unit/test_rebind_request_shape.py -v
```

Expected: 2 passed.

- [ ] **Step 4: Commit**

```bash
git add tests/unit/test_rebind_request_shape.py
git commit -m "test(rebind): pin the request shape a batched multi-speaker session makes"
```

---

### Task 3: Make the multi-speaker path exercisable on TEST

This is the gap that let a real-looking `NoSuchKey` sit in the TEST logs. TEST could
never have run the interesting case, because the audio it needs is only in prod.

**Files:**
- Modify: `tools/asr-eval/README.md` (a "seeding TEST" section)
- No product code

**Interfaces:**
- Consumes: nothing
- Produces: a seeded TEST bucket; no code symbols

- [ ] **Step 1: Copy the session's raw chunks and batch maps into the TEST bucket**

```bash
export MSYS_NO_PATHCONV=1
SRC=s3://fieldsight-data-509194952652
DST=s3://fieldsight-data-test-509194952652
SID=sid93396a6ac8434fdf908c25a50cc7e167
for P in users/Ben_UCPK2/audio/2026-08-27 audio_segments/Ben_UCPK2/2026-08-27 \
         transcripts/Ben_UCPK2/2026-08-27; do
  aws s3 cp "$SRC/$P/" "$DST/$P/" --recursive --exclude "*" --include "*$SID*"
done
```

- [ ] **Step 2: Verify TEST now has what the embedder reads**

```bash
echo -n "raw chunks: "; aws s3 ls $DST/users/Ben_UCPK2/audio/2026-08-27/ | grep -c "$SID"
echo -n "batch maps: "; aws s3 ls $DST/audio_segments/Ben_UCPK2/2026-08-27/ | grep -c batch_map
```

Expected: 58 raw chunks, 13 batch maps. Both non-zero is the point; zero is what caused
the original failure.

- [ ] **Step 3: Re-run the verification against TEST and confirm it now succeeds**

```bash
uv run --with boto3 python scripts/rebind_verify.py \
  --bucket fieldsight-data-test-509194952652 --user-folder Ben_UCPK2 \
  --date 2026-08-27 --session-base sid93396a6ac8434fdf908c25a50cc7e167 \
  --company-id "$TEST_COMPANY_ID" --put
aws logs tail /aws/lambda/fieldsight-test-speaker-embed --since 10m --follow
```

Expected: `28 (call,label) pairs -> 3 groups`, and **no** `could not read` lines. This is
the same assertion as Task 1 Step 4, on the environment where a regression would be
caught first.

- [ ] **Step 4: Document the seeding so the next person does not re-diagnose it**

Add to `tools/asr-eval/README.md`, after the "Setting up a working directory" section:

```markdown
## Seeding TEST so the re-bind can actually run there

`lambda_speaker_embed._rebind` reads the device's raw chunks under `users/…/audio/` and
the batch maps under `audio_segments/…/*_batch_map.json` — from **its own** bucket. A
session whose audio only exists in prod fails there with `NoSuchKey`, which reads like a
code defect and is not one. Copy all three prefixes for the session before expecting a
TEST run to mean anything:

    for P in users/{FOLDER}/audio/{DATE} audio_segments/{FOLDER}/{DATE} \
             transcripts/{FOLDER}/{DATE}; do
      aws s3 cp "s3://fieldsight-data-509194952652/$P/" \
                "s3://fieldsight-data-test-509194952652/$P/" \
                --recursive --exclude "*" --include "*{SID}*"
    done
```

- [ ] **Step 5: Commit**

```bash
git add tools/asr-eval/README.md
git commit -m "docs(asr-eval): TEST needs the session's audio before a re-bind run means anything"
```

---

### Task 4: Enable in production and observe

Only after Task 1 Step 4 produced three groups on prod.

**Files:**
- No repository files. This is a GitHub repository variable plus a deploy.

**Interfaces:**
- Consumes: a passing Task 1
- Produces: nothing later tasks depend on

- [ ] **Step 1: Record the pre-flip baseline**

```bash
aws rds-data execute-statement --resource-arn "$CL" --secret-arn "$SEC" \
  --database fieldsight --sql \
  "SELECT count(*) AS rows, count(DISTINCT session_base) AS sessions
     FROM speaker_label_groups"
```

Write the two numbers down. Task 4 Step 4 compares against them, and Task 5 restores to
them.

- [ ] **Step 2: Set the repository variable**

```bash
gh variable set PROD_REBIND_SPEAKERS --body true \
  --repo benzn-tech/fieldsight-pipeline
gh variable get PROD_REBIND_SPEAKERS --repo benzn-tech/fieldsight-pipeline
```

Expected: `true`. The variable is read by `.github/workflows/deploy-prod.yml:233` into
the `RebindSpeakers` parameter, which `src/template.yaml:2895` puts on the item-writer's
`REBIND_SPEAKERS` env.

- [ ] **Step 3: Deploy prod and have a human approve it**

Push to `main` (or re-run the latest `deploy-prod` workflow). The `production`
environment gate needs a human; the approval command is in CLAUDE.md and may be
permission-gated, in which case the user runs it with `!`.

- [ ] **Step 4: Verify the flag actually reached the function**

```bash
aws lambda get-function-configuration --function-name fieldsight-prod-item-writer \
  --query 'Environment.Variables.REBIND_SPEAKERS' --output text
```

Expected: `true`. **This step is not ceremony.** A three-part toggle with the middle
part unwired takes the default and reports nothing at all
(memory: `fieldsight-unwired-toggle-trap`). The env key is the only proof.

- [ ] **Step 5: Wait for real sessions and read the effect**

```bash
# did the producer fire?
aws logs filter-log-events --log-group-name /aws/lambda/fieldsight-prod-item-writer \
  --start-time $(( ($(date +%s) - 86400) * 1000 )) --filter-pattern '"rebind"' \
  --query 'events[*].message' --output text

# did the embedder group anything?
aws logs filter-log-events --log-group-name /aws/lambda/fieldsight-prod-speaker-embed \
  --start-time $(( ($(date +%s) - 86400) * 1000 )) --filter-pattern '"rebind"' \
  --query 'events[*].message' --output text
```

Read the pair — the producer logs `rebind: requested for … (N pairs across M calls)` and
`rebind: not requested for … — N label(s) across M call(s)` for the skips, and the
embedder logs `rebind: N (call,label) pairs -> K groups`. **Expect most sessions to
produce one group**: a single person across one call is the common case, which is why
the producer has a pre-check. A run of nothing but `-> 1 groups` is not a failure; it is
a fleet of short single-speaker recordings, and TEST already showed that shape.

The number worth watching is **K relative to the people actually in the room** on
sessions with several calls. Any session where `K == pairs` (nothing merged) or `K == 1`
on a visibly multi-person meeting is a case to pull the audio for.

- [ ] **Step 6: Confirm a reader sees it**

```bash
uv run --with boto3 python scripts/invoke_org_api.py \
  --path "/api/org/sessions/<session_base>/transcript" | \
  node -e "let d='';process.stdin.on('data',c=>d+=c);process.stdin.on('end',()=>{
    const j=JSON.parse(d); const s=(j.segments||[]);
    console.log('segments', s.length, 'with speaker_group',
                s.filter(x=>x.speaker_group).length);})"
```

Expected: a non-zero count on a session the embedder grouped. Zero means the write
landed and the read did not — the two halves are different Lambdas and different
tables, and `guard-caught-it-is-not-it-works` is the note about exactly this gap.

---

### Task 5: Prove the rollback actually rolls back

A kill switch nobody has pulled is a claim, not a switch.

**Files:**
- No repository files.

**Interfaces:**
- Consumes: a live Task 4
- Produces: nothing

- [ ] **Step 1: Flip the flag off and redeploy**

```bash
gh variable set PROD_REBIND_SPEAKERS --body false \
  --repo benzn-tech/fieldsight-pipeline
```

Then redeploy prod (human-approved, as in Task 4 Step 3).

- [ ] **Step 2: Verify the function stopped asking**

```bash
aws lambda get-function-configuration --function-name fieldsight-prod-item-writer \
  --query 'Environment.Variables.REBIND_SPEAKERS' --output text
```

Expected: `false`.

- [ ] **Step 3: Confirm the transcript still reads**

Re-run Task 4 Step 6 against a session that WAS grouped. Expected: it still returns its
segments and still carries `speaker_group`, because the rows outlive the flag — the flag
only stops new requests. **This is the intended behaviour**: turning the producer off
must not blank out transcripts that were already grouped.

- [ ] **Step 4: Know how to remove the data, and confirm the reader survives it**

If the groups themselves must go (not just the flag):

```bash
aws rds-data execute-statement --resource-arn "$CL" --secret-arn "$SEC" \
  --database fieldsight --sql \
  "DELETE FROM speaker_label_groups WHERE session_base = '<session_base>'"
```

Then re-run Task 4 Step 6 on that session. Expected: segments still return, now with
zero `speaker_group` — the reader fails open by design. If it errors instead, that is a
blocking defect and the flag alone is not a sufficient rollback.

- [ ] **Step 5: Restore whichever state the rollout decision calls for**

Set `PROD_REBIND_SPEAKERS` back to `true` if Task 4 passed, or leave it `false` and write
up what failed. Either way say which, in the session notes — a flag left in an
undocumented state is the next person's mystery.

---

## Self-review notes

- **Spec coverage.** §2 of the findings doc argues for the local re-bind over a second
  ASR pass, and gives the 28-pair / 3-group / 96.3 % result. Task 1 checks exactly that
  number against the deployed chain; Task 2 pins the input shape it depends on; Task 4
  turns it on. The spec's cost claim (~55 s CPU, zero vendor cost) is not re-verified
  here — the deployed function's 600 s timeout leaves ample margin, and Task 4 Step 5's
  logs carry the real duration if it turns out to matter.
- **Out of scope, deliberately.** Backfilling historical sessions (their artifacts have
  no `speaker_turns`); naming groups (needs the consent UI); the frontend rendering of
  `speaker_group`; and a second session for cross-validation, which is a measurement
  task and not a rollout one.
- **The one number that gates everything** is Task 1 Step 4 producing `-> 3 groups`. If
  the deployed code disagrees with the offline measurement on identical audio, nothing
  after Task 1 should run until that is explained.
