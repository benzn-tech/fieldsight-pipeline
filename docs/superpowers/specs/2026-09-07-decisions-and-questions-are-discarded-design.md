# Two of the five things the extractor produces are thrown away

**Date:** 2026-09-07
**Status:** design, SECOND draft. The first was reviewed and had two blocking
defects; both are corrected here and recorded in §11, because the pattern
repeats. Measured against the prod lake rather than the schema.
**Scope:** backend (Aurora + item-writer + payload). The frontend half is one
conditional section that already exists and cannot fill.

---

## 1. What was found, and how

Sweeping every key in `render_report_shape`'s topic dict against the frontend
turned up a field the UI never reads. Following the same method the other way
— every field the *extractor is asked to produce*, against what survives to
the payload — turned up something larger.

The extraction prompt (`lambda_extract_session.py:938`) asks the model for
four child collections per topic, alongside the summary it asks for elsewhere
in the same schema:

> action_items, **findings**, **decisions**, and **questions**

Three of them reach the product. Two do not.

```
    extractor (S3 JSON)          Aurora                 payload
    ─────────────────────────────────────────────────────────────
    action_items         →   action_items      →   action_items    ✅
    findings             →   findings          →   findings        ✅
    summary              →   topics.summary    →   summary         ✅
    decisions            →   (no table)        →   "key_decisions": []
    questions            →   (no table)        →   (absent)
```

`lambda_org_api.py:5850` is explicit about it:

```python
"key_decisions": [],                    # D3: v1, decisions table deferred
```

## 2. The measurement

Across the 120 extraction artifacts in the prod lake (274 topics):

| | topics carrying it | total items |
|---|---|---|
| **decisions** | **81 of 274 (30 %)** | **91** |
| **questions** | **88 of 274 (32 %)** | **121** |

Verbatim from prod, unedited:

- *"Door replacement will be completed in two phases: floors 1–3 by next
  Tuesday, remaining 3 floors the week after"*
- *"Onboarding/training session for Neil and James to be conducted at UCP
  today"*
- *"Arrange a dedicated catch-up with Brad to introduce OpenSpace and
  360-camera site capture"*
- *"What is the full name and company details of the contractor ('Alex')
  performing the door…"*
- *"Why is the location feature not working, and how can it be fixed?"*

The first of those is a phased-delivery commitment with a date. It is exactly
what a daily report exists to record, and today it is written to S3 and then
discarded.

**Honest caveat on quality.** As with findings, a slice of the corpus is
device-test sessions, and those produce decisions like *"Set recording duration
to one minute for this test session"* and questions about garbled transcript
characters. The findings sweep measured this precisely — 36 of 219 were the
model commenting on the recording rather than the site — and the same
proportion should be expected here. It lowers the value; it does not change
that 30 % of topics carry one.

## 3. Nothing stores them

Verified rather than assumed:

- no `CREATE TABLE decisions` / `questions` anywhere in the repo
- no `INSERT INTO decisions` / `questions` in `src/`
- `lambda_item_writer.py` does not mention either

So `render_report_shape`, which reads Aurora, has nothing it *could* send. The
hardcoded `[]` is a symptom, not the cause.

This is a **deferred feature, not a regression**. The unified-extraction design
(`2026-07-13-unified-extraction-labeling-design.md`) lists "add `decisions`,
`questions` child tables (or a typed `topic_items` table)" as future work. This
spec is that work, with the numbers that were missing when it was deferred.

## 4. One table or two

**Recommendation: two child tables, mirroring `findings`.**

The alternative — a single typed `topic_items` table, which the 2026-07-13
design floats — is the tempting one and should be declined here. The two
records have different shapes — `decision`/`rationale`/`decided_by` against a
single `question` — and a typed table forces both into a nullable union where
every consumer re-learns which columns apply to which type. (The first draft
also argued from "different lifecycles: a question can be answered". That arm
is withdrawn: §5 establishes nothing on these rows can be answered yet. The
shape argument stands on its own.) The
repo already has the two-table precedent working: `findings` (migration 0010)
and `action_items` are separate, batched by the same `list_for_topics` pattern,
and neither pays for the other's columns.

## 5. Schema

Mirrors `0010_findings.sql` — including `site_id` denormalised onto the row,
which is what lets the compliance re-key and the ACL scope resolve without a
join back through `topics`.

```sql
CREATE TABLE decisions (
  id           uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  topic_id     uuid NOT NULL REFERENCES topics(id) ON DELETE CASCADE,
  site_id      uuid NOT NULL REFERENCES sites(id) ON DELETE CASCADE,
  decision     text NOT NULL,
  rationale    text,
  decided_by   text,
  created_at   timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX ON decisions (topic_id);

CREATE TABLE questions (
  id           uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  topic_id     uuid NOT NULL REFERENCES topics(id) ON DELETE CASCADE,
  site_id      uuid NOT NULL REFERENCES sites(id) ON DELETE CASCADE,
  question     text NOT NULL,
  created_at   timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX ON questions (topic_id);
```

