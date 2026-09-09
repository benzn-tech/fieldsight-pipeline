# Procore integration — feasibility and risk

**Date:** 2026-09-09
**Status:** spike result. Not a design, not a plan — the output is a recommendation and a
list of what has to be settled before any code is written.
**Direction chosen by the user:** two-way. Projects and people come *from* Procore;
what we extract from recordings goes *to* Procore.
**Companion page:** https://claude.ai/code/artifact/bed05867-22a9-40ab-bfca-35e7f18ca4d1
(the pipeline diagram, same content)

---

## 1. Verdict

**Feasible, and the shape is cleaner than expected.** The authentication model
(DMSA — a company-level service account not tied to any human) removes the failure this
would otherwise have had: no "the PM who authorised us left, sync is dead". A free
developer sandbox is created automatically with every app and is seeded with exactly the
objects the four functional blocks touch, so B/C/D/E can all be built and verified with
**no customer**.

**Two things must be settled before the first line of code**, and neither is technical:

1. **Where the usage policy puts our Ask/RAG path** (§5.1). The policy explicitly routes
   semantic search and RAG to *Agentic APIs*, which are not generally available.
2. **Organisational verification** (§5.2), which needs a company-domain email address —
   a personal Gmail is not accepted — and which we cannot start without.

Both are things a person has to do; neither can be worked around in code.

---

## 2. What was verified, and against what

Two sources, in this order:

