# One meeting, one record

**Status:** design v5. Reviewed four times; every round found something a
previous version had asserted instead of checked. **Not implemented, and not
ready to implement** — the three items under "Before this can be planned" are
decisions, not details.
**Date:** 2026-08-11

## What is wrong

The same recording is summarised by an LLM **twice**, by two prompts that have
never been compared, and the two results go to two different audiences.

```
transcript ─┬─→ lambda_extract_session               → Aurora → timeline (web)
            └─→ lambda_session_finalize
                  └─ _complete_summary (:128) → rolling_summary.summarize_turns
                                              → the confirmation email
```

**Measured, 2026-08-10, one recording:** timeline 7 action items, email 6. The
missing one: `Renew subcontractor access cards | Ben | This week`. Phil's due
read `Before work resumes` on the web and was empty in the email.

The email is what the customer acts on; the timeline is what they audit
against. When they disagree, an item missing from one is missing *silently* —
no error, no count, nothing that fails.

Either prompt could be the better one and the problem would be identical. The
defect is the **fork**, and every future prompt improvement has to be made twice
or the gap widens.

## The design

**The email becomes a consequence of the final extraction, not a chain beside
it.** One transcript, one LLM pass, one set of actions, two renderings.

```
close → finalize sweep: CAS-claim, stamp claimed_at, request FINAL extraction
                              ↓                       (no email enqueued)
        lambda_extract_session (final tier)
                              ↓
        lambda_item_writer  → Aurora topics                     → timeline
                            → finalize request, kind=final      → email
                              ↓
        lambda_session_finalize → email (todos as handed, no re-summary)

        …and if that never happens: the sweep's timeout arm sends the old way.
```

`_complete_summary` — the send worker's own re-summarisation — becomes the
**fallback only**.

## What four reviews killed

Each of these was in a version of this spec, asserted with confidence, and
false. They are kept because the pattern matters more than any one of them:
**"we already have a mechanism for that" is a hypothesis.**

