# Handoff: site decision loop, phase P0 (shadow)

For: the backend line (fieldsight-pipeline), plus the frontend line for the review page.
From: the architecture research session, 2026-11-06.

**Read first:** `docs/superpowers/specs/2026-11-05-site-decision-loop-design.md` (rev 5). Its
§13 lists what the owner decided; all of it is approved, so do not re-ask. The diagram and a
Chinese version are in `docs/superpowers/specs/2026-11-05-site-decision-loop/`.

**Status (2026-11-08): ready.** The owner approved the plan
`docs/superpowers/plans/2026-11-08-site-decision-loop-p0.md` (Fable-reviewed). Work from the plan,
not from this note alone. Tasks 1-7 and 9: backend line. Task 8: frontend line.

## What P0 is

Cards are written and **never sent**. The owner labels them on a review page. No model calls.

In scope:
1. Tables: `site_events`, `event_runs`, `decision_cards`, `decision_ledger` (§5). The latest
   migration on develop today is 0096; take the next free number when you start. The ledger's
   append-only rule is a trigger (single DB role, so no grants).
2. Hub: auth + tenancy, late check, dedupe, rule match, unmatched pool, urgency, queue,
   dispatch, retry (§4.2). Back-off 5 min doubling to 1 h, notify at 3, stop at 5, and merge
   same-subject events into a waiting retry.
3. Two deterministic rules (§4.1):
   - `checklist.gap`, from a timer scan in a VPC function over `checklist_reports` rows that
     reached `done` (gaps live in the S3 result JSON). Not from `lambda_session_report`, which
     has no DB access and does not know whether an extraction is final.
   - `todo.overdue`, from a daily timer scan over open `action_items`.
4. Card writing with deadline, consequence and evidence, the lookups (§4.5 steps 1-2), and
   routing that computes recipients but sends nothing. No card timers in P0.
   The ask-first check is code, not a prompt (§4.4 step 4).
5. A flag, default off everywhere. Suggested name `SITE_DECISION_LOOP` with values
   `off` / `shadow`.
6. Review page (frontend line, repo fieldsight-ui, integration branch `dev`): list shadow
   cards; label each `needed` / `not_needed` / `wrong_evidence`, with a note. Platform admin only.

Out of scope for P0:
- sending anything (email, push, voice);
- the card inbox;
- the model-call layer and token brakes (§4.3; there are no model calls yet);
- cross-vendor review;
- spoken project onboarding (§9);
- new roles and delegation;
- action packs as a handler;
- marking inspections as passed.

## The gate to leave P0 (§12)

- At least 50 shadow cards labelled by the owner: needs-a-person precision ≥80 %, fabricated
  evidence 0, cards per person per day ≤3.
- Engineering: replay judge over the event log; invariants (no card without an event, no
  answer counted twice, no cross-company row); mutation testing where every planted fault is
  caught; nightly run. `github.com/KKKKhazix/agent-autoloop` (MIT) `sim/` is the model for
  this. Read it, keep the licence notice if you copy code.

## Traps already found

- `PROD_DECLARE_CONTINUITY` is true on prod since 2026-11-05, so `stable_id`s carry across
  re-extraction on prod as on TEST.
- A re-extraction replaces `result_key` and resets the checklist row to pending. Count a
  report only when its session's latest extraction is final, and rely on the dedupe key plus
  the open-card key so a re-run cannot produce a second card.
- Today's read paths are per-caller. Handlers need new repository functions filtered by
  (company_id, site_id) under a system principal (§4.8). Do not borrow a person's sub.
- A fake DB connection in tests does not execute SQL. Run tenancy and trigger tests against
  the local Postgres harness.

## Ownership

| What | Who |
|---|---|
| Backend, migration on TEST, flag on TEST | backend line |
| Review page to `dev` | frontend line |
| Prod migration, prod flag, merging ui `main`, labelling the 50 cards | owner |

Open a release PR for the owner when P0 is verified on TEST. Do not turn prod on.

## TEST result (2026-11-10, backend line)

Built: pipeline#1278 (migration 0100, pure hub, repositories, scan code) and pipeline#1282
(scan function, two schedules, review routes, replay judge, nightly workflow). Repo variable
`TEST_SITE_DECISION_LOOP=shadow`. TEST deploy run 42735696613 applied
`0100_site_decision_loop.sql`; run 42745342516 created `fieldsight-test-decision-scan`.

Deviations from the plan, each recorded as a ruling in the build ledger:
- **Migration 0100**, not 0097: 0097-0099 were taken by other lines first.
- **Two schedules, not three.** `checklist_gap` every 30 min and `todo_overdue` at 18:30 UTC, each
  followed by `process` in the same call. A 5-minute process schedule would keep Aurora from ever
  auto-pausing (`test_sweep_cadence_vs_autopause`). Cost: a failed event retries at the next
  30-minute tick.
- TEST deploys with `EnableSchedules=false`, so the schedules exist on TEST but are disabled. TEST
  cards come only from manual invokes.

Manual invokes on TEST:

| Input | Result |
|---|---|
| `checklist_gap` | emitted 0 (no checklist report finished in the last 48 h), errors 0 |
| `todo_overdue` | emitted 58, processed 58, cards 58, faults 0, errors 0 |
| `process` | nothing left to process |

- SQL: 58 `site_events` `done`, 58 `event_runs` `done`, 58 `decision_cards`.
  - The cards are all `choose`, `open`, urgency `normal`, on 2 sites in 2 companies.
  - Every card has recipients: 62 pm and 27 site_manager slots across 3 people.
  - Deadline = 23:59:59 NZ on the next working day.
- Replay judge plus invariants, run against TEST read-only in one REPEATABLE READ snapshot: 0
  findings. There are no moved-site rows to explain.
- `GET /api/org/decision-shadow`, invoked directly as the platform admin: 200. The field names
  match ui#469.

**Finding for the owner: the first overdue scan carded the whole backlog at once.**
- The deadlines on TEST run from 2026-08-07 to 2026-11-09, so 58 cards landed on 3 people in one
  day. `cards_per_recipient_day_p95` is **31**, against the gate of ≤ 3.
- The P0 plan has no per-day cap and no queue. The spec has both (§4.2 step 4 "per-day cap", step 6
  queue), but they were not in P0's task list.
- A prod shadow run would do the same on its first day.
- Options:
  - (a) a per-recipient daily cap with a queue (spec §4.2), before prod shadow;
  - (b) card only items that became overdue since the flag went on (backlog stays in the report);
  - (c) accept a one-off first-day spike and judge the gate from day 2.
- The owner chooses. Nothing is sent in shadow either way.

Not done by the backend line: labelling 3 cards on the review page (plan Task 9 Step 4). TEST's
only platform admin is the owner, and labelling as the owner is the owner's act. The label POST is
covered by integration tests on real Postgres (status stays `open`).

Must fix before cards leave shadow (from the reviews):
- evidence quotes are checked against the S3 deletion mirror only, not the Aurora tombstones or
  redactions, and only for the report's own session (walk part sessions are not checked);
- the gap card's "remind <name>" label is not checked against deletions;
- empty item text renders `''` in a question.

P0 gap cards carry no quote today, so evidence is `[]` on every card.
