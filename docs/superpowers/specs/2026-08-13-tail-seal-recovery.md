# A tail seal that fails is never retried

Status: **spec only — the design below the diagnosis was reviewed and rejected. Do not
implement it as written.** 2026-08-13.

The diagnosis (next two sections) was verified and stands. Everything from "Options"
onward was found broken by adversarial review; the corrected design is at the end, and
is deliberately **not implemented yet** — see "Why this is not being shipped tonight".

## The gap

`_seal_tail_batches` (`lambda_finalize_claim.py:548`) is called from exactly one place:
inside the sweep loop, immediately before `finalize_claim`
(`:603-604`). It catches every exception and returns:

```python
except Exception:
    logger.exception("batch: could not seal the tail of session %s — the final "
                     "extraction may be missing its last chunks", session_id)
```

`finalize_claim` then runs regardless and CAS-claims the session to `finalizing`.
`list_due_finalize` selects `status = 'pending_close'`, so the session is never returned
by the sweep again. **Nothing calls `_seal_tail_batches` for it a second time, ever.**

The 900-second stale-claim takeover in `batch_ledger.claim_seal` does not help. It only
fires when something calls `seal_ready_runs` again, and after finalize nothing does.

## It has already happened, twice, for two different reasons

Both on TEST, 2026-08-12, both discovered by reading logs rather than by anything
failing:

1. `AccessDenied` on `s3:GetObject` for the member chunk — the sweep had no grant.
   Session `7a112e50…` finalized with chunk `c0004`'s 30 seconds never transcribed.
   The only trace was one WARNING; the extraction, the email and the report all looked
   complete.
