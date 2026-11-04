# Site decision loop: site events run themselves, only decisions reach a person

Status: design, rev 2 (2026-11-05). Architecture research. Not built, no flag, no migration yet.
Rev 2 folds in a Fable builder check (§15): six grounding corrections and seven builder
questions, answered as my calls where the owner had not ruled.
Source: owner conversation 2026-11-03..05 after reading 卡兹克's "Agent 自动化系统" article (2026-11-02).
Diagram (owner-reviewed, Fable-reviewed rev 6): https://claude.ai/artifact/TKCKhHFSduQuU3HWdz4Hif
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

## 2. Grounding: what exists (origin/develop 69fb69a8, 2026-11-05)

Each line names a piece the loop reuses. Items marked (verify) were read from specs or
memory, not re-read in code for this document.

| Piece | Where | Role in the loop |
|---|---|---|
| Session end / finalize, extraction | `lambda_extract_session`, `lambda_item_writer` | event source "session ended"; handler output |
| Decisions extracted per topic | 0058 `topics.decisions` jsonb, plus `topic_decisions` rows with `stable_id` / `carried_from` since 0073 (`repositories/topic_decisions.py`) | not a ledger (rewritten per extraction). A meeting decision's `subject_ref` is `topic_decisions.stable_id`, as status suggestions key on `action_items.stable_id`. Caveat: ids survive re-extraction only partly on prod while `PROD_DECLARE_CONTINUITY` is false |
| Checklist gaps (step B) | spec 2026-10-16, on prod; **computed at read time** from the session-report result JSON, no table, null until status `done`, reset by a longer-window re-extraction | needs a new emitter (§4.1) |
| Check done (step A) | #1149, TEST; invoked from `AskVoiceWorkerFunction`, on demand with a human caller | not a background handler in v1 |
| Status suggestions | spec 2026-10-27, migration 0090 | closest existing card: propose, human accepts/dismisses, model never writes status |
| Action packs | spec 2026-11-01, #1210, migrations 0093-0095, TEST internal; runs as `created_by_sub`; records planner/writer/verifier model ids (`action_pack_stages.py`) | stays its own entry point in v1 (§4.8) |
| Multi-unit walks | spec 2026-11-03, #1218, migration 0096 | handler; also the first user of a project location list (§9) |
| Model fallback | **three separate chains**: `llm_utils.model_chain` (`LLM_FALLBACK_MODELS`), `llm_tools_client._model_chain` (`ASK_JUDGE_FALLBACKS`, ignores the former), action packs (`ACTION_VERIFIER_FALLBACKS`) | the model-call layer (§4.3) is a refactor of two clients, not a wrapper; deferred past P0 |
| Tool-calling client with Anthropic route | `llm_tools_client.py` (#1214) | Claude models go only via `ANTHROPIC_API_KEY`, never OpenRouter (owner rule 2026-11-04) |
| Token accounting | `llm_usage.log_usage`: provider, model, caller (call-site name), latency, tokens. **No company, site, run id or cost** | a per-company budget needs a run context threaded through the clients (§4.3) |
| Device liveness | `devices.last_seen_at` (`device_heartbeat.py`), `device_alerts.py` "quiet" rule in working days, pushed to ops via Notion. `device_upload_freezes` (0035) is written by nothing yet | "device offline" is a new timer scan, not an existing source |
| Company status | migration 0092 (`tenancy_status.py`), `COMPANY_STATUS_ENFORCE` | hub drops events for exited companies |
| Project documents | #1145 | source for the project location list (§9) |
| Roles | `global_role` admin / gm / pm / worker / platform_admin; site roles pm / site_manager / worker; external memberships. No foreman, H&S, QS, no delegation | v1 maps cards onto these (§6.4) |
| Channels | email via `EMAIL_SENDER` (SES or stub), device voice (`lambda_ws_send_voice`). No push, no Teams | P1 uses a web card inbox + email (§12) |
| Inspection pass/fail | none: 0082 / 0084 carry no pass status, org-api has no write path | "mark as passed" is out of v1 (§6.2) |

## 3. The loop in one paragraph

An event arrives → the hub authenticates it and pins it to company + project → drops it as
"late" if it describes something already over → dedupes → matches a rule (no rule: unmatched
pool) → sets urgency → queues → dispatches to a handler → the handler gathers evidence and
runs through the model-call layer → the check stage verifies evidence, runs a cross-vendor
review (≤3 rounds), enforces project isolation, and asks "does a person need to decide?" →
if no: publish (internal) and verify; if yes: write a card → the judgement stage checks the
ledger, routes to people with authority, chases until a deadline, applies a conservative
default on timeout, writes the answer to the ledger → the answer is a new event, back to the
hub. Failures and timeouts at any stage report back to the hub, which retries up to 3 times
and then raises a fault card to the company admin and FieldSight ops (not the PM).

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

**Scheduled scans are a source.** "Overdue to-do", "inspection due", "device silent for N
hours" do not happen by themselves; a timer scan emits them (the article's 定时 source).

**Emitters for the two P0 sources (my call):**
- `checklist.gap`: emitted by `lambda_session_report` when a checklist result lands with
  status `done`, **only for the final extraction of a session** (not live, not a re-run that
  replaces `result_key` with identical gaps). `subject_ref` = (session id, checklist item key).
  A later run that still has the gap hits the same `dedupe_key` and is dropped; a run where the
  gap disappeared closes any open card for it as `superseded`.
- `todo.overdue`: a daily timer scan (EventBridge) over open `action_items` with a parsed due
  date in the past. `subject_ref` = `action_items.stable_id`; bucket = the NZ day, so one item
  raises at most one event per day and the ledger's "asked before" rule stops repeats.

### 4.2 Hub

1. **Auth + tenancy.** Resolve `company_id`/`site_id` from the source; refuse events for
   companies whose status is not active (0092).
2. **Late?** If `occurred_at` plus the rule's relevance window is already past (the pour has
   happened; the device was offline for six hours), record it as after-the-fact only. No card.
