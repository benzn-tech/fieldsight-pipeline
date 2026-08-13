# Batching by wall clock, not by surviving chunk count

**Status:** proposed
**Supersedes the batching rule in:** `docs/superpowers/specs/2026-08-11-batched-transcription.md` §3
**Prod impact:** none. `BATCH_TRANSCRIPTION=false` on prod; this changes TEST only.

---

## 1. The rule that shipped, and why it is wrong

A batch is currently **four consecutive chunk indices**. `batch_stitch.plan_batches` splits the
run at every gap, and a chunk that VAD judged silent never produces an `audio_segments/`
object, so it *is* a gap.

The hard target was never four chunks. It was **two minutes of wall clock, or the end of the
session** — and if VAD removes one of the four 30-second chunks inside that window, the
remaining three should still go as one request. A window with nothing in it should cost
nothing.

What happens instead, for `c4, c5(silent), c6, c7`:

```
[4]     -> a one-chunk "batch": sealed only after the 150 s deadline
[6,7]   -> a two-chunk batch:   sealed only after the 150 s deadline
```

Two requests instead of one, a map and a seal written for a batch of one, and **150 seconds
of latency added for no saving at all**.

## 2. How often — measured, not assumed

Every chunk-session in the lake (2026-08-13), comparing the device's own uploads under
`users/` against what survived VAD under `audio_segments/`:

```
sessions with >=4 uploaded chunks        35
  with >=1 interior chunk dropped        13   (37%)
  shattered into >1 short run            11   (31%)
```

The two worst:

```
sid c8c54d6e   46 uploaded, 38 interior dropped   ->  batches [1,1,1,2,1,1]
sid 5b373d67  385 uploaded, 238 interior dropped  ->  58 batches [4,1,4,2,3,1,1,4,4,1,3,...]
```

## 3. What the change is actually worth

Simulated over the same 35 sessions. **The cost argument does not survive contact with the
data and must not be used to justify this:**

| rule | transcription requests |
|---|---|
| no batching (one per chunk) | 1719 |
| current: 4 consecutive indices | 486 |
| fixed 2-minute window | 480 |
| **greedy 2-minute window** | **470** |

3%. The saving was already taken by batching at all.

What does change materially is the shape:

| batch size | current | greedy window |
|---|---|---|
| 1 chunk | **50** | **26** |
| 2 | 27 | 22 |
| 3 | 21 | 39 |
| 4 | 388 | 383 |

A one-chunk batch is a pure loss — 150 s of added latency, a map, a seal, and a request that
per-chunk transcription would have made anyway. They halve.

**The real justification is speaker continuity.** The only benefit batching has ever been
measured to deliver is a smaller speaker namespace (33 labels -> 9 on the same material; see
`fieldsight-batching-measured`). A conversation interrupted by one silent chunk is currently
split into two transcription requests, so the same person gets an unrelated label on each
side of a 30-second pause. That is precisely the thing batching exists to prevent, and the
current rule reintroduces it in 37% of sessions.

Write this down because the numbers invite the wrong summary: **this change is a quality
change with a rounding-error cost benefit, not a cost change.**

## 4. The rule

A batch opens at the first surviving chunk not already in a batch, and takes every surviving
chunk whose **base time** is within `BATCH_WINDOW_SEC` (default 120) of it. Greedy, anchored
on the batch's own first member — not on a grid aligned to the session start, which would cut
dense stretches at the boundary (480 vs 470 requests above, and it can split a run of four
that a grid boundary happens to straddle).

* A window containing no surviving chunk produces **no batch and no request**.
* A gap inside the window does **not** end the batch.
* At session close, whatever is open is sealed regardless of the window (deadline 0 — this
  already exists, `lambda_finalize_claim._seal_tail_batches`).
* `BATCH_MAX_CHUNKS` stays as a **safety cap**, not as the rule. Nothing should reach it while
  chunks are 30 s, but a device that ever emits shorter chunks must not build an unbounded
  request.

### Late uploads

Sealing the moment the window closes would permanently exclude a chunk that was merely slow —
uploads arrive out of order and can be hours late, and **a sealed batch is never reopened**.
So a batch seals when `now >= window_end + BATCH_SEAL_GRACE_SEC`, or at session close. The
grace period is the existing `BATCH_SEAL_DEADLINE_SEC`, renamed for what it now means.

This is the one place lateness is decided, and it stays that way.

## 5. The part that can silently go wrong

Batching across a gap makes filename arithmetic wrong for everything after the gap, by 30
seconds per skipped chunk. Today that is safe only because a batch never spans a gap.

`normalize_transcript()` computes `base_time + word.start`. Four call sites consume it:

| caller | reads the batch map? |
|---|---|
| `lambda_extract_session` | **yes** — `_rebase_batch_turns` |
| `lambda_ask_agent` | no |
| `lambda_ingest` | no |
| `lambda_org_api` (transcript viewer) | no |

The map itself needs **no change**: every member already carries its own absolute base time,
so a gap is represented correctly by construction. What is missing is the other three callers
reading it.

`_rebase_batch_turns` moves out of `lambda_extract_session` into a shared module and is
applied at all four sites. Each new site needs the IAM to read `audio_segments/*` — the
omission that has now silently broken this feature three times (`guard-caught-it-is-not-it-works`),
so each one is verified with `simulate-principal-policy` against the deployed role, not by
reading the template.

**Failure mode to design against:** a missed call site does not raise. It renders times that
are quietly 30 seconds early. The test that catches it must drive the real seal and the real
reader against one recording S3 double — the shape that caught the `transcripts/` vs
`audio_segments/` prefix mismatch — not a mock of the map at an asserted key.

## 6. The risk this accepts

A batch that bridges a gap splices audio that is **not contiguous in time**. The ASR hears a
30-second silence as no pause at all and may bridge it with a fabricated clause, which is the
same failure family as sending VAD-zero audio to the transcriber (10.7% fabricated words,
`fieldsight-asr-hallucination-vad-gating`).

Bounded, not eliminated: the window is 2 minutes, so the largest bridgeable gap is under 2
minutes. Padding the gap with silence to keep the timeline exact is **rejected** — it feeds
the transcriber exactly the material that causes the fabrication.

Verification: on a TEST session with a bridged gap, read the turns either side of the splice
and check the text is not a sentence that runs through it.

## 7. Out of scope

* The VAD threshold, the merge rules, `DROP_SILENT_CHUNKS`, normalisation order. All four are
  measured dead ends and changing them turns `tests/unit/test_vad_tuning_rationale.py` red.
* Turning batching on for prod. Separate decision, after this lands and runs on TEST.
