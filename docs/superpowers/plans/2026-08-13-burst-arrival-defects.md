# Implementation plan — burst arrival: deterministic seal keys and a rate limit that cannot lose audio

**Date:** 2026-08-13 · **Spec:** `docs/superpowers/specs/2026-08-13-burst-arrival-defects.md`
**Verdict of the adversarial review: GO WITH CHANGES.** This plan IS the spec plus the
required changes from that review; where the two disagree, this plan wins. The changes are
recorded in §0 so nobody re-derives the spec's original wording.
**Branch:** `fix/burst-arrival-defects` off `origin/develop`.
**Prod impact:** none from Phases 1–3 and 6 (`BATCH_TRANSCRIPTION=false` on prod — re-verify
live, don't cite the 08-13 check). Phases 4–5 touch the transcribe function's *invocation*
plumbing, which exists on prod too; each states its prod posture explicitly.
**TEST impact:** real — batching is on there. Each phase states whether merging it alone
changes TEST.

Run tests exactly as the repo does:

```
export UV_LINK_MODE=copy
export AWS_ACCESS_KEY_ID=x AWS_SECRET_ACCESS_KEY=x AWS_DEFAULT_REGION=ap-southeast-2
uv run --with pytest --with boto3 --with "psycopg[binary]" --with urllib3 --with numpy pytest tests/unit -q
```

2618 pass at branch point. Strict TDD: every phase writes its RED test first and watches it
fail before implementation. After any fix that pins writer↔reader agreement, revert the
writer's half and confirm the test goes red — this feature has already lost a shipped fix
to a whole-file rewrite with CI green throughout.

---

## 0. Decision record — what the review changed and why (do not re-litigate without new data)

### 0.1 The bucket key alone does NOT fix defect A — it converts duplication into silent loss

The spec's direction (seal key = `floor(base_epoch / BATCH_WINDOW_SEC)`) makes the *key*
deterministic. It does nothing to make the *member list* deterministic, and the member list
is a DynamoDB Query snapshot (eventually consistent by default) taken mid-burst. Trace:

> Chunks 8–11 all belong to bucket B. Worker X (invoked for chunk 10) lists members and
> sees {8, 9, 10} — chunk 11's registration has not replicated, or has not happened yet.
> X claims `SEAL#B`, wins, seals a 3-member batch, marks 8–10 consumed, marks `SEAL#B`
> `sealed`. Chunk 11's own invocation registers, plans bucket B = [11], calls `claim_seal`
> → conditional failure, status is `sealed`, `claim_seal` returns `None` — by design,
> "a claim that is already sealed is never re-driven" (`batch_ledger.py:claim_seal`).
> Every later arrival re-plans [11] and is refused again. `_seal_tail_batches` (deadline 0)
> is refused too. **Chunk 11 is never transcribed, with no error anywhere.**

Under the shipped greedy rule the same divergence produced *duplication* (123 objects for
39 windows) — expensive but recoverable, and the extraction survived it by luck. Under the
bucket key as the spec words it, the same divergence produces *permanent silent loss*,
which is defect B's outcome grafted onto defect A. That is strictly worse.

**The load-bearing addition — the sealed-bucket straggler rule:** an unconsumed member
whose bucket's seal record is already `sealed` is handed to the per-chunk path (the proven
copy-to-self re-drive), marked with a member-scoped bypass record, and consumed. One extra
transcription request; the words survive; nothing is billed twice because the member was
never in the sealed WAV. The same rule covers the genuinely-late chunk (hours later, bucket
long sealed) — the spec's "a late chunk forms its own window" is impossible under a bucket
key, and per-chunk is the correct replacement.

With the straggler rule, the residual race cost is bounded and correct in both directions:
concurrent snapshots can only *shrink* a batch, never duplicate one (the claim is one per
bucket), and every shrunk-out member is transcribed per-chunk. Additionally `list_members`
switches to `ConsistentRead=True` — the table is single-region, the read is one partition,
and it removes the replication half of the divergence for a few milliseconds of latency.

### 0.2 The bypass check in `_maybe_batch` breaks under bucket keys — member-scoped BYPASS records

