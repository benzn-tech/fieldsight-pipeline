# Let the database sleep, and make the wake-up invisible

**Goal:** the Aurora cluster pauses at night and at weekends, and no human ever
meets a cold cluster as an error message.

**Why now:** measured 2026-09-21/22. The cluster is configured `MinCapacity=0`,
`SecondsUntilAutoPause=600`, yet `ServerlessDatabaseCapacity` has sat at a floor of
**0.5 ACU, never 0**, for at least seven days. September usage to date:
`Aurora:ServerlessV2Usage` **$49.62 over 21 days** (~$71/month) out of an ~$84/month
account total -- everything else combined is under $13. The single reason it never
sleeps is `SWEEP_REQUIRE_PENDING`, which is `true` on `fieldsight-test-finalize-sweep`
and **`false` on `fieldsight-prod-finalize-sweep`**, while both stages' `rate(1 minute)`
rules are ENABLED and both stages share one cluster.

**The trade the owner accepted:** after a long pause (>24 h) a resume can exceed API
Gateway's 29 s hard ceiling, so the first request of a quiet Monday can fail. Pilot
users can accept slowness; they must not be shown a 504. Steps 1 and 2 remove the
ordinary case entirely; step 3 turns the saving on.

**Order matters.** The safety net and the pre-warm ship BEFORE the cluster is ever
allowed to sleep. Reversed, the first person to meet a cold cluster is a real user.

## Global constraints

- Repo `fieldsight-pipeline`, base `develop`; frontend `fieldsight-ui`, base `dev`.
  Fresh worktree off the remote tip. Never `git add -A`, never `git stash`.
- Comments, commit messages, PR bodies in English.
- `fieldsight-ui` **has no CI**: an empty checks list on a UI PR is not a pass.
  Run `node --test --test-reporter=tap tests/*.test.js` (the glob form) locally.
- Backend tests: `export UV_LINK_MODE=copy AWS_ACCESS_KEY_ID=testing AWS_SECRET_ACCESS_KEY=testing AWS_DEFAULT_REGION=ap-southeast-2`
  then `uv run --with pytest --with boto3 --with "psycopg[binary]" --with urllib3 --with numpy --with tzdata pytest tests/unit -q`.
- Read the DEPLOYED function env to verify a flag, never the template (this cluster
  is `IMPORT_COMPLETE`; the template does not describe it).

---

## Step 1: the frontend stops showing a cold backend as an error

**Files:** `fieldsight-ui/scripts/api/_fetch.js`; a new
`scripts/composites/waking-notice.js`; `tests/api-fetch-waking.test.js`.

What is already there, and is better than expected: `fetchWithRetry` retries **3
times on a 1s/2s/4s ladder** for 5xx and transport errors, with a **10 s per-request
timeout** (`DEFAULT_TIMEOUT_MS`). So a cold start already produces roughly 45 s of
attempts rather than one 29 s failure, and may well succeed on its own. What is
missing is only what happens when the ladder is exhausted: `request()` reaches
`if (!res.ok)` and throws `HTTP 504` / `Endpoint request timed out`, or rethrows the
`TimeoutError`, and the page renders whichever generic error it has.

- [ ] Classify the exhausted case. A wake looks like: **every** attempt failed, and
      each failure was a transport timeout or a **502 / 503 / 504**. A `500` is an
      application fault and must keep its current behaviour -- do not fold it in, or
      a real bug starts reading as "please wait".
- [ ] Keep throwing. Set `err.waking = true` and `err.name = 'BackendWakingError'`.
      **Do not** return a `{ _waking: true }` envelope in the style of `_notFound` /
      `_accessDenied`: every existing caller currently reaches a `catch` on this
      path, and an envelope would be silently consumed as data by all of them.
