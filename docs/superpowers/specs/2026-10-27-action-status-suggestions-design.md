# Action-item status suggestions from speech — design (v1, same-session)

Date: 2026-10-27. Owner decisions: 2026-10-27 (closed status + suggestions scheduled together).
Depends on: fieldsight-pipeline#1188 (`closed` status, merged), item continuity
(`2026-09-30-extractor-declares-item-continuity-design.md`).
Contracts: continuity line (jev TrackB) Part C, confirmed 2026-10-27.
Review: Fable, 2026-10-27 — 1 critical / 7 important, all folded in below (rev 2).

## 1. Problem

People say on site that a job is finished or no longer needed ("Sarah sent it… that's done",
"we don't need that anymore"). Nothing listens for that today: the to-do stays open until somebody
taps it. The system should **propose** a status change and a person accept or dismiss it in one
tap. The model never writes `action_items.status`.

## 2. Scope

**v1 = same session only (agreed with the continuity line).** A suggestion can only attach to an
item the continuity machinery carried from an EARLIER PASS OF THE SAME SESSION (live → final): the
item was raised, and later in the same recording it was reported done / closed / blocked / started.

Out of scope: cross-day. It needs a prior block of earlier days' open items keyed by stable_id
with its own hard-negative measurement — continuity line, later. The interface stays the same: a
suggestion rides on a child whose identity resolved through a claim; any other child's is dropped.

## 3. Contracts (Part C, must hold)

- C1. A suggestion attaches to the SAME item through its continuity identity, never by text.
- C2. Human status always wins. Suggestions never write `action_items.status`; only a person's
  accept does, through the normal audited PATCH. Re-extraction never resets done/closed to open
  (carry-forward already copies a human-touched status).
- C3. If the targeted item was not carried by identity, the suggestion is dropped.

## 4. Design

### 4.1 Extraction (`lambda_extract_session`, flag `STATUS_SUGGESTIONS`, default off)

**Where the instruction lives:** appended to the CONTINUITY block (`item_continuity.render_block`,
reached via `build_extraction_prompt(continuity_block=…)`), not to `_instructions_block` — that
block is shared with the group prompt. Consequences: no prior → no block → prompt byte-identical
to today; group prompts never ask for it; `scripts/continuity_eval/run.py` (calls `render_block`
directly) picks it up for free. `render_block(prior, extra_instruction=...)` carries it, driven by the flag.

Field, optional, per action item:

```json
"status_suggestion": {"status": "done | closed | blocked | in_progress",
                      "at": "HH:MM:SS", "quote": "VERBATIM words"}
```

Instruction (summary): only when someone in THIS transcript says the work is finished, no longer
needed, blocked, or started; an intention or plan is not a status ("I'll send it tomorrow" is not
done); quote the WHOLE line it was said in, at least five words, verbatim; `at` copied exactly as
printed, THE HOUR INCLUDED (same wording as the evidence instruction).

Code decides what survives, pure functions in `item_continuity` / a new `status_suggestions.py`,
in this order:
1. Shape: `status` in the four values, `quote` non-empty string, `at` parseable. Else drop
   (`dropped_by.shape`).
2. C3 at resolve: keep only on `action_items` children whose `item_id` was INHERITED (exact match
   or accepted claim). `assign_fresh_ids`, the flag-off path, and `extract_group` (beside its
   existing `continues` pop) always pop the field (`dropped_by.not_carried`).
3. Evidence: `evidence_match.check_quote` with `_parse_at`, run by THIS feature regardless of
   `EMIT_EVIDENCE` (off on prod). Window/fuzzy as topic evidence; floor =
   `STATUS_EVIDENCE_FLOOR_TOKENS` (default 5, tunable, separate from the topic floor). Keep
   `verified` / `verified_fuzzy`; drop `weak` / `unverified` / `unchecked` (`dropped_by.<status>`).
   The eval reports `dropped_by.weak` so the floor is set on data, not guessed.
4. Group tier: none (step 2 already pops).

Kept suggestions carry `segment_key` + `offset_sec` (the audio anchor; turn-start granularity,
up to one chunk early — the UI says "around"). Artifact gets
`status_suggestions: {offered, kept, dropped_by: {...}}`; one log line of COUNTS. Quote text is
never logged.

### 4.2 Storage (migration 0090; TEST via develop; prod migration is the owner's)

