# Aurora Scale-to-Zero — Design

Date: 2026-08-11 · Status: DESIGN (reviewed 2026-08-11 — revised after review, not implemented)
Repo: `fieldsight-pipeline` (backend only)
Cost owner: AWS account 509194952652, ap-southeast-2

> **Revision note.** The first draft of this spec would have saved **nothing**: it
> paired a 15-minute unconditional safety sweep with `SecondsUntilAutoPause=900`,
> so the idle timer could never reach its own threshold. That error, plus two
> missed database clients and a wrong cost model, are corrected below and called
> out where they were. Read §"Errors the review caught" before trusting any
> number here.

## The problem

The Aurora cluster costs **~$73/month of instance capacity for a database that is
almost never doing anything**. July's bill was $55.31 for RDS, of which $53.99 was
`Aurora:ServerlessV2Usage` — pure capacity rent. Storage was $0.005 and backup
$0.006. The data is tiny; the queries are few. We are not paying for work, we are
paying for a floor.

Daily ACU consumption is flat at **12.05–12.6 ACU-hr/day, every single day since
2026-07-11** — 0.5 ACU × 24h, the minimum, pinned. Measured daily average
capacity is 0.51, with occasional real spikes to the 2.0 cap.

The bill is currently masked: a credit of **-$74.9955** offset July's usage of
**$74.9955** almost exactly, so actual charges are $0. The Billing console's
"Cost breakdown" chart shows pre-credit usage, which is why the number looked
alarming without a corresponding invoice. **This is a run-rate problem, not a
current-cash problem** — but the credit will run out, and August is tracking to
~$103.

## What we found

Two independent silent failures, stacked. Either alone would have wasted the
money; together they made the waste invisible.

### Finding 1 — scale-to-zero was already configured, and silently never applied

`infra/db-template.yaml:199-211` declares:

```yaml
  # 16.4+ is required for ServerlessV2ScalingConfiguration MinCapacity 0
  # (scale-to-zero). scale-to-zero requires 16.3+; 16.4 pinned for margin.
  EngineVersion: '16.4'
  ...
  ServerlessV2ScalingConfiguration:
    MinCapacity: 0
    MaxCapacity: 2
```

Someone already made this decision deliberately. The **deployed** template says
the same. The live cluster does not:

```
DbCluster  MODIFIED
  /ServerlessV2ScalingConfiguration/MinCapacity
    ExpectedValue: 0
    ActualValue:   0.5
```

That is the only drifted property on the resource. The stack is
**`IMPORT_COMPLETE`** — the cluster was adopted by resource import, not created
by CloudFormation. Import records the template as truth without applying it, so
CFN believes 0, RDS knows 0.5, and no later deploy produces a diff to reconcile.

*(Review note: even if the true cause were a later manual edit rather than the
import itself, the operational conclusion is unchanged.)*

This is the unwired-toggle failure class one layer further out: not "the
parameter never reached the template" but "**the template never reached the
resource**". The verification rule generalises — **read the live resource, never
the template.**

### Finding 2 — even with MinCapacity 0, it would never have paused

`FinalizeSweepFunction` runs on `rate(1 minute)`, on **both** prod and test
stacks, against the **same cluster**. `src/lambda_finalize_claim.py:279-291`
connects unconditionally:

```python
def lambda_handler(event, context):
    from db.connection import get_connection
    with get_connection() as conn:          # <- always connects, work or not
        swept = sweep(conn)
        reconciled = reconcile(conn, _read_result)
```

AWS's minimum auto-pause idle window is **300 seconds**. A user connection every
60s means the idle timer never gets there. The instance would have been
configured to pause and then never paused — and since a paused instance still
reports status `available`, nothing would have looked wrong.

Measured over 7 days:

| | prod | test |
|---|---|---|
| Invocations | 10,079 | 10,082 |
| Ticks that did any work | ~99 | ~16 |
| **No-op rate** | **99.0%** | **99.8%** |

### Everything that touches the DB

Audited `src/template.yaml` for `PGHOST` — **17 functions carry it**. Those that
can reach the DB without a user present:

| Client | Cadence | Blocks pause? |
|---|---|---|
| `finalize-sweep` (prod + test) | `rate(1 minute)` | **yes — the primary blocker** |
| **`NonWorkExpiryFunction`** | `rate(1 hour)`, in-VPC, PGHOST | **currently no — rule DISABLED and `NONWORK_EXPIRY_ENABLED=false`, handler returns before connecting. Becomes a blocker the moment life-conversation separation ships to prod.** |
| `lambda_rag_search` via **`get_cached_connection`** | per Search/Ask, then **held open** | **yes, intermittently** — see below |
| `org-api` (`lambda_org_api.py:295`) | one connection per HTTP request | only if something polls |
| `voice-reaper` | `rate(6 hours)` | 4 wakes/day, acceptable |
| `session-activity` | S3 chunk events | only while recording — legitimate usage |
| `device-report` → device-ledger | daily | 1 wake/day |
| `orchestrator`, `report-generator`, `fargate-trigger` | scheduled | no `PGHOST` |
| CI smoke tests via RDS Data API | per deploy | Data API requests resume the writer (documented) |

`src/db/connection.py:78-98` keeps a **module-level cached connection**
(`autocommit=True`, deliberately not closed so warm invokes skip the 1–2s
connect). `lambda_rag_search.py:61` uses it. AWS counts any open user-initiated
connection as activity, so after any Search/Ask the cluster cannot begin its idle
countdown until that Lambda container is recycled — a delay we do not control.

## Why we can't just turn the sweep off

The 1-minute cadence is the delivery mechanism for the **≤2-minute confirmation
email** promise (`2026-07-31-voice-timeliness-prod-promotion-runbook.md`):

- stop → `pending_close`, 30s grace (`STOP_GRACE_SECONDS`, guards mis-taps)
- tick N: grace elapsed → CAS-claim → enqueue to S3 → non-VPC worker sends
- tick N+1: read the worker's result → mark `sent`/`failed`

Observed in prod, exactly as designed:

```
08-09T06:31:37  finalize sweep: enqueued=1 reconciled=0
08-09T06:32:35  finalize sweep: enqueued=1 reconciled=1
08-09T06:33:35  finalize sweep: enqueued=0 reconciled=1
```

It polls rather than arming a timer because of **BUG-36**: `session_close` lives
in org-api, which runs in-VPC and cannot reach the EventBridge Scheduler API.
Unable to be woken, it asks every minute.

It also carries **inferred idle-close** (`INFER_IDLE_CLOSE=true`): a device that
died without sending `/close` gets its session closed after
`SESSION_GAP_MINUTES` (15) so the email still goes out — the server-side backstop
for exactly the silent-delivery class the mobile side spent last week fixing.

The existing kill switch (`PROD_ENABLE_FINALIZE=false`) would disable the
flagship feature. **Not an option.**

## Errors the review caught

Recorded rather than quietly fixed, because each is a reusable trap.

1. **The 900/900 deadlock (fatal).** Draft decision 6 was "unconditional sweep
   every 15 min"; draft decision 7 was `SecondsUntilAutoPause=900` (15 min). Two
   equal intervals: the gap between forced connections is always *less* than the
   threshold, so pause never triggers. Worse, two stages unaligned average a
   connection every 7.5 min. **Every line of code would have shipped, and the
   saving would have been exactly zero.** Generalised rule: *the unconditional
   wake interval must be several times `SecondsUntilAutoPause`, never equal to
   it.*
2. **Audit was incomplete.** "Only functions with `PGHOST` on a schedule" missed
   `NonWorkExpiryFunction` (harmless today, a blocker the day it ships) and the
   whole category of *persistent* connections (`get_cached_connection`), which no
   schedule-based audit could ever have found.
3. **The cost model was built on the wrong unit.** The draft feared "resume lands
   at MaxCapacity 2.0". AWS's own example resumes at 2.0 on a cluster whose max
   is **80** — 2.0 is its "relatively small", not its cap. The real unit is
   **cost per wake**, not average ACU while awake. Break-even is ~dozens of wakes
   per day, not "6 awake hours".
