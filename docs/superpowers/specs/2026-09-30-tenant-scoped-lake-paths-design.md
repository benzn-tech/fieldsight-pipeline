# Tenant-scoped lake paths — retiring globally-unique `folder_name` (design, 2026-09-30)

Status: **design only. Nothing here ships tonight.** Other sessions are merging to
`develop` and `dev` continuously today, and this change touches 33 source files and
~34k objects. Starting it tonight would collide with their deploys.

## Why

`users.folder_name` is **globally unique** and that constraint is load-bearing. Migration
0012 says why, in its own words:

> The shared-lake pipeline routes S3 objects to a tenant by folder_name alone
> (`users/{folder}/…`, `reports/{date}/{folder}/…`) — two companies claiming one folder
> would silently cross-attribute data. Fail loudly at onboarding instead. Additive-only
> (shared-Aurora rule); safe on current data (**single company today**).

That parenthesis was true when it was written. There are now **six companies** in the
prod directory (Southbase, Cassidy Construction, Fletcher Construction, Oceania Dairy,
FieldSight-platform, plus the operator rows), so the index has gone from a precaution to
the only thing standing between two tenants and each other's data.

The cost of that constraint is paid at onboarding, in the customer's own name. Two real
people called *John Smith* at two different companies cannot both be `John_Smith`; the
second one gets a disambiguating suffix, and that suffix is a **path segment that leaks
into the product**. This is already visible: prod holds `Ben_Lin_admin` and
`Ben_Lin_test2`, which are not names, they are collision artefacts.

The owner's framing, 2026-09-30, is the right one and this spec adopts it:

> 我希望 Ben Lin 永远是名字，但是 `Ben_Lin_test2` 可能是文件夹

Three layers already exist in the directory. Only the third is overloaded:

| layer | column | property |
|---|---|---|
| identity | `users.id` (uuid), `users.cognito_sub` | unique, never changes |
| routing key | `users.folder_name` | **globally** unique — this is the problem |
| label | `first_name` / `last_name` / Cognito `name` | **not unique, by nature** |

Migration 0056 already states the rule this spec generalises:

> Keyed by user rather than folder because `users.folder_name` can be changed and the
> person cannot.

**Putting the company in the path lets `folder_name` fall back to per-company
uniqueness**, which is what 0007 originally had (`idx_users_company_folder`). The
human-readable segment stops having to be globally distinct, and the display name goes
back to being only a display name.

It also closes a second hole as a side effect: the legacy report API has **no company
dimension at all** (`can_access_user_data` returns True for `admin`/`gm` before consulting
any list, and the key `reports/{date}/{folder}/daily_report.json` is flat). With the
company in the path, tenant isolation is structural rather than a predicate someone has to
remember to apply.

## Measured surface

Counted 2026-09-30 against prod (`fieldsight-data-509194952652`) and `origin/develop`.

**Objects, per folder-keyed prefix:**

| prefix | objects | path shape |
|---|---|---|
| `audio_segments/` | 18,587 | `{prefix}/{folder}/{date}/…` |
| `users/` | 11,209 | `{prefix}/{folder}/{media}/{date}/…` |
| `transcripts/` | 3,590 | `{prefix}/{folder}/{date}/…` |
| `reports/` | 573 | `{prefix}/{date}/{folder}/…` |
| `extractions/` | 172 | `{prefix}/{folder}/{date}/…` |
| `embeddings/` | 81 | `{prefix}/{date}/{folder}/…` |
| `session_reports/` | 8 | `{prefix}/{folder}/…` |
| **total** | **~34,220** | |

**Two shapes, not one.** `reports/`, `embeddings/` and `meeting_minutes/` put the date
first; `users/`, `audio_segments/`, `transcripts/`, `extractions/` and `session_reports/`
put the folder first. Any rewriter must handle both, and any reader that parses a key by
position must be found — see below.

**Code:** 33 files under `src/` reference a folder-keyed prefix. Known key *builders* and
*parsers* include `lambda_fieldsight_api.py`, `lambda_ask_agent.py`,
`lambda_extract_session.py`, `lambda_extraction_backlog.py`, `lambda_embed_report.py`,
`lambda_finalize_claim.py`, `batch_seal.py`, `batch_redrive.py`, `lambda_downloader.py`.
`batch_seal.py` parses by position (`parts[1]`, `parts[2]`), which is exactly the shape that
breaks silently when a segment is inserted.

## The orphan case is not hypothetical

`Ben_Lin` exists in the lake with data and **no directory row claims it**:

| prefix | objects under `Ben_Lin` |
|---|---|
| `audio_segments/` | 976 |
| `users/` | 532 |
| `extractions/` | 21 |
| `reports/` | 4 dates |

It was written when the folder was `Ben_Lin`, before the directory disambiguated the two
accounts to `Ben_Lin_admin` and `Ben_Lin_test2`. Nothing points at it now.

So the migration cannot assume every folder in S3 maps to a user. **A rewriter that looks
up the company by `folder_name` will find nothing for these 1,529+ objects**, and the
default must be decided deliberately, not discovered at 2am. This spec's position: an
unmapped folder is **left where it is** and reported, never guessed into a tenant. Guessing
is how one company's recordings end up under another's prefix, which is the exact failure
0012 exists to prevent.

## Shape

Target layout, company first:

```
reports/{company}/{date}/{folder}/daily_report.json
users/{company}/{folder}/{media}/{date}/…
audio_segments/{company}/{folder}/{date}/…
transcripts/{company}/{folder}/{date}/…
extractions/{company}/{folder}/{date}/…
embeddings/{company}/{date}/{folder}/…
```

`{company}` is the company's **uuid**, not its name. Names are edited; uuids are not, and a
renamed company must not orphan its own data. (This is the same reasoning 0056 gives for
keying on the user rather than the folder.)

After the cutover, `idx_users_folder_global` is dropped and `idx_users_company_folder`
(0007's original) becomes the only uniqueness rule.

### Dual-write, in this order

Each step is independently revertible, and no step depends on a later one having shipped.

1. **Readers learn both shapes.** Every reader tries the tenant-scoped key first and falls
   back to the flat key. Ships alone, changes nothing observable, and is the step that makes
   every later one safe to revert.
2. **Writers write both.** New objects land at both keys. Storage roughly doubles for new
   data only; at 34k objects that is not a cost concern.
3. **Backfill.** Copy existing objects to their tenant-scoped key, resolving the company via
   `users.get_by_folder_name`. Unmapped folders are skipped and listed. Idempotent, resumable,
   and it must report counts per prefix so "it finished" is a number rather than an absence
   of errors.
4. **Flip the read order** — tenant-scoped becomes authoritative, flat becomes the fallback.
   Still revertible by flipping back.
5. **Stop writing the flat key.**
6. **Drop `idx_users_folder_global`**, restore per-company uniqueness.
7. **Delete the flat copies.** Last, separately, after a deliberate retention window.

Steps 1–2 and 4 are switch-gated so a revert is a config change, not a deploy. Follow the
existing convention (`FS_*` build vars in `amplify.yml`, env vars in `template.yaml`) and
wire **all three pieces** of each switch — a flag whose value is never emitted only ever
reads its default, which this repo has shipped more than once.

## Out of scope

- Merging `FieldSight-platform` into a customer company. It is the deliberate operator
  company from migration 0015, and `platform_admin` depends on it existing separately.
- The remaining display-name-as-identity sites (`lambda_fieldsight_api`'s mapping fallback,
  `lambda_ask_agent`'s device→name map). Neither lambda can reach Aurora (`VpcConfig=[]`,
  no psycopg layer, `PGHOST=null`), so they cannot be fixed by reading the directory; they
  are retired by not being routed to, via `FS_LEGACY_READ_FALLBACK=false`.
- `config/user_mapping.json` itself. It is frozen (prod last written 2026-08-01) and unwritten
  by any code; it stops mattering when its last reader stops being reached.
- The voiceprint identity link's `full_name` / `first_name` rules (migration 0045). Same bug
  class, separately auditable via `linked_on`, and low volume today.

## Tests that must go red without the change

- A reader given only a tenant-scoped key finds the object; given only a flat key it still
  finds it. Both directions, at every step where both shapes are live.
- A key parsed by position (`batch_seal.py`) resolves the same `(folder, date)` under both
  layouts. This is the one most likely to fail silently.
- Two users with the same `folder_name` in **different** companies can both be created once
  the global index is dropped, and their objects never collide.
- A folder with no directory row is **skipped** by the backfill and appears in its report —
  never written into a tenant.
- An `admin` of one company cannot read another company's report through any path, including
  the legacy one.

## What is not yet decided

- Whether `{company}` should also appear in the prefixes this spec does not list
  (`voice/`, `web_video/`, `meeting_minutes/`, `session_reports/`). They were counted but
  their readers were not traced.
- Whether the backfill runs as a Lambda or a one-off local job against the bucket. 34k
  objects is small enough for either; the deciding factor is who is allowed to run it.
- The retention window before step 7.