```sql
CREATE TABLE action_status_suggestions (
  id               uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  company_id       uuid NOT NULL REFERENCES companies(id) ON DELETE CASCADE,
  site_id          uuid NOT NULL REFERENCES sites(id) ON DELETE CASCADE,
  stable_id        uuid NOT NULL,              -- action_items.stable_id (item identity)
  suggested_status text NOT NULL CHECK (suggested_status IN ('done','closed','blocked','in_progress')),
  quote            text,
  segment_key      text,
  offset_sec       double precision,
  run              text NOT NULL,              -- writer's pass id: "<tier>:<extracted_at>"
  state            text NOT NULL DEFAULT 'pending' CHECK (state IN ('pending','accepted','dismissed')),
  resolved_by      uuid REFERENCES users(id),  -- caller["id"], same as content_edits.actor_user_id
  resolved_at      timestamptz,
  created_at       timestamptz NOT NULL DEFAULT now(),
  UNIQUE (stable_id, suggested_status)
);
CREATE INDEX idx_status_suggestions_pending ON action_status_suggestions (stable_id) WHERE state = 'pending';
```

`UNIQUE (stable_id, suggested_status)` + `ON CONFLICT DO NOTHING`: dismissed is never re-shown for
the same item+status; re-running a pass is idempotent. Keyed on `stable_id` because rows are
superseded on every re-extraction. Known limits, accepted for v1: after a human reopens an
accepted item the same status is never suggested again (revisit with cross-day); a pending row
whose item a later pass does not carry stays pending but is never shown (no live row has its
stable_id) — harmless.

### 4.3 Writer (`lambda_item_writer`, after `_carry_forward_children`, own SAVEPOINT, never fatal)

Read suggestions from `extraction_topics` children AFTER `clean_item_ids` (not from
`lambda_ingest._map_action_items`, a closed key set). For each kept suggestion, find this pass's new
row by (`topic_id` in this pass's new topics, `item_id`); a cleaned-to-NULL id finds nothing →
drop. **C3 second guard (identity, not text):** insert only if

```sql
EXISTS (SELECT 1 FROM action_items o WHERE o.id = new.carried_from AND o.item_id = new.item_id)
```

`carried_from` alone is NOT enough: `carry_identity` sets it for exact/fuzzy TEXT carries too
(carry_forward_apply.py:56-67). Also require `new.status <> suggested_status`. This is the SECOND guard; the extractor's filter after `resolve` is the authoritative one. Known limit: a redelivery of an artifact that bypassed the extractor (hand-built) is not caught, because the first write makes a fresh id its own lineage. Never updates
`action_items.status`. Counts (inserted / dropped_by reason) logged.

### 4.4 Read (org-api)

The three action-item SELECTs feeding `render_report_shape` (topics.py ~739, ~1015, ~1119) add
`stable_id`; the serializer allowlist (org-api ~10420) adds `status_suggestion`. One batched lookup
of PENDING suggestions by stable_id per render (no N+1); attach the NEWEST pending one whose status
differs from the item's current status. `todo_collapse`: a suggestion on a collapsed-away row is
re-attached to the surviving row via its `collapsed_ids`. Same visibility as the item (served only
where the item is). Tasks and Today build from the same report/timeline payload
(tasks-aggregator / today-adapter), so they get it with no extra endpoint.

### 4.5 Accept / dismiss (org-api)

`POST /api/org/action-items/{id}/status-suggestion` body `{suggestion_id, decision: accept|dismiss}`.
- ACL first, before revealing anything about the suggestion: the block in `patch_action_item`
  (reach + admin / site pm|site_manager / assignee) is extracted into `_action_item_edit_acl`
  and used by both. `get_action_item` widened to return `stable_id`. Then 404 unless the
  suggestion's stable_id is this item's and its state is pending.
- accept: inside one `conn.transaction()`: call `patch_action_item(…, {"status": suggested})`;
  if it returns non-200, raise so the transaction rolls back and return that response; else mark
  `accepted` (`resolved_by = caller["id"]`). Audit row is the PATCH's own (actor = the person).
- dismiss: mark `dismissed`. No status change.

### 4.6 UI (fieldsight-ui, dev)

Chip on the action row (timeline) and task detail (Tasks/Today): "Heard: done — “…quote…”" with
Accept / Dismiss; accept calls the endpoint and refreshes the row; dismiss hides it.

## 5. Measure before proposing the flag for prod

No labelled set exists; build one. `label.py`'s unit (new item ↔ prior assignment) does not fit, so
a new blind worklist: per carried action item in a replayed live → final chain
(`scripts/continuity_eval/run.py`), "did speech in this session report it done / closed / blocked /
started / none?". This gives precision, recall and hard negatives. Owner reviews disagreements
(label.py pattern). Commit counts only, never transcript text. Owner-alias users only (TEST sends
real SES email). The 10-27 recording that motivated the instruction is excluded from the gate set.

The harness never touches Aurora, so the writer guard (§4.3) is covered by integration tests, not
by the eval.

Metric: precision per suggested status, `done` reported separately. Verdict "insufficient" below
30 labelled positives (house pattern: score.py `HARD_NEGATIVE_MIN_PAIRS = 30`). Gate to propose
flag-on: `done` precision ≥ 0.9 with n stated. Recall reported, not gated.

## 6. Not doing

- No auto-apply in v1.
- No cross-day prior block (continuity line, later).
- No suggestions on findings / decisions / questions.
