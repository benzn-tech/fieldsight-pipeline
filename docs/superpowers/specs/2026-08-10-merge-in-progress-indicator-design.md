# Say that the meeting is still being merged

**Status:** design v2. v1 was reviewed and had four blocking defects; **two of
them were bugs in the merge itself**, not in the spec, and are now fixed
(PR #346, PR for the empty-merge delete). What changed is at the bottom.
**Date:** 2026-08-10

## Why this exists, and why it is late

The user asked for it when the multi-device merge was first described:

> 在 AI 合并的同时，在我的网页上面要写清楚今天的会议，就是有一个窗口吧，小窗口就行了，
> 就说会议还在合并处理，更完整的会议报告会在之后更新。

**It was not carried into the Phase C spec, so none of its ten tasks covered it.**
An omission, not a deferral — a deferral would appear in that spec's
out-of-scope list beside the acoustic-fingerprint fallback, and it does not.

## What goes wrong without it

A merge replaces each member's own topics with one merged record. The two happen
in a single transaction, so there is no window where both or neither is visible —
the page simply *changes*:

1. your own session's topics, from your own recording
2. the merge commits
3. they are gone, replaced by a record covering everyone

With no indicator that reads as **content disappearing and being rewritten by
something you did not ask for**. The likely reaction is to distrust the record,
which costs far more than the feature is worth.

For the QR path the gap is tolerable — the merge happens minutes after a meeting
with nobody watching. **For the manual merge it is not**: the user presses a
button and watches. That is why this lands before manual merge.

## Which state to read, and why not the obvious one

v1 read `merged_at` / `merge_result` as a 2×2. The real lifecycle has more
states than that, and reading it that way produced a panel that flickers.

**The signal is `merge_count`, not `merged_at`.**

| condition | meaning | shown |
|---|---|---|
| `merge_count = 0`, no result | never started — the group has not settled, or nobody recorded, or the flag is off | **nothing** |
| `merge_count > 0`, `merge_result IS NULL` | **in flight, including between retries** | the panel |
| `merge_result = 'merged'` | done | nothing; the merged record is simply there |
| `merge_result` = `failed` / `rejected` / `empty` | it will not happen | the not-merged line |

`merged_at` is the wrong field because it is **cleared on every retry**. Recovery
re-arms a failed attempt below the cap, so a `merged_at`-driven panel would go
*merging → nothing → merging* — the exact "the message is noise" behaviour this
feature exists to avoid. `merge_count` only ever increments, so once a merge has
genuinely begun the panel stays up until the group reaches a terminal state.

**Queued groups show nothing, deliberately.** Rows accumulate that will never
merge: a group whose members never recorded fails `list_due`'s
`segment_count > 0` test, and `session_group.ensure_row` runs in **org-api**,
which does not read `ENABLE_GROUP_MERGE` — so with the flag off every joiner
still creates a row. All of these sit at `merge_count = 0` forever. Saying
"merging" about them would be false and permanent.

### The one state this cannot rescue

Turning `ENABLE_GROUP_MERGE` off *while a group is claimed* freezes it at
`merge_count > 0, result NULL` — the sweep and the recovery are both behind that
flag, so nothing will ever move it. The panel would then say "merging" forever,
truthfully and uselessly.

This spec does **not** add a timeout for that. A second timeout would give two
components different opinions about the same group, and the panel's job is to
report state rather than adjudicate it. It is an operational hazard — do not
disable the flag mid-merge; if you must, clear the claimed rows — and it is
written here so the next person meets it as a documented edge rather than a
mystery.

## Who sees it

v1 used `groups_for_user_on_date`, which matches on **membership**. That is the
wrong audience and it fails the motivating case: `/live-items` serves everyone
within `_allowed_site_ids` reach, so a PM or admin looking at the site's day sees
the members' topics change and would get **no panel** — and the person pressing
a manual-merge button is far more likely to be that manager than a device-
carrying member.

**Rule: the indicator goes to whoever can see the topics that are about to
change.** That is site reach, the same set `/live-items` already uses. A group is
relevant to a date if any of its members' sessions belong to a site the caller
can see.

**No `memberCount`.** v1 included it; it is the one field that would leak
something a viewer does not already have — how many people were in a meeting
they were not part of — and the copy does not need it. State and group id are
enough.

## Shape

`/live-items` gains, per date:

```
"merges": [ { "groupId": "...", "state": "merging" } ]
```

Present only when there is something to say — a day with no in-flight or failed
group keeps a **byte-identical** response. Most traffic here has no part in this
feature, the same rule `groupEnded` already follows on the upload path.

**Endpoint, pinned:** `/live-items`, and the `/timeline` shim if the customer
site is on `FS_TIMELINE_SOURCE=aurora`. v1 said "whatever the timeline shim
serves", which is how a feature ships to nobody.

## The wording is the feature

One job: make the replacement **expected** rather than alarming. Three things and
no more — what is happening, what you are looking at now, that it will change:

> **This meeting is still being merged.** You are seeing your own recording. A
> fuller record covering everyone will replace it shortly.

A spinner alone does not do this. It says "wait" without saying for what, and it
does not explain why the content afterwards differs from the content before.

**No percentage, no ETA.** A merge is one LLM call, measured at 347 seconds for a
large prompt (recorded in the claim-provenance spec), and nothing the client
knows predicts it. A bar stalled at 80% is worse than no bar.

### The not-merged line

> **These recordings were not merged into one record.**

v1 said "each person's own record is below", which the writer did not guarantee:
an empty merge used to delete the members' topics anyway. That is fixed, so the
promise would now hold — but the copy stays free of it, because a sentence about
what is on screen is only as reliable as the code that put it there, and this one
does not need to make the claim.

`empty` (nothing usable was recorded) shares this line rather than getting its
own. Telling someone who recorded nothing that something *failed* is worse than
telling them it was not merged, which is true either way.

The line is permanent for that date. Nothing later moves a terminal result.

## Failure behaviour

| case | behaviour |
|---|---|
| the group lookup raises | omit `merges` and serve the items. A missing panel costs an explanation; a 500 costs the page |
| a merge running implausibly long | still "merging". The panel reports state; the stuck-group recovery is what moves it |
| flag off | no group is ever claimed, so `merge_count` stays 0 and nothing is shown — see the frozen-mid-merge edge above |

## Scope

**In:** the `merges` field on both surfaces, the `merge_count`-driven state rule,
site-reach audience, the two lines of copy.

**Out:** progress or ETA; push/websocket delivery — the page polls and a merge
takes minutes; changes to merging itself.

That last exclusion is now honest. v1 excluded the writer by fiat while relying
on guarantees it did not make; the two defects that created have since been
fixed in the writer, so the exclusion no longer hides anything.

## Verification

The states cannot be observed without a real group, so this rides on Phase C
Task 10 (two devices, one meeting).

What is provable before then: a fabricated `session_group` row in each state —
**including `merge_count > 0` with `merged_at` NULL, the between-retries state
that v1's rule got wrong** — produces the right rendering; a day with no group
produces a **serialized-body-identical** response (not a dict comparison); and a
viewer with site reach but no membership *does* get the panel, which is the
audience v1 excluded.

## What v1 got wrong

Four items. Two were mine to fix in the spec; **two were bugs in the merge that
the spec had assumed away**:

1. **The state table was not exhaustive, and one extra state was permanent.**
   `rearm` cleared `merged_at` and left `merge_result`, so any post-merge re-arm
   produced a group invisible to both scans — while the late member's own topics
   had already been suppressed. Not a stalled merge: lost content. Fixed in
   PR #346.
2. **An empty merge deleted the records it was replacing.** The member delete ran
   for every group artifact while the merged rows were written only if there were
   any. Fixed alongside.
3. **"The flag off means no group rows are ever created" was false.** `ensure_row`
   lives in org-api, which does not read that flag. I asserted it without
   checking which module the function was in.
4. **The audience was wrong**, and wrong specifically for the case the spec named
   as its priority.

The pattern in 3 and 4 is the same one this project keeps producing: **"we
already have a mechanism for that" treated as verification.** It is a
hypothesis. The failure branches are where it breaks, and so is the question of
who the mechanism actually serves.
