# Decisions are extracted and then discarded

**Status:** design v2 (reviewed once; the review found the backfill under-specified and one read surface I had not looked at)
**Date:** 2026-08-11

## The finding

The extraction prompt asks for decisions, the model supplies them, and nothing
stores them.

**Measured over 90 real extractions, 1,127 topics: 101 topics (9.0%) carry a
non-empty `decisions` array.** (The artifacts live in the test bucket, not in
this repo, so nothing here can re-derive that number — the plan should carry
the artifact list rather than restate the figure.) The content is not filler:

> `{"decision": "Restrict subcontractor access to basement except for three
> designated pool areas", "rationale": "…"}`
> `{"decision": "RFI to be raised regarding panel reinstatement contradiction",
> "rationale": "Documentation conflict …"}`

`lambda_item_writer`, `repositories/topics.py` and every migration contain **no
reference to `decisions`**. The field is produced, written into the S3 artifact,
and then dropped at the database boundary.

## What this is not

The open-issues note filed this as *"the model produced a topic but
`key_decisions` was empty"* and read it as a prompt defect. Two corrections:

- **`key_decisions` is a different field, fed by a different LLM pass.**
  `lambda_meeting_minutes` **and** the nightly `lambda_report_generator` both
  produce `key_decisions`; `lambda_session_report`, `chunking._topic_text` and
  `lambda_ask_agent` read it off the report shape. `lambda_extract_session`
  produces `decisions`. The two never converge today -- which is why the
  premise holds -- but my first map of this was wrong in a way that hid a read
  surface (below).
- **The prompt is fine.** 9% of topics carry decisions, which is a plausible
  rate for site conversation. Rewriting the prompt would have been work against
  a component that was already doing its job — the failure is one layer down.

## What this also is not: a data loss

The decisions are in `extractions/{folder}/{date}/{session}.json` and every
artifact is retained. Nothing is gone; it is unqueryable and unservable.
**A backfill over existing artifacts is possible and should be part of the
plan** rather than an afterthought — 101 topics of real decisions already exist
in test alone.

## Shape: a child table, like findings

`action_items` and `findings` are both child tables keyed on topic, attached on
read by `list_topics_for_date` with one batched `ANY(%s)` query each. Decisions
have the same shape — discrete records with their own fields
(`decision`, `rationale`, `decided_by`) that a reader will eventually want to
filter and count.

**Not a jsonb column on `topics`.** `evidence` is jsonb because it must express
three states of a *measurement about the topic*. A decision is a record, and the
codebase already has the pattern for records.

```sql
create table if not exists topic_decisions (
  id           uuid primary key default gen_random_uuid(),
  topic_id     uuid not null references topics(id) on delete cascade,
  decision     text not null,
  rationale    text,
  decided_by   text,
  created_at   timestamptz not null default now()
);
create index if not exists idx_topic_decisions_topic on topic_decisions (topic_id);
```

One supersession edge, same exposure as findings and not new: if the nightly
ingest's defer test throws, it falls back to superseding, deleting extraction
topics and cascading their decisions away while the report path writes none.

`ON DELETE CASCADE` matches the siblings, and item-writer's existing
scope-delete-then-reinsert idempotency (keyed on `source_s3_key`) then covers
decisions for free — the topic row goes, its decisions go with it.

⚠️ **The cascade is exactly the shape that bit `programme_tasks`** (a scoped
DELETE removed local children through the cascade). The difference: decisions
have no independent origin — every row comes from one extraction — so there is
nothing a cascade can destroy that should have survived. That is the reason it
is safe here, and it should be checked rather than inherited if a decision ever
becomes user-editable.

## The read path is not free, and there are TWO of them

`list_topics_for_date` attaches children by **explicit per-child query**, not
generically. `/live-items` passes whatever the repository attaches
(`ok({"topics": rows})`, no allowlist), so the frontend needs no change to
receive them — but the repository does. One more batched query, mirroring
`findings.list_for_topics`.

