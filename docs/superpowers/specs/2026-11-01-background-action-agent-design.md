# Background action agent ("action packs") — design

Date: 2026-11-01. Status: rev 2 (Fable builder check folded in; plan: docs/superpowers/plans/2026-11-01-background-action-agent.md).

**Rev 2 overrides:** §14 lists every change from the builder review. Where §14 and an earlier section disagree, §14 wins.
Owner intent (2026-10-24/25 brainstorm, owner's words in brackets):

- One instruction, then everything runs in the background. No back-and-forth, no
  "which do you prefer" questions. Result-oriented. ["目的是能一个指令把事情做了而不是指导我们用户怎么去做"]
- The worked scenario: "Based on the 10-10 concrete pour records, what was not done
  well, and what should we do next, per NZ standards or our own procedures."
  The useful result is not a report. It is concluded actions: close #69/#77/#71, add
  an RFI, raise a follow-up for X by Friday.
- The result must be **storable and traceable**, and not only a report. ["首先是可以储存可以追溯的"]
- The agent drafts actions; **a person's click makes each one take effect** (owner confirmed).
- What a foreman does next with a result: (1) archive it, (2) send it (report/email),
  (3) track it as a todo list.
- Web search may run as its own lane, with search wrapped as tools.

Decisions marked **my call** were made by the main agent on the owner's standing
instruction to choose the long-term reasonable option. The owner can overturn any of them.

---

## 1. What we are building

An **action pack** is the stored result of one background run. It holds:

1. **Conclusion.** A few short findings, each one cited.
2. **Draft actions.** Each action is typed, each one has its own Apply button, and
   each one shows the evidence behind it.
3. **Sources.** Every record, document and web page the run relied on. For web
   pages, a snapshot of the text is stored at run time, so the pack still reads the
   same after the page changes or disappears.
4. **Run trace.** Tools called, models used, timings, budget spent and verifier
   verdicts. Counts and ids only, with no free text in logs (repo-wide rule).

A report or an email is an **export view** of a pack, not a separate artifact.
Accepted actions become ordinary action items: the same table, the same Tasks page,
the same audit trail. Nobody has to learn a second todo list.

### 1.1 v1 scope

**Action types that apply:**

| Type | Applies as | Requires |
|---|---|---|
| `close_item` | `status` = `done` or `closed` on an existing action item | The item's `stable_id` |
| `update_item` | Changes to `deadline`, `responsible`, `priority` or `status` on an existing item | The item's `stable_id` |
| `create_item` | A new action item: text, responsible, deadline, priority | A site in the caller's scope |

**Draft-only types** (shown with Copy and Export, never sent or written anywhere):

| Type | Why draft-only |
|---|---|
| `draft_rfi` | There is no RFI module |
| `draft_email` | SES is paused until the domain switch (owner, 10-26) |
| `note` | Free-form recommendation, with no structured target |

**Instructions in scope:** "review this day/pour/period against standards or our
procedures and tell me what to do", and "tidy up the open items for site X" (close
what records show is finished, update what has moved, add what is missing).

### 1.2 Goals

- **G1. One instruction to a stored pack**, with no clarifying questions. When the
  instruction is ambiguous, the run picks the most reasonable reading and states that
  reading at the top of the pack.
- **G2. Every claim and every action is cited** to a record (`R`), a project
  document (`D`) or a web source (`W`). Anything that cannot be cited reads
  "uncertain". It is never a guess dressed as a finding.
- **G3. Nothing changes until a person applies it.** Applying is per action,
  idempotent and audited exactly like a manual edit.
- **G4. Tenancy holds.** A run sees only what its caller can see, and applying uses
  the caller's own permissions at apply time.
- **G5. The pack is the archive.** It can be found again, read the same way later,
  exported, and its applied state is visible.

### 1.3 Non-goals (v1)

- No autonomous writes: no auto-apply, even for "obvious" closes.
- No outbound sends (email, RFI systems, third parties).
- No proactive or nightly runs. Standing tasks are the first follow-on (§11).
- No new todo UI. Accepted items appear in the existing Tasks views.
- No chat-style refinement of a pack. A new instruction means a new run; "rerun with
  this changed" is a v2 convenience.

### 1.4 Anti-goals: it is a failure even if it "works"

- **A1.** An action that cites evidence which does not say what the action claims.
  This is the measured DeepSeek failure: two thirds of report decisions were invented
  in the 10-21 replay.
