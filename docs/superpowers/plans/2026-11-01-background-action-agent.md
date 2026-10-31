# Background action agent (action packs): implementation plan

Spec: `docs/superpowers/specs/2026-11-01-background-action-agent-design.md` (rev 2; §14 overrides §1–§13).
Repo: fieldsight-pipeline. UI work is handed off (Task 15). Branch per phase off `origin/develop`.

**Every task follows the same order.** The test is written first (TDD). Any new SQL is
exercised by an integration test against local Postgres (`fswork/run-tests.sh`,
`PGTZ=UTC`, one pytest at a time). Fake-connection unit tests never execute SQL, so
they do not count as coverage.

**Commands.**
- Unit tests: `C:\Users\camil\fspy\Scripts\python.exe -m pytest -q tests/unit`.
  Five bench-pin failures are known and pre-existing: `test_report_bench_candidates`
  and 4 in `test_web_answer_bench_matrix`.
- Template: cfn-lint at the CI pin, plus `sam validate --lint` (SAM CLI 1.169.0).

**Default state.** The flag is `ACTION_PACKS=off` everywhere until Task 14. With the
flag off, behaviour must match today exactly.

---

## Phase 0: prerequisites

### Task 1: server-side task numbers (D1)
- Migration (next free number at build time, ≥ 0092):
  - table `action_task_numbers (stable_id uuid PK, site_id uuid NOT NULL, task_no int NOT NULL, created_at)`;
  - `UNIQUE (site_id, task_no)`;
  - a per-site counter, either a `site_task_counters` row or `max+1` under `pg_advisory_xact_lock(hashtext('task_no:'||site_id))`.
- **Writer.** `write_extraction_items` assigns a number to every new `stable_id` in the same transaction.
  - A carried `stable_id` keeps its existing number: the row is keyed by `stable_id`, so nothing is reassigned.
  - Numbers are never reused, including after deletion.
- **Backfill script.** Dry run by default. It numbers existing live `stable_id`s per site in `created_at` order. Running it on prod is an owner step.
- **org-api.**
  - Task list and item payloads include `task_no`.
  - New lookup `action_items.get_live_by_task_no(conn, site_id, task_no)`.
- **Tests:**
  - assigned once;
  - survives re-extraction with carry;
  - concurrent writers on one site never collide (integration test, two connections);
  - backfill is idempotent.
- **Hand-off:** the ui shows `#<task_no>` (Task 15).

### Task 2: `action_items.get_live_by_stable_id` (S12)
- Mirror `topic_questions.get_live_by_stable_id`:
  - join the live-topic predicate and `sites` for `company_id`;
  - `ORDER BY created_at DESC LIMIT 1`;
  - a malformed id returns None.
- Integration test: a superseded pass that shares the `stable_id` is never returned.

### Task 3: timeline and Tasks carry `topics.source` (D2)
- The live/nightly label is `t["is_live"]` in `src/repositories/topics.py:774`; make it false-but-not-nightly for `source='agent'` (badge instead).
- Add `source` to timeline and Tasks rows.
- An `agent://` topic is labelled "agent", never nightly.
- Test: an agent topic's row carries `source='agent'`, and the label is not nightly.

## Phase 1: storage and endpoints (flag off)

### Task 4: migration for the pack tables (spec §3 + §14)
- **`action_packs`**:
  - identity: `site_id NOT NULL` (D4), `company_id` = the site's company;
  - content: `title`, `instruction`, `reading`, `conclusion`;
  - run state: `state`, `failure`, `heartbeat_at` (S9), `run_trace`, `models`;
  - timestamps: `as_of`, `archived_at`, `created_at`, `updated_at`.
- **`action_pack_sources`** and **`action_pack_actions`**, per §3.2 and §3.3, with:
  - `CHECK` constraints on `state`, `type` and verifier label;
  - `UNIQUE (pack_id, ref)` and `UNIQUE (pack_id, seq)`.
- **Indexes:** `(company_id, created_at)`, `(created_by, state)` and `(state, heartbeat_at) WHERE state='running'`.
- Migration test: apply, then re-apply as a no-op.

### Task 5: repository `src/repositories/action_packs.py`
- `create_queued`
- `claim(pack_id)`: CAS `queued→running`, returns bool (S1)
- `heartbeat`
- `store_result`: one transaction for pack, sources and actions
- `fail`
- `cancel`
- `list_for_reader`
- `get_for_reader`
- `set_action_state`
- `sweep_stale(older_than)` (S9)
- Integration tests for every statement, including:
  - claim races: two connections, exactly one wins;
  - `store_result` is atomic.

### Task 6: org-api endpoints (spec §10, ACL per D4 and S15)
- Routes:
  - POST create (writes `action_pack_requests/<id>.json`, S1);
  - GET list;
  - GET one;
  - cancel (sets state and writes the S3 cancel marker);
  - archive;
  - dismiss.
  All of them return 404 while `ACTION_PACKS=off`.
