# Multi-Device Session Merge — Phase C (wiring) — Design

**Date:** 2026-08-08 · **Status:** DESIGN (v3 — two adversarial review rounds folded in)
**Builds on:** `specs/2026-08-04-multi-device-session-merge-design.md` (approved),
`plans/2026-08-04-multi-device-session-merge.md` (Phases A + B)
**Repos:** `fieldsight-pipeline` only. No mobile change.

---

## Why this exists

Phases A and B both shipped. The column, the repository functions, the
cross-company rejection, the `/open` groupId path, the QR scanner, the group
exit — all live. **The merge itself is wired to nothing.**

Three functions exist and are called by no production code:

| function | file | called by |
|---|---|---|
| `assemble_group_turns` | `lambda_extract_session.py:744` | tests only |
| `group_is_settled` | `repositories/meeting_session.py:80` | tests only |
| `group_span_ok` | `repositories/meeting_session.py:128` | **nothing at all** |

Not theoretical. On **2026-08-07 a real four-device group ran on prod**:

| session | role | segments | outcome |
|---|---|---|---|
| `61be49d5` | lead | 151 | its own report, its own email |
| `39ad6c92` | joiner | 129 | its own report, its own email |
| `126c149f` | joiner | 2 | its own report, its own email |
| `036cf2f1` | joiner | 4 | its own report, its own email |

One meeting, four partial records, four emails, no merge, and no signal anywhere
that the feature had not run.

---

## What is already decided

Carried from the approved Phase A/B spec; not reopened:

- group identity is the lead's `session_id`; joining is by QR scan
- merged content is stored **once**, presented per person
- transcripts are merged **by the extraction LLM**, not by an alignment
  algorithm — there is no shared clock (BUG-37 is a shipped case of a device's
  wall clock being 12 hours out)
- the first device to stop emails immediately; an `updated` email follows
- the bias is **under-merge, never over-merge**

Decided in this brainstorm:

- **On merge, the members' pre-merge topics are deleted**, not flagged. Reuses
  the existing `delete_topics_for_source` idempotent-overwrite path rather than
  adding a permanent `superseded_by_group` state to every read path. Recovery
  from a bad merge is re-extraction — the transcripts are never touched.
- **Every member gets identical content.** Scanning the code is the consent, and
  the product reason the feature exists is that the visiting inspector gets
  value from the recording too.

---

## v1 was wrong. What the review found

The first draft hung the merge check on a finalize event: *the sweep finalizes
the last member, then asks `group_is_settled`.* That is broken four ways, and
all four have the same root — **a group becomes mergeable at a moment when no
event is firing.**

1. **`group_is_settled` is false on the tick that finalizes the last member.**
   It counts a member unsettled when `status NOT IN ('sent','failed')` **and**
   its last activity is inside the grace (`meeting_session.py:91-101`). At claim
   time that member is `finalizing` and its last chunk is seconds old. The group
   settles about a minute later, when `reconcile` flips it to `sent` — with no
   event attached.
2. **`sweep()` only iterates `list_due_finalize` (`lambda_finalize_claim.py:237`).**
   Once every member is claimed there are no due sessions, so there is no later
   tick that would re-ask.
3. **The lead carries no `group_id` of its own** (by design — the group id *is*
   its session id). If the lead stops last, the finalizing row has `group_id =
   NULL` and there is nothing to pass to `group_is_settled` at all.
4. **The late-arrival re-merge had the same hole.** Clearing `group_merged_at`
   assumed "the next sweep re-merges", but a `sent` session never re-enters the
   due path: `touch_segment` only flips `pending_close` → `open`
   (`meeting_session.py:179`).

The corrected design replaces the event-hung check with a **standing scan**.
Everything else in this spec follows from that.

---

## Architecture