- **A2.** An action that closes, or shows as closable, an item the caller could not
  close by hand.
- **A3.** Applying an action silently overwrites a change a person made after the pack
  was created.
- **A4.** A web page outranks the project's own documents or records on a project
  fact.
- **A5.** The run asks the user questions.

---

## 2. Grounding in what exists (recon, 2026-11-01, origin/develop)

| Area | What exists | Reuse |
|---|---|---|
| Records tools | `src/ask_tools.py`: the `TOOLS` list (`day_overview`, `search_records`, `latest_activity`, `find_people`, `count_metric`) plus `search_project_documents` (`DOC_TOOL`). Envelope `{tool, id, items, meta}`; items carry refs `R`/`D`. `execute(calls, ctx)` runs them through rag-search with identity injected from the caller, never taken from the model. | The agent's records and documents tools, **unchanged**. |
| Web lane | `ask_orchestrator.web_tool`. Items are `web_answer` (W1, ≤1,200 chars) and `web_source` (W2.., ≤4), provenance `web`; `web_answer.general_answer` runs draft → verify → labels the result "unverified" on failure. | Same item shape. The agent adds a fetch-and-snapshot step (§6). |
| Orchestration | `ask_orchestrator`: rounds (`MAX_ROUNDS` 3, `MAX_CALLS_PER_ROUND` 4), render budget of 8k tokens of evidence, `sanitize`, a fence marking evidence as data, tool-call markup scrubbing. The judge is `ASK_JUDGE_MODEL` (DeepSeek on CoreWeave on prod and TEST) with `ASK_JUDGE_FALLBACKS`. | The render, sanitize and fence code is reused as is. The loop is a new, longer-budget variant (§4). |
| Async turns | `ask_voice_turn.start_screen`: mint a turn, write state to S3 `ask_turns/`, Event-invoke the worker, poll `/api/ask/result`. The worker runs 60 s and is not in the VPC. | The same pattern, but packs need Aurora, a longer worker and a durable record (§4.1). |
| Action items | `action_items`: `stable_id` (0073), `carried_from`, `status`, `deadline`, `responsible`, `priority`, `updated_at`/`updated_by`, `audience`. Rows are superseded on every re-extraction; the identity is `stable_id`. | Address items by `stable_id`, never `id`. |
| Human edits | `patch_action_item` (org-api): ACL = company scope, site reach, then admin/gm, pm/site_manager of that site, or the assignee. One `content_edits` row per changed field, in the same transaction. | **Apply goes through this exact function** (§5). |
| Status suggestions | `action_status_suggestions` (0090): a proposal keyed by `stable_id`, accepted or dismissed by a person, unique per (`stable_id`, status). | The closest precedent: same propose-then-accept posture. |
| Continuity | Track B: `DECLARE_CONTINUITY` is on in TEST and off on prod (`PROD_DECLARE_CONTINUITY` unset); carry-forward by text otherwise. | §3.3 says how v1 degrades when continuity is off. |
| Project documents | #1145 library, `search_project_documents` with page/heading/revision. | Authoritative standards source (§6). |

---

## 3. Data model

New migration (number at build time; next free is 0092 or later). All tables carry
`company_id` and `site_id` (nullable for multi-site packs) and follow the
project-owned tenancy rule: data belongs to the site's company.

### 3.1 `action_packs`

| column | type | notes |
|---|---|---|
| id | uuid pk | |
| company_id | uuid not null | The **caller's** company at run time |
| created_by | uuid not null → users | |
| site_ids | uuid[] not null | Sites the run was scoped to, resolved from the instruction and checked against the caller's scope |
| instruction | text not null | Verbatim |
| reading | text | The run's stated interpretation (G1) |
| state | text | `queued` → `running` → `ready` / `failed` / `cancelled` |
| failure | text | A reason code only (`budget`, `model`, `tools`, `verifier`, `internal`) |
| conclusion | jsonb | `[{text, refs: [..], confidence: "supported" or "uncertain"}]` |
| run_trace | jsonb | Counts, models, timings, cost estimate; never free text |
| models | jsonb | `{planner, writer, verifier}` with the actual model ids used |
| as_of | timestamptz | When the evidence was read. Apply compares against it (§5.2) |
| archived_at | timestamptz | Hidden from the default list, never deleted |
| created_at, updated_at | timestamptz | |