`_maybe_batch` (lambda_transcribe.py:398) decides whether a copy-to-self event should fall
through to normal transcription by reading `seal_status(table, session_id, chunk_index)` —
i.e. it looks up `SEAL#{chunk_index}`. Once seal records are keyed `SEAL#{bucket}`, that
lookup finds nothing, the re-driven event re-enters batching, finds the member consumed,
plans no window, and reports `batched_pending` for audio nothing will ever transcribe —
exactly the vanishing act the code comment there warns about.

Fix: "this member goes per-chunk" becomes a **member-scoped record**, `BYPASS#{index:04d}`,
written for both the singleton bypass and the 0.1 straggler re-drive. `_maybe_batch` checks
it with one query. Ordering keeps the three-writes discipline `bypass_singleton` already
has: BYPASS record first (the copy's event can beat the next line), copy second, consumed
last (a failed copy leaves the member unconsumed and the record retakeable via `claimed_at`
staleness, same as today). `claim_seal`-style stale-retake semantics move with it.

Old-record compatibility: existing `SEAL#{first_index}` records use `%04d` (values 0–9999);
bucket values are epoch/120 ≈ 14.9 million — **no key collision is possible**, verified by
a test, not by this sentence. Members consumed under the old scheme stay excluded (the
planner reads `sealed_into` per member, which is unchanged). The one transition race — an
old-code worker mid-seal under `SEAL#0005` while a new-code worker claims `SEAL#{bucket}`
for the same chunks during the deploy window — is bounded to in-flight sessions at deploy
time and costs one duplicated batch at worst; merge in a quiet window and check that
morning's sessions.

### 0.3 One claim per bucket, even past the cap

`BATCH_MAX_CHUNKS=4` could split one bucket into two batches, and two batches under one key
means the second can never claim. For 30-second chunks the cap cannot bind — at most four
base times spaced ≥30 s fit in `[0, 120)` — and a test pins that arithmetic. If a device
ever emits shorter chunks, the rule is: **the single claim holder for the bucket seals all
its members**, emitting `ceil(n/cap)` WAVs under the one claim, consumed and `mark_sealed`
together. No `SEAL#{bucket}#{ordinal}` sub-keys — a second key is a second race.

### 0.4 This reverses a shipped, test-pinned decision — say so and delete the pin by name

`test_the_window_anchors_on_its_first_member_not_on_a_grid` (tests/unit/test_batch_stitch.py:287)
pins the greedy anchor, and the 08-13 wall-clock plan chose greedy over the grid for 2 %
fewer requests. The replay measured what that comparison never asked: the greedy anchor is
a function of "who is left", which under concurrency is not a function at all. The 2 % (and
the boundary-straddling split, ~one extra seam per session at worst) is re-accepted as the
price of a key every worker computes identically from the filename alone. The pin test is
deleted **by name, in the same commit as the planner change**, and replaced by the
determinism pins in Phase 1. The gap/splice machinery (`crosses_gap`, seam kinds, the
embedded map) is anchor-agnostic and unchanged.

### 0.5 Bucket arithmetic must be a pure function of the filename — pin the clock convention

`extract_base_time_from_filename` returns a **naive** datetime (device-local NZ). The
bucket must be derived with one fixed convention — `calendar.timegm(base.timetuple())`
(treat naive as UTC) — never `datetime.timestamp()` (interprets in the server's local
zone), never `time.time()`, never any tz lookup. With that: a device clock 12 hours wrong
still buckets deterministically (every worker parses the same filename — the bucket is
*wrong on the wall*, but identical everywhere, which is the property the key needs); a DST
fold co-buckets chunks up to an hour apart once a year (accepted, bounded by the cap and by
0.3); identical base times order by `(base_time, index)` as today. Membership stays
device-clock-only, readiness stays `registered_at`-only — the two-clocks rule from the
wall-clock plan §0.1 is unchanged and its tests stay green.

### 0.6 Defect B — the spec's order is kept, but "raise on 429" is NOT sufficient alone, and the sweep is the actual guarantee

Facts verified in code, not assumed:

- `elevenlabs_utils.transcribe_segment` **already** retries 429/5xx in-handler — 4 attempts,
  1+2+4 s backoff (`elevenlabs_utils.py:33-35, 181-184`) — then raises `RuntimeError`. The
  replay's 27 losses happened *through* that retry: a 141-wide burst against a 20-concurrent
  ceiling outlives 7 seconds of backoff by construction. In-handler retry is not the
  mechanism and must not be extended (it holds a Lambda open, which *worsens* concurrency).
- The per-record `except Exception` (`lambda_transcribe.py:549`) converts the raise into
  `status: error` + HTTP 200, which async invocation treats as success. No retry occurs.
- S3 → Lambda async retry semantics, stated so nobody re-guesses them: an **unhandled**
  function error is retried **twice** (three attempts total), at roughly one then two
  minutes; a **throttle** is retried with growing backoff for up to
  `MaximumEventAgeInSeconds` (default 6 h); after either budget is exhausted the event is
  **discarded silently** unless an `OnFailure` destination or DLQ exists. This function has
  neither (`template.yaml:1001-1046` — no `EventInvokeConfig`, no
  `ReservedConcurrentExecutions`).

Therefore: raising on retryable errors converts "lost immediately, logged once" into "lost
after three attempts, logged three times" — better odds against a decaying burst, still a
silent drop in the tail. The three layers land in this order and none is optional:

1. **Ceiling first** (`ReservedConcurrentExecutions`) — the only mechanism that acts before
   the request is made. Throttled events queue inside Lambda for hours, not seconds: at
   reserve 12 and ~60 s per batch, the 123-batch replay drains in ~11 minutes, four orders
   of magnitude inside the 6 h event age. The number is derived in Phase 4 from the live
   list of **every consumer of the same ElevenLabs key** — the key reaches functions as
   plaintext env (`fieldsight-voice-timeliness-spec`), and if test and prod share a
   workspace, TEST's reserve must leave headroom for prod's callers. Recorded as the pair
   (provider limit 20, reserved N, other consumers listed) so a plan change is noticed.
   Side effects accepted and stated: the reserve also caps the cheap member-registration
   invocations (they queue behind transcriptions during a burst — harmless, registration
   is idempotent and grace is judged on `registered_at`, which is stamped when the
   invocation finally runs); and the reservation is subtracted from the account's
   unreserved pool in both stages.
2. **OnFailure destination in the same phase as the ceiling**, because the ceiling is what
   introduces event-age expiry as a (remote) drop path. `EventInvokeConfig` with
   `MaximumRetryAttempts: 2`, `MaximumEventAgeInSeconds: 21600`, `OnFailure` → a new SQS
   queue, plus an alarm on queue depth ≥ 1. A message on that queue is a batch that
   exhausted every retry — the thing that today leaves one ERROR line among many.
3. **Raise on retryable, keep 200 on malformed** — a typed split in `elevenlabs_utils`
   (retryable-exhausted vs permanent 4xx), re-raised by the handler. Safe with S3 events
   because each notification carries a single record (pinned by a test that a
   multi-record event would still fail loudly rather than double-process). Lands only
   after 2, or the third failed attempt is a silent drop again.
4. **The sweep is the guarantee, not a candidate.** Only a "batch WAV with no transcript
   after N minutes → re-drive by copy-to-self, bounded attempts" sweep catches every class:
   swallowed errors, dropped events, the tail-seal family
   (`2026-08-13-tail-seal-recovery.md` — this sweep is the subsumption that spec
   anticipated), and whatever the next provider invents. The replay's acceptance number is
   only an invariant because this layer exists.

### 0.7 What the spec missed, now in scope or explicitly parked

- **The VAD fan-out** also ran 153-wide at 3008 MB each (~450 GB-s per burst-minute of
  account concurrency). Parked, with the math recorded: VAD has no external ceiling to
  respect and its work is per-chunk-independent, so the herd is a cost/pool concern, not a
  correctness one. If the account pool (raised to 1000 on 2026-08-04 — re-verify live)
  ever tightens, a reserve on VAD is the dial. Not done here.
- **DynamoDB hot partition**: all ledger writes for a session share `PK=BATCH#{sid}`, and
  `mark_members_consumed` rewrites full member rows per seal. The 153-chunk replay stays
  well under the 1000 WCU/s partition ceiling, and boto3 retries throttles; noted so the
  next 10× replay doesn't rediscover it. Verify the live table is on-demand in Phase 7
  (the table is external to the stack — `TranscriptTableName` is a parameter).
- **Extraction inflation is now asserted, not lucky**: `verify_batch_session.py` gains a
  bound — normalized transcript characters per audio minute — so 4× duplication fails the
  script instead of being merged away by the model (Phase 7).

### 0.8 Unchanged, and forbidden territory untouched

VAD threshold/merge rules, `DROP_SILENT_CHUNKS`, normalisation order, microphones: measured
dead ends, untouched; `tests/unit/test_vad_tuning_rationale.py` stays green and unedited.
The embedded-map design, seam kinds, singleton-bypass rationale, and the two-clocks rule
from the wall-clock plan all survive this change; only the anchor and the key move.

---

## Phases

Dependency order: 1 → 2 → 3 (defect A); 4 → 5 (defect B; independent of 1–3, and Phase 4 is
the highest-value single merge — it may land first in calendar order); 6 after 4 (its
re-drives must run under the ceiling); 7 after everything is deployed.

---

### Phase 1 — the bucket planner (`batch_stitch`) [INERT — nothing calls it yet; safe to merge alone]

**RED first**, `tests/unit/test_batch_stitch.py` (extend):

1. `test_the_bucket_is_a_pure_function_of_the_filename` —
   `bucket_of(extract_base_time_from_filename(name), 120)` called twice, in two "workers"
   holding different member snapshots, returns the same integer for the same filename.
   Fails now: `bucket_of` does not exist.
2. `test_two_snapshots_plan_the_same_bucket_for_every_common_member` — snapshot A
   `{5,6,7,8}` and snapshot B `{0..8}` (the spec's own trace): assert every index common to
   both plans lands in the same bucket key in both. Fails now: `plan_windows` anchors at 5
   in A and at 4 in B — this is defect A, demonstrated red at the pure layer.
3. `test_naive_base_times_bucket_by_a_fixed_convention_not_the_server_zone` — same naive
   datetime buckets identically with `TZ=UTC` and `TZ=Pacific/Auckland` monkey-patched
   environments (pins §0.5's `timegm` rule; fails against any `.timestamp()`
   implementation).
4. `test_a_boundary_straddling_run_splits_at_the_bucket_edge` — members at `t+110, t+140`
   where `t+120` is a bucket edge → two buckets. This **replaces**
   `test_the_window_anchors_on_its_first_member_not_on_a_grid`, deleted by name in the
   same commit (§0.4 — the reversal is deliberate and this pair of edits is its record).
5. `test_at_most_cap_base_times_fit_one_bucket_at_thirty_seconds` — generated base times
   ≥30 s apart never exceed 4 per 120 s bucket (pins §0.3's "cap cannot bind" arithmetic).
6. `test_short_chunks_overflow_one_bucket_into_multiple_wavs_under_one_key` — 6 members
   10 s apart in one bucket, cap 4 → `plan_buckets` yields ONE bucket entry whose members
   split into `[4, 2]` WAV groups (the shape `seal_batch` consumes in Phase 3).

**Change:** add `bucket_of(base_time, window_sec) -> int` and
`plan_buckets(members, window_sec, cap, consumed) -> list[(bucket, [wav_groups])]` beside
`plan_windows` (which stays, untouched, until Phase 3 removes its last caller). Pure module
rules unchanged: no boto3, no env, no `time.time()`.

**Verified by:** the suite. No deploy, no IAM.

---

### Phase 2 — ledger: bucket seal keys, the straggler rule, member-scoped bypass [INERT — nothing calls it yet]

**RED first**, `tests/unit/test_batch_ledger.py` (extend, fake table double as today):

1. `test_two_workers_with_different_snapshots_contend_for_one_key` — worker A claims the
   bucket seen from `{5,6,7,8}`, worker B claims it seen from `{0..8}`: assert both compute
   the same `SEAL#{bucket}` SK and exactly one claim succeeds. Fails now (two different
   `SEAL#{first_index}` keys, both granted — defect A at the ledger layer, red before
   green).
2. `test_a_member_of_a_sealed_bucket_is_redriven_not_orphaned` — seal bucket B with members
   `{8,9,10}`; register 11 (same bucket); assert the next planning pass proposes 11 as a
   **redrive** (not a claimable window), a `BYPASS#0011` record is written, the re-drive
   hook is invoked with 11's unit key, and 11 is consumed with `sealed_into = "direct"`.
   Fails now: nothing implements the rule, 11 is proposed and refused forever (§0.1's
   trace, red before green).
3. `test_the_redrive_orders_bypass_copy_consumed` — a copy hook that raises leaves the
   member unconsumed and the BYPASS record stale-retakeable (the `bypass_singleton`
   three-writes discipline, pinned on the new path).
4. `test_old_first_index_seal_records_cannot_collide_with_bucket_keys` — a table holding
   `SEAL#0005` (legacy) and a bucket claim for the same chunks: the bucket claim succeeds
   and neither record shadows the other (§0.2's migration claim, tested not asserted).
5. `test_list_members_reads_consistently` — the fake table records `ConsistentRead=True`
   on the members query.
6. `test_a_singleton_bucket_writes_a_member_scoped_bypass` — sealing a one-member bucket
   produces `BYPASS#{index}` (not a status on the bucket key alone) and the bucket record
   ends `sealed`.

**Change:** `_seal_sk` takes the bucket; `claim_seal` unchanged in mechanism (conditional
put, stale takeover) but keyed on the bucket; new `mark_member_bypassed` /
`member_bypass_status` on `BYPASS#{index:04d}` with `claimed_at` staleness; `pending_windows`
becomes `pending_buckets(rows, now, grace_sec, window_sec, cap)` returning both sealable
buckets and redrive lists; `_cannot_grow` reworked for the grid (a bucket is
grace-exempt when its indices are contiguous AND the next index is registered in a later
bucket, or the cap group is full — same asymmetry as today: an interior hole always waits
for grace). `list_members` gains `ConsistentRead=True`.

**Verified by:** the suite. No deploy, no IAM.

---

### Phase 3 — flip the sealer, the transcriber and the sweep to buckets [CHANGES TEST ON MERGE]

**RED first**, `tests/unit/test_batch_seal.py`, `test_lambda_transcribe_batching.py`,
`test_finalize_batch_seal.py` (extend):

1. `test_a_burst_of_interleaved_workers_produces_one_seal_per_bucket` — replay N synthetic
   registrations against the fake table in several randomized interleavings, each arrival
   running the real `register → seal_ready_runs` path with a snapshot taken at its own
   step: assert batches written == the offline `plan_buckets` count, every member consumed
   exactly once, and **no member is neither in a WAV nor redriven**. This is the 123-vs-39
   defect as a unit test; it must fail against the current greedy code before the flip.
2. `test_the_bypass_check_reads_the_member_record_not_the_seal_key` — a copy-to-self event
   for a bypassed member falls through to normal transcription under bucket keys. Fails
   now (`seal_status(table, sid, chunk_index)` misses — §0.2 red before green).
3. `test_the_tail_seal_still_uses_deadline_zero_under_the_bucket_rule` — at session close
   the open bucket seals regardless of grace AND any straggler redrives fire (pins the
   twice-broken `_seal_tail_batches` line against a third erasure, extended to the new
   redrive branch).
4. `test_an_overflowed_bucket_seals_every_group_under_its_one_claim` — the Phase 1 §6
   shape driven through the real `seal_batch` against the dict-backed fake S3: two WAVs,
   one claim, all members consumed, `mark_sealed` once.

**Change:** `seal_batch`/`seal_ready_runs` take bucket entries; `_maybe_batch` checks
`member_bypass_status`; `_seal_tail_batches` routes through `pending_buckets`. Delete
`plan_windows`/`pending_windows` and their superseded tests **by named, single-anchor
edits** (Windows CRLF repo: single-line Edit anchors, never `git add -A`, never a
whole-file write — this file has eaten a fix that way once already). Update the
docstrings in `batch_stitch`/`batch_ledger` that say "keyed by the window's FIRST index".

**Merge note:** merging deploys the behaviour change to TEST. Merge alone, in a quiet
window (the §0.2 deploy-transition race), on a morning someone will watch that day's
sessions.

**Verified against live state:** after the first real session — `SEAL#` items in the live
table carry bucket-sized numbers, zero `_bn1` objects, `verify_batch_session.py` green on
that session, and any `BYPASS#` record has a matching per-chunk transcript.

---

### Phase 4 — the ceiling and the failure destination [TEMPLATE + IAM; CHANGES TEST AND PROD PLUMBING; may merge first]

No unit RED — this is template work; the RED is the live gap itself (no
`ReservedConcurrentExecutions`, no `EventInvokeConfig` on `TranscribeFunction`,
`template.yaml:1001`). cfn-lint (pinned version, per `fieldsight-ci-cfn-lint-pin`) gates
the diff.

**Change, in one phase because the second exists to catch what the first introduces:**

1. `ReservedConcurrentExecutions` on `TranscribeFunction`, as a stage-mapped parameter.
   **The number is derived, not guessed, before the template edit:** enumerate every
   deployed function whose live env carries `ELEVENLABS_API_KEY` in **both** stacks
   (`aws lambda get-function-configuration`, both accounts if applicable —
   `fieldsight-two-accounts-two-architectures`), and establish whether test and prod hold
   the same key (same EL workspace ⇒ the 20-concurrent ceiling is **shared across
   stages**). Reserve = 20 − (peak of other consumers) − margin; starting point 12 on
   TEST, and the pair (provider limit, reserve, consumer list) is recorded in the template
   comment so a plan change is noticed. Prod: the same parameter, set conservatively —
   this is a prod plumbing change even with batching off, because the per-chunk EL path
   runs there and has the identical 429 exposure at high enough chunk arrival rates.
2. `EventInvokeConfig` on the same function: `MaximumRetryAttempts: 2`,
   `MaximumEventAgeInSeconds: 21600`, `OnFailure` → new SQS queue
   (`fieldsight-{stage}-transcribe-failed`), 14-day retention.
3. CloudWatch alarm: queue depth ≥ 1 for 5 minutes → the existing alert path. A message
   here is a batch or chunk that exhausted every retry — the artifact that replaces "one
   ERROR line among many".
4. **Deploy-role check** (`fieldsight-org-api-new-route-iam-trap`): the new queue and
   `lambda:PutFunctionEventInvokeConfig`-equivalent CFN actions must be creatable by the
   pipeline's deploy role — check before merging, not after the rollback.

**Verified against live state, not the deploy record:**
`aws lambda get-function-concurrency` shows the reserve;
`aws lambda get-function-event-invoke-config` shows destination + retry settings;
`aws sqs get-queue-attributes` on the new queue; and
`simulate-principal-policy` on the **function role** for `sqs:SendMessage` to the queue ARN
(destinations publish with the function's execution role — an unsent OnFailure message is
this whole phase silently dead, the `guard-caught-it-is-not-it-works` shape). Then force
one failure on TEST (temporarily rename the EL key env on a scratch invoke, or replay one
chunk with the key unset) and watch the message actually arrive — positive evidence the
destination fires, not just that it is configured.

---

### Phase 5 — a retryable answer raises; a malformed one still returns 200 [needs Phase 4 deployed first; CHANGES TEST AND PROD BEHAVIOUR]

**RED first**, `tests/unit/test_elevenlabs_utils.py` and
`tests/unit/test_lambda_transcribe*.py` (extend):

1. `test_exhausted_retryable_statuses_raise_a_typed_error` — 4× HTTP 429 from the fake
   pool manager raises `ProviderRetryableError` (new, in `elevenlabs_utils`), while 4×
   HTTP 400 (non-keyterms) raises plain `RuntimeError`. Fails now: both paths raise
   `RuntimeError`.
2. `test_a_retryable_provider_error_fails_the_invocation` — the handler re-raises
   `ProviderRetryableError` out of `lambda_handler` (no `status: error`, no 200), so
   Lambda's async retry machinery sees a function error. Fails now: the per-record
   `except Exception` (`lambda_transcribe.py:549`) swallows it.
3. `test_a_permanent_provider_error_still_records_error_and_returns_200` — malformed
   request stays a recorded per-record error; re-invoking it forever would never succeed.
4. `test_a_multi_record_event_with_a_retryable_failure_raises_after_recording_the_rest` —
   S3 sends single-record events, but the loop must stay correct if that ever changes:
   the raise happens after other records' results are logged, and reprocessing is
   idempotent for them (`register_chunk` conditional put; EL rewrite of the same
   transcript key is a same-content overwrite).

**Change:** the typed exception; the per-record `except` narrows to let it escape. Prod
note: this changes prod's failure behaviour too (per-chunk EL errors currently die as
`status: error`) — that is the *point*: with Phase 4's destination in place, a prod 429
becomes two spaced retries and then an alarmed queue message instead of a silent hole.
In-handler backoff in `elevenlabs_utils` is left exactly as is (§0.6 — extending it holds
Lambdas open and re-raises the ceiling problem).

**Merge order hard rule:** never before Phase 4 is **deployed and its destination
verified** — raising without a destination trades one silent drop for another, three
attempts later.

---

### Phase 6 — the sweep that notices: sealed audio with no transcript is re-driven [needs Phase 4; CHANGES TEST ON MERGE; inert on prod]

This is the guarantee layer (§0.6.4) and the anticipated subsumption of the tail-seal
recovery decision's "sweep" candidate (`2026-08-13-tail-seal-recovery.md` — the corrected
design's claim-release half is already shipped in `batch_seal.seal_batch`; this phase does
not touch the finalize/hold decision, which stays open).

**RED first**, `tests/unit/test_batch_untranscribed_sweep.py` (new file):

1. `test_a_batch_wav_with_no_transcript_past_the_grace_is_redriven` — fake S3 holds a
   `_bn` WAV (age > `BATCH_REDRIVE_AFTER_SEC`, default 900) and no matching
   `transcripts/` object → exactly one copy-to-self, and a `redrive_count` attribute
   incremented on the batch's seal record.
2. `test_a_batch_with_a_transcript_is_left_alone` — the negative, driven through the same
   real key-derivation (`map_key_for_transcript`'s inverse shape — never a hand-built
   key, the `transcripts/` vs `audio_segments/` lesson).
3. `test_the_redrive_is_bounded_and_the_exhaustion_is_loud` — at
   `BATCH_REDRIVE_MAX_ATTEMPTS` (default 3) the sweep stops, logs ERROR with the batch key
   and attempt count, and never copies again (no loop — the spec's stated requirement).
4. `test_a_young_batch_is_not_redriven` — age below the grace is untouched (the transcript
   may simply not have landed yet).
5. `test_batching_off_means_the_sweep_does_nothing` — `BATCH_TRANSCRIPTION=false` returns
   before any S3 call (prod inertness, pinned — the R6 lesson from the tail-seal review).
6. `test_the_sweep_logs_a_zero_count_when_everything_has_a_transcript` — the
   positive-evidence line (`batch: redrive sweep, candidates=0`), because a guard that
   speaks only on failure cannot be told apart from one that never ran.

**Change:** a `_redrive_untranscribed_batches(session_id)` pass in the finalize sweep's
per-tick loop (it already iterates sessions every minute and already imports the S3
client), scoped to sessions with batch ledger rows, driven by an S3 list of `_bn` WAVs
against `transcripts/` heads. Attempt count lives on the seal record (`redrive_count`),
so it survives Lambda restarts. Two new env knobs as **template Parameters**, not code
constants (`BATCH_REDRIVE_AFTER_SEC`, `BATCH_REDRIVE_MAX_ATTEMPTS`) — a dial reachable
only by editing code is not a dial, and remember the unwired-toggle trap: wire
template → function env → code read in one commit, and Phase 7 reads the deployed env to
prove all three segments exist.

**IAM:** the sweep role needs `s3:ListBucket` (prefix-scoped), `s3:GetObject` and
`s3:PutObject` on `audio_segments/*` for the copy-to-self, and HeadObject (=GetObject) on
`transcripts/*`. Verified by `simulate-principal-policy` against the **deployed** role
after deploy — the missing-ListBucket 403-as-404 trap means an unlisted prefix looks like
"nothing to re-drive" with zero errors, so the simulation is mandatory, not hygiene.

---

### Phase 7 — the 153-chunk replay is the acceptance test [no code; gates the "done" claim]

Precondition: Phases 1–6 merged and deployed to TEST; live env read back
(`get-function-configuration` on `fieldsight-test-transcribe` and the finalize sweep) shows
`BATCH_TRANSCRIPTION=true`, the reserve, the invoke config, and both new redrive knobs —
against the deployed stack, never the template or the workflow file.

**Before the replay**, compute the expected shape offline: run `plan_buckets` over the 153
prod filenames (a 20-line script against the local checkout — the filenames alone carry
everything the planner may use, which is the entire point of this design). Call the result
`E` (expected batch WAV count; for this session's 13:25–14:36 span it must land in
**37–42**; the greedy measurement said 39, the grid pays ±1–2 at boundaries). Write `E`
down before pressing go.

**Run:** copy the same 153 chunks from the prod bucket into TEST `users/` as before (same
mechanism, all at once — the burst is the test).

**The session passes when every one of these holds, measured, not eyeballed:**

1. **Batch WAV count == `E` exactly** (not 123; not `E±1` — the planner ran offline on the
   same inputs, so any difference is a determinism failure by definition).
2. `scripts/verify_batch_session.py` (extended with the chars-per-minute bound, §0.7):
   zero "chunk in more than one batch", zero "sealed but never transcribed", zero
   unconsumed registered members, and normalized transcript volume for the session under
   **90,000 characters** (the 4×-inflated replay measured 241,056 for 71 minutes; clean
   speech at this density is ~60 k).
3. **Every surviving chunk transcribed exactly once**: for each of the 153 (minus
   VAD-dropped), it appears in exactly one batch map **or** has exactly one per-chunk
   transcript (a straggler/singleton redrive) — and the count of redrives is itself
   reported and must be **< 10** (a redrive storm means the straggler rule is doing the
   planner's job).
4. **Zero permanently failed transcriptions**: the OnFailure queue is empty at +30 min;
   every `ElevenLabs STT error HTTP 429` line in the logs (if any) is followed by a later
   `completed` for the same key; `batch: redrive sweep` lines show `candidates=0` by the
   final tick.
5. **Peak concurrency == the reserve**, read from CloudWatch `ConcurrentExecutions` /
   `Throttles` on the function (not 141) — and remember throttled invocations produce no
   logs, so the Throttles metric is the witness, never log volume.
6. **Zero manual interventions** — the 27-by-hand recovery class must not recur; if any
   hand copy is needed, the run FAILS regardless of the other numbers.
7. Extraction sanity unchanged from the first replay: topics/speakers present, and the
   final extraction ran once against the deduplicated set.

**Standing step, recorded as the spec demands:** this replay (any real ≥100-chunk session,
burst-copied) is a required gate before `BATCH_TRANSCRIPTION` is ever proposed for prod —
it goes in the prod-promotion runbook, not in anyone's memory.

---

## Standing constraints

- `tests/unit/test_vad_tuning_rationale.py` stays green and unedited; VAD
  threshold/merge/`DROP_SILENT_CHUNKS`/normalisation/mics are measured dead ends — if a
  phase seems to need one, stop.
- Windows worktree: single-line Edit anchors; never `git add -A`; never a whole-file
  rewrite of any `src/` module.
- Superseded tests (the greedy-anchor pin, the `pending_windows` suite) are deleted by
  name, in the same commit as the code they pinned, with the new pins already green.
- Phase 5 never merges before Phase 4 is deployed **and its destination proven to fire**.
- No phase changes prod configuration except Phase 4's ceiling/destination, which is named
  as a prod change in its own section and deploys inert-by-default nowhere — it is real
  plumbing and gets watched like one.
