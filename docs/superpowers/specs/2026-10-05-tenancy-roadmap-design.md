# Tenancy roadmap — one company boundary, enforced everywhere, built for scale (design, 2026-10-05)

Status: **master design, approved direction 2026-10-05.** Four sub-projects, each with its
own spec → plan → implementation. This document holds the decisions that bind all four,
the order, and the scale-up risks they are designed against. Sub-project 1 is specified
in `2026-10-05-retire-the-legacy-gateway-design.md`.

## The goal (owner, 2026-10-05)

> 我还是希望根治，但是需要你自己规划，为之后 scale-up 后可能遇到的潜在问题预判并设计。

**No request — any path, any role, any client — can read another company's data, and
that stays true as tenants, users and joint ventures grow.** The owner delegated the
design decisions; this document records them so they can be reviewed and reversed.

## Where we are (measured 2026-10-05, prod)

| fact | evidence |
|---|---|
| org-api (in-VPC, Aurora) scopes every read by company | `visible_scope`, `accessible_site_ids`, `is_cross_company` |
| the legacy gateway `fieldsight-prod-api` has **no company concept** | roles from DynamoDB stop at admin/gm; `can_access_user_data` returns True for admin/gm; presign skips owner checks for admin/gm |
| legacy traffic, 14 days | `/api/actions` 2559 · `/api/timeline` 777 · `/api/sites` 149 · `/api/search` 63 · `/api/users` 26 · `/api/site-users` 19 · `/api/ask` 15 · `/api/ask/voice` 10 · transcripts / audio / video / presign / history / dates ≈ 0 |
| legacy traffic, 30 days | 9,644 invocations vs org-api 40,652 (~19%) |
| the lake is keyed by folder, with no company segment | `reports/{date}/{folder}/…`, `users/{folder}/…`, `audio_segments/{folder}/…` |
| `users.folder_name` is globally unique | migration 0012; collision suffixes leak into the product (`Ben_Lin_test2`) |
| a user belongs to exactly one company | `users.company_id`; `_resolve_staffing` requires a membership's user and site to share a company |
| ingest files a recording under the **recorder's** company | `lambda_ingest.resolve_company(folder)` |
| frozen `config/user_mapping.json` (2026-08-01) is still read by 14 source files | report generator stamps site/role by name; orchestrator idle but armed |
| test and prod share one Cognito pool | `ap-southeast-2_q88pd6XXr` on both stacks |
| `/api/actions` and `/api/users` on the legacy gateway check **nothing** about the caller | any signed-in user of any company reads/writes ticks and reads the roster |
| a second, forgotten legacy gateway is still deployed | `khfj3p1fkb` → `fieldsight-api` (2026-07-13), same authorizer, company-blind, 0 calls in 60 days |

## Decisions (binding on all four sub-projects)

**D1. Tenancy follows the project, not the person.** *(owner-approved 2026-10-05)*
A recording, its transcript, its topics, its report rows and its lake objects belong to
the company that owns the **site** (project) it was captured on. A person has a home
company — their employer, used for display, billing and voiceprint consent — but where
they may work, and therefore whose data they produce, is decided by their memberships.

*Why:* joint ventures and subcontractors are normal in construction. A site manager of
company A working on a JV project must produce JV data, not company-A data. Tying data to
the person makes every cross-company engagement a data-ownership argument.

*Fallback:* a recording with no project (no site chosen, or the site cannot be resolved)
belongs to the recorder's home company. It is never guessed into another tenant.

**D2. One authority for "who may see what": org-api over Aurora.** Every read of tenant
data is decided in-VPC against the directory. Components without database access do not
make access decisions; they either call something that can, or they carry no tenant data.

**D3. The company boundary is enforced twice: by query and by storage layout.**
Query scoping (D2) is the first line. The second is structural: the company id is a
segment of every lake key (sub-project 4), so a scoping bug cannot reach another
tenant's prefix, and a tenant can be exported or deleted as one prefix.

**D4. Identity is the login, never a name.** A person is `users.id` / `cognito_sub`.
Display names, Cognito `name`, device ids and folder names are labels or routing keys,
not identities. No code finds a person or a company by a name (already true for
companies since #1023).

**D5. No frozen side-copies of the directory.** `config/user_mapping.json` and the
DynamoDB `fieldsight-users` table are retired as sources of truth. Where a component
outside the VPC needs a fact from the directory, org-api **publishes** that fact as a
machine-owned object (the pattern `config/site-coords.json` already uses), with a
single writer.

**D6. Device firmware is a slow-moving client.** Any endpoint a device calls keeps a
compatible contract until the device fleet has moved. Retirement never breaks a device
in the field.

## The four sub-projects, in order

| # | sub-project | closes | depends on |
|---|---|---|---|
| **1** | **Retire the legacy gateway's data routes** | the live cross-company reads | — |
| **2** | **Retire the frozen mapping** (D5) | stale labels on reports; the orchestrator's armed device→name map | 1 (removes half its readers) |
| **3** | **Project-owned data + cross-company memberships** (D1) | JV / subcontractor work; recordings filed by recorder | 2 |
| **4** | **Company segment in lake keys** (D3; supersedes #979's company choice) | structural isolation; per-tenant export/delete; bounded listing | **3** |

**Why 3 must precede 4.** Sub-project 4 writes a company into every lake key. If it used
the recorder's company and sub-project 3 then moved ownership to the project's company,
all ~34k objects would be rewritten twice. Settling D1 first means one rewrite.
Spec #979 (tenant-scoped lake paths) is kept for its mechanics (dual-write, backfill,
orphan rule) but its `{company}` becomes **the owning site's company**, not the user's.

## Scale-up risks this roadmap is designed against

| risk | when it bites | addressed by |
|---|---|---|
| every company-blind read path is a cross-tenant leak; more tenants, more exposure | now | 1 (remove the paths), 4 (structural) |
| `summary_report.json` aggregates every tenant into one document | grows with tenants; leak surface and size | 4: one summary **per company** (`reports/{company}/{date}/summary_report.json`); the lake-wide one is retired |
| listing `reports/{date}/` enumerates every tenant's folders | latency and cost at thousands of users | 4: listings are bounded to one company prefix |
| globally-unique `folder_name` forces collision suffixes into names | already visible | 4: per-company uniqueness |
| test and prod share one Cognito pool | a TEST tool can read or mutate prod identities (the seed incident's shape) | **separate item (S1)** — split pools before onboarding many more tenants |
| org-api opens a database connection per invocation | connection exhaustion under concurrency | **separate item (S2)** — measure first; RDS Proxy or pooling if it shows |
| offboarding a tenant | the first customer who leaves | 4: export/delete = one company prefix + its rows |
| a person who changes employer keeps old data under the old company | staff turnover | D1: data follows the project, so a move changes nothing already captured |
| devices move between people monthly | already true | D4: identity from login; sub-project 2 retires the device→name map |

S1 and S2 are recorded here so they are not lost; neither is part of the four
sub-projects, and each gets its own decision when its trigger approaches.

## Non-goals

- Changing who may see what *within* a company (roles, graded scope) — unchanged.
- Rewriting the report generator's model calls — only where it reads the frozen mapping.
- The mobile app beyond keeping D6's contracts.