### 3.2 `action_pack_sources`

One row per cited ref, so a pack is self-contained.

| column | notes |
|---|---|
| pack_id, ref | `R3`, `D2`, `W5`; unique per pack |
| kind | `topic`, `chunk`, `document`, `web_answer`, `web_source`, `item` |
| provenance | `records`, `documents`, `web` |
| record ids | `topic_id`, `chunk_id`, `document_id`, `revision`, `page_from`/`page_to`, `stable_id` (for items) |
| title, date, site_id, who | |
| excerpt | The text the model actually read, as windowed by `ask_tools` (≤ its cap) |
| url, fetched_at, snapshot_s3_key | Web only. The fetched page text is stored at `action_packs/<company>/<pack>/web/<ref>.txt` |

Why snapshot web sources: a pack is an archive (G5). A cited page that changes later
would make the pack quietly wrong.

### 3.3 `action_pack_actions`

| column | notes |
|---|---|
| id uuid pk, pack_id | |
| seq | Display order |
| type | `close_item`, `update_item`, `create_item`, `draft_rfi`, `draft_email`, `note` |
| target_stable_id | Required for `close_item` and `update_item` |
| target_snapshot | jsonb: the target item's `status`, `deadline`, `responsible`, `priority`, `text`, `updated_at` **as the run read it** |
| proposed | jsonb: the fields to set, or for `create_item`: text, site_id, responsible, deadline, priority |
| rationale | Short, cited text |
| refs | text[]: at least one ref is required for every action |
| verifier | `supported`, `weak`, `unsupported` (§4.4). `unsupported` is never shown as applicable |
| state | `proposed` → `applied` / `dismissed` / `stale` / `failed` |
| applied_by, applied_at, applied_result | jsonb: the item id and the `content_edits` ids written, or the error |
| created_item_stable_id | For `create_item` once applied |

**Continuity on or off.** v1 never targets an item it cannot address by `stable_id`.

- **TEST** (`DECLARE_CONTINUITY` on): ids survive re-extraction, so a pack made today
  can still close #69 after tonight's re-extraction.
- **Prod** (continuity off): the text-match carry-forward still carries `stable_id` on
  most re-extractions, but not all.

When apply-time lookup finds **no live row** for `target_stable_id` (§5.2), the action
becomes `stale`. The UI shows "the item changed since this pack was made". Apply never
guesses a different row. **My call:** stale beats a wrong close (A3).

"#69" in an instruction is resolved through the same lookup the Tasks page uses for
its display number. The pack stores the `stable_id`, never the display number.

### 3.4 Where agent-created items live

`action_items.topic_id` is NOT NULL, and every extracted topic is superseded on
re-extraction of its source. A `create_item` needs a parent that re-extraction never
touches.

**My call:** one synthetic topic per (pack, site), created at first apply, with:
- `source = 'agent'`
- `source_s3_key = 'agent://action_pack/<pack_id>'`
- `title` = the pack's title
- `report_date` = the apply date
- `user_id` = the applier

Builder must verify that:
- every superseding path keys on `source_s3_key` and therefore never matches `agent://`;
- day views and counts that should not show synthetic topics filter
  `source <> 'agent'`, or deliberately show them as "Added by agent";
- the export views include these topics.

Rejected alternative: an `origin` column on `action_items` with a nullable `topic_id`.
That relaxes a constraint dozens of queries rely on.

### 3.5 Retention and deletion

Packs are kept, and the user can archive them.
- **Deletion and redaction:** replaced by §14 S13. Packs are not rewritten; at read
  time, sources whose records are deleted, redacted or no longer visible are hidden.

---

## 4. Run lifecycle

### 4.1 Topology

```
UI ── POST /org/action-packs {instruction, site?} ──► org-api (in VPC)
        │  ACL: caller in scope; resolve site(s); insert pack (queued); Event-invoke ↓
        ▼
ActionAgentFunction (NOT in VPC: needs model + web egress; Timeout 900 s)
  ├─ records/documents tools ──► rag-search (in VPC) via ask_tools.execute (caller identity injected)
  ├─ web lane ─────────────────► web_answer + fetch/snapshot (S3)
  ├─ items tool (new) ─────────► rag-search new mode "items" (in VPC, read-only, ACL'd)
  └─ write results ────────────► org-api internal task "action_pack_store" (in VPC) — one call at the end,
                                  plus one "running" heartbeat
UI ── GET /org/action-packs/{id} (poll) ◄── org-api
```