1. Procore's official developer documentation. The developers.procore.com site is a JS
   SPA and returns navigation only to a fetcher; the same content is served
   server-rendered at **procore.github.io/documentation/**, which is what was read.
2. `development_materials/procore/procore-dev-guide.md` — an internal digest of the same
   official documentation, considerably deeper than what could be pulled page by page.
   **It corrected two conclusions drawn from source 1** (§5.2 and §5.1). Where the two
   agree, this document states the fact; where only the digest carries it, it is marked.

Everything in §3 is from one of those. Everything not established by them is in §7,
which is the list of things that need the sandbox to answer. **The two are not mixed.**

---

## 3. Established facts

### 3.1 Authentication

| | |
|---|---|
| Grant type | `client_credentials`, implemented as a **DMSA** (Developer Managed Service Account) |
| Scope | Company-level. **Not tied to a user.** A directory user for the DMSA is created automatically at install. |
| Access token | `expires_in=5400` (90 minutes), **no refresh token** — request a new one on expiry |
| Permissions | Declared in the app manifest; the installing company admin approves them and picks which projects the app may reach |
| Credentials | **One set serves every company the app is installed in.** Tenancy is carried by the `Procore-Company-Id` header |

The alternative (authorization code) is user-scoped and carries a genuinely dangerous
detail we avoid by not using it: its **refresh token is single-use**, and if the refresh
response is lost in flight the old token is already dead — recovery is a full
re-authorisation. DMSA has no refresh token at all.

Procore requires that an integration implement **token revocation** (`POST /oauth/revoke`)
to handle deactivated accounts. That is a compliance obligation, not an optional feature.

### 3.2 Environments

| Environment | Auth host | API host | Installed with |
|---|---|---|---|
| Production | login.procore.com | api.procore.com | Production version key |
| Developer Sandbox | login-sandbox.procore.com | sandbox.procore.com | **Sandbox** key |
| On-Demand Sandbox | login.procore.com | api.procore.com | **Production** key |
| Monthly Sandbox | login-sandbox-monthly.procore.com | api-monthly.procore.com | **Production** key |

Credentials do not cross environments. The two customer-facing sandboxes are named
"sandbox" but install with **production** credentials — the digest names this as a
high-frequency misunderstanding, and it matters to us because it means customer trials
sit behind the production gates in §5.2, not before them.

The Developer Sandbox is created within minutes of registering an app, **cannot be
refreshed or deleted**, and is seeded with a project (`1234 – Sandbox Test Project`)
carrying 3 project users, 8 schedule tasks, 1 photo, a drawing set, 1 RFI and 1 submittal.
A clean environment means a new app, which means a new `company_id`.

### 3.3 Rate limiting

Two windows: hourly (60 min) and a **spike limit over 10 seconds**. Every response
carries `X-Rate-Limit-Limit` / `-Remaining` / `-Reset`, and those report **whichever
limit you are closest to hitting** — usually the spike one.

**The numbers are not published**, and the documentation says explicitly not to assume
them. The headers are the only source of truth. 429 on either limit; 503 with
`Retry-After` when the platform is under load.

### 3.4 Webhooks

Company- and project-scoped. Setup is hook → trigger → (deliveries, for debugging).
Each integration uses one unique lowercase `namespace` across every company.

- **Notification only.** The payload carries ids, not data; details need a callback to
  the REST API. Every event therefore costs at least one extra request against §3.3.
- **Best-effort.** 5-second timeout, exponential backoff from 1 second to 1 hour,
  **events discarded after 12 hours of continuous failure**.
- **Must be idempotent** — deduplicate on `ulid` or event id.
- The official guidance is to **pair webhooks with a periodic reconciliation sync**. We
  should treat that as required, not advisory: a missed event produces no error anywhere.
- **`metadata.source_application_id` identifies which app caused the change**, which is
  the echo-suppression signal (§4.3) handed to us directly.

### 3.5 Writes

- **Sync Actions** batch up to 1000 creates/updates in one call. The **HTTP status is
  always 200**; successes are in `entities` and failures in `errors`.
- **`origin_id`** is unique within a company and is documented as the place to put an
  external system's primary key. `id + origin_id` together is the only way to change it.
  This gives us idempotent upsert without maintaining our own map on the push side.
- **Observations created through the API are not emailed** to the assignee or
  distribution group. A second call (Send Unsent Observation Items) is required.
- **File upload** is a three-step direct upload (create → PUT segments → PATCH etags),
  segments 5 MB–5 GB, SHA-256 required per segment.

### 3.6 Permissions at runtime

401 means the app is not installed, or the token is wrong for the environment. 403 means
the subject lacks project membership or the tool is disabled on that project. **404 is
returned for resources the subject cannot see at all** — it does not mean "gone".

---

## 4. The pipeline

Two constraints determine the shape. Neither is a matter of taste.

### 4.1 One credential set, N tenants

Tenancy is carried entirely by a request header. A mapping table must be the **only**
source of `company_id` for any outbound call — no default, no fallback, no
"if unset then". The consequence of getting it wrong is not an internal visibility bug;
it is **writing one customer's site issue into another customer's Procore**, in their
system, possibly triggering their notifications, with no undo.

We have a documented history in exactly this shape: BUG-41 (`SITE_NAME` env default
silently attributing recordings to a named customer), and the empty-list-means-no-filter
class. The difference is where the damage lands.

### 4.2 The VPC split

Code that can write Aurora has no outbound network (BUG-36: an in-VPC lambda with no NAT
black-holes any outbound call until timeout, **with no log output**). Code that can call
Procore cannot reach the database.

So the connector that talks to Procore runs **outside** the VPC and is the only component
that does, and it exchanges work with the in-VPC writer through **S3 request files** —
the same pattern already in use for `extraction_requests/`. This is an existing shape,
not a new invention.

### 4.3 The echo loop

We POST an observation → Procore emits a change event for our own write → we fetch it →
it becomes a new item. The closing edge must exist from the moment both directions do.

`metadata.source_application_id` (§3.4) is the signal: Procore tells us which app made
the change, so suppression is a field comparison rather than a lookup against our own
table.

---

## 5. Risks

### 5.1 CRITICAL — RAG and semantic search are routed to an API that is not GA

The usage policy forbids more than building training/fine-tuning/benchmark datasets. It
also forbids **high-frequency retrieval driving non-complementary analytics products**,
and states that **AI agents, semantic search, RAG and large-scale analysis should use
Agentic APIs**.

Agentic APIs are built on Datagrid (acquired January 2026) and are **not generally
available** — Design Partner pilot only, gated behind an application, a discovery call
and a pilot agreement.

The boundary is therefore **not** "training versus inference". It is:

| Explicitly allowed | Routed elsewhere |
|---|---|
| CRUD on business records (RFI, submittal, daily log) | Semantic search over Procore data |
| Reading project / user / financial data | RAG grounded in Procore data |
| Change-driven workflow automation | Large-scale analysis |

Blocks B (pull projects and people) and D (push items) sit in the left column. **Feeding
Procore-sourced content into our retrieval index sits in the right one**, and that is the
core shape of Ask. This needs a decision before any code, not before listing.

### 5.2 CRITICAL — production access is two gates, and the first one is early

**This corrects an earlier conclusion** that custom installation made the marketplace a
non-blocker. Custom installation bypasses the marketplace **listing review**; it does not
bypass either gate below.

- **Gate 1 — organisational verification.** New accounts are Unverified and can use the
  sandbox only. Production credentials require verification, in one of two tiers, and the
  criterion is explicit: **anything productised, sellable, or reusable across customers —
  even with a single pilot customer today — belongs in Marketplace Partner**, which
  requires a technical feasibility assessment and a signed partnership agreement. The
  faster Private Developer tier is for work done for one named client and does not apply
  to us.
- **Gate 2 — per-app production credential request**, reviewed against the API Terms of
  Use. Every app submits separately.

A **company-domain email address is mandatory**; personal Gmail/Outlook addresses are
rejected. Sandbox work is unaffected at every stage — verification gates production only.

### 5.3 CRITICAL — one credential set across tenants

See §4.1. Mitigation is structural: the mapping table is the sole source of `company_id`,
and the tests must drive the absence of any fallback path rather than assert on the happy
path. Our own history says a default value here is the failure mode, and CI stayed green
through every previous instance of it.

### 5.4 HIGH — a release silently unstaffs projects

The permitted-projects list is chosen by the customer's admin at install, and **must be
reconfigured by them on every app update or reinstall**; new projects need adding by hand.

So shipping a version stops sync for some projects, **and nothing reports an error on
either side**. It looks exactly like "nothing happened on that project lately". Release
notes must say so, and we need a detector for "this project should have synced and
produced nothing" — the same reasoning as the guard-passed-but-logged-nothing rule.

### 5.5 MEDIUM — outbound calls must live outside the VPC

See §4.2. Known shape, existing pattern, but it must be in the design from the start
rather than discovered when a lambda hangs for 15 minutes with an empty log.

### 5.6 MEDIUM — sync jobs consume Lambda concurrency

Lambda occupies a slot for wall-clock time, waiting on HTTP included (BUG-43). A
full-project sync plus per-event callbacks is exactly that shape. Account concurrency is
now 1000, but **throttled invocations produce no log lines** — this has to be sized in
advance, not diagnosed afterwards.

### 5.7 MEDIUM — pushed items notify nobody by default

§3.5. Without the second call, the customer's experience is "FieldSight pushes things
across and nobody sees them", while everything on our side looks healthy.

---

## 6. Things that fail silently

Grouped because they share a property: doing them wrong produces no error, only a wrong
result. Each of these should become a test rather than a comment.

| Area | Trap |
|---|---|
| Dates | POST/PATCH need full ISO 8601 UTC (`2018-08-26T00:00:00Z`). A date-only value is **not rejected — the field is written as null.** |
| Batch writes | Sync Action always returns **HTTP 200**; per-item failures are in `errors`. Checking the status code treats a batch of failures as success. |
| Daily Logs | Different filter syntax (`log_date`, or `start_date`+`end_date`). **Omitting the date returns only today** — a normal-looking, silently truncated result set. |
| Pagination | Reading only page 1 loses data. `Link` / `Total` / `Per-Page` headers; `per_page` ≤ 2000; not every endpoint paginates. |
| Identifiers | Resource ids **can exceed 32-bit**. Columns holding a Procore id are bigint or text. REST v2 returns all ids as strings. |
| Not-found | An invisible resource returns **404, not 403**. Treating 404 as "deleted upstream" will prune live mappings. |
| MPR header | Under DMSA, `/rest/v1.0/me` and `/rest/v1.0/companies` — the two endpoints one would assume are exempt — **also require `Procore-Company-Id`.** Enforced in sandbox too. |
| Time zones | A project `time_zone` must be an exact identifier (`US/Pacific`); a friendly string is a 422. |

---

## 7. Open — needs the sandbox

These are unresolved. They are listed separately from §3 on purpose.

- **Actual rate limit values.** Not published, and the documentation says not to assume
  them. Measure, then size the sync design against the measurement.
- **Which resources actually emit webhooks.** The documentation names examples, not a
  complete list. The downloadable Webhook Resources list needs checking against the
  resources blocks B–E depend on.
- **Whether Daily Log, Photos and RFI writes are reachable under a DMSA manifest** with
  the permissions we would declare.
- **Whether Observation custom fields exist**, which would affect how much §4.3 needs
  beyond `source_application_id`.

---

## 8. Build order

`A → B → D → C → E`, with F defined inside A.

| Block | | Why here |
|---|---|---|
| **A** | Connection and tenant mapping | Blocks everything else, and contains §5.3 — the highest-consequence thing to get right |
| **B** | Pull projects and directory → `sites` / `users` / `memberships` | The pain we already have; replaces the manual staffing just built |
| **D** | Push items → Observations / RFI / Punch | The only block a customer experiences as *gaining* something. Which target is a product decision, not a technical one — an RFI demands a formal response and cannot be recalled |
| **C** | Pull schedule → `programme_tasks` | Reuses `programme_import`, but the conflict semantics invert (external is authoritative) and local rows must survive — we have already been bitten by the CASCADE on `parent_id` |
| **E** | Push daily logs and photos | Largest volume, least clear value |
| **F** | Echo suppression and conflict authority | Not a phase. Must already exist the moment any two of B–E do |

---

## 9. What has to happen before code

1. **Settle §5.1.** It can change the product's shape, so it cannot wait.
2. **Register a developer account on a company domain** and take the sandbox. That
   unblocks §7 and starts the clock on gate 1 (§5.2), which has a review step we do not
   control.

Neither is an engineering task.
