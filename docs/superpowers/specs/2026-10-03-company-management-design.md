# Company management — create from the web, rename, one name per tenant (design, 2026-10-03)

Status: **design, approved in conversation 2026-10-03.** Spans `fieldsight-pipeline`
(backend, migration) and `fieldsight-ui` (Sites page).

## Why

The owner onboards client companies. On 2026-10-02 two were needed for a client meeting
(`Frequency`, `Preformance Technologies`) and **neither could be created from the web**:
the owner had to wait for a backend release and then have each tenant created by
`aws lambda invoke` against `fieldsight-prod-org-api`.

**Success** = the owner signs in to the web app, creates a company, creates its first
project, and invites people — without a terminal at any step.

## What already exists

| | backend | web |
|---|---|---|
| list companies | `GET /api/org/companies` (#989), platform_admin | used by the New Project company picker (#384) |
| create a company | `POST /api/org/companies` (#1013, live on prod 2026-10-02), platform_admin, case/space-insensitive duplicate guard, 409 carries the existing id | **none** — zero callers in `fieldsight-ui` `main` or `dev` |
| create a project in a chosen company | `create_org_site` + `target_company_id` | New Project picker, platform_admin only (#383) |
| GM creates a project in own company | `create_org_site` allows `admin`/`gm`/`platform_admin` | `canCreate = isAdmin \|\| role === 'gm'`, identical on `main` and `dev` |
| rename a company | **none** | none |
| archive a company | **none** — `companies` has no `archived_at` | none |
| one name per tenant | **no constraint** (`0002_core_relational.sql`) | — |

GM project creation is already live on prod and is **out of scope**.

## Decisions (owner, 2026-10-03)

1. **No archive in this cut.** Archiving decides whether a client's people can still sign
   in, who owns their recordings, and what the devices do — a separate design. Not needed
   to onboard.
2. **Fix the root of the name-keyed lookups (B), rather than ship rename around them.**
3. **The lake-wide summary goes to `platform_admin` only.**
4. **An unrecognised recording folder keeps being refused** — with an honest error.
5. **Only `platform_admin` may rename a company.**
6. **Retire `lambda_org_seed`** (added 2026-10-03, see §4).

## The finding that shaped this design

Three code paths find a company **by its name**, through a constant:

```python
COMPANY_NAME = os.environ.get("COMPANY_NAME", "FieldSight")
```

`COMPANY_NAME` is set on **no** deployed function (checked: `fieldsight-{test,prod}-{org-api,ingest}`),
so every one of them looks for a company called `"FieldSight"` — and **there is none**. The
operator company is called `FieldSight-platform`. So all three lookups already return
`None`, on both environments, and have for some time:

| where | what `None` does today |
|---|---|
| `lambda_ingest.resolve_company` — shared by **ingest and `lambda_item_writer`** | an unrecognised folder raises `org company 'FieldSight' not found — run the org seed`. Loud, not silent. **The message is wrong:** the seed does not fix it, and the seed re-applies mapping-derived roles over every role changed since. |
| `lambda_org_api` admin disambiguation (~8615) | the branch serving the lake-wide `summary_report.json` is fail-closed and **skipped for everyone** |
| `lambda_org_seed` | find-or-create by exact name |

That is the hazard a rename API would make routine: one rename, years ago, silently turned
off two features and nobody noticed. Making renames safe means **no code may find a company
by its name** — hence decision 2.

### The dead branch was protecting customer data

`summary_report.json` is built by `lambda_report_generator` from **every**
`reports/{date}/{folder}/daily_report.json` — a prefix with **no company segment**. It is
every tenant's daily reports in one document.

The dead branch serves it to any caller **whose company is the operator company**, regardless
of role. On prod that company has six active members:

```
benl.tech@outlook.com          platform_admin
sam.kiwi@outlook.com           gm
james.lamb@southbase.co.nz     site_manager   ← a Southbase employee
nick.cameron@southbase.co.nz   site_manager   ← a Southbase employee
benlin.chch+test2@gmail.com    pm
benlin.chch+test3@gmail.com    pm
```

**Repairing the lookup as written would have handed two Southbase site managers a summary
containing Fletcher's, Cassidy's and Oceania's reports.** The rename that broke the lookup
closed a cross-tenant leak by accident. Decision 3 is what makes repairing it safe: the gate
becomes the **role** that is already the only cross-tenant one (`is_cross_company`,
`acl.py:17`), not company membership.

With decisions 3 and 4, **no remaining code needs to identify "the operator company" at
all**, so no `is_operator` column and no `COMPANY_ID` setting are added. (An earlier draft
had the column; it fell out once both answers were in.)

## Backend

### 1. Remove every lookup-by-name

| site | change |
|---|---|
| `lambda_ingest.resolve_company` | drop the name fallback. A folder with no directory row returns `None`. |
| `lambda_ingest` (~632) and `lambda_item_writer` (~903) | replace the error with the truth: *folder `X` has no directory row; not ingesting rather than guessing a tenant.* Name the folder; never mention the seed. |
| `lambda_org_api` summary branch (~8615) | gate on `is_cross_company(caller["global_role"])`; delete the `get_company_by_name` call |
| `lambda_org_seed` (~88) | **removed entirely** — see §4 |
| `COMPANY_NAME` in ingest, item_writer, org-api | delete — unused |

After this, `git grep -n "get_company_by_name(" src/` returns only its definition. A test
pins that, so a future lookup-by-name has to argue with it.

**Behaviour change on prod:** the owner starts receiving the lake-wide summary. Everyone
else's behaviour is unchanged — they did not receive it before and do not now.

### 2. `PATCH /api/org/companies/{id}` — rename

- `is_cross_company` only; everyone else 403, asserted per role.
- Body `{name?, industry?}`. `name` trimmed and non-empty when present; `industry` trimmed,
  blank stored as absent. Neither present → 400.
- Malformed id → 400. Unknown id → 404.
- **Duplicate guard excluding self.** `find_company_by_name_ci` returning a row whose id is
  *not* this company → 409, carrying that company's name and id. Renaming `frequency` to
  `Frequency` matches only itself and is allowed.
- Response `{id, name}`, the same projection as create and list.

Safe because nothing finds a company by name after §1, `site-coords` keys on site slug, and
reports do not store a company name (`lambda_report_generator` has no company field). A
rename does **not** rewrite any past report, because none contains it.

### 3. One name per tenant, in the database

```sql
CREATE UNIQUE INDEX idx_companies_name_ci ON companies (lower(btrim(name)));
```

The endpoint guard stays — it is what returns the existing id — but it is not enough on its
own: it does not stop a race between two creates, and it does not stop paths that bypass the
API (`lambda_org_seed`, a hand-written `INSERT`). The index stops all of them.

- Checked 2026-10-03: **zero duplicate names** on prod (8 companies) and TEST (4).
- If a duplicate exists when the migration runs, `CREATE UNIQUE INDEX` fails and the deploy
  stops. That is the intended failure: a duplicate tenant has to be resolved by a person.
- A `UniqueViolation` raised by the index inside create or rename (the race) is answered
  **409**, not the generic 500.

### 4. Retire `lambda_org_seed` (decision 6, added 2026-10-03)

"Seed" here means *populate a database with its initial data*, not a random-number seed.
It was the Phase 3 one-shot move of identity from `config/user_mapping.json` into Aurora:
create the company, copy the Cognito users in, create sites and memberships from the JSON.
That move finished long ago. The function is still deployed on **both** stacks
(`fieldsight-{test,prod}-org-seed`), manual-invoke only, and **both read the same shared
Cognito pool** (`ap-southeast-2_q88pd6XXr`, 29 users).

Read as code rather than as its docstring, one default invoke today would:

| step | effect |
|---|---|
| `get_company_by_name("FieldSight")` → none → `create_company("FieldSight")` | a new tenant |
| `upsert_user(..., company_id=<that tenant>)` for **every** Cognito user — `upsert_user` overwrites `company_id` on conflict | **every person in every tenant moved into one company: multi-tenancy collapses in a single call** |
| `global_role = resolve_role(...)` from the frozen JSON | every role changed through the org API since is rolled back |
| `set_folder_name` from the frozen JSON | people re-pointed at folders that may belong to no one (`Ben_Lin`) |

The TEST stack's copy would pull prod-only customers into `fieldsight_test` the same way.

The earlier draft only switched its lookup to the case-insensitive one. That stops it
colliding with the new unique index and stops nothing in the table above. A guard
("refuse when more than one company exists") would still leave a deployed function whose
only purpose is a migration that has already run, so the decision is **removal**:

- delete `OrgSeedFunction` and `OrgSeedLogGroup` from `src/template.yaml`
- delete `src/lambda_org_seed.py` and `tests/unit/test_lambda_org_seed.py`
- `tests/unit/test_lambda_org_api.py` and `tests/unit/test_the_starter_templates_are_usable.py`
  also reference it — read what they use it for; anything still needed moves to where it
  belongs (starter-template seeding already lives in `report_templates.seed_starters`)
- recoverable from git history should a fresh stack ever need bootstrapping

This code would produce exactly the misfiling seen on prod — Southbase people inside the
operator company. **There is no evidence it is what did**; the mechanism matches, nothing more.

## Web (`fieldsight-ui`, Sites page)

The Sites page already holds `+ New project`, and its company picker reads
`GET /companies` — so a company created here is immediately offered for its first project.

- **`+ New company`** — page header beside `+ New project`, `platform_admin` only, gated by
  the existing `orgApi.isCrossCompany`. Modal: name, optional industry.
- **Manage companies** — `platform_admin` only. Lists `GET /companies`; each row renames in place.
- **409** — "A company called *X* already exists", with a control that takes you to it.
- Through the api layer: `createCompany` / `renameCompany` in `scripts/api/org.js` build the
  body from **named fields**. This layer silently drops anything it does not name, which has
  bitten this repo repeatedly — tests assert on the body handed to `orgRequest`.

## Tests that must be red without the change

**The safety property, stated separately because it is the reason decision 3 exists:**

- The lake-wide summary is **refused** to a `gm`, a `site_manager` and a `pm` who belong to
  the operator company, and to an `admin` of a customer company. Granted to `platform_admin`.
  Testing only "platform_admin gets it" would pass against the leaking version.

Then:

- An unrecognised folder raises an error naming the folder, assigns no company, and the
  message does not mention the seed — for **both** ingest and item_writer.
- A recognised folder still resolves to its company (no regression on the normal path).
- No `get_company_by_name(` call outside its definition.
- Seed: a case variant of an existing name finds the existing company and inserts nothing.
- Rename: per-role refusal; 409 on another tenant's name in any case/spacing; case-only
  rename of itself allowed; 404 / 400; projection is exactly `{id, name}`.
- Index (real Postgres, `fswork/run-tests.sh`): a case/space variant `INSERT` raises
  `UniqueViolation`; the handler maps it to 409.
- Web: `+ New company` and Manage are absent for `admin`, `gm` and below; the api layer's
  body carries the fields.

Every claim gets a red-proof — revert the change, watch the test fail — and the
summary-gate test additionally gets the mutation that restores the company-membership gate.

## Rollout

1. Pipeline PR → `develop`. **TEST first**: the migration must be applied and verified on
   `fieldsight_test` before it goes near `main`.
2. Narrow release branch from `main` carrying only this change — the pattern prod releases
   already follow — then the owner merges and approves the `production` environment.
3. UI PR → `dev`, then the `dev` → `main` promotion. **After** the backend is on prod: the
   rename call does not exist there until then.
4. `fieldsight-ui` has **no CI and no approval gate** — merging to `main` publishes to
   customers immediately.

## Out of scope, recorded

- **Archive / unarchive a company** (decision 1).
- **James Lamb and Nick Cameron are filed under `FieldSight-platform`**, not Southbase.
  This design removes the leak they would have caused through the summary branch, but their
  placement is still wrong: anything else gated on company membership treats them as
  operator staff. Correcting it is a data change and the owner's.
- No company **delete** — `companies` is referenced by 22 foreign keys. **Archive** is
  possible (an `archived_at` column, the pattern `sites` and `users` already use); it is
  deferred because what archiving does to a company's people is a product decision.
- **Moving a person or site to another company carries no history.** Many tables hold a
  denormalised `company_id` beside `site_id`/`user_id`; neither #987's site move nor any
  person move updates them. Not designed here.