**My call: a new function rather than reusing AskVoiceWorker.** The worker is sized
for 60 s turns. A pack run is minutes long, with a different budget and failure mode,
and keeping them apart means a pack run cannot starve voice turns of concurrency.

The non-VPC → in-VPC leaf pattern is the one `sendVoice` → `voice-resolve` already
uses.

### 4.2 Budgets

These are starting points. The eval (§8) measures them before any flag goes on.

| Budget | v1 |
|---|---|
| Wall clock | 600 s (function timeout 900 s; 60 s reserve for storing) |
| Tool rounds | ≤ 8, with ≤ 4 calls per round |
| Evidence rendered per model call | 8k tokens (same as Ask) |
| Web lane | ≤ 3 searches and ≤ 8 fetched pages per run |
| Cost guard | Per-run token cap from env; on breach, stop and store what is verified as a partial `ready` with `run_trace.truncated = true` |
| Concurrency | Reserved concurrency on the function (e.g. 3), and one running pack per caller |

### 4.3 Stages

1. **Plan.** One model call returns the reading of the instruction (G1), the sites,
   the date range and the evidence needs. This is the only place ambiguity is
   resolved, and the reading is shown to the user.
2. **Gather.** Deterministic first round, with no model, in the same style as Ask's
   round 1:
   - the day overview(s);
   - the open items for the scoped sites, through the items tool;
   - `search_project_documents` for the named procedure or standard.

   Then up to 7 model-chosen tool rounds over records, documents, items and web.
3. **Write.** One call produces the conclusion and the draft actions, under a strict
   JSON schema:
   - every element must list refs;
   - action types and fields come from a fixed enum;
   - targets are `stable_id`s copied from the items tool, and are never free text.
4. **Verify** (§4.4).
5. **Store.** Through org-api's internal task: pack, sources (with excerpts), actions,
   and the web snapshots already on S3.

### 4.4 Verifier: the guard against A1

The verifier is code first, then a model.

**Code checks.** Any failure drops the action, or marks it `unsupported`:
- every ref exists in this run's evidence;
- `close_item` and `update_item` targets exist in the items tool's output and are
  in-scope sites;
- a proposed status is a legal transition;
- a `create_item` is not a near-duplicate of an open item on the same site. The check
  reuses the `carry_forward` text normalisation at a high threshold. A near-duplicate
  becomes `update_item` or is dropped.

**Model check.** For each surviving conclusion line and action, a second model
receives only that claim and the text of its cited refs, and answers `supported`,
`weak` or `unsupported`.
- **My call:** the verifier is a different model family from the writer. That way
  agreement is not one model grading itself. Default writer is the Ask judge
  (DeepSeek on CoreWeave); default verifier is Gemini 3.8 Flash (`CHEAP_MODEL`).
  Both are configurable.
- `unsupported` is never shown as an applicable action. It is kept in the trace for
  the eval.
- `weak` is shown with an "uncertain" label and cannot be bulk-applied.

**Standards claims.**
- A claim citing a **document** (`D`) can be `supported`.
- A claim citing **only web** (`W`) is at most `weak`, and is labelled "web source,
  not your project's documents" (A4).
- A claim with no document and no web support says "no standard found in your
  project library", and the pack suggests uploading it. It never states a clause
  from model memory.

### 4.5 Failure behaviour

- Any stage error moves the pack to `failed` with a reason code. Evidence gathered so
  far is kept in the trace counts only; the excerpts are not stored.
- The model chain uses the existing fallback behaviour (`LLM_FALLBACK_MODELS` /
  `ASK_JUDGE_FALLBACKS`). The model actually used is recorded in `models`.
- Hitting a budget stores a partial pack: verified items only, with
  `run_trace.truncated`. The UI says it is partial.
- **Cancel** sets `cancelled`. The worker checks the pack state between rounds.

---

## 5. Apply semantics

### 5.1 One action, one request

`POST /org/action-packs/{pack}/actions/{action}/apply`, with an empty body or
`{"fields": {...}}` to apply an edited version. Editing before applying is allowed;
the edit is audited as the applier's.

- **`close_item` / `update_item`:**
  1. Look up the live row by `target_stable_id`.
  2. Call **`patch_action_item`'s logic**: the same ACL, the same field validation,
     and the same `content_edits` rows, in one transaction with the action's state
     change.

  Because the ACL is evaluated at apply time for the applier, a pack made by a
  site manager and opened later by a worker cannot be used to close that site's
  items (A2).
