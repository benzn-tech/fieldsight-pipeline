# Manual meeting merge — bundle sessions after the fact, and think harder

**Status:** design v3 — reviewed twice. v1 had six blocking defects, two of which
inverted decisions it had argued for; v2 fixed those and introduced two more, both
from the same minted-id change. What changed at each round is at the bottom.
**Date:** 2026-08-08

## What is being asked for

Back at the office, pick several recordings that were actually one meeting,
bundle them, and have the system redo the extraction properly — a slower, more
careful pass over the combined material — with the result appearing in the web
app.

Three things, and only one is new:

1. **form a group by hand** — new
2. **merge and re-extract from it** — built (Phase C)
3. **show the better result** — built (the timeline union)

## Reuse the downstream. Do not reuse the lifecycle.

v1 said "a second door into the same room" and meant it as *change nothing*.
That was too broad. The correct split:

**Reuse, unchanged:** `extract_group`'s parallel-source merge, member topic
supersession, `Updated:` emails, the timeline union, the claim/re-arm/recovery
lifecycle. This is the hard, settled part and there must be exactly one of it.

**Do not reuse:** how a group is *identified* and which *guards* apply. Every
invariant in `session_group` encodes device-lifecycle semantics, and a manual
group violates several of them by design.

### The group id must be minted, not borrowed

In the QR design, `group_id` **is the lead device's session id** — deliberately,
so membership is derivable without the lead having to send anything
(`meeting_session.py:58-68`).

If a manual group reused its earliest session's id, and that session had ever led
a QR group — even one that ended `rejected` or `empty` — then `ensure_row`'s
`ON CONFLICT (group_id) DO NOTHING` (`session_group.py:26-30`) attaches silently
to the terminated row. Its `merge_result` is non-NULL, `list_due` never selects
it, and **the merge simply never happens, with nothing logged.**

A manual group mints a fresh id. Every member — including the earliest — gets
`group_id` set explicitly, so `list_group_members`' `group_id = X OR session_id
= X` still returns exactly the right set; the `OR` arm just matches nothing.

#### Minting breaks one thing, and it has to be replaced

`_site_from_group_lead` (`lambda_item_writer.py:135-157`) resolves the merged
artifact's site by looking up `meeting_session.get(conn, group_id)` — which only
works because a QR group id *is* a session id. A minted id has no such row, so
that rung returns None.

That rung is not decorative: the other rungs miss `grp` bases by construction
(its own docstring, `:541-552`). `site_for_day` may cover a worker-led merge, but
an **admin- or gm-led one resolves no site at all and the write is skipped
entirely** (`:569-573`) — before member deletion, so nothing is lost, but the
merge silently never publishes, burns its attempts through the stuck-group
recovery, and ends `failed`.

So `session_group` gains a `site_id`, written at formation from the members'
own `meeting_session.site_id` — which is the authority for site attribution
anyway (BUG-41: the App's `recordings.site_id` outranks every fallback). The
item-writer rung reads that column when present and falls back to the lead
lookup for QR groups, so both origins resolve a site by the same rule and
neither depends on a coincidence of identifiers.

### The span guard must know which kind of group it is

v1 cited `group_span_ok` as *support* for allowing cross-day merges. It is the
opposite: `sweep_groups` applies it **unconditionally** and marks anything over
`GROUP_MAX_SPAN_SECONDS` (12 hours) as `rejected`
(`lambda_finalize_claim.py:264-274`).

So the cross-midnight and skewed-clock cases v1 named as the reason for having no
date restriction are precisely the ones the reused path would **guarantee** to
reject.

The guard exists because a device's clock cannot be trusted. A manual group has
something better: a person looked at the recordings and said so. So
`session_group` gains an `origin` column (`'qr'` | `'manual'`), and the span
guard applies to `'qr'` only. This is the one place a branch is correct rather
than a smell — the guard's own comment says its purpose is guessing, and manual
merges are not guessing.

## Which sessions may be bundled

**Eligibility, which v1 omitted entirely and which determines what the picker
greys out:** only recordings that have a `meeting_session` row. The whole group
machinery — membership, span, context resolution — hangs off that row, and
legacy / RealPTT / pre-chunk recordings do not have one. The endpoint takes
32-hex device session ids, *not* the extraction `session_base` the export picker
uses (`lambda_org_api.py:975-982` is explicit that these are different things).

Then:

- **same company** — a cross-tenant merge is a data leak, not a mistake
- **the caller can see every session selected** — otherwise merging is a way to
  read someone else's recording by bundling it with your own
- **not already in another group** — and this needs **two** tests, not one. A
  member carries `group_id`, but a QR **lead carries NULL** by design
  (`meeting_session.py:58-72`) — the group id *is* its session id. So
  `group_id IS NULL` alone lets a lead be manually grouped a second time.
  Membership then matches in both groups (via the `session_id = X` arm), both
  merges claim it, both delete its topics, and two merged records overlap.
  Eligibility must also require
  `NOT EXISTS (SELECT 1 FROM session_group WHERE group_id = <candidate>)`.
- **at least one member with `segment_count > 0`** — not a nicety. `list_due`
  requires it (`session_group.py:75-77`), so a group of members that all
  pre-date per-chunk touch is **never selected**: no merge, no error, and a
  progress indicator that spins forever. Reusing the standing scan means
  inheriting its preconditions as eligibility rules.
- **not still recording** — merging a live session produces a record that is
  stale on arrival
- **no date restriction** — now actually true, because of the `origin` branch
  above

### Forming the group must be atomic

v1 claimed "the claim is already a CAS". Wrong layer: `session_group.claim`
(`session_group.py:92-109`) is the merge-time CAS *within* a group. Two users
selecting overlapping sets is a check-then-write race at *group formation*, and
would produce two groups splitting the members.

The formation is therefore one statement whose rowcount is the guard:

```sql
UPDATE meeting_session m SET group_id = %s
 WHERE m.session_id = ANY(%s)
   AND m.group_id IS NULL                       -- not already a member
   AND NOT EXISTS (SELECT 1 FROM session_group g   -- and not already a LEAD,
                    WHERE g.group_id = m.session_id)  -- which carries NULL
