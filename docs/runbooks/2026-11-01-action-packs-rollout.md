# Runbook: action packs rollout (TEST, then prod)

Feature: `docs/superpowers/specs/2026-11-01-background-action-agent-design.md` (§8 eval gates,
§9 rollout, §14 D1/D3/S6). UI hand-off: `docs/handoffs/2026-11-01-action-packs-ui.md`.
Deploy mechanics: `DEPLOYMENT-RUNBOOK.md`.

**Owner-only** marks every step that touches prod, a repo variable, a prod migration or prod
data. Everything else the main agent may run.

```bash
export AWS_PROFILE=fieldsight-deployer AWS_DEFAULT_REGION=ap-southeast-2
REPO=benzn-tech/fieldsight-pipeline
```

## 0. What ships, and the switches

| Piece | Where | Effect while the flag is `off` |
|---|---|---|
| Migration `0093_action_task_numbers.sql` | both DBs, run by the deploy's migrate step | items get per-site `task_no` from then on; older items need the backfill (step 9) |
| Migration `0094_topics_agent_probe_index.sql` | both DBs | one partial index for the `/timeline` agent-topic probe |
| Migration `0095_action_packs.sql` | both DBs | three empty tables |
| `fieldsight-<stage>-action-agent` Lambda | both stacks (`HasOrgApi`) | inert: returns before any client is built; reserved concurrency 3 comes out of the shared account pool |
| org-api `/api/org/action-packs*` routes and internal tasks | both stacks | routes 404, internal tasks answer `{"error": "disabled"}` |
| S3 trigger `fs-action-agent` (`action_pack_requests/*.json`) | TEST: added on every deploy. Prod: owner command once (step 8), kept afterwards | nothing writes the prefix while off |

Additive payload fields that are visible with the flag off: `task_no` and `source` on
timeline items, `task_no` on `/live-items` items, and `task_no` and `source` in the
`PATCH /action-items/{id}` response.

**The switch.** The repo variables `TEST_ACTION_PACKS` / `PROD_ACTION_PACKS` take `off`,
`internal` or `on`, and pass to the `ActionPacks` stack parameter. Unset means `off`.

- `internal` means platform_admin only.
- Both org-api and the agent read the switch per call.
- A repo-variable change takes effect **only on the next deploy** of that stack.
- A push that only touches `docs/` or `scripts/` does not trigger a deploy (`paths-ignore`),
  so run the workflow by hand.

**Rollback** for any flag step:

1. Set the variable back to `off`.
2. Redeploy (`gh workflow run ...`).

Editing the Lambda env in the console is faster, but the next CloudFormation deploy
overwrites it. Packs already stored stay readable again once the flag is back on.

## 1. Before merge to develop: deploy-role check (main agent)

`ActionAgentFunction` is a new CloudFormation resource with `ReservedConcurrentExecutions`
and `EventInvokeConfig`. By precedent the deploy role already has both, but this was not
verified. A denial rolls the whole TEST stack back.

```bash
DEPLOY_ROLE=$(aws iam get-role --role-name github-actions-fieldsight-deploy --query Role.Arn --output text)
FN=arn:aws:lambda:ap-southeast-2:509194952652:function:fieldsight-test-action-agent
aws iam simulate-principal-policy --policy-source-arn "$DEPLOY_ROLE" \
  --action-names lambda:CreateFunction lambda:PutFunctionConcurrency \
                 lambda:PutFunctionEventInvokeConfig lambda:UpdateFunctionEventInvokeConfig \
                 lambda:TagResource iam:CreateRole iam:PutRolePolicy iam:PassRole \
  --resource-arns "$FN" "arn:aws:iam::509194952652:role/fieldsight-test-*" \
  --query 'EvaluationResults[].[EvalActionName,EvalResourceName,EvalDecision]' --output table
```

