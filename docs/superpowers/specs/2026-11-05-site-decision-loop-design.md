# Site decision loop: site events run themselves, only decisions reach a person

Status: rev 6 (2026-11-11). P0 built and on TEST (pipeline#1278, #1282; migration 0100).
Rev 6 records the owner's 2026-11-11 ruling on the first-day overdue backlog (option B,
relayed by the web line session 前端优化v2): only newly overdue to-dos get their own card;
the older backlog becomes one digest card (§6.5, §12). On TEST the first `todo.overdue`
scan had carded all 58 overdue items at once to 3 people (p95 31 per day against ≤3).
Rev 2 folds in a Fable builder check (§15): six grounding corrections and seven builder
questions, answered as my calls where the owner had not ruled.
Rev 5 re-baselines §2 on origin/develop 43dce308 (2026-11-08), after the owner had every line
push and merge its work first, so later changes do not confuse the review. Rev 5 also folds
in a Fable review of rev 3-5 (§17).
Rev 4 records the owner's 2026-11-06 rulings: all my calls approved, the spoken location
onboarding (§9), answers do not add a chain level (§4.7), quiet hours (§6.6).
Rev 3 compares the design with the author's open-sourced code (§16,
github.com/KKKKhazix/agent-autoloop, MIT) and adopts what fits a multi-tenant product.
Source: owner conversation 2026-11-03..05 after reading 卡兹克's "Agent 自动化系统" article (2026-11-02).
Diagram (owner-reviewed): `2026-11-05-site-decision-loop/diagram.html` and `diagram.pdf` in this repo (live copy: https://claude.ai/artifact/TKCKhHFSduQuU3HWdz4Hif). Chinese version: `2026-11-05-site-decision-loop/design-zh.pdf`.
(tabs: overview / full product flow / dev flow). The product-flow tab is the picture of this document.

## 1. What and why

A project manager can follow three to five live threads at once. Today every FieldSight
outcome (report, gap list, to-do, alert) is pushed at the PM and they decide what matters.
The article's idea, carried over: site events trigger agents automatically; agents finish
what they are allowed to finish; only the small part that needs a human judgement reaches a
person, as a one-tap **decision card**, and the answer flows back as a new event.

Owner's framing: "工地上的事，自己跑；只有要拍板的，才递到项目经理面前".

### 1.1 Goals
- One entry point for everything that happens on a site (an **event**), one rules table that
  says what handles it, one place that records what happened.
- People see 1-3 cards a day per person that genuinely need them, not reports to read.
- Every card is honest: what happened, the consequence, the recommendation, the deadline,
  and what happens if nobody answers.
- Nothing irreversible or outward-facing happens before an authorised person says yes.
- A decision, once made, is never asked again (the decision ledger).

### 1.2 Non-goals (v1)
- Replacing reports, Ask, or action packs. They become handlers behind the hub.
- A generic workflow engine or customer-editable rules UI (rules are ours in v1; companies
  get a small override set, §4.2).
- Our own dev pipeline (the diagram's "dev" tab). That is a separate, later piece.

### 1.3 Anti-goals: a failure even if it "works"
- A PM gets more notifications than before.
- A card whose quoted evidence is not in the recording (fabrication).
- An outward action (email to a subcontractor, marking an inspection passed) taken before
  approval, or taken on a timeout.
- Any event, card, ledger row or record visible across companies.
- A loop that keeps calling models with no bound.
- "Accept as is" on a safety item with no recorded reason.

## 2. Grounding: what exists (origin/develop 43dce308, 2026-11-08; first written on 69fb69a8)

Each line names a piece the loop reuses. Items marked (verify) were read from specs or
memory, not re-read in code for this document.

| Piece | Where | Role in the loop |
|---|---|---|
| Session end / finalize, extraction | `lambda_extract_session`, `lambda_item_writer` | event source "session ended"; handler output |
| Decisions extracted per topic | 0058 `topics.decisions` jsonb, plus `topic_decisions` rows with `stable_id` / `carried_from` since 0073 (`repositories/topic_decisions.py`) | not a ledger (rewritten per extraction). A meeting decision's `subject_ref` is `topic_decisions.stable_id`, as status suggestions key on `action_items.stable_id`. `PROD_DECLARE_CONTINUITY` is true on prod since 2026-11-05 (set for status suggestions), so ids carry across re-extraction on prod as on TEST |
| Checklist gaps (step B) | spec 2026-10-16, on prod. A `checklist_reports` row (0084: `company_id`, `result_key`) exists per report, but the **gaps themselves live in the S3 result JSON** (`lambda_org_api.py:5612`, `:5653`), null until status `done`, reset by a longer-window re-extraction. `lambda_session_report` is non-VPC, has no DB access and does not know whether an extraction is final; the final gate is in `lambda_item_writer.py:1662-1667` | needs a new emitter (§4.1) |
| Check done (step A) | #1149, TEST; invoked from `AskVoiceWorkerFunction`, on demand with a human caller | not a background handler in v1 |
| Status suggestions | spec 2026-10-27, migration 0090; **on prod since 2026-11-06** (`PROD_STATUS_SUGGESTIONS=true`) | closest existing card, live with real users: propose, human accepts/dismisses, model never writes status. Its accept/dismiss rates are the first real measure of how people treat a one-tap suggestion |
| Action packs | spec 2026-11-01, #1210, migrations 0093-0095, TEST internal; runs as `created_by_sub`; records planner/writer/verifier model ids (`action_pack_stages.py`). Since rev 4: config (#1230), date handling (#1229, #1249), targets (#1235), a scope gate with field provenance (#1247), notes survive the writer schema (#1248) | stays its own entry point in v1 (§4.8) |
| Multi-unit walks | spec 2026-11-03, #1218, migration 0096 | handler; also the first user of a project location list (§9) |
| Model fallback | **four separate chains**: `llm_utils.model_chain` (`LLM_FALLBACK_MODELS`), `llm_tools_client._model_chain` (`ASK_JUDGE_FALLBACKS`, ignores the former), action-pack writer (`ACTION_WRITER_FALLBACKS`, `lambda_action_agent.py`), action-pack verifier (`ACTION_VERIFIER_FALLBACKS`, excludes the writer's family) | the model-call layer (§4.3) is a refactor of two clients, not a wrapper; deferred past P0 |
| Tool-calling client with Anthropic route | `llm_tools_client.py` (#1214) | Claude models go only via `ANTHROPIC_API_KEY`, never OpenRouter (owner rule 2026-11-04) |
| Token accounting | `llm_usage.log_usage`: provider, model, caller (call-site name), latency, tokens. **No company, site, run id or cost** | a per-company budget needs a run context threaded through the clients (§4.3) |
| Device liveness | `devices.last_seen_at` (`device_heartbeat.py`), `device_alerts.py` "quiet" rule in working days, pushed to ops via Notion. `device_upload_freezes` (0035) is written by nothing yet | "device offline" is a new timer scan, not an existing source |
| Company status | migration 0092 (`tenancy_status.py`: active / suspended / exited), `COMPANY_STATUS_ENFORCE` | hub drops events for companies that are not active |
| Project documents | #1145 | source for the project location list (§9) |
| Roles | `ALLOWED_GLOBAL_ROLES` (`lambda_org_api.py:245`): admin, gm, regional_manager, pm, site_manager, worker, platform_admin; site roles pm / site_manager / worker; external memberships. Existing authority predicate `is_site_authority` = site role pm or site_manager (`:9079`). No foreman, H&S, QS, no delegation | v1 maps cards onto these (§6.4); `is_site_authority` seeds the answer authority check (§4.5.6) |
| Channels | email via `EMAIL_SENDER` (SES or stub), device voice (`lambda_ws_send_voice`). No push, no Teams | P1 uses a web card inbox + email (§12) |
| Inspection pass/fail | none: 0082 / 0084 carry no pass status, org-api has no write path | "mark as passed" is out of v1 (§6.2) |
| Action items | `action_items.deadline` date (0003), `deadline_text` unparsed fallback (0011), status `'open'` (`repositories/action_items.py:152`); has `site_id`, **no `company_id`** (resolve through the site) | source of `todo.overdue` (§4.1) |

## 3. The loop in one paragraph

An event arrives → the hub authenticates it and pins it to company + project → drops it as
"late" if it describes something already over → dedupes → matches a rule (no rule: unmatched
pool) → sets urgency → queues → dispatches to a handler → the handler gathers evidence and
runs through the model-call layer → the check stage verifies evidence, runs a cross-vendor
review (≤3 rounds), enforces project isolation, and asks "does a person need to decide?" →
if no: publish (internal) and verify; if yes: write a card → the judgement stage checks the
ledger, routes to people with authority, chases until a deadline, applies a conservative
default on timeout, writes the answer to the ledger → the answer is a new event, back to the
hub. Failures and timeouts at any stage report back to the hub, which backs off and retries;
on the 3rd failure a fault card goes to the company admin and FieldSight ops (not the PM);
after the 5th it stops and the same fault card is updated to "stopped" (no second card).

## 4. Components

### 4.1 Event envelope

Every source writes the same short record (the article's "very short message"):

| Field | Notes |
|---|---|
| `event_id` | uuid |
| `company_id`, `site_id` | set by the hub from the source's own identity, never trusted from the payload |
| `source` | `session`, `checklist`, `todo_scan`, `inspection_scan`, `device`, `feedback`, `weather`, `programme`, `decision`, `fault`, ... |
| `type` | e.g. `session.ended`, `checklist.gap`, `todo.overdue`, `decision.made` |
| `subject_ref` | what it is about: session id, checklist id + item, action item id, device id, card id |
| `occurred_at` | when it happened on site (device clock / NZ), distinct from `received_at` |
| `summary` | one line, for logs and the unmatched pool |
| `link` | back to the source screen |
| `chain_id`, `chain_depth` | a decision event inherits the chain of the card that caused it (§4.7) |

**Events carry data, never instructions** (borrowed, §16). Unknown fields are rejected at the
hub. `summary` and any free text (transcript quotes, feedback) never enter a model prompt as
instructions; prompts get only validated fields (`type`, ids, links) and evidence spans
fenced as quoted data. Transcripts are outside text, so this matters more for us than for
the author.

**Scheduled scans are a source.** "Overdue to-do", "inspection due", "device silent for N
hours" do not happen by themselves; a timer scan emits them (the article's 定时 source).

**Emitters for the two P0 sources (my call):**
- `checklist.gap`: **a timer scan in a VPC function** over `checklist_reports` rows that
  reached status `done` since the last scan, reading each result JSON from S3. Not emitted from
  `lambda_session_report`: that function has no DB access and cannot tell a final extraction
  from a live one or an on-demand re-queue (§2). A report counts only if its session's latest
  extraction is final (the `tier == "final"` gate in `lambda_item_writer`). `subject_ref` =
  (session id, checklist item key); no time bucket. A later report that still has the gap is
  dropped by `dedupe_key` and the open-card key; a report where the gap disappeared closes any
  open card for it as `superseded`.
- `todo.overdue`: a daily timer scan (EventBridge) over `action_items` with status `'open'` and
  `deadline` (date) in the past; items with only an unparsed `deadline_text` are skipped in P0.
  `company_id` comes from the item's site. `subject_ref` = `action_items.stable_id`; bucket =
  the NZ day. `occurred_at` = the scan day, not the due date, so long-overdue items are not
  dropped as late. While a card for the item is open no new card is made (open-card key);
  "asked before" is a `decision_cards` lookup, not a ledger one.

### 4.2 Hub

1. **Auth + tenancy.** Resolve `company_id`/`site_id` from the source; refuse events for
   companies whose status is not active (0092).
2. **Late?** If `occurred_at` plus the rule's relevance window is already past (the pour has
   happened; the device was offline for six hours), record it as after-the-fact only. No card.
3. **Dedupe.** One key, defined in §5.1. `decision.made` events are keyed by card id and
   are never merged with the event that produced the card. The table's unique index is the
   real dedupe (an SQS FIFO dedupe id only covers 5 minutes).
4. **Match rule.** Rules table, one row per `source`+`type`:
   handler, model tier, skill, concurrency, who to notify, urgency inputs, per-day cap.
   Companies may override a small set: who receives cards, per-day cap, quiet hours.
   - **No rule → unmatched pool** (visible on an admin page). If the same type recurs
     (starting value: 5 times in 7 days), an agent drafts a rule and sends a card to the
     FieldSight ops admin; approved rules are written to the table. Otherwise it goes into a
     weekly digest to the company admin. An unmatched event that falls in an ask-first
     category (§6.2) skips the pool and goes straight to a person.
5. **Urgency** (§7). 6. **Queue** (only the injury override bypasses per-day caps, budget
   pauses and quiet hours).
7. **Dispatch** to the handler.
8. **Results.** Every handler returns one of: success, nothing to do, failure, or (lease
   expired without an answer) unknown. Failure and unknown: back off and redispatch, waiting
   5 min doubling to a 1 h cap (the author's numbers). Two separate thresholds (my call):
   **notify** (fault card to company admin + ops) on the 3rd failure, **stop retrying** on the
   5th. The author stops at 10; we stop earlier because each attempt can spend model calls
   (§8). **Failure path only:** while a run is `queued` waiting to retry, new events for the
   same subject are joined to it (`run_events`, §5.2) and the handler receives all of them on
   the next attempt; an event that arrives while a run is `running` queues behind it, it is
   not merged. (Duplicate suppression in the normal path is the dedupe key plus the open-card
   key, not this merge.) Success without a person needed, or nothing to do: close.

### 4.3 Handlers and the model-call layer

Handlers are the existing pieces (§2) plus small new ones. **P0 handlers are deterministic**
(a gap or an overdue item becomes a card from data, no model), so P0 does not wait for this
layer; it arrives with the first model-backed rule (P2/P3). When it does, every model call
from any stage goes through one layer (today there are four separate fallback chains, §2):
- **Registry:** model + parameters per task, one line to change (prompt and parameters stored
  together; see memory prompt-tuning-history-per-model).
- **Fallback chain** on provider failure (existing).
- **Accounting:** tokens, latency and cost per company per run. `LLM_USAGE` carries none of
  company, site, run id or cost today, so the hub passes a run context into each client call.
- **Brakes** live here (§8): calls per attempt, per-company daily budget.
- Claude models only through `ANTHROPIC_API_KEY`.

### 4.4 Check stage

In order:
1. **Evidence:** every claim the handler wants to show must cite a span that exists in the
   recording/document. Claims without evidence are dropped and recorded, never shown.
2. **Cross-vendor review:** a model from a different vendor than the writer checks the
   output. Problems → back to the handler, at most 3 rounds; still failing = failure (§4.2.8).
   The action-pack evals (2026-11-04) are the measured basis: Opus as verifier blocked 60 %
   of bad output while hiding 15 % of good; pick per handler from measurement, not by name.
3. **Project isolation:** the output references only this company's and this project's
   records. Failure **blocks**, alerts ops, no retry, no publish.
4. **Gate: does a person need to decide?** Two sources, combined:
   - the **handler declares** it in its result (a choice or a fact it cannot infer, §6.1);
   - the **hub re-checks** every proposed next step against the ask-first list (§6.2), in
     code, whatever the handler said.

   Yes from either → card. In the shadow phase the two are counted separately. This gate is
   our addition: the author's kernel has none and relies on the ask-first list written into
   the agent's prompt (§16), and a prompt instruction is not proof of compliance. No →
5. **Publish internally** (report, to-do, timeline). Outward sends never happen here.
6. **Verify after publish:** the item is actually there / the email actually left. One
   resend, then failure.

### 4.5 Judgement stage

1. **Write the card** (§6.3), with deadline and "if nobody answers".
2. **Lookups:** decided before (a ledger row of kind `decided` or `declined`) → act on it,
   no card. Asked before (an open card with the same card key in `decision_cards`) → do not
   re-send. A `defaulted` ledger row does not count as decided.
3. **Route** to people who have authority for this project and this kind of decision
   (§6.4), by urgency, through their channels (app, email, Teams, device voice).
4. **Delivery check;** undelivered → retry at 0/1/3/7/15/25/35/45/55 min (the author's
   schedule), then the next channel; the card stays open throughout. Each delivered message
   carries the card's version, so an update edits the original message instead of sending a
   new one. The idempotency key for a send is the card id + version.
5. **Deadline:** before it, one reminder, then escalate (v1: to the company admin). At the
   deadline with no answer → the card kind's conservative default (§6.2) and a ledger row of
   kind `defaulted`. A defaulted subject may be asked again when it changes; it is never
   treated as decided.
6. **Answer checks**, all required:
   - the answer refers to the message actually delivered (card id + version);
   - **only the first valid answer counts**; later answers are told it is already decided;
   - the person is signed in (no answer-by-link; the author's open link form suits one
     user, not a company);
   - "has authority to decide for this project?" (owner ruling: not "is it the PM";
     delegation while on leave must work). v1 starts from `is_site_authority` (site role pm or
     site_manager) plus company admin/gm. No → not counted, and the person is told.
7. **Ledger write** (append-only, §5.3) → a `decision.made` event → hub.

### 4.6 Records

All rows carry `company_id` and `site_id`. Event log, run log (model, tokens, latency,
outcome), cost ledger, check log, decision ledger.

### 4.7 Chain depth

Events emitted by a handler inherit `chain_id` and increment `chain_depth`. Events with
depth greater than 3 are recorded, not dispatched, and one fault card goes to ops, so two
handlers cannot keep triggering each other.
**A person's answer does not add a level** (owner, 2026-11-06, same as the author's code).
Only events a handler emits count, and timer scans start at 0. A chain that passes through a
person (PM decides → foreman reminded → foreman confirms) is never cut short. The cap of 3
stops agents triggering each other with no person in between, which is where a runaway
loop comes from.

### 4.8 Run identity (my call)

The hub has no human caller, yet today's read paths are per-caller (`lambda_rag_search`
needs a `sub`, `patch_action_item` checks the caller's ACL, action packs run as
`created_by_sub`). Choice:
- Handlers run as a **per-company system principal scoped to one site**, through new
  repository functions filtered by (company_id, site_id), never by a human's sub. No
  cross-site and no cross-company reads, by construction.
- **Card evidence is re-filtered per recipient** before sending: a site_manager only sees
  quotes they could see in the app (self + workers); a pm sees the site. A recipient who
  cannot see the evidence gets the card without the quote and a link to ask the pm.
- Writes that change shared records (closing an item, sending email) happen only after a
  card is answered, **as the answering person**, through the existing per-caller endpoints.
  So the ACL that protects the app also protects agent actions.
- Action packs keep their own entry point in v1 (a human asks, they run as that human). Making
  them a hub handler waits for the system principal to be proven in P0-P1.

## 5. Data model (sketch, for the builder to refine)

### 5.1 `site_events`
`event_id`, `company_id`, `site_id`, `source`, `type`, `subject_ref`, `occurred_at`,
`received_at`, `summary`, `link`, `chain_id`, `chain_depth`, `dedupe_key` (unique),
`seq` (bigserial, the replay order), `status` (`late`, `unmatched`, `duplicate`, `merged`,
`queued`, `running`, `done`, `nothing`, `failed`, `blocked`, `too_deep`), `rule_id`.
**The one dedupe key:** `dedupe_key` = (company_id, site_id, type, subject_ref, bucket), where
the bucket is set per rule (`todo.overdue`: the NZ day; `checklist.gap`: none). Events the hub
itself emits get a suffix so they can never collide with a source's keys (borrowed).

### 5.2 `event_runs` and `run_events`
`run_events(run_id, event_id)` joins every event a run handles (one at dispatch, more when a
retry absorbs same-subject events, §4.2.8).
`event_runs`: `run_id`, `attempt`, `handler`, `claim_id`, `lease_until`, `started_at`, `ended_at`,
`outcome` (`done`, `nothing`, `failed`, `unknown`, `cancelled`), `result_hash`, `error`,
`tokens_in`, `tokens_out`, `cost_usd`, `model_calls`, `models` (per-call model names; action
packs already record planner/writer/verifier ids, reuse that shape). A result reported twice
with the same `result_hash` gets the same acknowledgement; a different result for the same
claim is rejected (borrowed: makes retries and lost responses safe).

### 5.3 `decision_cards` and `decision_ledger`
- `decision_cards`: `card_id`, `event_id`, `company_id`, `site_id`, `kind`
  (`approve`, `choose`, `fact`, `fault`, `rule`), `question`, `consequence`, `options` (jsonb,
  one marked recommended), `evidence` (jsonb spans), `urgency`, `deadline_at`,
  `timeout_default`, `recipients`, `version`, `delivered_refs`,
  `status` (`open`, `answered` → `done` once the follow-up action has run, `timed_out`,
  `superseded`; a `superseded` card writes no ledger row). P0 adds `shadow_label`
  (`needed` / `not_needed` / `wrong_evidence`), `labelled_by`, `labelled_at`, `label_note`;
  labelling never changes `status`. Card key = (company_id, site_id, rule, subject_ref, purpose); dedupe applies
  only while a card with that key is open.
- `decision_ledger`: **append-only**, enforced by a trigger that rejects UPDATE and DELETE
  (the stack has a single DB role, see 0092/0093/0095 "No GRANT", so a grant cannot do it).
  `entry_id`, `company_id`, `site_id`, `card_id` (nullable: meeting decisions confirmed
  later), `subject_ref`, `decision`, `kind` (`decided`, `declined` = "decided not to do",
  `defaulted` = timed out with the conservative default; excluded from "decided before"),
  `decided_by`, `authority_basis` (role / delegation), `decided_at`, `evidence`,
  `supersedes_entry_id`, `source` (`card`, `timeout_default`, `meeting`).
  Changing one's mind is a new row that supersedes the old one.

`topics.decisions` (0058) stays as the per-extraction view. A meeting decision enters the
ledger only once confirmed (by card or by a later explicit restatement), because 0058 rows
churn on re-extraction.

## 6. Cards

### 6.1 What a card is for (owner, 2026-11-04)
Only three things:
- **Approve** an action that is on the ask-first list.
- **Choose** between options, with a recommendation.
- **Fact**: supply something the system cannot infer.

Not a card: a plain notification (that belongs in the feed or daily report).

**Ask as little as possible (owner ruling).** Anything that can be inferred is inferred and
shown as "推测，可改" (assumed, editable): "next Wednesday" resolves from the recording date;
an ambiguous place is attached to the most likely building. Ask only when the answer cannot
be inferred **and** a wrong guess has a consequence (a defect attached to the wrong building
sends people to rework the wrong place).

### 6.2 Ask-first list and timeout defaults
| Action | Ask first | Default if nobody answers |
|---|---|---|
| Send anything outside the company (subcontractor, client) | yes | do not send |
| Mark an inspection / checklist as passed (**not in v1**: no pass status or write path exists; needs its own design) | yes | not passed |
| Change the programme | yes | no change |
| Delete data | yes | no delete |
| Assign recordings to a person | yes | leave unassigned |
| Accept a gap on a safety item | yes, reason required, copy to the H&S rep (until P3: the company admin) | not accepted |
| Internal to-do, reminder, report section | no | n/a |

The default is always the side that changes nothing outside and certifies nothing.

### 6.3 Card content (honesty rules)
- One-line question, the consequence, the evidence (quote + play button), options with one
  recommended, deadline, what happens if nobody answers.
- Say only what the system can do: "remind Tom to recheck at 15:00 and send a photo", not
  "book a recheck".
- English for NZ customers. The diagram's Chinese example is illustrative.

### 6.4 Who gets cards
Target set: project manager, site foreman, H&S representative, QS (money), subcontractor
foreman (outward; always behind approval), company admin (rules, budget, faults), FieldSight
ops (circuit breaker, tenancy alerts). Recipient = the role the rule names, filtered by
authority on this project, plus any delegate.

**v1 (P0-P2) maps onto roles that exist** (my call): site `pm` → project manager,
site `site_manager` → foreman, company `admin`/`gm` → company admin, `platform_admin` →
FieldSight ops; `regional_manager` is treated as company admin for the sites it reaches.
Escalation goes pm → company admin (no delegate table yet). New roles (H&S,
QS) and a delegation table (who, for which site, from-to dates) are P3, with their own
migration and membership UI.

### 6.5 Examples (owner-approved list, 2026-11-04)
- Approve (after v1, needs pass status): "Mark Block B pre-pour inspection as passed?"
- Approve: "Send this remediation email to ABC Plumbing?" (draft attached)
- Choose: "3 pre-pour items not covered" → remind Tom / accept with reason / reassign.
- Fact: "'the north wall': Block A or Block B?" (only if the location list cannot decide
  and the item is a defect).
- Conflict: "Monday said C30, Wednesday said C35, which stands?"
- Not a card: "Is next Wednesday 12 Nov?" (inferred).
- `todo.overdue` (P0): kind `choose`, question "'<item>' was due <date>, still open", options
  *remind the owner* (recommended) / *mark done* / *set a new date* / *no longer needed*,
  deadline end of the next NZ working day, timeout default: nothing changes.
  **Rev 6 (owner, 2026-11-11, option B):** only a *newly* overdue item gets this card.
  The event's `occurred_at` is the NZ midnight that starts the day after the deadline (the
  moment the item became overdue) and the rule's `late_after_days` is 2 (my call: one daily
  scan may be missed twice and the item is still carded). So an item is carded while
  today_nz − (deadline + 1 day) ≤ 2, i.e. on its first three overdue NZ days; after that
  its daily event is recorded as late, with no card. Every live overdue item is therefore
  either carded alone (fresh) or listed in a digest (stale), never neither.
- `todo.overdue_backlog` (P0, rev 6): one event per (company, site) per NZ day when the site
  has at least one stale overdue item (one the rule above treats as late). subject_ref =
  the site id. The payload holds the total count and the first 50 items by oldest deadline
  (stable_id, text, deadline, responsible). Card: kind `fact`, question
  "<n> to-dos on <site> are more than 3 days overdue", one option *open the list*
  (recommended), no deadline, timeout default: nothing changes. Routed to the site's
  `pm`/`site_manager`, like `todo.overdue`. The owner asked for one summary per person; a
  card belongs to one site (`decision_cards.site_id` is NOT NULL, and the replay judge,
  invariants and review page join `sites`), so this is one digest **per person per site**
  (my call; a cross-site digest needs a migration and is the alternative). The card key
  dedupes only while a card is open and P0 never closes cards, so in P0 each site gets one
  digest, once. Refreshing it when the list changes is P1.
- `checklist.gap` (P0): kind `choose`, options *remind <person> to recheck* (recommended) /
  *accept with reason* (reason required; a safety item also copies the company admin) /
  *reassign*. In P0 a safety item is one whose template section or item text names safety,
  H&S or a hazard; the builder makes this a list in the rule, not a model call.

### 6.6 Quiet hours (owner, 2026-11-06)
Default 21:00-06:00 NZ time, plus all of Sunday. Cards raised in that window wait until
06:00. Saturday is a working day (NZ sites often work Saturday mornings); the owner may
change this. Possible injury ignores quiet hours (§7). Each company can change the window.

## 7. Urgency

Owner, 2026-11-04:
- **Possible injury → highest, always** (override, not a rank): bypasses per-day caps and
  budget pauses, and copies the H&S representative (until that role exists in P3: the
  company admin).
- Then by: (1) time to the deadline, (2) downstream work it blocks, (3) complexity and number
  of stakeholders (more people, more friction, so earlier).

The builder turns (1)-(3) into a score; the owner reviews the weights on real cards in the
shadow phase.

## 8. Brakes against runaway cost

Any brake that trips stops the work and sends one alert.
1. Model calls per attempt: 8.
2. Attempts per task: notify at 3, stop at 5, back-off 5 min doubling to 1 h (worst case 40
   calls).
3. Chain depth: events deeper than 3 are recorded, not dispatched (§4.7).
4. Per-company daily budget: warn at 80 %, pause non-urgent work at 100 %.
5. Circuit breaker: a burst of the same error type pauses that handler, one alert.

Plus the existing ones: cross-vendor review ≤3 rounds; dedupe so one thing runs once.
Numbers are starting values, to be set from shadow-phase measurements.

## 9. Project location list

Needed to resolve "the north wall" and by multi-unit walks.

**Spoken onboarding (owner, 2026-11-06).** Drawings are not a reliable source yet: drawing
reading is not accurate enough. PM data entry is not one either: people under site pressure
do not fill forms. So:
1. **When.** A project with no location list prompts once, wherever someone first meets it:
   - a microphone button when the project is created on the web;
   - if that was skipped, a short voice prompt on the device at the first recording on the
     project.

   Skippable; if skipped, one reminder a few days later. Once confirmed, never asked again.
2. **How.** A short spoken description, no typing (the no-manual-typing rule), for example:
   - "four storey, three zones: north, south and detached";
   - "20 houses: 6 at 115 X Street, 4 at 117 X Street, 10 at 120 X Street".
3. **Confirm card.** The system turns it into a structure and sends one card: "Is this your
   project?" with the parsed list, and the options *correct* / *say it again* / *later*.
4. **Free shape.** No fixed schema. A project can be address → unit, or building → level →
   zone, and can span several addresses. Store it as a tree of named nodes with a kind
   (address, building, unit, level, zone).
5. **Later additions are candidates.** Names heard in recordings that are not on the list,
   and anything read from drawings once that is reliable, become candidates. They are asked
   about only when it matters (a defect would attach to a place not on the list).
6. **Risks to design for (Fable review, 2026-11-08):**
   - ASR fails on digits and street or Māori names ("115" vs "150"). The confirm card shows the
     raw transcript beside the parsed tree, with the clip; each node offers "say this part
     again"; never auto-confirm.
   - Anyone may describe; only `is_site_authority` members (and company admin) confirm. The
     device prompt fires only for pm / site_manager, not for the first worker who records.
   - The list belongs to the site (`sites.company_id`). External members (0085) can read it but
     never get the prompt or confirm. No merging across sites in v1, even when two companies'
     sites share an address.
   - Node ids are stable because defects reference them: rename, never delete; edits are
     versioned. Reconcile with `inspection_windows.units` (0096), which already stores unit
     names.

## 10. Tenancy, privacy, retention

- Every table keyed by company and site; every read filtered by both; the check stage's
  isolation step is a second guard, not the only one.
- The ledger is evidence under HSWA 2015 investigations and building disputes.
  **Retention and what happens on company exit are deferred by the owner (low priority,
  2026-11-04).** Suggested default to be confirmed by a lawyer: project end + 10 years
  (Building Act 2004 s393 longstop). Until decided, nothing is deleted.

## 11. Offline sites

Devices and phones lose signal for hours. Events carry `occurred_at`; the hub's late check
(§4.2.2) stops stale cards; delivery checks fall back across channels; device voice cards
only when the device is online.

## 12. Phasing and gates

| Phase | What | Gate to move on |
|---|---|---|
| P0 shadow | hub + rules + ledger tables; two deterministic rules (checklist gap, overdue to-do), no model calls; cards written and routed (recipients computed) but **not sent**; no card timers, status stays `open`; owner labels them on a shadow-review page | ≥50 shadow cards labelled by the owner: precision = needed / (needed + not_needed) ≥80 %; fabricated evidence = count(`wrong_evidence`) = 0; cards per would-be recipient per NZ working day ≤3 (p95). In P0 every card comes from a deterministic rule, so "handler-declared vs hub-checked" is first measured in P2. Engineering gate (borrowed): a replay judge that runs `site_events` in `seq` order through the pure hub (late, dedupe, match, gate) and must reproduce `decision_cards` modulo ids; invariants as SQL checks (no card without an event, no cross-company row, no two open cards with one key); mutation testing where each planted fault is caught (drop the company filter, skip dedupe, skip the late check, duplicate an event, skip the attempt counter); nightly. Rev 6: the p95 counts cards created on or after the day option B (§6.5) reached the environment; the review route takes `since` (an NZ date) for it. TEST's 58 backlog cards of 2026-11-10 predate it. "No answer counted twice" cannot be exercised until P1 |
| P1 internal | cards sent to FieldSight staff on Test Project, via a **web card inbox + email** (no push or Teams yet) | same metrics hold for 2 weeks; timeout defaults and pm → admin escalation fire correctly |
| P2 one customer | one pilot company, PM + foreman | PM-rated useful ≥70 %; no outward action without approval (audited) |
| P3 | more rules (device, inspection due, weather), more roles | per rule |

Prod flags, migrations and the pilot choice are the owner's. The web pieces (shadow-review
page for P0, card inbox and admin unmatched pool for P1) are fieldsight-ui work, and ui main
is merged by the owner, so they gate P0's review and P1.