- **Read ACL:** the creator, that company's admins/GM, and that site's pm/site_manager.
- **Read-time source visibility (S13):**
  - re-check each source with `topics.get_topic_visible` using the reader's `site_ids` and `author_ids`;
  - withhold excerpts of removed sources;
  - hide claims and actions that cite only removed sources.
- **Per action:** an `applicable` flag computed with the `_action_item_edit_acl` predicate (S15).
- **Internal tasks**, not routed from the gateway:
  - `action_pack_claim`;
  - `action_pack_heartbeat`;
  - `action_pack_store`, with a payload under 6 MB, rejecting with an error code otherwise;
  - `action_pack_fail`.
- **Stale-run sweep:** runs on list (S9).
- **Company exit:** if `src/tenancy_status.py` exists on develop at build time, call it on create (D5).
- **IAM:** org-api gets `s3:PutObject` on `action_pack_requests/*` only. New routes follow the "new route IAM" memory: check the authorizer and every resource policy.
- **Tests:** whitelist and ACL matrix, flag-off 404s, and an oversized store payload is rejected.

### Task 7: apply endpoint (spec §5 + S10/S11/S12)
- **`close_item` / `update_item`:**
  1. Take `pg_advisory_xact_lock(hashtext(source_s3_key))`.
  2. Look up the live row with `get_live_by_stable_id`.
  3. Run the staleness check: touched fields vs `target_snapshot`, and `content_edits` after `as_of` along the `carried_from` chain (S11).
  4. Check the transition against the table (S10).
  5. Run the `patch_action_item` logic, refactored into a shared function so both paths use it. `responsible` must be a `members_for_site` name; `text` is refused.
  6. Write the action's state.

  All of this is one transaction.
- **noop** only when every touched field already equals the proposal; any other difference is `stale` (S10).
- **`create_item`:**
  1. Get or create the synthetic topic (`source='agent'`, `source_s3_key='agent://action_pack/<pack>'`, `report_date` = the apply date) under the same lock.
  2. Insert the item.
  3. Assign its `task_no` (Task 1).
  4. Write a `content_edits` row with field `created`.
- Idempotent: re-applying an applied action returns the stored result.
- **Integration tests:**
  - human edit after `as_of` gives stale;
  - an edit on a carried predecessor gives stale;
  - concurrent apply and re-extraction serialise;
  - double apply is idempotent;
  - an agent topic is untouched by a re-extraction of another source.

## Phase 2: tools

### Task 8: rag-search `items` mode (S4)
- **Read-only.** Returns `stable_id`, `task_no`, text, status, deadline, responsible, updated_at, site, `source`.
- **Includes** `source='agent'` topics.
- **Scope:** applies the same site/author scoping as `ask_tools`' rag-search path. Start from `_ask_tools_where` (`src/repositories/topics.py:1340`) and add `OR t.source = 'agent'` to its source clause.
- **Filters:** `site_id`, status set, text query, `task_no`.
- **Tests:** an integration ACL matrix, including an external member and an author-filtered role.

### Task 9: web lane for packs (S5, D6)
- **Query discipline:**
  - queries come from the Plan stage, at most 300 chars each;
  - later stages pass a query id, and code rejects anything else.
- **`web_search` wrapper:** reuses `web_answer.general_answer`. A web answer that is not verified is dropped from the evidence (D6).
- **Fetcher** (new `src/web_fetch.py`):
  - https only;
  - DNS-resolved address checked against private, link-local, loopback, metadata and IPv6 ULA ranges;
  - every redirect re-checked, at most 3;
  - timeout per page;
  - at most 2 MB downloaded, at most 200 kB of text kept;
  - HTML to text with the standard library only;
  - snapshot to the lake bucket, recorded with `lake_objects.record_best_effort`.
- **Tests:** an SSRF table (literal IPs, IPv6, a DNS rebind via a redirect, non-https), size caps, and a dropped unverified draft.

## Phase 3: the agent

### Task 10: `ActionAgentFunction` (template + handler skeleton)
- **Configuration:**
  - not in the VPC; Timeout 900; MemorySize ≥ 1024;
  - reserved concurrency of 3;
  - `EventInvokeConfig MaximumRetryAttempts: 0`;
  - `LLM_CHAIN_BUDGET_SECONDS` below 900 minus the store reserve;
  - env: `ACTION_PACKS`, `ACTION_WRITER_MODEL`, `ACTION_VERIFIER_MODEL`, `ACTION_VERIFIER_FALLBACKS`, the token cap, `RAG_SEARCH_FUNCTION`, the org-api function name, the bucket;
  - gated on `HasOrgApi`.
- **Deploy wiring:**
  - `scripts/wire-s3-events.sh` adds `ActionAgent ← action_pack_requests/**.json`;
  - `deploy.yml` applies it on TEST;
  - deploy-prod wiring is listed as an owner step.
