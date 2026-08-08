# Manual meeting merge — bundle sessions after the fact, and think harder

**Status:** design, not yet built
**Date:** 2026-08-08

## What is being asked for

Back at the office, pick several recordings that were actually one meeting,
bundle them, and have the system redo the extraction properly — a slower, more
careful LLM pass over the combined material — with the result appearing in the
web app.

Three things in one sentence, and they are worth separating because only one of
them is new:

1. **form a group by hand** — new
2. **merge and re-extract from it** — already built (Phase C)
3. **show the better result** — already built (the timeline union)

## The central decision: reuse the group, do not build a second path

Phase C already merges several recordings into one record. It has a
`session_group` table, a claim/re-arm lifecycle, a merge prompt that presents
each device as a **parallel source** rather than concatenating them, member
topic replacement so the merged record supersedes the parts, `Updated:` emails to
everyone involved, and a timeline that unions "my topics" with "groups I was in".

The only thing it does not have is a way to form a group other than scanning a
QR code at the time.

**So this feature is a second door into an existing room.** A manual merge
creates a `session_group` row the same way a scan does, and everything
downstream is untouched. Building a parallel "manual merge" path would mean two
implementations of merging, which would drift, and the merged-record semantics —
which topics get superseded, who gets emailed, what the timeline shows — are the
hard part and are already settled.

This also means the feature inherits Phase C's known gap: there is no
"merging in progress" indicator in the web app. For a QR merge that gap is
tolerable because the merge happens minutes after a meeting nobody is watching.
**For a manual merge it is not**: the user presses a button and watches. The
indicator is therefore *in scope here* even though it was missed in Phase C.

## Which sessions may be bundled

The picker already exists in a related form: `assign_blocks` groups a day's
sessions into blocks by a 15-minute inactivity gap for the export flow, and its
docstring already states the right principle — *"a wrong merge costs the user one
untick, never a silently wrong scope"*.

Constraints on a manual group, each with a reason:

- **same company** — the tenant guard `/open` already applies; a cross-tenant
  merge is a data leak, not a mistake
- **the caller must be able to see every session selected** — otherwise the
  merge is a way to read someone else's recording by bundling it with your own
- **not already merged into a different group** — a session belongs to one
  meeting; re-merging is "unmerge, then merge", not an overlay
- **no date restriction** — deliberately. Sessions from two days *can* be one
  meeting (a site visit spanning midnight, a device 12 hours out). Phase C's
  `group_span_ok` exists precisely because time cannot be trusted here, and a
  manual merge has something better than a heuristic: a person said so

The lead of a manual group is the earliest session by `opened_at`, matching the
QR case where the lead is the device that started first.

## Thinking harder is a tier, not a new mode

`extract_group` already runs with `enable_thinking=True` — the merged extraction
is the final tier by construction. So "redo it with deep thinking" is what a
merge already does; there is no new mode to build.

What *is* new: a manual merge may target sessions whose solo extractions ran in
the cheap live tier and were never re-run. The merge replaces them, so the deep
pass is what the user ends up with. That is the feature working, not a special
case.

**Measured, so nobody has to guess at the cost:** a thinking call on
`qwen3.7-max` with no `max_tokens` returned 18,744 completion tokens in 347
seconds against `LLM_HTTP_TIMEOUT: 540`. A manual merge of several long sessions
is the largest prompt this system builds. The button must therefore be
asynchronous and say so — which is the same requirement as the progress
indicator above, arrived at from the cost side.

## Shape

- `POST /sessions/merge` with a list of session ids. Validates the constraints
  above, writes the `session_group` row with the earliest as lead, and returns
  immediately with the group id.
- The existing sweep picks it up. **No new merge trigger** — the standing scan
  already claims due groups, and adding a second trigger would mean two things
  can claim the same group.
- `/live-items` (or the timeline payload) exposes the group's merge state, so the
  web app can show "this meeting is being merged; a fuller record will replace
  this shortly". `session_group` already carries `merged_at` and `merge_result`;
  nothing new needs storing.

### Undo

A merge replaces the member topics with the merged record. Without an undo, a
mis-selected session is unrecoverable through the UI, and the recovery is a
manual database edit.

The merged artifact records its member keys, and the member extractions are
still in S3 — the topics were deleted from Aurora, not the source. So undo is
"drop the group row and re-run the member extractions from their artifacts",
which the existing idempotent path already supports. **In scope**, because a
destructive action a user can trigger from a button needs a way back.

## Failure behaviour

| case | behaviour |
|---|---|
| a selected session is still recording | rejected with the reason — merging a live session would produce a record that is immediately stale |
| a selected session has no transcript yet | allowed; the merge waits, as the QR path does |
| the merge produces nothing usable | `merge_result = 'empty'`, members are **not** deleted, the UI says so — never silently destroy the parts to produce nothing |
| the LLM call times out | the group re-arms and the sweep retries; the members stay intact until a merge succeeds |
| two users merge overlapping sets at once | the claim is already a CAS; the second sees the session is in a group and is rejected |

## Scope

**In:** the merge endpoint and its guards, the progress indicator (including the
Phase C gap it inherits), undo, and the picker UI.

**Out:** automatic suggestion of what to merge (a "these look like one meeting"
heuristic — the whole point is that the human knows better); merging across
companies; changing the merge prompt; changing what the email contains.

## Dependencies

Phase C is on prod but **inert** (`ENABLE_GROUP_MERGE=false`), and its Task 10 —
two real devices, one scan, one meeting — has never been run. **This feature
must not ship before that.** It would be the first real exercise of a merge path
whose only evidence today is a synthetic smoke test, and it would exercise it
through a button a user presses on data they care about.