```
every sweep tick (~1 min, in-VPC), after the existing finalize + reconcile loops
  │
  ├─ list_due_group_merges(idle_grace)          ← NEW standing scan
  │     session_group rows where merge_result IS NULL   ← partial index, bounded
  │     AND some member has segment_count > 0
  │     AND group_is_settled(...)                ← now asked at a moment it can be true
  │
  └─ for each due group:
        group_span_ok(...)  AND  single-company  ← the two guards the parent spec
           │                                        called unconditional and that
           │                                        Phase A/B never wired
           ├─ fail → merge_result='rejected', log, leaves the scan
           └─ pass → claim_group_merge (CAS on merged_at — exactly once)
                     stamp merged_key, merge_count += 1
                     write extraction_requests/group-{lead}.json
                            { members: [{userFolder, date, sessionBase}, ...],
                              leadSessionId, groupId, mergedKey }
  ▼
extract-session (non-VPC)
  │  key starts with "group-" ──► assemble_group_turns over every member
  │  one LLM call, members as labelled PARALLEL sources
  │  write extractions/{leadFolder}/{date}/grp{groupId}.json     ← ITS OWN KEY
  │        + mergedMembers: [ each member's extraction_key ]
  ▼
item-writer (in-VPC)
  │  delete_topics_for_source(each key in mergedMembers)   ← the members' solo topics
  │  delete_topics_for_source(own grp key)                 ← existing idempotent path
  │  write ONE merged topic set, source_s3_key = the grp key
  │  write session_finalize_requests/{member}-updated.json × N, carrying the
  │        merged summary text so every member's email is byte-identical
  ▼
session-finalize (non-VPC, SES) — kind="updated", one email per member
```

### The merged artifact needs its own key

v1 wrote the merge to `extractions/{leadFolder}/{date}/sid{lead}.json` — **byte
identical to the lead's own final-pass key** (`extraction_key`,
`lambda_extract_session.py:787`). The final pass is unthrottled and does no
supersede read (`:903`, it writes blind), and `_rerun_if_the_session_grew` can
request another one later. So a lead-solo final landing after the merge would
overwrite it, item-writer would `delete_topics_for_source(lead key)` — removing
the **merged** topics — and write lead-solo topics instead. Every joiner's
content would be gone from Aurora, with the members' own topics already deleted.
Silent loss, strictly worse than doing nothing.

`grp{groupId}.json` collides with nothing. It also gives the merge a durable
identity, which the read path needs (below) and which makes a re-merge a plain
idempotent overwrite.

**It also breaks item-writer's site attribution, which must be fixed with it.**
`write_extraction_items` derives `session_base` from the key
(`lambda_item_writer.py:343`); for a `grp` base, `site_for_media` misses (filename
pattern), `_site_from_meeting_session` returns None (`:116` — `_device_session_id`
only recognises `sid` bases), and for an admin/gm lead with no recordings rows
that day `site_for_day` and `resolve_site` both miss too. The result is
`identity bridge miss … zero writes` (`:349`): the merge silently discarded after
the members' topics were deleted. One rung is added — for a `grp` base, resolve
the site from the lead's `meeting_session` row, company-checked exactly as
`_site_from_meeting_session` already does.

**Routing order matters.** Everything under `extraction_requests/` currently flows
into `parse_final_request` → solo extraction (`lambda_extract_session.py:1167`).
The `group-` test must come first, and a group request's `members[]` shape must
not be parseable as a valid solo request.

### Suppressing solo writes after a claim