Every row must be `allowed`. If the stack names its roles differently, read the
role-name pattern from an existing function's role (`get-function-configuration --query
Role`).

## 2. Merge to develop: TEST deploy (main agent)

1. Merge the PR into `develop`. "Deploy FieldSight TEST (SAM)" runs:
   - `sam deploy`;
   - **migrate** (`fieldsight-test-migrate` applies 0093, 0094 and 0095; check that the step
     payload lists them);
   - `wire-s3-events.sh ... test --apply` (adds `fs-action-agent` automatically on TEST);
   - smoke.
2. Confirm the trigger:
   ```bash
   aws s3api get-bucket-notification-configuration --bucket fieldsight-data-test-509194952652 \
     --query "LambdaFunctionConfigurations[?Id=='fs-action-agent']"
   ```
   It should show prefix `action_pack_requests/`, suffix `.json`, and the
   `fieldsight-test-action-agent` ARN.
3. Check CI: the 5 whole-script wire tests skip locally (no `jq`). Their first real run is
   in CI. Confirm they ran and passed.

## 3. TEST IAM check (main agent; read-only AWS)

Run the simulations against each function's **runtime** role:

```bash
ORG_ROLE=$(aws lambda get-function-configuration --function-name fieldsight-test-org-api --query Role --output text)
AGENT_ROLE=$(aws lambda get-function-configuration --function-name fieldsight-test-action-agent --query Role --output text)
B=fieldsight-data-test-509194952652      # data bucket = lake bucket on TEST (and on prod: fieldsight-data-509194952652)
sim() { aws iam simulate-principal-policy --policy-source-arn "$1" --action-names "$2" \
          --resource-arns "$3" ${4:+--context-entries "$4"} \
          --query 'EvaluationResults[0].EvalDecision' --output text; }

# org-api
sim "$ORG_ROLE"   s3:PutObject  "arn:aws:s3:::$B/action_pack_requests/x.json"            # allowed
sim "$ORG_ROLE"   s3:PutObject  "arn:aws:s3:::$B/action_pack_requests/x.cancel"          # allowed
sim "$ORG_ROLE"   s3:GetObject  "arn:aws:s3:::$B/action_packs/c/p/web/W1.txt"            # allowed
sim "$ORG_ROLE"   s3:ListBucket "arn:aws:s3:::$B" \
    "ContextKeyName=s3:prefix,ContextKeyValues=action_packs/c/p/web/W1.txt,ContextKeyType=string"  # allowed
sim "$ORG_ROLE"   s3:GetObject  "arn:aws:s3:::$B/action_pack_requests/x.json"            # implicitDeny is fine (org-api never reads it)

# agent
sim "$AGENT_ROLE" s3:GetObject  "arn:aws:s3:::$B/action_pack_requests/x.json"            # allowed
sim "$AGENT_ROLE" s3:GetObject  "arn:aws:s3:::$B/action_pack_requests/x.cancel"          # allowed
sim "$AGENT_ROLE" s3:ListBucket "arn:aws:s3:::$B" \
    "ContextKeyName=s3:prefix,ContextKeyValues=action_pack_requests/x.cancel,ContextKeyType=string" # allowed