4. **A verification step that could not run.** Draft step 2 proposed counting
   `skipped: no-pending` returns — but the handler only logs when it did work
   (`if swept or reconciled`), and return values are not logged. The check needed
   code that did not exist.

## Decisions

| # | Decision | Rationale |
|---|---|---|
| 1 | Close the drift by **adding `SecondsUntilAutoPause` to the template and deploying normally** | The new property creates a real template diff, so CFN pushes the whole `ServerlessV2ScalingConfiguration` block — including `MinCapacity: 0` — in one deploy. Closes IaC and reality together; an out-of-band `modify-db-cluster` would leave them divergent again. |
| 2 | Fix the sweep **before** enabling scale-to-zero | Enabling first yields zero savings and manufactures a false negative. |
| 3 | The sweep keeps its 1-minute cadence | The 2-minute promise is a product commitment; cost work must not spend it. |
| 4 | No-op ticks must not open an Aurora connection | 99% of ticks are no-ops. This is the lever. |
| 5 | The pending-work signal lives in **DynamoDB** | Gateway endpoint `vpce-01233d5b756ffefcb` already exists — in-VPC access with no NAT and no new paid endpoint. ~87.6k reads/month ≈ $0.02. |
| 6 | Unconditional safety sweep **hourly**, gated **inside the handler** (keep the existing `rate(1 minute)` rule; the handler decides by wall-clock minute), both stages on the **same** minute | Must be several × `SecondsUntilAutoPause` (error 1). **Do NOT create a separate `rate(1 hour)` rule: EventBridge `rate()` phase is set by the rule's creation time and cannot be aligned across two stacks** — two unaligned hourly rules would halve the effective interval. Handler-internal gating (or a `cron()` expression pinning the minute) aligns by construction. It guards only against a lost flag write; 1h first, consider 6h once measured. |
| 7 | **`SecondsUntilAutoPause = 600`** | Comfortably below the 1h safety interval, while leaving autovacuum a window. Append-heavy transcript tables plus an HNSW vector index degrade if autovacuum is chronically cancelled. |
| 8 | `lambda_rag_search` closes its connection at handler exit | A held-open connection blocks pause for an uncontrollable duration. Costs the 1–2s warm-connect saving on Search/Ask. |
| 9 | Both stacks change together | prod and test share one cluster; a 1-minute sweep on either keeps it awake for both. |
| 10 | A **pinning test** fails CI if any `PGHOST`-carrying function has a schedule interval < 2 × `SecondsUntilAutoPause` and connects unconditionally | Error 2 will otherwise recur silently the day `NonWorkExpiry` ships. Same shape as `test_vad_tuning_rationale.py`. |
| 11 | Accept that the first synchronous request after a >24h pause **will fail** | API Gateway's 29s ceiling is below the 30s+ deep-sleep resume. Not fixable client-side; must be absorbed by retry. |

## Architecture

### Option A — pending-work flag (recommended, ship first)

One DynamoDB item answers "is there any session the sweep could act on?" — any
session in `open`, `pending_close`, or `finalizing`.

```
PK: SWEEP_STATE#<stage>          # stage = prod | test
attrs: pending (bool), updated_at
```

**Writers**
- `ensure_open` callers (org-api `/open`, `session-activity` from the chunk
  stream) set `pending = true` — must cover `open`, not only `pending_close`, or
  a session whose device died would never be inferred closed.
- The sweep sets `pending = false` only when it has just connected and observed
  zero rows across all three states. Cleared by the component holding ground
  truth, never inferred.

**Reader**
```python
def lambda_handler(event, context):
    if not _work_pending() and not _due_for_safety_sweep():
        logger.info("finalize sweep: skipped (no pending work)")   # decision: must log
        return {"swept": 0, "reconciled": 0, "skipped": "no-pending"}
    ...
```

The skip must emit a log line (or metric). Without it, verification step 2 cannot
run — that was error 4.

**Safety net.** Hourly, aligned across stages, the sweep connects regardless. If
that pass finds work the flag denied, it logs **ERROR with session ids** — a
flag-miss is a correctness bug and must never be absorbed silently.