- **`create_item`:**
  1. Insert the synthetic topic (§3.4) and the item in one transaction.
  2. Write a `content_edits` row with field `created` and the pack/action ids, so the
     History panel shows "added from action pack ‹title›".
  3. Store the new item's `stable_id` on the action.
- **Draft types** have no apply. They have Copy, and Export as `.docx`/`.md`.

### 5.2 Idempotency and staleness

- An `applied` action returns its stored result and writes nothing. A double-click is
  safe.
- **Before** a close or update, the live row is compared with `target_snapshot`:
  - If any field the action touches has changed since `as_of`, **or** a
    `content_edits` row exists for that item after `as_of`, the action becomes
    `stale`. The response shows both values and nothing is written. **Human wins**
    (A3), consistent with status suggestions.
  - A stale action can be re-proposed by a new run. v1 does not merge.
- If the target is already in the proposed state (someone closed it by hand), the
  action becomes `applied` with `applied_result.noop = true`.

### 5.3 Bulk

"Apply all supported" runs the single-action path in order and reports per-action
results. It never applies `weak` actions. It is not one transaction across actions:
one stale action must not block the others.

---

## 6. Web lane

- **Same item shape as Ask.** `web_answer` and `web_source`, refs `W`, provenance
  `web`. Code that renders or cites Ask web items works on packs unchanged.
- **Plus fetch and snapshot.** For each `web_source` a writer cites, the agent
  fetches the page once:
  - text extraction only, ≤ 200 kB;
  - a timeout per page;
  - a robots-respecting user agent.

  The extracted text goes to S3 (§3.2), and the excerpt the writer used is stored
  in `excerpt`. A page that fails to fetch keeps the URL and is marked
  `snapshot: none`; the verifier then caps its claims at `weak`.
- **Priority** (A4): records, then project documents, then web.
  - NZ standards (NZS 3109 and similar) are mostly paywalled, so web results are
    usually secondary sources (BRANZ, suppliers, councils).
  - The planner is told to search project documents first, and the writer to use
    the web only for what documents and records lack.
- **What leaves the system.** The search engine receives queries the agent writes
  from the instruction. Like Ask's rule, it never receives record excerpts.
  **My call:** queries are written from the instruction and the named standard only,
  never from transcript text. A query generator that pastes a transcript sentence
  into a search engine is a data leak.
- **Language.** Instructions may be Chinese or English; web queries are English
  (NZ sources). The pack is written in the instruction's language.

---

## 7. Permissions and tenancy

- **Creating a pack.** Any member with Ask access, scoped to the sites they can see.
  The site in the instruction is resolved against `visible_scope`. An instruction
  naming a site outside scope gets a reading that says "site not found in your
  projects". It never errors with the site's existence.
- **Run identity.** Every tool call goes through `ask_tools.execute` and the items
  tool with the creator's `caller_sub`. The model never chooses company, user or ACL
  (an `ask_tools` invariant).
- **Reading a pack.**
  - Its creator.
  - Admin/GM of the pack's company.
  - **My call:** pm/site_manager of every site in `site_ids`. A pack about a site is
    site business; a worker's pack about a site is visible to that site's manager.

  External members see packs they created, limited to the sites they are members of.
- **Applying.** Read access plus `patch_action_item`'s ACL for each action (§5.1).
- **Company exit** (spec 2026-11-01). Inactive companies cannot create, run or apply
  packs. A run in flight when the company becomes inactive finishes into `failed`
  with reason `company_inactive`.

---

## 8. Evaluation: gates before any flag goes on

Measured on TEST data and on read-only prod copies. The method follows the retrieval
ruler and Track B continuity measurements.

1. **Gold set.** At least 20 instructions over real site-days, with the owner's
   expected actions:
   - the 10-10 pour scenario;
   - "tidy open items" for 3 sites;
   - 5 instructions naming a standard that **is** in the project library;
   - 5 naming one that **is not**.

   A blind second annotator labels a subset, which gives the ceiling (memory:
   second-annotator method).
2. **Metrics.**
   - **Fabrication rate:** shown claims or actions whose cited refs do not support
     them, as judged by a person.
   - **Wrong-target rate:** close/update on the wrong `stable_id`.
   - **Recall** of expected actions.
   - **"Uncertain" honesty:** standards claims without a document marked uncertain.
   - Latency p50/p90 and cost per run.
