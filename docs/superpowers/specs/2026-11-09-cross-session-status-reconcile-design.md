# Cross-session status reconcile: design

Status: draft for owner review, 2026-11-09. Revised after an adversarial review (14 findings, all folded in).
Line: status suggestions.
Builds on:
- `2026-10-27-action-status-suggestions-design.md` (v1, same-session, live on prod since 2026-11-06)
- `2026-11-08-track-c-item-history-and-evidence-chain-design.md` §5.2 (`find_items`, merged in #1260)

## 1. Problem

Today a status suggestion can only target an item that an **earlier pass of the same session** published (`status_suggestions.filter_suggestions`, rule `not_carried`).

On site, the assignment and the report usually live in different recordings:
- assigned at the morning briefing, reported on the afternoon walk;
- assigned Monday, reported Wednesday by someone else.

The open item is already in Aurora (`action_items`, with `stable_id`, `site_id`, `status`). Nothing reads it when a later recording reports its status.

Measured on TEST scripted runs (2026-11-05 to 11-07, 7 sessions): within a session, 0 false positives and 0 wrong-item suggestions. Cross-session misses have not been measured; the scripts were same-session by design. The cross-session scripts in §8 are the first measurement.

## 2. Goal and non-goals

**Goal:** after a session's final extraction, propose a status for open items **from other sessions**, when this session's transcript reports that status. Proposals land in the same `action_status_suggestions` table, and a person accepts or dismisses them as today.

**Non-goals for v1:**
- Writing any status. A person always confirms (unchanged from v1).
- Same-session suggestions. Those stay with the extractor and continuity (TrackB).
- Merging duplicate items, such as the same work assigned twice. That is a later step. Until then, exact duplicates have no distinguishing words, so the anchor guard (§6) **always drops** them. This is a counted miss, not a pass.
- One statement resolving many items ("level one is all done"). The anchor guard drops it in code (§6), not by asking the prompt to refrain.
- The case where a session's assignment and status arrive in one pass and the item is written as a finding (video mode, offline backlog). The item never existed as open, so there is nothing to reconcile. Noted for TrackB.
- Semantic retrieval. Track C's `find_items` is lexical; semantic search is added only if recall measurements need it (§9).

## 3. Scope ladder

One set of knobs. Each rung must pass the §8 trap scripts with **0 wrong-item** before the next rung is switched on.

| Rung | Candidates (`find_items` filters) | Ranking |
|---|---|---|
| R0 (v1) | same site, `recorded_by` = this recorder, `since = until` = this session's NZ date | none: all open items, cap 40, recency order |
| R1 | same site, any recorder, same date | none, cap 40 |
| R2 | same site, any recorder, last 7 days | one `find_items` call **per status-bearing line**, top 5 each, union, deduplicated by `stable_id` |
| R3 | 14 days | as R2 |
| R4 | project lifetime | as R2; semantic search only if recall@cap < 0.9 |

- R2 issues one query per line because `find_items` scores by `|query ∩ item| / |item|` (`item_text_match.overlap`). A concatenated query ranks by item length. `score` is used only for order and never gets a threshold.
- Known R0 exclusion: an item first seen in a nightly report, not an extraction, has `recorded_by = None` and is never a candidate at R0. This is rare since the authority flip.

Env vars (TEST first, prod by repo var later):
- `RECONCILE_STATUS` (bool, default false)
- `RECONCILE_WINDOW_DAYS` (0 = same day)
- `RECONCILE_SAME_RECORDER` (bool)
- `RECONCILE_CANDIDATE_CAP`

## 4. Flow

```
extract-session (final)  ->  item_writer (in-VPC)  ->  reconcile_requests/ artifact (S3)
                                                              |
                         S3 event -> MatcherFunction, reconcile branch (non-VPC, LLM)
                                                              |
                         Lambda invoke -> suggestion_writer (in-VPC) -> action_status_suggestions
```

**Why this shape.** The extractor and the LLM caller have no Aurora access, and the in-VPC Lambdas have no internet (BUG-36). This is the existing programme-matcher two-hop pattern.

The worker is a **new branch in the existing non-VPC `MatcherFunction`**, keyed on the `reconcile_requests/` prefix, not a new Lambda. That function already reads a per-site S3 artifact, calls an LLM and invokes `SuggestionWriter`, so it brings IAM, invoke permissions and wiring with it. The plan checks its timeout and memory against one reconcile call. A separate function is used only if they don't fit.

Rejected: giving the extractor a cross-session prior block through S3 and reusing v1's guard chain. It changes TrackB's prompt and continuity contract, and it would run on every live pass instead of once per final.

### 4.1 item_writer: request (in-VPC)

**Gate (all must hold):**
- `RECONCILE_STATUS`;
- `tier == "final"` (this already excludes group merges, which are `tier == "group"`);
- `not extraction.get("incomplete")`. A known-short final is followed by a fuller one; the checklist report uses the same gate;
- the site is resolved;
- the session is **not a group member** (`_group_for_session(conn, session_base)` returns no row). Otherwise a member's request would list another member's items from the same meeting as "cross-session" candidates. Group sessions are deferred.

**Candidates are computed inside the existing `with get_connection() as conn:` block.** They are other sessions' rows, already durable, so nothing depends on this pass's commit. The artifact is emitted **after** the block, beside `match_request.emit`.

```python
cands = item_history.find_items(conn, company_id=owner_company_id,   # the SITE's company
    site_id=site["id"], kinds=["action_item"],
    statuses={"action_item": ["open", "in_progress", "blocked"]},
    since=window_start, until=date,
    recorded_by=user_folder if RECONCILE_SAME_RECORDER else None,
    exclude_source_keys=[extraction_key], limit=RECONCILE_CANDIDATE_CAP)
```

- `company_id` must be `owner_company_id` (`site.get("company_id") or company["id"]`), not the recorder's home company. `find_items` silently returns `[]` when the site belongs to another company (project-owned tenancy).
- Log `reconcile candidates=N` separately from "flag off", so a silent no-op is visible.
- No candidates means no artifact and no LLM cost.

**Artifact.** The key is `reconcile_requests/{site_id}/{date}/{sha256(extraction_key)[:16]}.json`. It is deterministic, so a re-delivered final overwrites it. Body:

```json
{"site_id": "...", "date": "YYYY-MM-DD", "user_folder": "...", "session_base": "...",
 "extraction_key": "...", "rung": "R0",
 "candidates": [{"kind": "action_item", "stable_id": "...", "text": "...", "status": "open",
                 "responsible": "...", "deadline": "...", "first_seen_date": "YYYY-MM-DD",
                 "recorded_by": "...", "score": 0.0, "matched_terms": [], "place": null}]}
```

- The candidate fields are exactly the Track C §5.2 whitelist. `origin` and other sessions' transcript text never leave the VPC.
- No `company_id` is carried. The writer derives it from the live row (§4.3); it never trusts what a non-VPC worker wrote.

### 4.2 Reconcile worker (non-VPC; MatcherFunction branch)

Pure logic lives in `src/status_reconcile.py`: prompt, parse and guards, with no I/O and no psycopg.

1. **Read** the artifact. Rebuild this session's turns with **`assemble_session_turns`**, the extractor's exact chain: boundary and window de-duplication, audio-tag and device-announcement filters, and `agent_turn_filter`. The quote check therefore runs against the text the extractor saw.
   - Plan task: lift `assemble_session_turns` and `_check_status_quote` (with `_parse_at`) out of `lambda_extract_session.py` into a shared module with no psycopg import. Do not import the 2.4k-line Lambda module.
2. **Prompt** (§5): candidates aliased `C1..Cn`, plus the transcript lines with `[HH:MM:SS]`. One call via `llm_utils.call_llm(..., force_json=True, enable_thinking=True)`, the extractor's final-pass setting.
   - Claude models only through the Claude API, never OpenRouter.
3. **Parse**, then run the **guards** (§6). Guards are code; the prompt is never a guard.
4. **Invoke** `suggestion_writer` with a new `status_suggestions` list. Each entry carries:
   - `stable_id`, `site_id`, `suggested_status`, `quote` (focused), `segment_key`, `offset_sec`;
   - `run = "reconcile:<rung>:<extraction extracted_at>"`.

   Dropped verdicts go in the existing `verdicts` list (decision_records). The plan confirms the `kind` value with TrackB.
   - Check the writer's response the way the matcher does: `FunctionError`, then the counts.
5. **Fail closed.** Any read, LLM or parse error raises, and the S3 event retries. After retries are exhausted, nothing is written.

**Infra the plan must add:**
- the three evidence env vars on the worker (`EVIDENCE_WINDOW_SEC`, `STATUS_EVIDENCE_FLOOR_TOKENS`, `EVIDENCE_FUZZY_THRESHOLD`), the same values as the extractor, or "same floor as v1" is false;
- worker `GetObject` + `ListBucket` on `transcripts/`, the agent-filter prefix and `reconcile_requests/*`;
- ItemWriter `PutObject` on `reconcile_requests/*`;
- a `scripts/wire-s3-events.sh` entry. S3 events are wired by hand (BUG-33), not by the template.

### 4.3 suggestion_writer (in-VPC)

Add a new optional `status_suggestions` key on the event, backward compatible the way `impacts` and `verdicts` were. **Add it to the empty-batch guard** at the top of `lambda_handler`; otherwise a status-only payload (the normal case) returns `{"written": 0}` without opening a connection.

Per entry, in the same transaction:
1. `action_items.get_live_by_stable_id(conn, stable_id)`. It applies the visible-topics predicate and joins `sites` for `company_id`. Skip the entry when:
   - the row is None;
   - `row.site_id != entry.site_id`;
   - the status is terminal (`done`, `closed`);
   - the status already equals the suggestion.
2. `status_suggestions_repo.insert_suggestion(conn, row.company_id, row.site_id, ...)`. **`company_id` comes from the row**, never from the event.

**Accepted limit.** Migration 0090 has `UNIQUE (stable_id, suggested_status)` with ON CONFLICT DO NOTHING. A suggestion dismissed earlier blocks a later one with fresh evidence from another session, and two sessions' quotes for the same status cannot both be stored. The writer counts these as `duplicate`. Revisit by adding the source session to the key if the count is material.

**Re-extraction race.** If the *assigning* session is re-extracted, carry-forward can move the `stable_id` onto a reworded row (item_id, then exact text, then fuzzy text). The pending suggestion follows it. The risk is low; the writer's re-read handles the "superseded between request and write" race, because it finds the carried row or nothing.

Today's confirm path is the audited PATCH plus `state='accepted'`. Later, once Track C's `event_links` insert function exists, it also writes a `resolves` edge (statement → item). Not in v1.

## 5. Prompt (sketch; wording is fixed in the plan and measured)

- The candidates are open work items recorded **earlier**. For each, decide whether someone in THIS transcript reports that this exact item is:
  - finished (`done`),
  - no longer needed or cancelled (`closed`),
  - blocked,
  - or started (`in_progress`).
- Plans, intentions, hearsay ("I heard…"), questions and conditionals are not statuses.
- Answer only when the line clearly names the item. When two candidates differ only by location, unit, block or level, the line must name the distinguishing word.
- Output per hit: `{"candidate": "C3", "status": ..., "at": "HH:MM:SS", "quote": "<whole line, verbatim>"}`. Zero hits is the expected answer for most sessions.

## 6. Guards (code, in order; the first failure names the drop)

All token sets use Track C's tokeniser (`item_text_match.tokens`: ASCII words minus stop-words, CJK bigrams) after **numeral normalisation** ("four" ↔ "4", "level one" ↔ "level 1", "一" ↔ "1").

| Guard | Rule |
|---|---|
| `shape` | status in STATUSES; non-empty quote and `at`; `candidate` is one of the aliases sent |
| `evidence` | `check_quote` against this session's turns returns `verified` / `verified_fuzzy` (same floor and env as v1). The verified anchor `(segment_key, offset_sec)` is kept |
| `anchor` | Let `Q = tokens(quote)`. The pick must (a) be the **unique argmax** of `|Q ∩ tokens(c)|` over **all candidates sent**, and (b) share with Q at least one token that **no other candidate** contains. This drops a generic overlap: "the pipe's all done" against "fix west pipe leak" and "order pipe fittings". It also drops subject-less lines ("that's all done") and many-item lines ("level one is all done" when several level-one items exist) |
| `twin` | For near-duplicate siblings of the pick (scored with `action_pack_targets`' containment scorer, within `AMBIGUITY_MARGIN`), the quote must contain at least one of the pick's distinguishing tokens and **none** of the sibling's. This is the strict check for east/west and unit 3/unit 4 |
| `one_to_one` | Keyed on the **verified anchor** `(segment_key, offset_sec)`, not the quote string. One anchor → at most one candidate (otherwise drop all of them as ambiguous). One candidate → at most one status (keep the latest `at`) |
| `same_status` | The suggested status differs from the candidate's current status (rechecked in the writer, §4.3) |

Metric and log line per artifact: `reconcile offered / kept / dropped_by{...}`, plus the writer's `written / duplicate / skipped{gone, moved_site, terminal, same}`. Quote text is never logged (as v1).

## 7. UI

The suggestion shows on the original item, as v1 suggestions do. The quote comes from **another** recording. `segment_key` is already fully qualified (`audio_segments/{user}/{date}/{base}.wav`) and the API passes it through unchanged, so **the backend needs nothing**.

UI line question: does the card play `segment_key` as given, or assume the item's own session? If it assumes, the card needs "from <date> <recorder> recording" plus a link. This is a small UI task, scheduled before the prod flag.

## 8. Acceptance (TEST)

**Unit tests (pure module):**
- every guard, with a fixture per drop reason;
- the anchor guard's generic-overlap case;
- twin cases in English and 中文;
- the numeral normaliser;
- the alias whitelist;
- a parse failure.

**Integration tests:**
- item_writer: emits only on final + flag + candidates, and not when incomplete or a group member; excludes its own key; passes `owner_company_id`;
- writer: an empty-batch guard with status-only payloads, and the re-read skips (gone / moved / terminal / same).

Use the local Postgres harness, one pytest at a time.

**Scripted recordings** (owner, same site), file `cross_session_scripts.md`:
- **A** (day 1 morning) assigns 7 items, including the twins west/east pipe and unit 3/unit 4 inspection.
- **B** (day 1 afternoon, new session) reports:
  - west done, unit 4 closed, skip bin blocked;
  - traps: a plan, hearsay, an item never assigned, and a subject-less "all done".
  - Expected: exactly 3 suggestions, 0 on the twins' siblings, 0 for the traps.
- **C** (day 2): at R0, 0 suggestions (outside the window). At R2, exactly east done and unit 3 in_progress.

Pass bar for each rung: wrong-item = 0 and trap false positives = 0. Every miss is recorded with its cause:
- ASR;
- a guard, by name;
- the model;
- the `duplicate` unique key;
- an **ordering miss**: the assigning session's final landed after the reporting session's final, so it was never re-examined.

## 9. Measurement for widening

Per session, at each rung, record:
- the candidate count;
- whether the true target was in the candidate set (recall@cap), labelled from the scripts;
- kept, and dropped by guard.

Widen to R2+ only when recall@cap ≥ 0.9 with per-line lexical ranking. Otherwise propose semantic ranking to Track C, with the numbers.

## 10. Cost and latency

- One LLM call per final session with at least one candidate. At R0, most sessions on a site have fewer than 20 candidates, so the prompt is small.
- Runs asynchronously after the final; nothing is on the device path.
- `find_items` takes under 2 s (Track C target), once at R0/R1, or once per status line at R2+.

## 11. Relation to what already exists

**Background action agent** (`2026-11-01-background-action-agent-design.md`, `action_pack_*`):
- It already proposes `close_item` / `update_item` keyed by `stable_id` for an instruction such as "tidy up the open items for site X". A person accepts; there is no auto-apply.
- It is **on demand**, multi-round and broad. Reconcile is **automatic, per session**, one call, and narrow.
- Both are proposals on the same `stable_id`; the person sees both.
- Plan task: before the prod flag, decide whether the pack's Gather reads pending reconcile suggestions as evidence.

**Target resolution in code** (`action_pack_targets.py`): containment scoring over normalised tokens, plus `AMBIGUITY_MARGIN`. The `twin` guard reuses this scorer, so there is one implementation of "which item is this", shared by two features.

**Task numbers and the items tool** (`ask_items.list_items`, `task_no` "#n" on the Tasks page): the device Ask can already list a site's open, in-progress and blocked items. This is the foundation for the active path (§12 Q2). If people report against "#12", resolution needs no text matching at all.

## 12. Open questions for the owner

1. Is R0 = same recorder, same day the right first rung, or should v1 start at R1 (any recorder, same day)?
2. The active path ("walk the open list"): should the morning brief, the device or both list a recorder's open items, so that people report against a known item or number? Proposed as a separate small spec after R0 is measured.
3. Who may accept a cross-session suggestion: anyone on the project (as v1), or only the assigner / responsible person?