**Worst-case latency if a flag write is lost: up to 1 hour**, not 15 minutes.
This is the price of decision 6 and must be stated honestly wherever the
≤2-minute promise is documented — the promise holds on the normal path; the
safety net is a recovery bound, not a service level.

**Race.** "Sweep sees zero rows → clears flag" can interleave with a concurrent
`/open` setting it true. The lost flag is recovered by the hourly pass, i.e. the
race costs up to an hour on an already-rare path. If that is judged too weak, the
alternative is a conditional clear guarded by a version/timestamp written by
`ensure_open` — more moving parts, and worth doing only if measurement shows the
race actually occurs.

### Option B — event-driven finalize (follow-up; assumptions verified)

- org-api writes a close marker to S3 on `/close` (already reachable in-VPC via
  the gateway endpoint — the same channel used to hand work to the non-VPC worker)
- S3 event → **SQS with queue-level `DelaySeconds=30`** → finalize Lambda fires
  exactly when the grace expires
- the scheduled sweep drops to `rate(15 minutes)`, retaining only idle-close
  inference

All three technical assumptions were checked and hold: in-VPC S3 writes work;
S3→SQS delivery is native and `DelaySeconds` applies queue-wide; Lambda's SQS
poller runs service-side, so an in-VPC consumer needs no new endpoint. The bucket
lives outside the stack (BUG-33/34), but this repo's S3 triggers already go
through EventBridge, so an EventBridge→SQS target can live in the template
without hand-wiring bucket notifications.

B deletes the 1,440 no-op ticks instead of making them cheaper. **Ship A first**;
B's residual 15-minute idle-close sweep is subject to the same decision-6
constraint.

## Cost model

Rate, derived from July: $53.99 / 269.97 ACU-hr = **$0.19998/ACU-hr**.
Baseline: 12.05 ACU-hr/day × $0.20 × 30 = **~$72/month**.

**The unit is cost per wake, not average ACU while awake** (error 3). AWS
documents that a resumed instance "begins with a relatively small capacity and
scales up from there"; the 2.0 ACU in their worked example is small *relative to
that cluster's max of 80*, not a cap-pinning behaviour. Expect resume around
1–2 ACU settling back to 0.5 within minutes.

```
cost per wake ≈ resume transient (~2–4 min × 1–2 ACU)
              + forced idle window (SecondsUntilAutoPause × 0.5 ACU)
              ≈ 0.15–0.3 ACU-hr
```

Break-even against the 12.05 ACU-hr/day baseline is therefore **~40–60 wakes per
day**. For reference, the draft's rejected design would have produced up to 192
safety-pass wakes/day on its own — comfortably net-negative even before real
traffic.

With decision 6 (hourly, aligned) the wake budget is roughly: 24 safety passes +
a handful of recordings + voice-reaper 4 + ingest/report/ledger ≈ **10–30
wakes/day**, plus genuinely awake time during recording sessions. Projected
**2–5 ACU-hr/day ≈ $45–60/month saved**. Any web polling or device background
traffic erodes this, which is why verification step 3 measures a real overnight
window rather than trusting the arithmetic.

The VPC line is separate and already actioned: the `bedrock-runtime` interface
endpoint (retired in Phase 4d, replaced by DashScope) was deleted 2026-08-11,
removing roughly half of the $16.63/month VPC charge. `cognito-idp` stays.

## Verification plan

Evidence before assertions. Nothing here may be inferred from config.

1. **Drift closed**: `describe-db-clusters` reports `MinCapacity: 0` **and**
   `SecondsUntilAutoPause: 600` present in the output — the field is *removed*
   when auto-pause is off, so its presence is itself the proof. Read the live
   resource, not the template (Finding 1).
2. **The sweep stops connecting**: over 24h, `skipped (no pending work)` log
   lines ≈ 1,440 − (work ticks + 24 safety passes), per stage. *Requires the skip
   log added in Option A.*
3. **It actually pauses**: `ServerlessDatabaseCapacity` (Minimum, 1-min period)
   reads **0** across a sustained overnight window. The only proof of pause —
   instance status stays `available` throughout and proves nothing.