`site_id` carries `ON DELETE CASCADE` because `0010_findings.sql:11` does, and
a child that FK-errors on site deletion while every sibling cascades is a
deletion path that fails halfway.

**There is no `status` column, and that is the review's finding rather than a
simplification.** The first draft gave `questions` a
`status IN ('open','answered','dropped')` and argued it had to exist from the
start. It cannot exist at all yet:

`lambda_item_writer.py:756` runs `topics.delete_topics_for_source(conn,
extraction_key)` and reinserts on EVERY extraction write for a key — including
the routine live→final pass over the same session. `ON DELETE CASCADE` takes
the children with it and they are regenerated from the model's output. A human
marking a question `answered` would have the edit silently reverted by that
session's own final pass.

This is not speculation about the mechanism. The line immediately above that
delete is `_warn_if_discarding_checkoffs(conn, extraction_key)`, whose own
comment says the loss is not prevented, only made visible, because "a silent
permanent loss and 'nothing was ticked' produced identical output". The
codebase already knows human edits die here and chose to log rather than save
them. Adding a second mutable field to the same rows repeats a defect that is
already documented in place.

`findings` survives the same delete only because nothing human-mutable lives on
it that is not re-derived — the programme impact is re-applied by the matcher,
and `apply_impact` treats "the row vanished under me" as a normal skip.

**Closing a question is therefore out of scope until the write path can carry a
human edit across a re-extraction.** That is the same unsolved problem as
check-off preservation, and it should be solved once for both rather than
twice, badly.

`ON DELETE CASCADE` on `topic_id` is load-bearing and carries a known trap: the
programme refactor found that deleting a parent takes its children with it and
a `DELETE` scoped by `origin` was not enough on its own
(`fieldsight-programme-save-replaces-local`).

**The nightly-supersession question is answerable from code and the first draft
was wrong to defer it.** The authority flip gates at the TOPIC level:
`lambda_ingest.py:645-649` sets `defer_to_extraction` when the day has
extraction topics, and in that branch `delete_topics_for_source_prefix` on the
extraction prefix is never reached. `AUTHORITY_FLIP` has been on in prod since
2026-07-16. There is nothing per-child-table to inherit — any CASCADE child of
an extraction topic gets exactly the persistence `findings` has, automatically.
The wipe that does matter is the per-key delete-and-reinsert above, which is a
different mechanism with a different consequence.

## 6. Write path

`lambda_item_writer`, in the same transaction as `action_items` and `findings`,
via `repositories/decisions.py` and `repositories/questions.py` built on the
shape `repositories/findings.py` actually has — which is not what the first
draft described:

- a `_COLS` constant, one place (findings.py:24)
- `insert_decisions(conn, topic_id, site_id, decisions)` /
  `insert_questions(...)`, named and shaped after `insert_findings(conn,
  topic_id, site_id, findings)` (findings.py:42). The first draft proposed
  `insert_many(...)` with `_clean_enum` applied to `status`; there is no
  `insert_many` in the precedent, and with `status` gone (§5) there is no enum
  on either table to clean.
- **the insert is a per-row loop, as in the precedent.** Only the READ side is
  batched. Copying the precedent means copying that, not improving on it in
  passing.
- `list_for_topics(conn, topic_ids)` — ONE query with `= ANY(%s)`, never N+1

The N+1 point is not stylistic. `list_topics_for_date` already batches
`action_items` and `findings` for exactly this reason, and the timeline render
runs over every topic on a day.

## 7. Payload — strings, and this is the review's biggest correction

```python
"key_decisions": [d["decision"] for d in t["decisions"]],
"open_questions": [q["question"] for q in t["questions"]],
```

**The first draft proposed objects — `{id, decision, rationale, decided_by}`
and `{id, question, status}` — and claimed "filling it is the entire frontend
change" and "two producers, one field name, one renderer shape". Both claims
were exactly backwards, and shipping them would have thrown a React error that
killed the whole card.**

The renderer on THIS payload's path is `TopicCard`, and
`topic-card.js:283-287` maps each `key_decisions` entry straight into an
`<li>` as a React child. React children must be strings, not objects; feeding
it `{decision: …}` raises "Objects are not valid as a React child" and the card
stops rendering. The object-shaped decision renderer the draft had in mind
exists only in `MeetingTopicCard` (`meeting-topic-card.js:179-196`), which is
on the meeting-minutes path and consumes a different payload.

The same is true of questions in the other direction:
`meeting-topic-card.js:244-247` renders each `open_questions` entry directly as
a child, and its producer `lambda_meeting_minutes.py:130-132` declares
`"open_questions": ["Question or unresolved point…"]`. Strings. The draft had
the two shapes crossed.