sim "$AGENT_ROLE" s3:PutObject  "arn:aws:s3:::$B/action_packs/c/p/web/W1.txt"            # allowed
sim "$AGENT_ROLE" lambda:InvokeFunction "$(aws lambda get-function --function-name fieldsight-test-org-api --query Configuration.FunctionArn --output text)"     # allowed
sim "$AGENT_ROLE" lambda:InvokeFunction "$(aws lambda get-function --function-name fieldsight-test-rag-search --query Configuration.FunctionArn --output text)"  # allowed
```

**A missing key must read 404, not 403** (ListBucket):

- *Agent:* the cancel-marker probe on a pack with no `.cancel`. If that probe got 403, the
  run would fail. So the first TEST pack reaching `ready` (step 5) is the end-to-end proof.
- *org-api:* `GET /action-packs/{id}/sources/W9/snapshot` for a web ref whose object does not
  exist must return 404, never 500.

## 4. Verifier family pre-flight (main agent)

What the template sets:

- writer = `ASK_JUDGE_MODEL` (with `ASK_JUDGE_FALLBACKS`);
- verifier = `ACTION_VERIFIER_MODEL`, **hard-coded `''`** in `src/template.yaml`. It falls
  back to `CORROBORATION_CHEAP_MODEL` and then `web_answer.CHEAP_MODEL`, which is
  `google/gemini-3.8-flash`;
- `ACTION_VERIFIER_FALLBACKS` is also hard-coded `''`.

The verifier must be a different model family from the writer **as actually served** (S6).
So:

- If `TEST_ASK_JUDGE_MODEL` / `PROD_ASK_JUDGE_MODEL` is empty, or is a Google model, every
  pack fails with `failure: "model"`.
- The same happens when the judge falls back onto a Google model at run time.
- There is **no repo variable** to change the verifier. Fixing it is a template change
  (make `ActionVerifierModel` / `ActionVerifierFallbacks` parameters, or set the literals).

Check:

```bash
gh variable list -R $REPO | grep -E 'ASK_JUDGE_(MODEL|FALLBACKS)'
```

Expected today: a DeepSeek judge on both stages, with no Google model in the fallbacks.
After the first TEST pack, confirm that the family of `models.verifier` differs from the
families of `models.writer` and `models.writer_served` (GET /action-packs/{id}).

## 5. TEST `internal` and the eval (repo variable: owner-only; eval run: main agent)

1. **Owner-only:**
   ```bash
   gh variable set TEST_ACTION_PACKS -R $REPO --body internal
   gh workflow run "Deploy FieldSight TEST (SAM)" -R $REPO --ref develop
   ```
   Confirm the run: `aws lambda get-function-configuration --function-name
   fieldsight-test-org-api --query 'Environment.Variables.ACTION_PACKS'` should read
   `"internal"`, and the same on `fieldsight-test-action-agent`.
2. **First pack (main agent, as a TEST platform_admin).** POST `/api/org/action-packs` with a
   one-site instruction, then poll GET until `ready`. Check:
   - `models` (step 4);
   - `run_trace.rounds`, `tokens` and `ms.total`;
   - CloudWatch `/aws/lambda/fieldsight-test-action-agent` has no ERROR lines.
3. **TLS smoke test of the pinned-IP web fetcher.** Its TLS path has only run against fakes.
   Submit an instruction that names a standard that is **not** in the project library (for
   example "What does NZS 3109 require for curing a suspended slab, and do our records show
   we met it?"). Then:
   - the pack has a `web_source` with a non-null `snapshot_s3_key`;
   - `run_trace.snapshots` ≥ 1;
   - `GET .../sources/W<n>/snapshot` returns the page text.

   If every snapshot is skipped, read `run_trace.web` and `snapshots_skipped` and the agent
   log. A TLS or SNI fault shows as fetch errors, not as a run failure. The web lane does
   **not** depend on `ENABLE_WEB_ANSWER`; it runs whenever the plan asks for web queries.
4. **Eval**, per `scripts/action_pack_eval/README.md` ("After merge: the TEST run"):
   1. Write the gold set (≥ 20 instructions, §8.1 composition) to
      `action_pack_eval_runs/gold.json`, then `validate-gold`.
   2. Run `submit` as `base1`. Label, make the blind subset, get the second annotator, then
      `score --second ... --prices ...`.
   3. **Noise floor first**: submit the same gold set unchanged as `base2`, label and score
      it, then `compare --baseline base1 base2`. Compare any variant only after that.
   4. The gates (§8.3) are fabrication ≤ 5%, wrong target = 0, recall ≥ 60%, p90 ≤ 300 s.
      `score` exits 0 only when every gate passes.
5. Fix and re-run until the gates pass. Every fix goes through develop, so it redeploys
   TEST.

## 6. TEST `on` (owner-only)

```bash
gh variable set TEST_ACTION_PACKS -R $REPO --body on
gh workflow run "Deploy FieldSight TEST (SAM)" -R $REPO --ref develop
```

The owner hand-tests the 10-10 pour scenario from the TEST web app (fieldsight-ui `dev`, once
the UI PR has landed). Check:

- create;
- the reading shows while running;
- ref chips (R, D and W) open the right viewer;
- apply one close;
- trigger a stale (edit the task by hand, then apply);
- the created task shows `#n`, the Agent badge and "Added from action pack" history;
- Apply-all skips weak actions.

## 7. Release to prod with the flag off (owner-only)

1. The main agent opens the release PR `develop` → `main` and gives the PR number and link.
   **Do not set `PROD_ACTION_PACKS`**: unset means `off`.
