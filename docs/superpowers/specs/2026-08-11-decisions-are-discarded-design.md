# Decisions are extracted and then discarded

**Status:** design v1
**Date:** 2026-08-11

## The finding

The extraction prompt asks for decisions, the model supplies them, and nothing
stores them.

**Measured over 90 real extractions, 1,127 topics: 101 topics (9.0%) carry a
non-empty `decisions` array.** The content is not filler:

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

- **`key_decisions` is a different field on a different pipeline.**
  `lambda_meeting_minutes` (the legacy report path) uses `key_decisions`;
  `lambda_extract_session` produces `decisions`. Only `chunking.py` and
  `lambda_ask_agent` read `key_decisions`, and both read it off reports.
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

`ON DELETE CASCADE` matches the siblings, and item-writer's existing
scope-delete-then-reinsert idempotency (keyed on `source_s3_key`) then covers
decisions for free — the topic row goes, its decisions go with it.

⚠️ **The cascade is exactly the shape that bit `programme_tasks`** (a scoped
DELETE removed local children through the cascade). The difference: decisions
have no independent origin — every row comes from one extraction — so there is
nothing a cascade can destroy that should have survived. That is the reason it
is safe here, and it should be checked rather than inherited if a decision ever
becomes user-editable.

## The read path is not free

`list_topics_for_date` attaches children by **explicit per-child query**, not
generically. `/live-items` passes whatever the repository attaches, so the
frontend needs no change to receive them — but the repository does. One more
batched query, mirroring `findings.list_for_topics`.

## Scope

**In:** the migration; `repositories/decisions.py` mirroring
`repositories/findings.py`; item-writer writes them alongside action_items and
findings; `list_topics_for_date` attaches them; a backfill script over existing
extraction artifacts.

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