## 13. Decided, my calls, open

**Owner decided (2026-11-03..06):**
- The three card purposes.
- Ask as little as possible.
- The authority test (not "is it the PM").
- The urgency factors, with injury as an override.
- Retention is deferred.
- Claude models go only through the Claude API.
- 2026-11-06: every "my call" below was approved ("你定的没问题"). Also: the spoken location
  onboarding (§9); a person's answer does not add a chain level (§4.7); quiet hours (§6.6).

**My calls (approved by the owner 2026-11-06):**
- The recipient roles in §6.4, and mapping v1 onto existing roles with delegation deferred to P3.
- The conservative timeout defaults in §6.2.
- The unmatched threshold of 5 in 7 days.
- The brake numbers in §8.
- Run identity: a system principal per company and site, evidence re-filtered per recipient,
  writes performed as the answering person (§4.8).
- P0 emitters (§4.1). P0 is deterministic, and the model-call layer is deferred (§4.3).
- "Mark as passed" is out of v1.
- P1 channels are a web inbox plus email.
- Action packs keep their own entry point in v1.

**Open:**
- Channel order per role. Not needed until push or Teams exist. Starting point:
  - PM: web inbox plus a daily email digest, with an immediate email when urgent;
  - foreman: push, plus device voice when urgent;
  - company admin: email;
  - ops: internal alert.