4. **Wake count and resume capacity**: sample `ServerlessDatabaseCapacity` at
   1-minute granularity for 7 days; count 0→non-zero transitions (wakes/day) and
   record the starting ACU of each. Feed both into the model above before
   claiming a saving.
5. **Measured, not projected, saving**: `Aurora:ServerlessV2Usage` ACU-hr/day for
   7 days after, against the 12.05 baseline.
6. **The promise still holds**: on a real recording, stop → confirmation email
   inside 2 minutes, *including* the case where the instance was paused at the
   moment of stop.
7. **Idle-close still works**: leave a session open with no `/close`; confirm it
   finalizes after `SESSION_GAP_MINUTES` on the flag path.
8. **Flag-miss alarm is real**: force a missing flag write in test; confirm the
   hourly pass recovers the session *and* logs ERROR.
9. **Deep-sleep failure is survivable**: after >24h paused, issue a cold
   dashboard/App request; expect a 5xx at the API Gateway 29s ceiling, and
   confirm the client retry path recovers without user-visible loss (decision 11).
   The mobile 5xx-is-retryable work from the BUG-43 round should cover this —
   confirm, do not assume.
10. **The pinning test bites**: add a scheduled `PGHOST` function with a
    sub-threshold interval in a scratch branch and confirm CI goes red
    (decision 10), including a guard that the test cannot silently pass by
    matching nothing.

## Risks

| Risk | Severity | Mitigation |
|---|---|---|
| Flag fails closed → confirmation emails silently stop | **Critical** | Hourly unconditional pass + ERROR on flag-miss. The flag is never the only path. |
| A future scheduled DB client re-pins the floor silently | High | Decision 10's pinning test. `NonWorkExpiry` is the known pending case. |
| Warm `get_cached_connection` holds the instance awake | High | Decision 8 closes it at handler exit. Verify by watching capacity after a Search/Ask. |
| First sync request after >24h pause fails at the 29s API GW ceiling | Medium | Decision 11 + verification 9. Monday morning is the exposed case. |
| autovacuum chronically cancelled → bloat + HNSW degradation | Medium | `SecondsUntilAutoPause=600`; add a weekly Lambda that connects and runs `VACUUM ANALYZE` (a scheduled *connection* resumes the instance; `pg_cron` would not — Aurora never wakes for it). |
| prod and test share one cluster | Medium | Both sweeps fixed together (decision 9). Splitting test is deferred — today both environments are busy at the same hours, so two clusters would likely wake together anyway. |
| Subnets route to an IGW | Low | No public IP; SG admits only the Lambda SG on 5432. Hygiene, not urgent — but an *unauthenticated* connection attempt does trigger a billable resume, so this must never become reachable. |

## Rollback

`aws rds modify-db-cluster --serverless-v2-scaling-configuration MinCapacity=0.5,MaxCapacity=2 --apply-immediately`
restores today's behaviour in minutes. The flag check is independently revertible
behind `SWEEP_REQUIRE_PENDING` (default off until proven) — and per the
unwired-toggle rule, that gate is verified by reading the **deployed function's
env**, not the template.

## Settled by review

- **Resume capacity is not the threat** the draft thought; the per-wake model
  replaces it. Wake *count* is the number to watch.
- **Do not spend a night observing the current setup** before changing code: a
  1-minute sweep makes pausing mechanically impossible, so the observation can
  produce no new information. Decision 2's ordering stands.
- **A before B.** B's assumptions all hold, but A is far smaller and the polling
  architecture can be removed later.
- **Test stays on the shared cluster** for now.

## Open question remaining

Is ~$45–60/month worth touching the confirmation-email path at all? The saving is
real but modest, and the failure mode being introduced (a flag that could fail
closed) sits on the flagship feature. The conservative answer — hourly safety net
first, widen only after measurement — is what decisions 6 and 7 encode. If that
trade still reads badly, the honest alternative is to fix only the drift and the
`get_cached_connection` leak, leave the 1-minute sweep alone, and accept that the
floor stays rented.
