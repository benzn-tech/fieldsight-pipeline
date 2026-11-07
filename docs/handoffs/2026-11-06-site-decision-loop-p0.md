# Handoff: site decision loop, phase P0 (shadow)

For: the backend line (fieldsight-pipeline), plus the frontend line for the review page.
From: the architecture research session, 2026-11-06.

**Read first:** `docs/superpowers/specs/2026-11-05-site-decision-loop-design.md` (rev 5). Its
§13 lists what the owner decided; all of it is approved, so do not re-ask. The diagram and a
Chinese version are in `docs/superpowers/specs/2026-11-05-site-decision-loop/`.

**Status (2026-11-08): on hold until the implementation plan is written and reviewed.** It
will be at `docs/superpowers/plans/2026-11-08-site-decision-loop-p0.md`; work from the plan,
not from this note alone.

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