3. **Dedupe.** Key = (`company_id`, `site_id`, `type`, `subject_ref`, coarse time bucket).
   `decision.made` events are keyed by card id and are never merged with the event that
   produced the card (Fable review issue 5).
4. **Match rule.** Rules table, one row per `source`+`type`:
   handler, model tier, skill, concurrency, who to notify, urgency inputs, per-day cap.
   Companies may override a small set: who receives cards, per-day cap, quiet hours.
   - **No rule → unmatched pool** (visible on an admin page). If the same type recurs
     (starting value: 5 times in 7 days), an agent drafts a rule and sends a card to the
     FieldSight ops admin; approved rules are written to the table. Otherwise it goes into a
     weekly digest to the company admin. An unmatched event that falls in an ask-first
     category (§6.2) skips the pool and goes straight to a person.
5. **Urgency** (§7). 6. **Queue** (urgent work bypasses per-day caps and budget pauses).
7. **Dispatch** to the handler.
8. **Results.** Every handler returns success or failure (including timeout). Failure:
   back off and redispatch; on the 3rd failure, fault card to company admin + ops. Success
   without a person needed: close.

### 4.3 Handlers and the model-call layer

Handlers are the existing pieces (§2) plus small new ones. **P0 handlers are deterministic**
(a gap or an overdue item becomes a card from data, no model), so P0 does not wait for this
layer; it arrives with the first model-backed rule (P2/P3). When it does, every model call
from any stage goes through one layer (today there are three separate fallback chains, §2):
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
4. **Gate: does a person need to decide?** Yes when the next step is in the ask-first list
   (§6.2), or the handler needs a choice or a fact it cannot infer (§6.1). Yes → card. No →
5. **Publish internally** (report, to-do, timeline). Outward sends never happen here.
6. **Verify after publish:** the item is actually there / the email actually left. One
   resend, then failure.

### 4.5 Judgement stage

1. **Write the card** (§6.3), with deadline and "if nobody answers".
2. **Ledger lookup:** decided before → act on it, no card. Asked before and nothing changed →
   do not re-send.
3. **Route** to people who have authority for this project and this kind of decision
   (§6.4), by urgency, through their channels (app, email, Teams, device voice).
4. **Delivery check;** undelivered → next channel.
5. **Deadline:** before it, one reminder, then escalate to the delegate. At the deadline with
   no answer → the card kind's conservative default (§6.2), and a ledger row "timed out".
6. **Authority check on the answer:** "has authority to decide for this project?" (owner
   ruling: not "is it the PM"; delegation while on leave must work). No → not counted, the
   person is told.
7. **Ledger write** (append-only, §5.3) → a `decision.made` event → hub.

### 4.6 Records

All rows carry `company_id` and `site_id`. Event log, run log (model, tokens, latency,
outcome), cost ledger, check log, decision ledger.

### 4.7 Chain depth

A decision event inherits `chain_id` and increments `chain_depth`. At depth 3 the hub records
the event and stops asking, so two handlers cannot keep triggering each other.

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
`status` (`late`, `unmatched`, `queued`, `running`, `done`, `failed`, `blocked`), `rule_id`.

