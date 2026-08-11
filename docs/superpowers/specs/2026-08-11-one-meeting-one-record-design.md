# One meeting, one record

**Status:** design v2. v1 was reviewed and had five blocking defects; **the
review killed its central argument**, and the design that survives is smaller
than the one it replaces. What changed is at the bottom.
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

`_complete_summary` re-gathers the very same turns (`gather_session_segments` +
`assemble_deduped_turns` — extract_session's own helpers) and asks a *different*
prompt for a *different* list of actions, at send time.

**Measured, 2026-08-10, one recording:**

| | timeline | email |
|---|---|---|
| action items | 7 | **6** |
| missing | — | `Renew subcontractor access cards \| Ben \| This week` |
| Phil's due | `Before work resumes` | **empty** |

**The cost is not "two summaries". It is that there is no record.** The email is
what the customer acts on; the timeline is what they audit against. When they
disagree, an item missing from one is missing *silently* — no error, no count,
nothing that fails.

Either prompt could be the better one and the problem would be identical. The
defect is the **fork**.

## The one thing v1 got wrong, and why it matters

v1 proposed that the send worker read whatever extraction was already published
at claim time, arguing it was no staler than today's email.

**That is false, and backwards.** `_complete_summary` runs *after* close +
grace and re-gathers **every** transcript — full coverage is the reason it
exists. A live extraction at claim time is missing the last throttle window
(`MIN_REEXTRACT_INTERVAL_S = 90`) plus the live pass's own LLM latency plus ASR
latency. **The tail of a meeting is where actions get assigned** ("so Ben,
you'll renew the cards"). v1 would have shipped a first email that
systematically drops the tail.

Latency was never the problem. The email already lands in 91 s and 5/5 were
delivered. Coverage is the property worth protecting, and v1 traded it away.

## The design

**The email becomes a consequence of the final extraction, not a chain beside
it.** One transcript, one LLM pass, one set of actions, two renderings of it.

```
close → finalize sweep: CAS-claim, request FINAL extraction
                              ↓
        lambda_extract_session (final tier, full coverage)
                              ↓
        lambda_item_writer  → Aurora topics   → timeline
                            → finalize request (recipient + todos from those topics)
                              ↓
        lambda_session_finalize → email
```

item-writer is the right place and it needs nothing new to do it: it is already
holding the artifact, already has a live Aurora connection to resolve the
recipient, already builds `_todos_from_topics`, and already writes
`session_finalize_requests/` for the merge path.

### What this deletes

v1 needed a persisted "what did we email last time" baseline, a diff, a second
`Updated:` email, a `tier == "final"` gate against live passes emailing every
90 s, and an IAM grant so the send worker could read `extractions/*`.

**None of that exists in v2.** There is one email, sent once, from the artifact
that also produced the timeline. Five of the review's findings were about
machinery this design does not have.

## The cost, stated plainly

**The confirmation email moves from ~91 s after stop to roughly 6 minutes.**

The final pass runs with thinking on and was measured at 347 s for a large
prompt. Nothing shortens that except turning thinking off, which would make the
timeline worse to make the email faster — the wrong trade for a record.

I am proposing we accept it. The reasoning:

- The promise the 1–2 minute figure came from (`2026-07-27-voice-timeliness`)
  exists so the recorder can correct things **while still on site**. Six minutes
  does not break that; a wrong action list does.
- An email that disagrees with the web record is a defect the customer finds
  *later*, in front of someone else. A five-minute-later email is a delay they
  never notice.
- It removes an LLM call per session rather than adding one.

**If that trade is refused**, the fallback position is v1's two-email shape, and
then all five of the review's findings come back and must be designed for. This
spec does not hide that door; it argues for not using it.

## Failure behaviour

The email must not become hostage to the extraction.

| case | behaviour |
|---|---|
| final extraction succeeds | email from its topics — the normal path |
| final extraction fails, or never lands | a sweep sends the email the OLD way (`_complete_summary`) after a timeout |
| extraction lands with zero topics | old way. An empty action list from a failed extraction must never read as "nothing was agreed" |
| no recipient on file | unchanged: session marked failed, extraction still requested (that content still belongs on the website) |

**The timeout is the whole safety property.** Without it, one failed extraction
means one recording that silently never gets confirmed — strictly worse than
today. It reuses the existing sweep, which already walks claimed sessions.

## A defect this uncovers, worth fixing regardless

`_enqueue_updated_emails` (`lambda_item_writer.py:274`) writes
`{kind, sessionId, groupId, summary, openTodos}` — **and no `recipient`**.
`process_finalize_request` skips any request whose recipient is empty
(`lambda_session_finalize.py:165`). So every `-updated` request is silently
skipped; the unit tests pass only because they inject a recipient by hand.

**The multi-device "everyone gets the same record" email has plausibly never
sent.** It is masked on prod by `ENABLE_GROUP_MERGE=false`, so nothing is
broken for customers today — but the merge feature is not finished, and it
reads as finished.

v2 fixes this incidentally: resolving the recipient in item-writer is the same
work the solo path now needs. **It should still be verified separately** — a
side effect of another change is not a test.

## Scope

**In:** item-writer enqueues the finalize request from a final-tier artifact,
with recipient resolved; the send worker uses the todos it is handed instead of
re-summarising; the timeout fallback; the recipient fix for `-updated`; a flag;
a log line naming which path produced each email.

**Out:** changing either prompt; the email's layout; retiring the rolling
summary (it still serves `GET /sessions/{id}/rolling`, the mobile mid-meeting
poll — **not** `/live-items`, which reads Aurora directly); making the final
extraction faster.

## Open questions for the plan

1. **Where the timeout lives.** The finalize sweep already walks claimed
   sessions; the cleanest form is "claimed, not sent, older than N minutes →
   send the old way". N must exceed the p99 of the final pass, and 347 s is one
   measurement, not a distribution. The plan should set N from the actual
   distribution of final-pass durations in the test logs, not by argument.
2. **Final passes rerun.** `_rerun_if_the_session_grew` allows up to
   `FINAL_RERUN_MAX_GENERATIONS = 3`. Emitting on every final generation would
   send up to three emails. Emit on the first final only, and let the timeout
   path be the one that reconciles a later, wider one — or decide explicitly
   that a later generation replaces nothing.
3. **The claim/version protocol.** `finalize_claim` currently CAS-claims *and*
   enqueues in one step. Moving the enqueue to item-writer separates them, so
   the plan must say what state the session sits in between claim and email, and
   what the sweep does if it dies in that gap.

## The flag

`EMAIL_FROM_EXTRACTION`, three segments (template Parameter → function env →
both workflows), default **false on prod**, and it must reach **both**
item-writer and the send worker. Half-wired — the worker changed but not the
writer — sends nothing at all.

`tests/unit/test_template_workflow_parameter_wiring.py` fails on an incomplete
wiring, which is why that test exists.

## Verification

The 2026-08-10 divergence is the regression test: same session, the email's
action list equals the timeline's, item for item, including the due that came
through empty.

- **Unit:** a final artifact produces todos identical to `_todos_from_topics`;
  a live artifact produces no email; zero topics falls back; no recipient marks
  failed and still requests extraction; the flag off leaves today's path
  byte-identical.
- **Integration (test env):** one real recording. Compare `/live-items` actions
  against the **sent** email's todos — which requires the finalize result to
  record what it sent, since today it records only `{status, sessionId,
  recipient}`.
- **What cannot be unit-tested:** the timeout. It needs a session whose final
  extraction is deliberately failed.

## What v1 got wrong

Five blocking items, and the first one invalidated the design:

1. **"The live extraction is no staler than today's email."** Backwards.
   `_complete_summary` has full coverage by construction; a live artifact does
   not, and the missing part is the tail, where the actions are.
2. **"The corrected-email channel is already in production."** It carries no
   recipient, so it is silently skipped. The precedent v1 leaned on has
   plausibly never fired.
3. **The send worker has no IAM to read `extractions/*`** — and with no
   `ListBucket`, a missing key answers 403, not 404, so a mis-wired flag would
   look enabled and always fall back, logging "no extraction published" when the
   truth was "denied".
4. **"item-writer already reads the previous artifact."** It does not, and it
   could not: live and final deliberately collide at one key, so the previous
   artifact is gone by the time the comparison would run.
5. **The solo `-updated` emission had no gates**, so live passes would have
   emailed every 90 seconds.

The pattern in 2 and 4 is this project's recurring one: **"we already have a
mechanism for that" treated as verification.** Both times the mechanism existed
and did not do what its name implied — one had never run, the other lived in a
different module from the one I named.