3. **Thresholds** (**my call**, owner may tighten):
   - fabrication ≤ 5% of shown items;
   - wrong-target = 0 on the gold set;
   - recall ≥ 60%;
   - p90 ≤ 5 min.
4. **Measured noise floor first.** Run the gold set twice with no change before
   comparing variants (memory: "one before/after diff proves nothing").
5. **Every number is counted outside the model.** No model-graded thresholds.

---

## 9. Rollout

| Step | Flag / action | Where |
|---|---|---|
| 1 | Migration and endpoints; flag `ACTION_PACKS=off` (create endpoint returns 404) | TEST, then prod (inert) |
| 2 | `ACTION_PACKS=internal`: platform_admin only | TEST: run the eval, fix, rerun |
| 3 | `ACTION_PACKS=on` | TEST: owner hand-test |
| 4 | Prod `internal` | Prod: owner runs a few real instructions |
| 5 | Prod `on` | Only after the §8 thresholds pass and `PROD_DECLARE_CONTINUITY` is on (or the owner accepts stale-rate data showing it is low with continuity off) |

Any prod step is the owner's. Release PRs carry the flag default `off`.

---

## 10. UI contract (hand-off to the ui session)

Endpoints, all under `/api/org`:

| Method + path | Body / query | Returns |
|---|---|---|
| POST `/action-packs` | `{instruction, site_id?}` | `{id, state: "queued"}`; 409 if the caller already has a running pack |
| GET `/action-packs` | `?state=&site_id=&archived=` | List: id, title, state, created_at, site names, counts by action state |
| GET `/action-packs/{id}` | | The pack: reading, conclusion with refs, actions with verifier label and state, sources (excerpt, url, snapshot link) |
| POST `/action-packs/{id}/cancel` | | |
| POST `/action-packs/{id}/archive` | `{archived: bool}` | |
| POST `/action-packs/{id}/actions/{aid}/apply` | `{fields?}` | `{state, applied_result}` or `{state: "stale", live, snapshot}` |
| POST `/action-packs/{id}/actions/{aid}/dismiss` | | |
| GET `/action-packs/{id}/export` | `?format=docx|md` | The report/email view |

UI requirements:
- Poll GET every 3–5 s while `queued`/`running`; show the reading as soon as it
  exists.
- Every claim shows its ref chips. Clicking one opens the same evidence card Ask
  uses (records), the document page (D) or the snapshot (W).
- `weak` actions carry an "uncertain" badge and are excluded from Apply-all.
- Applied items link to the task; created tasks show "Added from ‹pack›" in history.
- Entry points:
  - Ask: "Do this in the background" turns the question into an instruction;
  - Day view: "Review this day";
  - Tasks: "Tidy open items".

---

## 11. Follow-on capabilities, in order (not v1)