- Saturday as a working day (§6.6).

## 14. Left to the builder
- Event transport: SQS FIFO (dedupe id = `dedupe_key`) or table-polling; EventBridge for
  timer scans.
- Whether `site_events` and `event_runs` live in Aurora (likely, for joins with tenancy) and
  how old rows are archived.
- The urgency score formula.
- How handlers declare "needs a person" (a field in their result, recommended).
- Card rendering per channel.

## 15. Builder check (Fable, 2026-11-05)

**Grounding corrections, folded into §2:**
- `topic_decisions.stable_id` exists (0073) and is the decision subject.
- Checklist gaps have no table and need an emitter.
- Check done is invoked on demand from the voice worker.
- There is no DeviceStatusWorker; `device_upload_freezes` is written by nothing.
- `LLM_USAGE` has no company or cost.
- There are three fallback chains, not one (four by rev 5, §2).
- Action packs already record model ids.
- Append-only cannot be enforced by a grant (single DB role).

**Questions and where they are answered:**

| # | Question | Answered in |
|---|---|---|
| 1 | Run identity | §4.8 |
| 2 | Roles and delegation | §6.4 |
| 3 | P0 emitters | §4.1 |
| 4 | Does P0 need the model layer? | §4.3 (no) |
| 5 | The verifier measurement source | below |
| 6 | "Mark as passed" target | §6.2 (out of v1) |
| 7 | P1 channels and ui prerequisite | §12 |