2. The owner merges, then approves "Deploy FieldSight PROD (SAM)" after the 15-minute gate.
   The workflow runs:
   - `sam deploy`, which creates the inert `fieldsight-prod-action-agent`;
   - **migrate** (`fieldsight-prod-migrate` applies 0093, 0094 and 0095). Read the step
     output. Between `sam deploy` and migrate, the new code runs without the tables. Every
     existing path tolerates this (savepoint plus 42P01/42703). Items written in that window
     get no `task_no` until step 9;
   - the lake wiring (`PROD_WIRE_LAKE=true`, so `--apply`). It **keeps** an existing
     `fs-action-agent` rule but does **not** add one on prod. The step output says
     `NOTE: fieldsight-prod-action-agent not added on prod (owner step: WIRE_ACTION_AGENT=true)`;
   - smoke `/api/health`.
3. Confirm with the flag off:
   - `GET https://<prod api>/api/org/action-packs` → 404;
   - `GET /api/org/timeline?...` still renders, with `task_no` present (null for older items);
   - `/live-items` renders.

## 8. Prod S3 wiring (owner-only, once)

Needs `jq` and the deployer profile:

```bash
WIRE_ACTION_AGENT=true bash scripts/wire-s3-events.sh fieldsight-data-509194952652 prod ap-southeast-2          # dry run: read the diff
WIRE_ACTION_AGENT=true bash scripts/wire-s3-events.sh fieldsight-data-509194952652 prod ap-southeast-2 --apply
aws s3api get-bucket-notification-configuration --bucket fieldsight-data-509194952652 \
  --query "LambdaFunctionConfigurations[?Id=='fs-action-agent']"
```

The script merges: it rewrites only the `fs-*` entries and keeps everything else. Later prod
deploys keep `fs-action-agent` without the variable. Until this step, a prod pack (once the
flag is on) would sit `queued` until the 20-minute sweep fails it as `internal`.

Then run the step 3 IAM simulations with `fieldsight-prod-*` and
`B=fieldsight-data-509194952652`. Read-only, but run with the owner's credentials.

## 9. Prod `task_no` backfill (owner-only)