### 5.2 `event_runs`
`run_id`, `event_id`, `attempt`, `handler`, `started_at`, `ended_at`, `outcome`, `error`,
`tokens_in`, `tokens_out`, `cost_usd`, `model_calls`, `models` (per-call model names; action
packs already record planner/writer/verifier ids, reuse that shape).

### 5.3 `decision_cards` and `decision_ledger`
- `decision_cards`: `card_id`, `event_id`, `company_id`, `site_id`, `kind`
  (`approve`, `choose`, `fact`, `fault`, `rule`), `question`, `consequence`, `options` (jsonb,
  one marked recommended), `evidence` (jsonb spans), `urgency`, `deadline_at`,
  `timeout_default`, `recipients`, `status` (`open`, `answered`, `timed_out`, `superseded`).
- `decision_ledger`: **append-only**, enforced by a trigger that rejects UPDATE and DELETE
  (the stack has a single DB role, see 0092/0093/0095 "No GRANT", so a grant cannot do it).
  `entry_id`, `company_id`, `site_id`, `card_id` (nullable: meeting decisions confirmed
  later), `subject_ref`, `decision`, `kind` (`decided`, `declined` = "decided not to do"),
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
| Accept a gap on a safety item | yes, reason required, copy to H&S rep | not accepted |
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
FieldSight ops. Escalation goes pm → company admin (no delegate table yet). New roles (H&S,
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

## 7. Urgency

Owner, 2026-11-04:
- **Possible injury → highest, always** (override, not a rank): bypasses per-day caps and
  budget pauses, and copies the H&S representative.
- Then by: (1) time to the deadline, (2) downstream work it blocks, (3) complexity and number
  of stakeholders (more people, more friction, so earlier).

The builder turns (1)-(3) into a score; the owner reviews the weights on real cards in the
shadow phase.

## 8. Brakes against runaway cost

Any brake that trips stops the work and sends one alert.
1. Model calls per attempt: 8.
2. Attempts per task: 3, with growing back-off (worst case 24 calls).
3. Chain depth: 3.
4. Per-company daily budget: warn at 80 %, pause non-urgent work at 100 %.
5. Circuit breaker: a burst of the same error type pauses that handler, one alert.

Plus the existing ones: cross-vendor review ≤3 rounds; dedupe so one thing runs once.
Numbers are starting values, to be set from shadow-phase measurements.

## 9. Project location list

Needed to resolve "the north wall" and by multi-unit walks. Proposed (owner has not ruled):
extract buildings / units / levels from uploaded drawings and documents (#1145), the PM
confirms once with a single card, and new names heard in recordings are added as candidates
until confirmed or matched to a drawing. Manual entry optional, never required.

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
| P0 shadow | hub + rules + ledger tables; two deterministic rules (checklist gap, overdue to-do), no model calls; cards written, **not sent**; owner reviews them on a shadow-review page | ≥50 shadow cards labelled by the owner: needs-a-person precision ≥80 %, fabricated evidence 0, cards/person/day ≤3 |
| P1 internal | cards sent to FieldSight staff on Test Project, via a **web card inbox + email** (no push or Teams yet) | same metrics hold for 2 weeks; timeout defaults and pm → admin escalation fire correctly |
| P2 one customer | one pilot company, PM + foreman | PM-rated useful ≥70 %; no outward action without approval (audited) |
| P3 | more rules (device, inspection due, weather), more roles | per rule |

Prod flags, migrations and the pilot choice are the owner's. The web pieces (shadow-review
page for P0, card inbox and admin unmatched pool for P1) are fieldsight-ui work, and ui main
is merged by the owner, so they gate P0's review and P1.

## 13. Decided, my calls, open

**Owner decided (2026-11-03..05):**
- The three card purposes.
- Ask as little as possible.
- The authority test (not "is it the PM").
- The urgency factors, with injury as an override.
- Retention is deferred.
- Claude models go only through the Claude API.

**My calls (owner has seen the diagram but not ruled on these):**
- The recipient roles in §6.4, and mapping v1 onto existing roles with delegation deferred to P3.
- The conservative timeout defaults in §6.2.
- The unmatched threshold of 5 in 7 days.
- The brake numbers in §8.
- The location-list approach in §9.
- Run identity: a system principal per company and site, evidence re-filtered per recipient,
  writes performed as the answering person (§4.8).
- P0 emitters (§4.1). P0 is deterministic, and the model-call layer is deferred (§4.3).
- "Mark as passed" is out of v1.
- P1 channels are a web inbox plus email.
- Action packs keep their own entry point in v1.

**Open:**
- Channel order per role.
- Quiet hours.

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
- There are three fallback chains, not one.
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