**The second surface is the one I missed.** `render_report_shape`
(`lambda_org_api.py:4455`) hardcodes `"key_decisions": []` with the comment
*"D3: v1, decisions table deferred"* — the codebase's own pre-planned wiring
point for exactly this table. That function serves the **Timeline day view**,
the **session-report modal and Word export**, and the **reindex builder** that
feeds RAG chunks (`chunking._topic_text` reads `key_decisions`).

Wiring only `/live-items` would put decisions in one place and leave Timeline,
the Word export and RAG showing nothing — a half-landed feature that reads as
"decisions still don't work". **Both surfaces are in scope**; the second is one
line, and the comment invites it.

## Scope

**In:** the migration; `repositories/decisions.py` (the shape of `findings`,
without its NOT-NULL hazard); item-writer writes them alongside action_items and
findings; **both** read surfaces -- `list_topics_for_date` and
`render_report_shape:4455`; the backfill specified below.

**Out:** rendering them in the UI; the legacy `key_decisions` path
(`lambda_meeting_minutes`), which is a different pipeline and out of this
line's scope; making decisions editable.

## The prod-migration question

A migration merged to `main` runs against prod on the next deploy. This one is
`CREATE TABLE IF NOT EXISTS` plus an index — additive, no rewrite of existing
rows, no lock of consequence on a table that does not yet exist.

**But it should not ride tonight's release.** Tonight's payload is already
merged and waiting on approval; adding a schema change to it means the one
thing that cannot be rolled back by re-deploying an earlier stack goes out
alongside changes that can. It costs nothing to land tomorrow.

## The backfill, specified

**The mapping key is `(source_s3_key, topic_title)`, and ambiguity is skipped.**
Position cannot be used: all topics of one extraction insert in a single
transaction, so `created_at DEFAULT now()` is identical across them and the `id`
tiebreaker is a random uuid — ordering by either does not reproduce artifact
order. Where a title repeats within one extraction, skip both and log; a wrong
attachment is worse than a missing one.

**101 is an upper bound, not a yield.** Artifacts whose topics were superseded
by the nightly report path, or deleted by a group merge, have no target row.
The backfill reports what it matched and what it could not, and the difference
is expected rather than a fault.

**Idempotency is the backfill's own problem.** Item-writer's dedup is
delete-by-`source_s3_key` on *topics*; a direct insert bypasses it entirely and
the table has no unique constraint, so a second run would duplicate every row.
The script inserts only for topics that currently have **zero** decisions.

## One thing not to inherit from findings

`insert_findings` passes `f.get("observation")` straight into an
`observation text NOT NULL` column. One malformed row therefore aborts the
whole topics transaction — a latent hazard in the file that otherwise takes
care ("one malformed finding must never abort the whole topic's insert").

The decisions repository **skips rows with an empty or missing `decision`**
rather than mirroring that. The mirror is for the shape, not for the bug.

**`site_id` is deliberately omitted.** `findings` carries one plus a
`(site_id, domain)` index; decisions reach a site through `topics`, which
already cascades from `sites`. Stated so a future site-scoped query is not
built on a column that was assumed present.

## Verification

- **Unit:** item-writer writes N decisions for a topic carrying N; zero for a
  topic with none; re-processing the same extraction key does not duplicate
  them (the existing source-key idempotency, exercised for this child);
  `list_topics_for_date` attaches them in the same shape as `findings`.
- **Against a real database, not FakeConn:** the cascade. `FakeConn` does not
  enforce foreign keys — the `programme_tasks` lesson is that a cascade defect
  passes the entire unit suite. Delete a topic in a transaction on the test
  cluster and confirm its decisions go with it, then roll back.
- **Integration:** re-run one of the 90 collected artifacts through item-writer
  on test and confirm the 9% that carry decisions produce rows.

## Open question for the plan

**Does the backfill re-run item-writer, or write decisions directly?** Re-running
item-writer over old artifacts would also rewrite topics — which is idempotent
by design, but it re-touches rows that other work has since edited (content
corrections are a shipped feature, `content_edits`). Writing decisions directly
for topics that already exist is narrower and does not disturb anything a human
has corrected. The plan should take the narrow path unless there is a reason
not to.