```

Anything less than the full count means someone else took one first; roll back
and tell the user which. **Both conditions are load-bearing** — the second is
the QR-lead case from the eligibility list, and without it the atomicity is
only apparent.

Note this is the first code that sets `group_id` on an already-closed session.
`ensure_open` COALESCEs and never clears (`meeting_session.py:51`); undo has to
clear it, which makes undo the first violation of that written invariant. It
must be a separate, deliberate repository function, not a relaxation of
`ensure_open`.

## Thinking harder is already what a merge does

`extract_group` runs with `enable_thinking=True`. There is no new mode. What the
user gains is that sessions whose solo extraction only ever ran the cheap live
tier get replaced by a deep pass.

**Measured:** a thinking call on `qwen3.7-max` returned 18,744 completion tokens
in **347 seconds** against `LLM_HTTP_TIMEOUT: 540`. A manual merge is the largest
prompt this system builds, and the margin is under 2×. So:

- the endpoint is asynchronous and says so
- **the number of members is capped**, with the cap chosen from a measured
  prompt-size-to-latency curve rather than picked — an uncapped button is a
  button that produces a timeout
- one user pressing it repeatedly must not exhaust Lambda concurrency; the
  account limit is now 1000, but `extract-session` is the longest-running
  function in it (BUG-43's arithmetic: occupancy = arrival rate × duration)

## The prompt is in scope, and v1 was wrong to exclude it

`build_group_prompt` (`lambda_extract_session.py:1135-1156`) hard-codes that the
sources are *"SEPARATE recordings of the SAME meeting, made at the same time by
different people"*, stamps a single date from `members[0]`, and instructs the
model not to report the same discussion once per device.

The most likely manual use — one person's recording split into several sessions
by stop/restart, which is exactly what `assign_blocks` groups for the export
picker — is **sequential**, not parallel. Told they are simultaneous, the model
is being asked to deduplicate material that is not duplicated. And on a cross-day
merge the stamped date is simply false.

So the prompt takes a `relationship` parameter: `parallel` (devices) or
`sequential` (one recorder, resumed). The QR path passes `parallel` and its text
is unchanged.

## Failure behaviour

The v1 table described two behaviours the code does not have. Corrected:

| case | behaviour |
|---|---|
| the merge LLM call fails or times out | `extract_group` returns `None` and writes nothing (`lambda_extract_session.py:1203-1214`). It does **not** re-arm itself — item-writer is the only caller of `rearm`, and it needs the artifact that was never produced. **The group-level recovery added in PR #311** re-queues it after `STUCK_MERGE_SECONDS`, bounded by `GROUP_MERGE_CAP`, then marks it `failed`. Without that fix this state was permanent and silent, and this feature's progress indicator would have spun forever. |
| the merge produces nothing usable *after* the claim | same path — no artifact. The `'empty'` result (`lambda_finalize_claim.py:284`) covers only the *pre-claim* no-context case, which is a different thing. |
| the merge succeeded but was never marked | recovery marks it `merged` **without re-running it**. This was not a corner case: `mark_result` ran on a closed connection (PR #313), so EVERY successful merge had this signature, and a recovery that only knew "dead or alive" would have re-merged and re-emailed all of them. |
| members are deleted but the merged record is never written | cannot happen: `_delete_member_topics` runs in item-writer, i.e. only once an artifact exists |
| two users merge overlapping sets | the formation rowcount guard above rejects the second |
| a selected session has no transcript yet | allowed; the merge waits, as the QR path does |

## Undo, honestly

A merge replaces member topics with the merged record. A destructive action
reachable from a button needs a way back — but v1 promised more than is
deliverable, and the shortfall has to be stated in the UI, not buried.

**What comes back:** the extraction *content*. Member artifacts are still in S3;
only the Aurora topics were deleted (`lambda_item_writer.py:591`). Re-running
them reproduces the topics.

**What does not:** their identity. Re-inserted topics get **new UUIDs**, so
anything a person attached to the old ones — content corrections, action-item
ticks, recurring-item threads, findings impact links, redactions — is gone and
cannot be restored by re-running anything.

Two consequences:

- undo must **also delete the merged topics and their `grp{id}` artifact**, which
  v1 omitted; otherwise undo leaves merged *and* solo records side by side,
  producing exactly the duplication merge exists to remove
- the confirmation before merging says which of the selected sessions carry
  human-edited state, because that is the state that will not survive a later
  undo

The alternative — snapshotting each member's topic subtree before deleting it —
is a real option and a larger one. It is deferred, and this is the reason the
warning exists.

`report_chunks.topic_id` also points at the deleted topics on both the merge and
the undo path (BUG-39's family: orphaned chunk topic ids are how search silently
returned nothing for two weeks). Re-indexing is part of both, not an afterthought.

## Scope

**In:** the merge endpoint with the eligibility and atomicity rules; the `origin`
and `site_id` columns and the span-guard branch; a minted group id; the
`relationship` prompt parameter; the progress indicator (inherited from Phase C's gap and not tolerable
here, because the user is watching); undo including merged-topic deletion and
re-indexing; the member cap; the picker UI.

**Out:** suggesting what to merge — the premise is that the human knows better;
cross-company merges; snapshot-based undo; changing the email contents.

## Dependencies

Phase C is on prod but **inert**, and its Task 10 — two real devices, one scan,
one meeting — has never been run. **This must not ship before that.** It would
otherwise be the first real exercise of the merge path, reached through a button
a user presses on data they care about. PR #311 removed one silent-stall from
that path; Task 10 is what would find the next one.

## What v1 got wrong

- cited `group_span_ok` as supporting cross-day merges when it **guarantees their
  rejection** — the citation was backwards
- "the claim is already a CAS" — real mechanism, wrong layer, and it does not
  protect what it was claimed to protect
- reusing the lead's session id as the group id — a silent no-op whenever that
  id had led a group before
- two failure-table rows describing behaviour the code does not have
- excluding the merge prompt from scope, when its "same time, different people"
  framing is false for the most likely use
- undo described as supported by "the existing idempotent path", when the path
  restores content but not the identity everything user-created hangs off

The pattern across four of the six: **v1 treated "we already have a mechanism for
that" as verification.** It is a hypothesis, and the failure branches are where it
usually breaks.

## What v2 got wrong

Both new defects came from the **same** change — minting the group id — and
neither was visible from the change itself:

- `_site_from_group_lead` only works because a QR group id *is* a session id.
  Minting silently removed the merged artifact's only site source for
  admin-led merges, and the symptom would have been a merge that never
  publishes rather than an error.
- excluding sessions "already in a group" via `group_id IS NULL` misses QR
  **leads**, which carry NULL by design — the one row where the invariant this
  design leans on is deliberately inverted.

Plus one inherited: reusing `list_due` means inheriting its `segment_count > 0`
precondition as an eligibility rule, or the group is never selected at all.

The pattern, and it is the same one as v1 in a different disguise: **a change
that is locally correct can invalidate a distant assumption that was never
written down as a dependency.** Both here were found by asking what else in the
codebase assumed `group_id == session_id`, which is a question worth asking of
any identifier this design touches.