2. The same grant, erased again by an unrelated whole-file rewrite (#386) and restored
   in #391. A real 6-minute recording lost its last 19 seconds.

Both root causes are fixed. The **recovery gap is not**: any future failure of the seal —
a throttle, a timeout, a transient 5xx — costs a session's tail with the same silence.

## What is deliberately right about the current code

Two properties must survive any fix, because each was learned expensively:

- **The sweep must not die.** "A missing tail costs the end of one email; a sweep that
  dies costs every email after it." A raise here stops finalization for every other
  session in the tick.
- **Batching off must mean untouched.** `_seal_tail_batches` returns immediately when
  `BATCH_TRANSCRIPTION` is false, which is prod today. Any change must keep that exact
  short-circuit, or it becomes a prod behaviour change disguised as a batching fix.

## Options

**A. Refuse to finalize while the tail is unsealed.** On failure, skip `finalize_claim`
for that session this tick; it stays `pending_close` and the next sweep (every minute)
tries again. Unbounded, so a permanently failing seal means **the email is never sent at
all** — strictly worse than today's outcome, which is an email missing its last 30
seconds. Rejected on its own.

**B. Bounded retry, then proceed and say so.** As A, but only while the session's
`closed_at` is younger than a bound; past it, finalize anyway. Converts "silent
permanent loss" into "up to N minutes of retries, then an explicit record". Needs no new
column: `closed_at` is already on the row the sweep reads.

**C. Proceed immediately, but record the loss where it can be queried.** A metric or a
column, so "which sessions shipped without their tail" is answerable. Complementary to
B, not an alternative — it does not recover anything.

**Recommended: B, with the recording half of C.** The bound is what makes B safe: the
failure modes that actually occurred (a missing IAM grant) are permanent, and under
unbounded A those sessions would never email. Retrying for a bounded window costs
nothing when the seal succeeds — which is every case where batching is working — and
turns transient failures into non-events.

## Design

`_seal_tail_batches` returns `True` when the tail is sealed (including the vacuous case
where batching is off or there is nothing to seal) and `False` when it caught an
exception. The sweep:

```
sealed = _seal_tail_batches(session_id)
if not sealed and within_retry_window(row):
    continue                     # stays pending_close; next tick tries again
results.append(finalize_claim(...))
```

- **Retry window**: `now - closed_at < TAIL_SEAL_RETRY_SEC`, default **900 s** — the same
  number as `batch_ledger.SEAL_RETRY_SECONDS`, and deliberately so: the stale-claim
  takeover needs a second call to fire, and this is the mechanism that provides one. A
  shorter window would expire before a mid-seal claim becomes takeable.
- **A template Parameter**, not a code constant. The whole point of this session's other
  work is that a dial reachable only by editing code is not a dial.
- **When the window expires**, finalize proceeds and logs at ERROR with the session id
  and how long it retried, so the loss is attributable rather than inferred.
- **Batching off short-circuits before any of this** — unchanged.

## What this costs

The confirmation email is delayed by up to 15 minutes **for a session whose tail seal is
failing**. That is a real regression against the "email within 1–2 minutes of stopping"
requirement, and it applies only to sessions that would otherwise have shipped incomplete.
Sessions that seal normally — every session where batching works — are not delayed at
all, because the retry branch is only reached on an exception.

Worth stating plainly: this trades **timeliness in the failure case** for
**completeness**. If that trade is wrong for this product, the answer is C alone
(record the loss, ship on time), not a smaller retry window.

## Verification

Not by unit test alone, because the failure this fixes is an IAM/S3 failure the unit
tests mock away:

1. Unit: a failing seal leaves the session `pending_close` and does not call
   `finalize_claim`; an expired window finalizes anyway; batching off never calls the
   seal at all. Each verified to fail with its own branch removed.
2. On TEST, reproduce the original failure by removing the sweep's `audio_segments/*`
   grant, run a session to close, and confirm: the session stays `pending_close`, the
   sweep retries on the next tick, and restoring the grant lets it seal and finalize.
   That is the only test that exercises the real failure.

## Not in scope

- Recovering the tails already lost. Both known cases are on TEST with synthetic audio.
- Alarming on the ERROR line. Worth doing, but it belongs with the DashScope
  content-filter alarm as one piece of observability work rather than bolted here.

---

# Review findings — the design above is broken

Adversarial review, 2026-08-13. Every claim below was re-verified against the code
before being accepted; one earlier review in this session was wrong, so none of this is
taken on trust.

## R1 — the retry lasts exactly one tick, and the ERROR never fires

The failures that motivated this spec (`AccessDenied` on `s3:GetObject`) happen **after**
`claim_seal` has already written the claim: `batch_seal.py:86` claims, `:92` reads S3.
So:

- tick 1: claim written → S3 raises → caught → `False` → session held. An orphaned
  `sealing` claim is now in the ledger.
- tick 2: `claim_seal` sees a 60-second-old `sealing` claim, returns `None`,
  `seal_batch` returns `None`, `seal_ready_runs` returns `[]` — **with no exception**.

The design defines `sealed` as "no exception was caught", so tick 2 reports success and
finalize proceeds with the tail still unsealed. Net effect versus doing nothing: a
60-second delay and the same silent loss. Because finalize is reached via the success
branch, the ERROR line that was the entire point of option C never runs either.

`sealed` must mean **"no pending runs remain"**, not "nothing raised".

## R2 — the 900 s window is an off-by-one against `SEAL_RETRY_SECONDS`

Takeover becomes possible at `claim_time + 900` (`batch_ledger.py`, strict `<` on age).
The window closes at `closed_at + 900`, and `closed_at <= claim_time`. **The window
expires at or before the first instant a takeover could fire.** The spec argued the
coupling made the takeover reachable; it guarantees the opposite.

## R3 — idle-closed sessions get zero retries

`IDLE_CLOSE_SECONDS = SESSION_GAP_MINUTES * 60 = 900` (`lambda_finalize_claim.py:58`,
`session_scope.py:146`), and `infer_idle_closes` anchors `closed_at` at `last_activity`.
Every idle-inferred close therefore enters `pending_close` already ≥900 s old — outside
the window on its first tick. Those are the offline and crashed-device sessions, i.e.
the population most likely to have an unsealed tail.

## R4 — the row does not carry `closed_at`, and the value is a device clock

`list_due_finalize` selects only `session_id, version`
(`repositories/meeting_session.py:260`), so `within_retry_window(row)` would raise
`KeyError` — and an uncaught raise in the sweep loop kills the tick, violating this
spec's own first invariant.

Worse, `closed_at` for a deliberate End is the device's `endedAt`, unvalidated and
optional. BUG-37 documents device clocks 12 hours out: a fast clock holds the email for
half a day, a slow clock gives zero retries. Any bound must be anchored on a **server**
timestamp (`updated_at`, written by `mark_pending_close`) or an attempt counter.

## R5 — holding a session ships a partial merged report

`session_group.list_due` treats a member as settled once its activity is >900 s old
(`repositories/session_group.py:82-86`); a session held in `pending_close` is not
excluded. Today the final extraction is requested at close and has ~15 minutes to land
before the group settles. Under a 15-minute hold, the request goes out at
`closed_at + 900` while the group settles at `last_segment_at + 900` — the same tick or
earlier — so the merge builds from the member's **stale live extraction**, and the
member's later solo final is suppressed by group-supersede. Every member of that group
receives a merged report missing one member's close-out content. The spec's stated cost
of "one delayed email" understated this by a wide margin.

## R6 — the batching-off short-circuit is not "unchanged"

Today `_seal_tail_batches` returns `None` when `BATCH_TRANSCRIPTION` is false
(`lambda_finalize_claim.py:563-564`). The design needs it to return `True`. An
implementer who preserves that line literally, as the spec instructed, makes every prod
session (batching off) fall into the retry branch — which, combined with R4, crashes the
sweep on prod, where this feature is supposed to be inert. A test must pin
`_seal_tail_batches(...) is True` with batching off.

---

# Corrected design

**The load-bearing fix is not in the finalize path at all.** The defect is that a failed
seal strands a `sealing` claim which blocks every retry for 15 minutes. Fix that first:

1. **Release the claim on failure.** Wrap the post-claim work in `seal_batch` so an
   exception deletes the claim — conditional on it still being ours
   (`status='sealing' AND claimed_at = :mine`) — before re-raising. With this, a
   transient failure is retryable on the **next tick** instead of after 900 s, R2
   dissolves entirely, and any retry window can be short.

2. **Redefine `sealed`** as "no pending runs remain for this session", not "nothing
   raised" (R1).

3. **Retry from somewhere that already revisits the session.** Two candidates, and this
   is the open decision:
   - *Hold in `pending_close` for 2–3 ticks*, anchored on `updated_at` (server clock),
     NULL-safe, with `list_due_finalize` widened to select it. Keeps the first email
     complete, which this codebase has previously argued is the one that matters. Cost:
     a ~3-minute delay in the failure case, and the R5 group interaction shrinks but
     must still be checked.
   - *Re-attempt in `reconcile`*, which iterates `list_finalizing` every tick anyway. No
     email delay, no group interaction, no new state — but the email has already gone
     without the tail, and only the final extraction catches up via the grew-rerun.

   The trade is timeliness in the failure case against completeness of the first email.
   That is a product decision, not an implementation detail, and it should be made
   explicitly rather than by whoever writes the code first.

4. Pin batching-off returning `True` (R6), and keep the two invariants the original
   spec got right: the sweep must not die, and batching-off must remain inert.

## Why this is not being shipped tonight

The correct fix touches the finalize path — the code that decides whether every session's
confirmation email goes out — hours before that email is the thing being hand-tested in
the morning. Its correctness depends on a failure mode that unit tests mock away, so the
verification that matters is the TEST reproduction (revoke the sweep's
`audio_segments/*` grant, run a session to close, watch it retry and recover). That
reproduction has not been run.

The gap it closes is real but dormant: batching is off in prod, so `_seal_tail_batches`
returns immediately there and no prod session can hit this. Shipping an unverified change
to the email path to fix a defect that cannot currently fire in production is the wrong
trade on this particular night.

Ready to implement, in this order: claim release → TEST reproduction → decide 3 → the
rest.
