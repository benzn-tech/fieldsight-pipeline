# The final extraction pass cannot notice it was overtaken

**Date:** 2026-08-08
**Status:** Diagnosis complete, design proposed.
**Supersedes:** the P1c framing in `2026-08-08-audio-and-attribution-roadmap.md`, which
said the final pass "ran before the tail was transcribed" and that "the
overtake-and-rerun mechanism did not fire". Both are wrong. The mechanism fired; the
transcripts were all on disk 34 seconds before the final pass wrote.

## What actually happened

Session `sid61be49d563524f51b17c54c67733b08c` (Ben_UCPK2, 2026-08-07, prod).

| time (NZ) | event |
|---|---|
| 14:17:46 | first transcript written |
| ~15:33:5x | a **final** pass is triggered; it lists the session and sees **95** transcripts |
| 15:33:55.634 | a **live** pass logs `overtook an early final pass -- requested a re-run` |
| 15:34:23 – 15:35:48 | transcripts `c0130`–`c0150` land — **21 more** |
| **15:35:48** | **last transcript written** |
| **15:36:22** | the final pass **writes**, covering **95** sources, `c0000`–`c0129` |

Verified from the artifact itself, not inferred:

```
tier: final | extracted_at: 2026-08-07T03:36:22Z | sources: 95 | speaker_count: 4 | topics: 9
chunk range: 0 -> 129 | distinct: 95
transcripts on disk for this session: 151 (c0000 -> c0150), last written 15:35:48
```

So: the tail **was** transcribed, and it was transcribed **before** the authoritative
extraction was written. The final pass simply never looked again.

## Root cause

`extract_session` gathers its segment list **once, before** the LLM call, and the
coverage re-check afterwards is guarded by `if not final:`:

```python
overtook_final = False
if not final:
    current = read_existing_extraction(bucket, out_key)
    if not _supersedes(source_filenames, current):
        ...
    overtook_final = isinstance(current, dict) and current.get('tier') == TIER_FINAL
```

Two consequences compose into the data loss:

1. **The final pass never re-checks its own coverage.** It spends ~170 s in a thinking
   call, during which 21 transcripts landed, and then writes the answer to the question
   it asked three minutes ago.
2. **Only a live pass can detect the gap, and a live pass only fires on a new
   `transcripts/` write.** The final pass writes *after* the last transcript by
   construction — the finalize sweep is triggered by session close, which is downstream
   of the last chunk. So at the moment the narrow final lands, **there is no trigger
   left in the system**. The recovery path exists and is unreachable.

`_request_final_rerun`'s docstring reasons about termination — "coverage stops growing
when the transcripts stop arriving, so the live/final ping-pong terminates on its own" —
and that is true. What it misses is that the loop terminates **one step too early**: the
last iteration is a final pass whose view is stale, and nothing runs after it.

This is BUG-43's shape once more, in the mirror. That fix removed "discard the expensive
result if the premise changed". This is "keep the expensive result but never re-examine
the premise". Both leave the authoritative record wrong; this one silently.

## What it cost

The last ten minutes of a 78-minute session are absent from the authoritative record,
including the discussion of steel and stair reinforcement. Nothing anywhere says so: the
artifact has 9 topics and reads exactly like a complete one.

Related but separate, visible in the same logs: three transcripts (`c0004`, `c0005`,
`c0064`) were dropped as `unnormalizable`. That is a different silent loss and is out of
scope here — noted so it is not forgotten.

## Design

**Give the final pass the same re-check the live pass has, and let it ask for one more
run of itself.**

**Write the extraction first**, then re-list the session's transcripts. If the set is now
strictly wider than what this pass gathered, emit an `extraction_requests/` artifact for
another final pass. Never discard the paid-for result (BUG-43 rule 2).

Two details that look like implementation trivia and are not:

- **Re-list strictly AFTER the write, not before.** Re-listing first reopens the window it
  is meant to close: a transcript landing between the re-list and the `put_object` is
  caught by neither this pass nor a live pass that already did its own write-time re-read.
  Writing first makes the argument airtight instead of probabilistic — anything landing
  before the re-list is caught here, anything after it triggers a live pass that now reads
  the published narrow final, overtakes it, and re-requests through the path that already
  exists.

- **Compare S3 keys to S3 keys** — the fresh listing against the `keys` gathered at entry,
  **not** against `source_filenames`. `assemble_deduped_turns` drops corrupt and
  unnormalizable segments, and this very session had three (`c0004`, `c0005`, `c0064`).
  Comparing against `source_filenames` would see those three as "new" on every round, so
  **every session containing one unnormalizable transcript would burn the entire
  generation budget on identical re-runs** — a fix that manufactures the cost it was
  written to avoid.

Termination is the same argument that already holds for live: coverage only grows while
transcripts are still arriving, and transcripts stop. The added run costs one extra LLM
call per session, and only for sessions where transcripts landed during the final call.

### Why not the alternatives

- **Delay the final pass** (wait N seconds after session close). Picks a number that is
  wrong for both a fast queue and a backed-up one, and adds latency to the common case to
  fix the rare one. The email-timeliness spec exists precisely to keep this short.
- **Have the finalize sweep wait for transcript count == chunk count.** The counts do not
  have to match: silent chunks are dropped by design (`DROP_SILENT_CHUNKS`), and three
  transcripts were unnormalizable in this very session. There is no reliable expected
  total to wait for.
- **Let a live pass overwrite a final** — already the behaviour, and already insufficient:
  it needs a trigger that no longer exists.
- **Loop inside the same final invocation** rather than emitting an artifact. Worse: a
  600 s timeout against ~170 s per thinking call means the chain can die mid-way with
  nothing durable written. The artifact re-request makes every round independently
  durable.

`extraction_requests/` becomes deliberately self-retriggering on the final path, which
contradicts the module docstring's claim that nothing can re-trigger itself (BUG-13). That
comment has to be rewritten rather than quietly falsified: the loop is intentional and
bounded by strict coverage growth plus the generation cap.

### Guard rails

- Bound the re-request. A malfunctioning transcriber that rewrites keys forever must not
  produce an unbounded chain of final passes. Carry a generation counter in the request
  artifact and stop after a small number of rounds, logging when the bound is hit.
- Record coverage in the artifact so a truncated authoritative extraction is visible
  without reading logs — this pairs with the `transcript_stats` field added for the
  prompt-truncation fix.
- The re-request must be best-effort and never fail an extraction that already succeeded,
  matching `_request_final_rerun`.

## Verification

- Unit: a final pass whose segment set grew during the call writes AND requests a re-run;
  one whose set did not grow writes and requests nothing; the generation bound stops the
  chain; a failing re-request does not fail the extraction.
- Real: re-run the 2026-08-07 session on test and confirm the published extraction reaches
  `c0150`.