On question 5: the 2026-11-04 verifier figures come from the action-pack eval runs. They are
in the gitignored `action_pack_eval_runs/` and in the action-pack line's notes, not in the
repo. A handler's verifier choice must be re-measured on that handler's own labelled set, not
taken from those figures.

## 16. Comparison with the author's code (rev 3, 2026-11-05)

The article's system was open-sourced as github.com/KKKKhazix/agent-autoloop (MIT; commit
ee74f43). It was read only, never run. It is single-user and single-machine (Node, SQLite),
and its kernel never calls a model: it takes events, routes them, hands out work and keeps
an append-only log.

**Confirmed:**
- The default ask-first list (`protocol/rules.ts`).
- Dedupe by source + id.
- Long-poll claiming.
- Backoff and redispatch, with a notification on the 3rd failure.
- An answer becomes a new event that resumes the same thread.
- The append-only log is the only authority.
- Separate tables for events, runs, cards and deliveries.

**Where the article misled us:**

| What we inferred | What the code does |
|---|---|
| A "needs a person?" gate in the hub | None. The agent decides, and the ask-first list is only text in its prompt (`worker/contract.ts`) |
| 3 failures → hand to a person | Notify at 3, stop at 10 (`kernel/decide.ts` `MAX_ATTEMPTS`) |
| Chain depth 3 | Cap 8, answers add no level (`MAX_HOPS`) |
| Code review ≤3 rounds | One review per head commit |
| Full checks in a fresh VM | Waits for the project's own CI |
| Auto-merge | Disables GitHub auto-merge and merges itself after checking that head and main are unchanged |
| "Decided / declined" files are a mechanism | A skill convention (`docs/decisions.md`, `docs/declined.md`); the kernel only passes the last 20 decided cards as context |
| Unmatched events draft rules | Only the last 20 unmatched events are listed |