1. **Standing tasks / nightly watch.** The same pack pipeline on a schedule ("every
   evening, review today's pours"), delivered to the web home first. Push and email
   come after the SES switch.
2. **Commitment ledger.** "Who promised what by when", from Track B decisions and
   items. Needs prod continuity on.
3. **Standards gap check.** Inspection gaps, already on TEST, plus uploaded standards
   produce per-item gaps as pack actions.
4. **Evidence pack** for disputes and variations. Needs `EMIT_EVIDENCE` on prod and
   voiceprint identity beyond shadow mode.
5. **Lookahead.** Programme plus weather, giving next-week risks as actions.
6. **Drafting:** NCR, site instruction, toolbox talk, EOT notice, weekly report, as
   draft types.
7. **Learning from approvals.** Applied vs dismissed rates per action type tune
   prompts. Measured, never model-graded.

---

## 12. Owner questions (short)

1. *(Resolved, rev 2: §14 D4.)*
2. **Thresholds** in §8: tighten them?
3. *(Resolved, rev 2: §14 D2. Agent todos show in Tasks and in the day view.)*

## 13. Left to the builder

- The exact JSON schemas for the planner, writer and verifier.
- The `items` mode in rag-search (read-only, scoped like `visible_scope`, returning
  `stable_id`, display number, text, status, deadline, responsible, updated_at,
  site).
- Page fetch and text extraction (reuse any existing fetch helper; no headless
  browser).
- Export rendering. Reuse the report section module library where it fits.
- Migration number and index choices.

---

## 14. Revision 2: builder review (Fable) and owner-level decisions

Every finding was checked against origin/develop on 2026-11-01. Anchors that could
not be found are marked unverified. Where this section disagrees with §1–§13, **this
section wins**.

### 14.1 Owner-level decisions

The main agent made these under the owner's standing instruction to choose the
long-term reasonable option. Each one is marked **[owner-level, main agent]**.

**D1. Server-side task numbers.** Naming "#69" in an instruction needs a number
that lasts.
- Add a per-site sequential `task_no`, tied to `stable_id`.
- A number is assigned the first time a `stable_id` is inserted, travels with it
  across re-extraction, and is never reused.
- org-api exposes `task_no` and the Tasks page shows it.

This is a separate prerequisite task in the plan, before the worked scenario.
Until it ships, instructions name items by text, and the planner turns text into
`stable_id`s through the items tool. **[owner-level, main agent]**

**D2. Agent-created todos appear in Tasks *and* in the day view** of the date they
were applied, with an "Agent" source badge.
- Timeline rows carry `topics.source`.
- The live/nightly label must not call `agent://` topics nightly. The label is
  `t["is_live"]` in `src/repositories/topics.py:774` (`source_s3_key` starts with
  `extractions/`), so an `agent://` topic currently reads as nightly.
- No logic that hides cards.

This reverses the rev 1 default (§12 Q3). **[owner-level, main agent]**

**D3. Kickoff is an S3 request object plus `wire-s3-events`.** Wiring the new
prefix on prod is an owner step in the rollout table. **[owner-level, main agent]**

**D4. A pack belongs to its site's company, and v1 packs cover one site.**
- `site_id uuid NOT NULL` replaces `site_ids uuid[]`.
- `company_id` is the site's company, not the caller's.
- Readers: the creator, that company's admins/GM, and that site's pm/site_manager.

**[owner-level, main agent]**

**D5. Company exit is not a dependency.** v1 ships without `company_inactive`. If
`src/tenancy_status.py` exists on develop at build time (it did not at rev 2), the
create, run-claim and apply paths call it. **[owner-level, main agent]**

**D6. A web draft that fails verification is never citable.** A `web_answer` whose
status is not verified is dropped from the pack's evidence. It cannot be cited, not
even as `weak`. **[owner-level, main agent]**

### 14.2 Builder findings folded in

**S1. Kickoff, queue and cancel** (replaces the Event-invoke in §4.1).
- **Kickoff.**
  - org-api writes `action_pack_requests/<pack_id>.json` to the data bucket. It
    holds the pack id, the caller's sub and the site id; never the instruction text.
  - `scripts/wire-s3-events.sh` gains `ActionAgent ← action_pack_requests/**.json`.
  - The TEST wiring runs in `deploy.yml`; the prod wiring is an owner step.
- **Duplicate deliveries.** S3 may deliver the same notification more than once.
  - The agent's first act is a compare-and-swap claim, `queued → running`, through
    an org-api internal task (RequestResponse; result payload up to 6 MB).
  - A second delivery loses the claim and exits.
- **No automatic retries.** Set `EventInvokeConfig MaximumRetryAttempts: 0` on
  ActionAgentFunction. A retry would rerun a paid run.
- **Cancel.**
  - org-api sets `cancelled` and writes an S3 cancel marker.
  - The worker checks the marker between rounds, following the
    `ask_voice_turn.is_cancelled` pattern (verified at `src/ask_voice_turn.py:148`).

**S4. Items tool scope.**
- It returns items under `source='agent'` topics too, so a later pack can close an
  agent-created todo.
- It applies the caller's author filter exactly as Ask does (`_author_filter` in
  org-api, verified at `src/lambda_org_api.py:8885`).
- The Ask tools' scoping is `_ask_tools_where` (`src/repositories/topics.py:1340`).
  Its source clause admits only `extractions/%` (and `reports/%` without an
  extraction), so the items tool must add `OR t.source = 'agent'` rather than
  reuse it unchanged.

**S5. Web query isolation** (tightens §6).
- Web queries are generated in the **Plan** stage, before any record evidence is
  shown to a model.
- Later stages may call `web_search` only with one of those fixed query ids. Code
  enforces this.
- Each query is at most 300 characters (`MAX_QUESTION_CHARS`, verified at
  `src/ask_rewrite.py:27` and `src/question_admission.py:24`).