- **Parameters and parity:** `ActionPacks` is a parameter in both deploy workflows with the bare-`Key` guard. The env-parity test is extended.
- **Handler:**
  1. Parse the S3 event.
  2. Claim (exit if lost).
  3. Run stages.
  4. Store, or fail.

  It checks the cancel marker between rounds (`ask_voice_turn.is_cancelled` pattern) and sends a heartbeat after each round.
- **Tests:** duplicate delivery runs once; a cancelled run stops; a missed claim exits without spending anything.

### Task 11: stages: plan, gather, write (spec §4.3 + S7/S8)
- **Plan:**
  - one call with a JSON schema;
  - output: reading, title, `site_id` within scope, date range, evidence needs, fixed web queries.
- **Gather:**
  - deterministic round 1: `day_overview`, items, project-document search;
  - then model-chosen rounds through `ask_tools.execute`, the items tool and the web wrapper;
  - limits: at most 8 rounds, at most 4 calls per round;
  - the rag-search hop retries once, on throttle or 5xx only (S8).
- **Write:**
  - its own render: the full stored excerpt of every citable ref (S7);
  - strict schema: action type enum, `target_stable_id` copied from items output, at least one ref per element.
- **Budgets:** reserves of 90 s for write and 90 s for verify. The tool rounds use the rest of the 600 s target. When a budget is hit, store a partial result with `truncated`.
- **Model records:** record the models actually used, after fallbacks.
- **Tests:**
  - fake models: schema violations are rejected;
  - budget exhaustion produces a partial result;
  - no record text reaches web queries (assert on the wrapper's inputs).

### Task 12: verifier (spec §4.4 + S6)
- **Code checks:**
  - every ref exists;
  - every target is in the items output and in scope;
  - transitions are legal (S10 table);
  - near-duplicate check against open items, aligned with `todo_collapse` (S16).
- **Model check:**
  - a different family from the writer's model **as used**;
  - fall back to `ACTION_VERIFIER_FALLBACKS`;
  - fail with reason `model` if every verifier collides.
- **Running:** verifications run in parallel under the verify reserve.
- **Labels:** web-only support is at most `weak`; `unsupported` is never applicable.
- **Tests:** family collision handling, label rules, parallel timeout.

## Phase 4: eval, rollout, hand-off

### Task 13: eval harness (spec §8 + S16)
- **Where:** `scripts/action_pack_eval/`.
- **Gold set:** at least 20 instructions, with expected actions supplied by the owner. The harness has an annotation file format and a blind second-annotator subset.
- **Metrics:**
  - fabrication rate, reported as the writer's raw rate **and** the post-verifier rate;
  - wrong-target rate;
  - recall;
  - "uncertain" honesty on standards claims;
  - latency p50/p90 and cost.

  All are counted outside the model.
- **Noise floor:** run the gold set twice before comparing variants.
- **Data:** runs on TEST only. Labels stay outside git, the same way the retrieval-ruler data does.

### Task 14: rollout (spec §9 + D3)

| Step | Action | Who |
|---|---|---|
| 1 | Merge Phase 0–3 to develop. TEST deploy and S3 wiring run automatically. | main agent |
| 2 | `ACTION_PACKS=internal` on TEST; run the Task 13 eval; fix and re-run | main agent |
| 3 | `ACTION_PACKS=on` on TEST; owner hand-tests the 10-10 pour scenario | owner |
| 4 | Release to main with the flag `off`. Prod deploy. Wire `action_pack_requests/` with `scripts/wire-s3-events.sh` on prod. | owner (merge, approve, wire) |
| 5 | Run the Task 1 `task_no` backfill on prod: dry run, then apply | owner |
| 6 | Prod `internal`, then `on`, after the thresholds pass and continuity is on (or the owner accepts the measured stale rate) | owner |

### Task 15: ui hand-off note
Write `docs/handoffs/2026-11-01-action-packs-ui.md` covering:
- the endpoints, with request and response examples;
- polling;
- ref chips, using the existing evidence card, the document page, and a snapshot viewer;
- the `weak` badge, and excluding weak actions from Apply-all;
- the `applicable` flag;
- the stale dialog, showing the live values against the snapshot;
- `#task_no` in Tasks;
- the "Agent" source badge in Tasks and the day view (D2);
- entry points: Ask, the day view, Tasks.

---

## Order and parallelism
- Tasks 1–3 are independent of each other.
- Tasks 4–7 depend on 2.
- Tasks 8–9 run in parallel with 4–7.
- Tasks 10–12 depend on 5, 6, 8 and 9.
- Task 13 depends on 12; Task 14 on 13.
- Task 15 can start once Task 6's response shapes are fixed.

## Unverified at plan time
- `src/tenancy_status.py` is not on develop yet (D5).
