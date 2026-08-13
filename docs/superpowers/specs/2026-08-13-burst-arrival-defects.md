# Two defects that only appear when chunks arrive all at once

**Status:** proposed — found by replaying a real 71-minute prod session into TEST, 2026-08-13.
**Prod impact today:** none. `BATCH_TRANSCRIPTION=false` on prod. Both defects are in the
batching path.
**But not replay-only.** A device that has been offline reconnects and uploads its backlog in
one burst — the thundering herd this repo has already had to handle once. That produces the
same arrival pattern as the replay.

---

## How they were found

153 chunks of one real session (`sid15770a28…`, 13:25–14:36, UCPK2) were copied from the prod
bucket into TEST's `users/` prefix, which drives the whole chain: VAD → batching →
transcription → extraction → item-writer.

The chain completed and the extraction is real — 6 topics, 3 speakers, recognisable content.
Two things were wrong on the way, and neither would have surfaced on the 13-chunk sessions
this feature was tested with.

---

## Defect A — concurrent arrival shifts the window anchor, and the seal key cannot see it

**123 batch objects for 153 chunks. The correct number is 39.**

```
anchors: 0, 4, 5, 8, 12, 13, 16, 17, 18, 19, 20, 24, 27, 28, 29, 30, ...
sizes:   123 x _bn4        (not one short batch anywhere)
```

Every arrival calls `seal_ready_runs`, which lists the members registered *so far* and plans
greedy windows from the earliest unconsumed one. With 153 concurrent invocations every worker
sees a different snapshot, so every worker computes a different anchor:

* a worker that sees `{5,6,7,8}` anchors at 5 and claims `SEAL#0005` — granted;
* a worker that sees `{0…8}` anchors at 0 and 4, claims `SEAL#0004` — **also granted**, and
  that window contains 5, 6 and 7 again.

The claim is keyed on the window's **first index**, so it can only ever exclude a second
worker that computed the *same* anchor. Two different anchors covering the same chunks hold
two different keys and neither sees the other.

`mark_members_consumed` (the #418 fix) does not close this. It lands *after* both artifacts,
by which time the other workers have already computed their plans. It fixes the late-arrival
case — chunks minutes or hours apart — and cannot fix the simultaneous one.

### The design error, stated plainly

The 2026-08-13 spec measured two anchoring rules over every session in the lake:

```
fixed grid      480 transcription requests
greedy anchor   470
```

and chose greedy for the 2%. **The comparison never asked whether the anchor can be computed
identically by every concurrent worker**, which is the property the seal key depends on. A
greedy anchor is a function of "who is left", and under concurrency that is not a function at
all — it is whatever this worker happened to read.

### What it costs

Requests roughly tripled: 123 paid transcriptions instead of 39. In the extraction, the
duplicated audio is visible as **241,056 characters over 3,661 lines for 71 minutes** — about
four times what that much speech produces. The topics still came out because the model merged
the repetition, which is luck, not a property anyone designed.

### Direction

Anchor on **absolute wall clock**: `floor(base_time_epoch / BATCH_WINDOW_SEC)`. Every worker
computes the same bucket for the same chunk from the filename alone, with no snapshot involved
and nothing to race. Gaps are still bridged — a bucket takes whatever survives inside it.
The seal key becomes the bucket, not the first index.

Cost: the 2% measured above, plus one real edge — a conversation straddling a 2-minute
boundary is split where greedy would have kept it whole. **Determinism under concurrency is
worth more than 2%**, and this is the evidence for saying so.

---

## Defect B — a provider rate-limit answer loses the audio permanently

**27 of the 123 batches (≈54 minutes of audio) were never transcribed, with no alarm.**

```
ElevenLabs STT error HTTP 429:
  {"type":"rate_limit_error","code":"concurrent_limit_exceeded",
   "message":"maximum of 20 concurrent requests"}
```

Measured peak concurrency on `fieldsight-test-transcribe` during the replay: **141**. Neither
transcribe function has `ReservedConcurrentExecutions` set. The provider ceiling is 20.

What happens to a rejected batch:

1. the batch WAV exists on S3 and its map exists beside it;
2. `transcribe_segment` raises; the per-record `except Exception` records `status: error`
   and the handler returns **200**;
3. S3 → Lambda async invocation retries only on an *unhandled* function error, and there is
   no DLQ or `EventInvokeConfig` on this function;
4. the ledger says the batch is `sealed`, so no sweep ever re-plans it;
5. nothing else ever looks at a batch WAV that has no transcript.

The audio is gone. The extraction, the report and the email all render normally without it.
The only trace is one ERROR line among many. **These 27 were recovered by hand**, by copying
each batch WAV onto its own key at one every nine seconds.

This is the same family as the tail-seal gap (`2026-08-13-tail-seal-recovery.md`): a failure
that is caught, logged, and never revisited. It is worse than that one because it needs no
missing IAM grant and no unusual state — just a burst.

### Direction, in the order that matters

1. **A ceiling that cannot be exceeded.** `ReservedConcurrentExecutions` on the transcribe
   function, set below the provider's limit. This is the only mechanism that bounds
   concurrency before the request is made; everything else reacts after the rejection.
   The number must come from the provider's documented limit, not from a guess, and the two
   must be recorded together so a plan change is noticed.
2. **A rejected batch must be re-drivable.** The copy-to-self mechanism already exists
   (`bypass_singleton`) and is proven — it is what recovered these 27 by hand. What is
   missing is anything that *notices*. Candidates: retry in-handler with backoff (simple, but
   holds a Lambda open and re-raises the same ceiling problem), or a sweep that finds batch
   WAVs with no transcript and re-drives them (matches the existing sweep, no new mechanism,
   but needs a bound so it cannot loop).
3. **429 specifically must not be swallowed as `status: error`.** A rate limit is retryable
   and a malformed request is not; collapsing them into one outcome is what made this
   invisible. Raising on a retryable error would let the Lambda's own async retry work — that
   is the cheapest correct answer and should be evaluated first.

---

## What this says about the verification that came before

Every earlier check on this feature used a session of 13 chunks arriving one at a time. Both
defects need a burst, so no amount of that testing could have found either. The replay took
about ten minutes and found two.

**One real session replayed through the real chain is worth more than any number of
unit tests here**, and it should be a standing step before this feature goes near prod, not a
thing that happened once.

`scripts/verify_batch_session.py` caught both, but only because the "sealed but never
transcribed" and "chunk in more than one batch" checks had been added hours earlier — after a
review pointed out that the script could pass on a broken session.

---

## Out of scope

* The tail-seal recovery decision (`2026-08-13-tail-seal-recovery.md` step 3) — a separate
  open question, though a sweep that re-drives untranscribed batches may subsume it.
* Turning batching on for prod. Neither defect can fire there today, and neither should be
  fixed under time pressure because of that.
* The VAD threshold, merge rules, `DROP_SILENT_CHUNKS`, normalisation order, microphones —
  measured dead ends, unchanged.