- Every page fetch has an SSRF guard:
  - https only;
  - the resolved address must not be private, link-local or metadata;
  - every redirect is checked again.
- Fetches are size-capped: at most 2 MB downloaded and 200 kB of text kept.
- No fetcher exists in the repo today, so this is new code with its own tests.

**S6. Verifier independence.**
- The verifier must come from a different model family than the model the writer
  **actually used**, as recorded after fallbacks.
- If the writer fell back onto the verifier's family, the run uses the second
  verifier from `ACTION_VERIFIER_FALLBACKS`.
- If no remaining verifier differs, the pack fails with reason `model`.

**S7. Write-stage rendering.** The Write stage renders its own evidence: the full
stored excerpt of every ref it may cite, inside its own budget. It does not reuse
Ask's `_budgeted` render, which trims to fit a 40 s turn.

**S8. Time and resource budget** (replaces the wall-clock line in §4.2).
- Fixed reserves: 90 s for write and 90 s for verify, with the verify calls in
  parallel. The tool rounds get whatever is left of the 600 s target.
- This function's `LLM_CHAIN_BUDGET_SECONDS` stays below 900 s minus the store
  reserve.
- MemorySize is at least 1024 MB.
- The rag-search hop retries once, and only on throttling or a 5xx.

**S9. Heartbeat and stale-run sweep.**
- The worker writes `heartbeat_at` through the org-api task after each round.
- A sweep marks any `running` pack whose heartbeat is older than 20 minutes as
  `failed` with reason `internal`. The sweep runs from an existing scheduled
  function, or from org-api on list.

**S10. Apply validation.**
- `responsible` must match a `members_for_site` display name, the same rule as
  `patch_action_item`.
- `text` is never patchable by an action.
- Allowed status transitions:

  | from \ to | in_progress | blocked | done | closed | open |
  |---|---|---|---|---|---|
  | open | ✓ | ✓ | ✓ | ✓ | – |
  | in_progress | – | ✓ | ✓ | ✓ | ✓ |
  | blocked | ✓ | – | ✓ | ✓ | ✓ |
  | done / closed | – | – | – | – | ✓ (reopen; the agent never proposes this in v1) |

- **noop** applies only when every field the action touches already equals the
  proposal. Any other difference makes the action `stale`.

**S11. The human-edit check walks the `carried_from` chain.** A human edit can sit
on an earlier row of the same `stable_id`. The check looks for `content_edits` rows
after `as_of` on any row in that chain, or compares the carried `updated_at`.

**S12. Apply concurrency and lookup.**
- Apply takes `pg_advisory_xact_lock(hashtext(source_s3_key))`, the same lock the
  writer path takes for that source.
- It uses a new `action_items.get_live_by_stable_id`, mirroring
  `topic_questions.get_live_by_stable_id` (verified at
  `src/repositories/topic_questions.py:106`).

**S13. No anonymiser exists** (replaces §3.5). Packs are never rewritten.
- At read time, each source row is checked against its live record, following the
  `ask_thread_check` pattern (`topics.get_topic_visible` with the reader's
  `site_ids`/`author_ids`; verified at `src/ask_thread_check.py:95-128`).
- A source whose record has been deleted, redacted or is no longer visible shows as
  "source removed", without its excerpt.
- A claim or action that cites only removed sources is hidden.

**S15. "Applicable by you."** The UI's applicable flag uses the same check as
`_action_item_edit_acl` (verified at `src/lambda_org_api.py:7292`), so the UI never
shows an Apply button that would be refused.

**S16. Smaller items.**
- `ACTION_PACKS` is wired through both deploy workflows, with the bare-`Key` guard
  against misformed parameter overrides.
- Web snapshots are stored on the lake bucket and recorded with
  `lake_objects.record_best_effort` (verified at
  `src/repositories/lake_objects.py:109`).
- An env-parity test covers the new function, and the function is gated on
  `HasOrgApi`.
- `todo_collapse` merges duplicates at read time (verified, `src/todo_collapse.py`).
  The near-duplicate check in §4.4 must agree with it, so a created item never
  collapses onto the item it was meant to differ from.
- The eval reports the writer's raw rates and the post-verifier rates separately.
- `action_packs` gains a `title` column, written by the Plan stage.

### 14.3 Unverified at rev 2

- `src/tenancy_status.py` is not on develop yet (D5).