The independent confirmation is the OTHER producer of this key:
`lambda_report_generator.py:182` emits `"key_decisions": ["Decision attributed
to person"]`. Two existing producers and two existing renderers all agree on
strings; only the draft did not.

**So `rationale` and `decided_by` are stored but not sent, in v1.** That is the
honest position rather than a compromise: the payload's job here is to fill a
section that already exists, and widening the contract to carry fields no
renderer reads would be inventing a consumer. Sending them is a separate change
with its own frontend half.

**And this is why there is no `id` in the payload either — the two review
findings agree.** §5 establishes that every extraction pass deletes and
reinserts these rows, so an id would change on every live→final pass. An
identifier that churns is not an identifier; exposing one would invite callers
to store it.

## 8. `lambda_session_report.py:121`

```python
"open_questions": [],
```

Hardcoded, with no comment — while the `photo_streams` line three rows below
carries an explicit one about absent-versus-empty. Every customer-facing
minutes document therefore states that the session raised no open questions.

Once §7 lands this becomes `content.get("topics")[i].get("open_questions")`.
Until then it should carry a comment saying what it is, because an unexplained
`[]` beside an explained one reads as a considered choice.

## 9. What this spec is NOT

**Not the open-points feature.** `open_points` (2026-08 briefs) marks *facts
stated with uncertainty* — "it's 150 in 3604 I think, I'd have to check". A
`question` is an explicitly asked, unresolved question. Related, distinct, and
already burned once: the open-points work found that real meetings put stance
markers ("我觉得") into the uncertainty bucket, and the fix was to narrow what
counts. Do not merge these two, and do not let `questions` quietly grow the
open-points admission rules.

**Not a new page.** The frontend change is that an existing conditional section
starts having content. `open_questions` needs a renderer on the daily-report
path, which is a small addition to the same topic-detail component — not a
section of its own on Today.

**Not urgency-ranked.** No ordering is specified beyond `created_at`, and any
future ranking must be measured first: `priority` is `High` on 48 % of open
items and `urgency` is `high` on 48 % of critical dates. Two fields have
already failed as sort keys for the same reason, and a third should be assumed
guilty until counted.

## 10. Open

1. **Does the nightly supersession wipe these?** §5. Must be answered by
   observing what `findings` rows do across a real nightly run, not by reading
   the supersession code.
2. **Who may close a question?** `status` is in the schema; the endpoint that
   sets it is not in this spec. It should reuse `_CORRECTION_ROLES`, which is
   `("admin", "gm", "pm", "site_manager", "platform_admin")` — note that the
   frontend's own `NAMING_ROLES` disagrees with it by one entry
   (`project_manager`, which is not even a valid `global_role`), so whichever
   list this reuses should be reconciled rather than copied.

## 11. What the first draft got wrong

Kept because it is the third spec in a row to be written against comments and
schemas rather than against the consumer, and the failure keeps taking the same
form: **the spec described what the field means and never checked what reads
it.**

| claim | reality |
|---|---|
| the payload should carry `{id, decision, rationale, decided_by}` objects, and "filling it is the entire frontend change" | `topic-card.js:283-287` passes each entry straight to React as a child. Objects raise "Objects are not valid as a React child" and the card stops rendering. The change would have BROKEN the section it claimed to fill. |
| `{id, question, status}` "matches what `meeting-topic-card.js` already reads — one renderer shape" | `meeting-topic-card.js:244-247` also renders each entry directly as a child, and its producer `lambda_meeting_minutes.py:130` declares plain strings. The two shapes were exactly crossed. |
| `questions.status` "has to exist from the start" | `lambda_item_writer.py:756` deletes and reinserts every child on each extraction pass; a human's `answered` is reverted by the session's own final pass. `_warn_if_discarding_checkoffs` on the line above proves the codebase already knew. |
| whether nightly supersession wipes these "can only be answered by observing a real nightly run" | Answerable from `lambda_ingest.py:645-649`: the flip gates at the topic level and CASCADE children inherit `findings`' persistence automatically. |
| the schema "mirrors `findings`" | it dropped `ON DELETE CASCADE` from `site_id`, which `0010_findings.sql:11` has. |
| `insert_many(...)` with `_clean_enum` on `status` follows the precedent | the precedent is `insert_findings(conn, topic_id, site_id, findings)` and its insert is a per-row loop; only the read side is batched. |

Two of these are the SAME defect wearing different clothes — the payload shape
and the `status` column both assumed a stable, addressable row, and the row is
deleted and rebuilt on every pass. Once that is known, the design gets smaller:
strings, no ids, no status.

The cheapest check that would have caught the pair: **open the file that
renders the field before specifying the field.** Both renderers are eight lines
long.