Once `session_group.merged_at` is set, item-writer must not write solo topics for any
session in that group — otherwise the lead's late final, or a `_rerun_if_the_
session_grew` re-run, reintroduces exactly the duplicate the merge removed.

The test is **coverage, not timing**: if the arriving artifact's
`source_transcripts` are already inside the merged artifact's coverage, it has
nothing new to say — skip, log, do not re-arm. Only genuinely new transcripts
count as a late arrival. This is the same `_supersedes` idea already used for
live-vs-final, and it is what stops the cap being consumed by ordinary traffic.

### Late arrivals

A joiner records offline, the group settles without it, the merge runs, emails
go out. Hours later the device syncs.

Its transcripts land and produce an extraction whose coverage is **not** inside
the merged artifact. item-writer then clears `merged_at` (conditionally, `WHERE
merged_at IS NOT NULL`); the next sweep tick's standing scan picks the group up
again, re-merges, and sends a second `updated` email — now including the late
device. The standing scan is what makes this work at all; the v1 design had no
trigger to come back on.

**`merge_count` increments at claim, not at re-arm.** Two late members — or one
late member's live and final passes — landing close together would each clear and
increment, burning the cap twice for a single re-merge. Counting actual merges
instead makes the cap mean what it says.

To read the merged artifact's coverage, item-writer needs `s3:GetObject` on
`extractions/*`; it has `PutObject` there but the read is new. It is in the IAM
table below for the same reason everything else is.

**Capped at `merge_count >= 2`.** Past that, the late content is logged
(`group merge cap reached`) and written as solo topics, so it is never lost —
only its inclusion in the merged record is. Without a cap a device drip-feeding
chunks would re-merge and re-email all day.

### Why the sweep, and why item-writer sends the email

An in-VPC lambda cannot invoke another lambda (BUG-36: no NAT, the call
black-holes until timeout), so the S3 request artifact is the established
crossing — `extraction_requests/`, `session_finalize_requests/`,
`match_requests/`, `reindex_requests/` all do this.

The email must contain the merged record, so it cannot be sent before the merged
topics exist. The sweep runs before the LLM call; only the step that lands the
result knows it landed. item-writer is in-VPC and already writes S3 artifacts.

**The artifact carries the merged summary text.** N member requests would
otherwise mean N independent LLM calls and N *different* summaries, contradicting
"identical content" — and costing N calls for one meeting.

Two code changes make that true, because neither end supports it today:

1. **Nothing produces a session-level summary.** `EXTRACTION_SCHEMA`
   (`lambda_extract_session.py:982-1014`) has per-topic `summary` fields and no
   top-level prose. The group prompt and schema gain a top-level `summary` (and
   `open_todos`); item-writer copies them into the N request artifacts. It cannot
   compose one itself — it is in-VPC and cannot call an LLM (BUG-36).
2. **`lambda_session_finalize` would overwrite it.** `process_finalize_request`
   (`:158-167`) calls `_complete_summary`, which re-summarises **that member's own
   solo transcripts** and *prefers* the result over `artifact["summary"]`. So
   without a change the N updated emails would each carry a fresh per-device solo
   summary — exactly the outcome this design claims to avoid, at exactly the cost
   it claims to save. `kind == "updated"` must skip `_complete_summary` and use
   the carried text verbatim.

**The updated result must not collide with the solo one.** The worker writes
`session_finalize_results/{sessionId}.json`, and `reconcile` reads that key to
settle a claimed session. A member can be counted settled by quietness while
still `finalizing`, so an updated-email result could be read as the solo outcome.
Updated results are keyed `{sessionId}-updated.json`.

### Schema — a group row, not columns on the lead

v2's first draft put `group_merged_at` on the lead's `meeting_session` row. That
fails in three ways the review found, all from the same assumption:

- **The lead row may not exist.** The failure table itself admits the lead may
  never upload and its `/open` may never land. Then there is nothing to stamp and
  nothing to CAS, and the group is permanently unclaimable.
- **The scan cannot be bounded.** A lead carries no `group_id`
  (`meeting_session.py:58-77`), so a scan must enumerate `DISTINCT group_id` over
  `idx_meeting_session_group` (`migrations/0031:17`) and *then* join to the lead
  row to test `group_merged_at IS NULL`. The bounding predicate is on the joined
  row, not the indexed one — so the scan re-reads **every group ever created**,
  every minute, forever. Groups that settle with no content are never claimed and
  accumulate in that set permanently.
- **The merged key would have to be re-derived** by three independent consumers
  (the read union, item-writer's suppression check, ingest's defer test), each
  reproducing the same folder + NZ-date + lead-never-uploaded fallback. Any drift
  is a silent miss: suppression stops suppressing, defer stops deferring, the
  member sees an empty day.

One table fixes all three:

```sql
CREATE TABLE session_group (
  group_id       text PRIMARY KEY,          -- the lead's session_id
  company_id     uuid NOT NULL,
  merged_at      timestamptz,
  merge_count    int NOT NULL DEFAULT 0,
  merge_result   text,                      -- NULL | 'merged' | 'rejected' | 'empty'
  merged_key     text,                      -- the authoritative merged artifact key
  created_at     timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX idx_session_group_pending ON session_group (created_at)
  WHERE merge_result IS NULL;
```

Created on the first successful join (org-api already writes `group_id` there, so
this is the same transaction), which means it exists whether or not the lead ever
uploads. The scan reads the **partial index of unresolved groups only**, so a
resolved group leaves the candidate set for good and the set stays small. A
settled group with nothing in it terminates as `'empty'` rather than lingering.

`merged_key` is written at claim, so no consumer ever re-derives it.

**Not `group_ended_at`** — PR #276 already gave that a different meaning (the
lead stopped, refuse new members). A group is "ended" the moment the lead presses
stop, while joiners may still be uploading for hours; triggering on it would
merge half a meeting.

### Read path

Merged topics carry the lead's `user_id` and `site_id`, so a member whose own
topics were just deleted would see an empty day. v1 proposed unioning on the
lead's identity — wrong three ways against `list_topics_for_date`
(`repositories/topics.py:237-300`):

- a graded-role member has `author_ids` active (`:286`), which filters merged
  topics out;
- a member without membership on the lead's site is excluded by the site filter;
- adding the lead's `user_id` to `author_ids` would leak the lead's *other* solo
  topics that day to every member.

**The union key is the merged artifact's `source_s3_key`**, which identifies
exactly the merged rows and nothing else. It is **read from
`session_group.merged_key`, never re-derived** — three consumers need it (this
union, item-writer's suppression check, ingest's defer test) and any drift in
re-deriving the folder, the NZ date, or the lead-never-uploaded fallback is a
silent miss in whichever one drifted.

"Groups I was in on that date" is a lookup by `user_id` + date, so it needs an
index on `(user_id)` where `group_id IS NOT NULL` — the existing partial index on
`group_id` does not serve it.

### Tenant safety is NOT inherited

v1 claimed the union needs no widening because Phase A rejects cross-company
groups. **That is false.** The rejection (`lambda_org_api.py:823`) fires only
when the lead row exists at join time; an unknown lead is deliberately accepted
(the joiner can reach the backend first), and nothing re-validates when the lead
later materialises in another company. A cross-company group is representable.

So the company check is re-run **at merge claim** (reject, mark, never retry) and
the read union stays inside the caller's existing site ACL rather than bypassing
it. The parent spec already required this: *"the server must not rely on"* the
UI preventing it.

### IAM — both grants are new

Named explicitly because this is the failure mode the codebase has now hit three
times (BUG-43 lesson 3, and the extract-session `ListBucket` gap found tonight):
a function gains a new S3 read or write, the grant does not follow, the error is
swallowed, and the feature is silently inert.

| function | needs | why | today |
|---|---|---|---|
| `ItemWriterFunction` | `s3:PutObject` on `session_finalize_requests/*` | writes the N updated-email requests | has `match_requests/*` + `keyframe_requests/*` only (`template.yaml:2080`) |
| `ItemWriterFunction` | `s3:GetObject` **and** `s3:ListBucket` on `extractions/*` | reads the merged artifact's coverage to decide suppress-vs-re-arm | has `PutObject` there; the read is new |
| `SessionFinalizeFunction` | no new **grant** — but it does need the code change above, or it ignores the carried summary | keeps `extractions/*` off its role | has `session_finalize_requests/*` + `transcripts/*` (`:1866`) |

`ListBucket` is listed beside `GetObject` deliberately: without it S3 answers 403
rather than 404 for a key that does not exist, and a "has this group been merged
yet" read would report *denied* where it means *no*. That is the exact defect
found on `ExtractSessionFunction` on 2026-08-08 (PR #288), one prefix over.

Already covered, verified, no change: the sweep's `group-{lead}.json` under its
existing `extraction_requests/*` Put (`:1832`); extract-session reading other
members' transcripts (`:1550`) and listing them (`:1567`).

**Every grant to be confirmed with `simulate-principal-policy` after deploy**,
not read off the template.

### Interaction with the nightly report layer

Deleting a member's solo topics empties `extractions/{member}/{date}/` as far as
Aurora is concerned, so authority-flip's defer test goes false and the nightly
ingest writes **report-sourced** topics for that member from their solo
`daily_report.json` — the duplicates come back overnight. Conversely
item-writer's I-4 guard (`lambda_item_writer.py:297`) makes a merge landing
*after* the member's nightly report was ingested skip with zero writes.

**Resolution:** the merged artifact's presence must satisfy the defer test for
every member of the group, not just for the lead. The test is one expression —
`AUTHORITY_FLIP and has_topics_for_source_prefix(conn, f"extractions/{user_folder}/{date}/")`
(`lambda_ingest.py:477`). The lead is covered for free, since the grp key lives
under the lead's prefix. Members need an OR clause: their group memberships for
that NZ date → `session_group.merged_key` → topic existence.

**Small in lines, not small in risk.** It gates the nightly branch that otherwise
*deletes* the extraction prefix (`:494`) and writes report topics, for every user
every night, and it needs its own NZ-date derivation from `opened_at` — BUG-37
and BUG-19 territory. A false-positive defer silently drops a genuine
zero-extraction day's report topics. It gets its own integration test, not just
the overnight happy-path check in the live plan.

---

## Failure behaviour

Inherits the Phase A/B table. What Phase C adds:

| Case | Behaviour |
|---|---|
| Group spans two companies, or `group_span_ok` fails | Reject at claim, stamp `merge_result='rejected'`, leave the standing scan. Members keep their solo reports — today's behaviour. A span rejection is genuinely permanent (`opened_at` is server-side and immutable), but a **company mismatch can come from fixable data** — the BUG-41 residue is exactly such rows. Recovery is an operator clearing `merge_result` and `merged_at`, which re-enters the scan. Runbook, not a dead end. |
| Group settles having produced nothing | Terminate as `merge_result='empty'` so it leaves the candidate set. "At least one member has produced content" is `segment_count > 0` on some member — a column already maintained by `touch_segment`, so the scan stays one indexed query and never lists S3. |
| Claim succeeds but the merge finds no usable turns | extract-session skips cleanly (`:927`) rather than raising, so the S3 event's retry does not cover it: `merged_at` is set, no artifact is written, solo topics are untouched and no email goes out. Acceptable (nothing is lost), but the claim must record `merge_result='empty'` when the artifact never lands, or the group sits claimed-but-unmerged forever. |
| Merge LLM call fails | Per-device reports and emails already sent stay valid. The S3 event's own retry covers a transient failure; a permanent one leaves four separate reports, a degradation not a loss. |
| One member's transcripts unreadable | `assemble_group_turns` already logs and continues without that member. The report states the omission. |
| A member's `delete_topics_for_source` removes 0 rows | **Log it loudly.** The key must be byte-identical to that member's `extraction_key`, including a date derived with the same NZ conversion `_resolve_context` uses (`lambda_finalize_claim.py:109`). A silent 0 means the duplicate survives. |
| Group straddles NZ midnight | Members legitimately have different dates. The merged artifact takes the **lead's** date; member delete keys use each member's own. |
| Lead never uploads at all | The merged key needs the lead's folder + date; if the lead has no users row, fall back to the earliest member's folder and the lead's `opened_at` date, and log. The group id remains the identity. |
| Merge produces worse output than the solo reports | Transcripts are untouched; re-run is always available. This is why deletion is acceptable — recovery is re-extraction, not un-flagging. |
| More than ~4 devices | Merge the first N by segment count, state the omission (Phase A decision). |

---

## Testing

**Unit** — `list_due_group_merges` returns a group only once every member is
terminal-or-quiet, and returns it on a tick where **no session is due** (the
regression that v1 would have shipped); a lead-stops-last group is found;
`claim_group_merge` is exactly-once under a racing second call; the span and
company rejections; `group-` key routing; item-writer deletes every key in
`mergedMembers` and no others; a solo artifact whose coverage is inside the
merged one does **not** re-arm; a genuinely late one does; the cap.

**Integration (RDS Data API, rolled back)** — the read union against real rows
including a member with zero topics of their own and a graded-role member;
`claim_group_merge` under two concurrent updates; the delete rowcount is
non-zero for a real member key.

**Live on test** — two devices, one scan, one short meeting. Assert one merged
topic set, both members see it, two identical `updated` emails. Then repeat with
the second device kept offline until after the merge, to exercise the re-merge.
Then leave it overnight and confirm the nightly ingest does not resurrect the
solo topics.

Replay of the prod group is **not** available: moving customer-derived data into
test fires the whole pipeline against a test identity map.

**Not testable in unit form:** whether the merged record is actually better than
the best single device's. That needs the two-device recording, and it is the
only evidence the feature delivers the coverage it exists for.

---

## Concurrency

Per merged group, honestly counted: **one** extract-session call (~170s+
thinking, on a prompt larger than any solo one), **one** extra item-writer run,
**N** session-finalize invocations, and in the late-arrival case all of that a
second time. For a four-device group that is 2 extract-session + 2 item-writer +
8 finalize calls across the life of the meeting.

Against the post-raise quota (1000, org-api holding a reservation) this is not a
risk. The number is stated because v1 said "one extra call per group", which was
wrong, and BUG-43 lesson 1 is that this arithmetic gets done **before** the
function ships, not after.

---

## Rollout

One repo variable, `*_ENABLE_GROUP_MERGE`, defaulting to **`false` on prod** and
`true` on test — the shape PR #281 used for `NormaliseAudio`. With it off the
standing scan is skipped entirely and prod behaves exactly as today, so the
migration and code can land on main without changing customer behaviour on the
night they ship.

Turning it on is a repo-variable change plus a redeploy. Turning it off is the
rollback — but merged topics that exist stay, and deleted solo topics do not come
back. Flip it on a day when a two-device recording can be watched.

---

## Out of scope

- Acoustic fingerprint fallback for "someone forgot to scan" (v2, Phase A/B spec)
- Persistent speaker identity (separate spec; see also the withdrawn
  `2026-08-07-speaker-attribution-measurement-design.md`)
- A content-alignment pre-pass for >4 devices (degradation point defined, not built)
- Any change to the chunk key format, VAD, or transcription