- [ ] `_fetch.js` calls the new notice module when it classifies a wake. The notice
      is presentational and global -- one line of human text ("Waking the service up;
      the first visit after a quiet spell takes a little longer"), not a toast that
      says a number. It dismisses itself when the next request succeeds.
- [ ] Tests, against a stubbed `fetch`: 504 x4 -> `waking` and the notice shown;
      timeout x4 -> same; **500 x4 -> NOT waking** (keeps today's error); 504 then
      200 -> no notice at all, because the ladder already recovered.

## Step 2: a write is never auto-retried into a duplicate

**Files:** `fieldsight-ui/scripts/api/_fetch.js`; tests.

Today `maxAttempts` is 4 for **every method** unless the caller passes `retry: false`
(only `/ask` does). A 504 means the gateway stopped waiting -- **not** that the Lambda
stopped working. So a POST that times out is currently sent up to four times, and the
first cold-cluster morning is exactly when that fires at scale.

- [ ] Auto-retry transport timeouts and 502/503/504 for **GET only**. A non-GET that
      hits one surfaces immediately, `waking = true`, and the caller re-enables its
      control so the person can decide. "Try again" offered to a human beats a retry
      the client guessed at.
- [ ] A caller may still opt in explicitly (`retry: true`) where the endpoint is
      genuinely idempotent. Nothing gains that flag in this PR.
- [ ] For GET, extend the ladder when the failures look like a wake: append `8s` and
      `16s`, taking coverage to roughly 75 s. Ordinary 5xx keeps the 1/2/4 ladder --
      a broken endpoint must not take 75 s to say so.
- [ ] **Mutation control:** restore the non-GET retry and prove a test goes red.
- [ ] Risk to state in the PR body: this changes behaviour for every non-GET caller
      in the app. It is the right default, and it is a behaviour change, not a
      refactor. It can ship as its own PR ahead of Step 1 if that reads safer.

## Step 3: the cluster is warm before anyone arrives

**Files:** `src/lambda_finalize_claim.py`; `tests/unit/test_sweep_cadence_vs_autopause.py`
(extend); no infrastructure change.

- [x] Extend `_is_safety_minute` (or add a sibling consulted in the same `or`) so the
      sweep also connects unconditionally at **06:45 NZ local time, Monday to
      Friday**. Time comes from `nz_time.to_nz` / `nz_now` -- **never a hardcoded UTC
      hour**: New Zealand is UTC+12 for half the year and UTC+13 for the other half,
      and a fixed UTC cron drifts an hour at each switch.
      *Built as `_is_prewarm_minute`, over a **two-minute** window (06:45 and 06:46),
      not one. `rate(1 minute)` means "about every 60 seconds", not a wall-clock
      alignment: ticks drift, and one at :44:59 followed by one at :46:01 would skip
      minute 45 entirely. For the hourly safety pass a miss costs an hour's delay and
      is already documented as acceptable; for the pre-warm a miss costs exactly the
      Monday-morning failure this step exists to prevent. The second minute is free --
      two connections 60 seconds apart wake the cluster once.*
- [x] No new EventBridge rule. A `rate()` rule's phase is fixed by when it was
      created, the two stacks deploy separately, and two unaligned unconditional
      wakes halve the effective idle window -- the failure that made the first
      version of this work save exactly nothing. A wall-clock minute aligns the two
      stages by construction, which is the same reason `SAFETY_SWEEP_MINUTE` is a
      minute and not a rule.
- [x] Both stages must compute the same instant. Built as module constants rather
      than environment variables, so the two stages cannot drift apart at all: a
      half-wired env knob reads as configurable while silently serving the default.
- [x] Test both offsets explicitly: a January date (UTC+13) and a July date (UTC+12),
      asserting the connect happens at 06:45 **local** in each. Also pinned: a UTC
      Sunday is an NZ Monday at this hour and MUST fire, so the weekday is read in
      NZ, not in UTC.
- [x] Cost: one extra wake per weekday. Break-even for this cluster is 40-60 wakes
      per day, so this is not material -- but say the number in the PR rather than
      calling it negligible.

## Step 4: turn it on in prod

**No code.** `SWEEP_REQUIRE_PENDING=true` on `fieldsight-prod-finalize-sweep`.
Rollback is the same variable set back to `false`.

- [ ] Steps 1-3 are deployed to prod first. This is the whole point of the ordering.
- [ ] Flip it in a **Saturday early-morning window**, not on a weekday.
- [ ] This is a prod write: the owner performs it. Nothing here routes around that.

### What must be observed before it is called done

1. `finalize sweep: skipped (no pending work)` appears in the **prod** log. A
   successful deploy is not evidence that the skip path runs; the absence of this
   line means the gate did nothing.
2. `ServerlessDatabaseCapacity` actually reaches **0**. It has never done so. Until a
   datapoint reads 0, nothing has been saved, whatever the configuration says.
3. A **real stop-recording** in that same window is emailed. The gate sits directly
   on the confirmation email; this is the thing that must not break.
4. The **first resume after a long pause**, measured: how long the cluster was at 0,
   and how many seconds the first request took. This is the number the whole trade
   rests on and it has never been observed, because the cluster has never slept.
5. Monday morning: the 06:45 pre-warm fired, and the first human request was served
   from a warm cluster.

## Risks, stated

* **The resume path has never run.** Not once, on either stage -- prod's ungated
  sweep keeps the shared cluster awake, so TEST cannot rehearse this. The only place
  to learn what a cold resume costs is prod. That is why the window is a Saturday and
  why Steps 1-3 ship first.
* **Savings are bounded by real idleness.** `fieldsight-prod-org-api` had traffic in
  82 of the last 168 hours and none in the other 86. Half the week is genuinely
  quiet, so expect a large fraction of ~$71/month, not all of it.
* **`voice-reaper` runs every 6 h on both stages with no gate** (~8 wakes/day). It
  does not threaten the break-even, but it is the next thing to look at if the saving
  comes in low. Out of scope here.
* **`rag-search` holds a cached connection** while its container stays warm, so any
  Search/Ask delays the next pause by an unpredictable amount. Already mitigated
  (released in a `finally`), noted so it is not rediscovered as a bug.
* **Step 2 changes a default for every non-GET caller.** Stated again here because
  it is the one item in this plan that can break something that works today.