| version | the claim | what the code said |
|---|---|---|
| v1 | "a live extraction is no staler than today's email" | `_complete_summary` re-gathers *everything* at send time; a live artifact misses the tail, which is where actions get assigned |
| v1 | "the corrected-email channel is already in production" | it carried no recipient, so every one was silently skipped — it had plausibly never sent (fixed since, PR #363) |
| v1 | "item-writer already reads the previous artifact" | it does not, and could not: live and final deliberately collide at one key |
| v2 | the flag gates two functions | it must gate three, or claim-time *and* item-writer both enqueue → two emails |
| v3 | "emit on the first final" | `generation` cannot express that — an overtaking rerun legitimately arrives as generation 0 again |
| v3 | "the worker already has GetObject on results" | that grant belongs to **FinalizeClaimFunction**; the send worker has PutObject only |
| v4 | "the stability check already exists — `_rerun_if_the_session_grew`" | that function compares **gathered keys**, and its own docstring explains why comparing `source_transcripts` is a trap |

## 1. The flag gates three functions

| function | with the flag ON |
|---|---|
| **finalize-claim** | claims, stamps `claimed_at`, requests the extraction — **stops enqueueing the email**; owns the timeout arm |
| **item-writer** | on a *complete* final artifact for a session still owed an email, enqueues `kind=final` |
| **send worker** | honours `kind`; consults the ledger |

Leaving finalize-claim on the old behaviour means both it and item-writer write
a request → two S3 events → two emails.

## 2. `kind` decides whether the worker re-summarises

| `kind` | source | worker behaviour |
|---|---|---|
| absent | today's claim-time enqueue | re-summarise (unchanged — requests already in S3 keep working) |
| `final` | item-writer | **use the todos as handed** |
| `fallback` | the timeout arm | re-summarise, exactly as today |
| `updated` | group merge | use as handed (already implemented) |

A single global flag cannot express this. Without `kind` the fallback would ship
the *rolling* todos — stale, missing the tail — in precisely the failure case
the timeout exists to cover, which is worse than today.

## 3. The gate is coverage stability, and it needs a new artifact field

A final-tier artifact is **not** automatically complete. On the deliberate-End
path the grace is zero, so the final's gather runs about a minute after the last
chunk while ASR is still trailing — roughly when `_complete_summary` gathers
today. The code says so itself (*"transcripts keep landing after it"*; *"21
transcripts landed during that call and the pass published a record ending ten
minutes early"*), which is why the overtake-and-rerun machinery exists at all.

So the gate is not the tier. It is: **was the session still growing when this
artifact was written?**

**It cannot be computed from `source_transcripts`.** That list holds the
segments that survived `assemble_session_turns`, which drops corrupt,
unnormalisable and **empty** ones — and on hardware whose recordings sit at a
−36 dBFS median, empty transcripts are routine. Comparing it against a fresh
listing would be false *forever* on any session with one silent chunk:
item-writer would never emit, every email would fall to the timeout, and the
fork this design exists to remove would return **silently, on exactly the
degraded-audio sessions that need the record most**.
`_rerun_if_the_session_grew`'s docstring says this in so many words; v4 cited
that function as proof while proposing the comparison it warns against.

**The final artifact must therefore record the gathered key set** — the same
`keys` the rerun check uses, before parsing drops anything — as a new field, and
item-writer compares keys to keys. Two gates, answering different questions:

| gate | question |
|---|---|
| session status is `finalizing` | is an email still owed? |
| `gathered_keys` == a fresh listing | was the session complete when this was written? |

**item-writer cannot do that listing today.** Its policy grants `GetObject` on
`extractions/*` and config, and `ListBucket` scoped to `extractions/*` and
`users/*` — nothing on `transcripts/*`. Without the grant the listing raises
inside the paginator and the gate fails **closed**: no error surfaced, every
email quietly deferred to the timeout. That is the PR#288 shape, and it belongs
in the task list rather than a footnote.

## 4. The result file is the send ledger

The worker sends and only then writes `session_finalize_results/{sid}.json`;
reconcile moves the session to `sent` a tick later. So a timeout at N and a
final at N+ε both send.

**The worker reads the result before sending and skips when one records
`status == "sent"`.** An `error` result is not a send and must not block a retry.

**New IAM, and the read must fail open.** The send worker has `PutObject` on
that prefix but not `GetObject`, and no `ListBucket` for it — so a missing key
answers **403, not 404**. A read failure must be read as *no ledger, send
anyway*; the opposite reading turns a permissions slip into "every first email
silently skipped".

**`kind=updated` is exempt from the ledger.** The worker rewrites the result key
to `{sid}-updated` only at send time while the request carries the plain sid, so
a naive check reads `results/{sid}.json` — which after the normal
solo-then-merge sequence always records a send, dropping every updated email.
That is PR #363's defect one layer up. And a *legitimate* second updated email
exists: the re-merge path re-enqueues when a late device brings new content, and
the recovery path explicitly budgets for it.

**Residual race, stated not hidden:** two invocations seconds apart can both
read "no ledger". Closing it needs a CAS in Aurora, which the send worker cannot
reach (BUG-36). Moving the send in-VPC costs more than a rare duplicate.

## 5. The timeout ages against a server-stamped `claimed_at`

Neither existing column works:

- **`updated_at`** — `touch_segment` bumps it with no status guard, so a crashed
  device flushing its upload backlog (precisely the fallback's scenario) resets
  the clock and defers the timeout indefinitely.
- **`closed_at`** — on the explicit-close path it is `endedAt` from the request
  body: the **device's** clock, unvalidated, and **NULL when absent**. This
  codebase documents ROMs twelve hours out. `list_due_finalize` still claims
  `intent='end'` sessions regardless, so a session can be `finalizing` with
  `closed_at` NULL — and then the timeout never fires and **the customer gets no
  email at all**.

`claim_finalize` today sets `status` and `updated_at`. It gains `claimed_at =
now()` — server clock, written exactly once, at claim. The timeout is *claimed,
not sent, `claimed_at` older than N*.

**Keyed on status, not on artifact presence**, because an extraction can land
and be *refused*: the I-4 guard skips an artifact arriving after the nightly
report was ingested, and with `ENABLE_GROUP_MERGE` on a member's solo final can
be suppressed. "Wait for an artifact" hangs forever in both.

**N must clear the whole chain**, not one pass: up to three thinking-mode
generations at 170–347 s each, plus trailing ASR. It is sized from the observed
distribution of end-to-end final-chain latency, not from one measurement.

## 6. Distinct request keys, or drop the logging promise

The timeout arm and item-writer would write the **same** key,
`session_finalize_requests/{sid}.json`. One PUT can overwrite the other's
unprocessed body — harmless for duplicates, but it means "a log line naming
which path produced this email" can name the wrong one. Either give the fallback
its own key suffix, or do not promise the log line.

## The cost

**The confirmation email moves from ~91 s after stop to roughly 6 minutes**, and
on any failure path to N.

Proposed for acceptance: the 1–2 minute figure exists so the recorder can
correct things *while still on site*, and six minutes does not break that — a
wrong action list does. An email that disagrees with the web record is a defect
the customer finds later, in front of someone else. And this removes an LLM call
per session rather than adding one.

## Failure behaviour

| case | covered by |
|---|---|
| final extraction fails or never runs | timeout |
| artifact lands and item-writer refuses it (I-4, group suppression) | timeout |
| session never stabilises within the rerun budget | timeout |
| identity-bridge miss — zero writes, no email enqueued | timeout |
| no recipient on file | unchanged: marked failed at claim, extraction still requested |

The timeout arm **runs regardless of the flag**. Otherwise: claim with the flag
on, roll it off before the final lands, and the session sits `finalizing`
forever with no email — a stranding today's design cannot produce. A rollback
must not be able to lose a confirmation.

## Known limit

A later final generation still rewrites the timeline while the email is not
re-sent, so those sessions diverge again. Strictly better than today (where they
diverge always, by construction), and bounded to sessions still growing after
the gate first passed. Closing it means an `Updated:` email — out of scope until
the group path's updated email is proven end to end.

## Before this can be planned

Three items, each a decision rather than a detail:

1. **The new artifact field.** Recording `gathered_keys` changes the extraction
   artifact's contract, which item-writer, the group merge and any future reader
   parse. It is additive and every consumer uses `.get()`, but it is a change to
   a document with several readers and should be agreed, not slipped in.
2. **`claimed_at`.** A migration on `meeting_session`, plus `claim_finalize`
   writing it and `list_finalizing` returning it.
3. **N.** Measured from the end-to-end final-chain latency distribution on test,
   not argued for. **Nothing in this spec is safe until that number exists** —
   too small and the timeout races every slow session, too large and a failed
   extraction costs the customer ten minutes of silence.

## Scope

**In:** the three-function flag; `kind`; `gathered_keys` and the stability gate;
item-writer's `transcripts/*` grant; the ledger and the send worker's two new
grants; `claimed_at` and the timeout arm; distinct request keys; a log line
naming the source of every email.

**Out:** changing either prompt; the email's layout; an `Updated:` email for
late finals; retiring the rolling summary (it still serves
`GET /sessions/{id}/rolling`, the mobile mid-meeting poll — **not**
`/live-items`, which reads Aurora directly); making the final extraction faster.

## The flag

`EMAIL_FROM_EXTRACTION`, three segments (repo variable → workflow
`--parameter-overrides` → template Parameter) reaching **all three** functions,
default false on prod, pinned by
`tests/unit/test_template_workflow_parameter_wiring.py`.
`FILTER_AUDIO_EVENT_TAGS` shipped with its middle segment missing and could only
ever hold its default.

## Verification

The 2026-08-10 divergence is the regression test: the email's action list equals
the timeline's, item for item. **Compare the due against `deadline_text`**, not
the resolved date — `_todos_from_topics` passes the raw deadline through while
the timeline stores the resolved value, so a naive comparison fails on
formatting while both are correct.

- **Unit:** a complete final artifact for a `finalizing` session enqueues
  `kind=final`; an artifact whose `gathered_keys` differ from a fresh listing
  enqueues nothing; a `sent` session enqueues nothing; the worker uses handed
  todos for `final` and re-summarises for `fallback`; a session with a `sent`
  ledger entry is not sent twice; an `error` entry does not block; the flag off
  leaves today's path byte-identical.
- **Integration (test):** one real recording; the *sent* email's todos equal
  `/live-items`. Requires the finalize result to record what it sent — today it
  records only `{status, sessionId, recipient}`.
- **Cannot be unit-tested:** the timeout (needs a deliberately failed
  extraction), and the residual double-send window.