**Adopted in rev 3:**
- Merging same-subject events into a waiting retry (§4.2.8).
- Run states `nothing` and `unknown`; `result_hash` idempotency (§5.2).
- Hub-emitted key suffixes (§5.1).
- Card version, delivered refs, open-only card keys and the delivery retry schedule (§4.5.4, §5.3).
- The answer must match the delivered message, and only the first answer counts (§4.5.6).
- Data-only events (§4.1).
- Replay judge, invariants and mutation testing as the P0 engineering gate (§12).
- Backoff numbers (§4.2.8).

**Not adopted:**
- Answer-by-link without sign-in, and per-channel answerer lists (a company needs per-project authority).
- Spend shown but never enforced (we brake, §8; we borrow only its accounting of unreported runs).

**Ours alone, so we must validate it ourselves:**
- multi-tenancy;
- the late-event check;
- urgency with the injury override;
- the hub's ask-first gate before publishing;
- the deadline → remind → escalate → conservative-default path;
- the authority test;
- the trigger-enforced append-only ledger;
- the five brakes;
- the cross-vendor reviewer;
- rule drafting from the unmatched pool;
- the model-call layer;
- the three card purposes;
- infer-don't-ask.

## 17. Fable review of rev 3-5 (2026-11-08), folded in

Contradictions fixed:
- §3 now matches notify at 3 / stop at 5.
- Duplicate suppression is the dedupe key plus the open-card key; the retry merge is for the
  failure path only and needs `run_events` (§4.2.8, §5.2).
- There is one dedupe key, with a per-rule bucket (§5.1).
- A timeout writes a `defaulted` ledger row that never counts as decided (§4.5, §5.3).
- Chain depth beyond 3 alerts like the other brakes (§4.7, §8).
- Only the injury override bypasses caps (§4.2.6).
- The H&S copy goes to the company admin until P3.
- "Not active" companies, not only exited ones (§2).

Code corrections:
- The full role list and `is_site_authority` (§2, §4.5.6, §6.4).
- The `checklist.gap` emitter moved to a VPC timer scan, because `lambda_session_report` has
  no DB and does not know "final" (§2, §4.1).
- `todo.overdue` columns named (§2, §4.1).

P0 made concrete:
- the label enum and the precision formula;
- routing without sending;
- no timers in shadow;
- `occurred_at` for overdue items;
- the replay judge, invariants and mutations;
- card shapes for both rules (§5.3, §6.5, §12).

§9 risks written down: ASR on digits and names, who confirms, external members, stable node ids.