The script (dry run by default; `--apply` writes; idempotent; one site per transaction, under
the writers' per-site lock):

```bash
python scripts/backfill_task_numbers.py                 # dry run: prints {"sites", "to_number", "numbered": 0}
python scripts/backfill_task_numbers.py --apply         # writes; a second run should print to_number 0
python scripts/backfill_task_numbers.py --site <uuid>   # optional, one site (repeatable)
```

**Blocker: this cannot run from a laptop as written.** The script opens a direct psycopg
connection (`db.connection.get_connection`: `DATABASE_URL`, `PG*`, or
`DB_SECRET_ARN` + `DB_HOST`). Aurora is not reachable from outside the VPC; that is why
`collapse_multibound_photos` became an org-api internal task (`src/photo_collapse.py`
docstring). There is no task_no backfill task in org-api. Options:

1. **Preferred (a code change, before step 9):** expose `run_backfill` as an org-api internal
   task, the same way as `collapse_multibound_photos`. The owner then runs:
   ```bash
   aws lambda invoke --function-name fieldsight-prod-org-api --cli-binary-format raw-in-base64-out \
     --payload '{"task":"backfill_task_numbers","apply":false}' /tmp/bf.json && cat /tmp/bf.json
   # then "apply": true; check the body, not only the invoke status
   ```
   (The task name is illustrative until that change lands.)
2. Run the script from something inside the VPC that has the DB secret.

Run it as soon as possible after step 7's migrate. Items numbered live after migrate get
lower numbers than older items the backfill numbers later, and the backfill never renumbers.
Until it runs, older tasks show no `#n`, and instructions that name `#n` cannot address them.

## 10. Prod `internal`, then `on` (owner-only)

1. Prod `internal` (platform_admin only):
   - first run step 4's check against `PROD_ASK_JUDGE_MODEL` / `PROD_ASK_JUDGE_FALLBACKS`;
   - then `gh variable set PROD_ACTION_PACKS -R $REPO --body internal`;
   - then run "Deploy FieldSight PROD (SAM)" (`gh workflow run "Deploy FieldSight PROD
     (SAM)" -R $REPO --ref main`) and approve it.

   The owner runs a few real instructions.
2. Prod `on` **only when both** hold:
   - the §8 thresholds passed on TEST (the step 5 reports, after the noise floor);
   - `PROD_DECLARE_CONTINUITY` is on, **or** the owner accepts the measured stale rate with
     continuity off. Measure it as the share of applied-or-stale actions that went stale, on
     prod `internal` packs.

   Then `gh variable set PROD_ACTION_PACKS -R $REPO --body on` and redeploy as above.
3. The UI entry points ship separately (fieldsight-ui `main`). They hide themselves while the
   routes 404.

## 11. Watch after each flag step

- `/aws/lambda/fieldsight-<stage>-action-agent`: `action agent payload rejected`, StageError
  and ERROR lines.
- org-api logs: `action pack sweep marked N stale pack(s) failed`, `action pack apply
  failed`, `action pack request put failed`.
- The agent has no Throttles alarm. A fourth concurrent pack waits for Lambda's async retry,
  up to an event age of 900 s. Watch `Throttles` on the function by hand.
- `SELECT state, failure, count(*) FROM action_packs GROUP BY 1,2;`: a rising
  `failed/model` count means the verifier family collides (step 4).

## Known limitations (v1)

From the build ledger's deferred notes and the spec's v1 cuts:

- **Apply race windows.** Apply takes the source's advisory lock, `FOR SHARE` on the topic
  and `FOR UPDATE` on the item. A supersede path that reads the item before apply commits
  could still carry the old value forward: the action shows applied while the item is
  unchanged. This is rare, and documented in Task 7. `_APPLY_LOCK_ROUNDS = 4`: a target whose
  source keeps changing returns 500, and the action becomes `failed` (retryable).
- **Near-duplicate check for creates** compares only against the items the run read. Round 1
  reads at most 100 (`ROUND1_ITEMS_LIMIT`), so a duplicate the run never read is not caught. A fuzzy-only
  match is kept and capped at `weak`; only an exact `todo_collapse` key match drops it.
- **Conclusion lines the verifier rejected are not stored.** They are counted in
  `run_trace.verify` only, so the eval reports a raw-fabrication upper bound for them.
  Unsupported actions are stored (never applicable).
- **A transient verifier error labels an element `unsupported`**, so it is hidden from
  Apply. If every check fails, the pack fails with `verifier`.
- **No .docx export.** `format=docx` returns 400. org-api has no python-docx layer.
- **Web-lane tokens and failed model-chain attempts do not count toward the per-run token cap**
  (`ACTION_TOKEN_CAP=400000`).
- **One active pack per person.** A second create is 409 until the first finishes.
- **A queued pack whose S3 trigger is delayed more than 20 minutes is failed** by the sweep
  (`internal`). The late delivery then loses the claim. One threshold covers queued and
  running.
- **The list returns at most 50 packs**, with no paging. Its action counts and titles are not
  filtered by the removed-source re-check.
- **One site per pack.** Round-1 day ranges are capped at 28 days.
- **The verifier model is not configurable** without a template change (step 4). The family
  check fails open on unrecognised id prefixes (`us.anthropic...`, `models/...`); today's
  ids are `vendor/model`.
- **Agent topics show on the first applier's day view only.** Later appliers' created items
  attach to the same per-pack topic. The fieldsight-ui `compliance-aggregator.js` skips
  `is_live=false`, so agent items miss the safety and quality views until the UI fixes it.
- **An agent-created task outlives deletion of the recording it was based on.** The pack's
  sources show "source removed", but the task stays.
- **Tenancy gate is shadow while `COMPANY_STATUS_ENFORCE` is off.** Packs can be created,
  claimed and applied for an inactive company, as with every other write.
- **The `task_no` writer degrades quietly.** A non-migration error in numbering is logged as
  a WARNING and the item stays unnumbered until a backfill. Every extraction and report write
  now takes a per-site advisory lock at the end of its transaction, with the flag off too.
- **robots.txt**: a 5xx or timeout skips the page (`robots_unavailable`), and each fetch has
  its own 10 s deadline, so a slow host can take about 20 s per page.
