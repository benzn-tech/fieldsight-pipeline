# One identity path — registration, app, web, reports, push (design, 2026-10-01)

Status: **design.** Answers the owner's question of 2026-10-01:

> 咱们能否统一这个最重要的用户信息，保证：注册，app使用，web使用，报告，推送，等等全都用一套一致的路径？

Short answer: **the write path is already one path, and it is correct.** The
divergence is on the *read* side, plus one missing write-back. This spec names
the three real gaps, measured, and deliberately leaves the healthy parts alone.

## What is already right — do not rebuild this

`create_member` (`src/lambda_org_api.py` ~4320) is the **only** account-creation
path in the repository — `admin_create_user` appears exactly once — and it does
the right things in the right order:

1. Cognito `admin_create_user`, username = the lowercased email, and
   `name` = `first_name + " " + last_name` (falling back to the email). So
   Cognito's `name` is **derived**, not authoritative.
2. `sub` is read back from the response, or from `admin_get_user` on
   `UsernameExistsException`, so a retried invitation is idempotent.
3. `users.upsert_user(conn, sub, email, company_id=…)` — the Aurora row is keyed
   on `sub`, and the company is explicit. A `sub` that already belongs to a
   different company is refused 409 rather than moved.
4. The recording folder is allocated by `_free_folder_name`, which **never**
   hands out a folder another login already holds: `base`, then `_2`..`_9`, then
   a user-id prefix that cannot clash. Its comment records the cost of getting
   this wrong — two people with the same name in one company had the second
   one's recordings written into the first one's folder for 18 sessions on prod
   (2026-09-11..15); their timelines were empty and their clips showed as
   someone else's.
5. Membership rows, then the company's starter templates.

The layering this produces is the correct one, and migration 0056 already states
the rule: *"Keyed by user rather than folder because `users.folder_name` can be
changed and the person cannot."*

| layer | column | property |
|---|---|---|
| identity | `users.id`, `users.cognito_sub` | unique, never changes |
| routing key | `users.folder_name` | unique; **may change** |
| label | `first_name`/`last_name`, Cognito `name` | **not unique, by nature** |

So "one consistent path" already exists as a shape. What is missing is that not
every consumer reads it.

## Gap 1 — a name change never reaches Cognito

`patch_me` (`src/lambda_org_api.py` ~3701) writes `first_name`/`last_name`
through `users.update_profile`. It does **not** touch Cognito.

**`admin_update_user_attributes` has zero hits in the entire repository.**

So Cognito's `name` is written once, at invitation, and frozen forever. Every
consumer that reads `cognito.name` is reading the name the person had on the day
they were invited.

Not hypothetical. The frontend's `callerFolder()` derived a folder from
`cognito.name` and produced `Ben_Lin` — **a folder belonging to nobody**, because
the directory holds `Ben_Lin_admin` and `Ben_Lin_test2`. That is the bug fixed by
ui #381 / pipeline #982, and it was reachable only because Cognito's copy of the
name is both stale and not unique. Measured on prod Cognito, two collisions among
enabled accounts:

```
name="Ben Lin"      benl.tech@outlook.com          + benlin.chch+test2@gmail.com
name="James Alcock" james.alcock@southbase.co.nz   + james.alcock@southhbase.co.nz
```

**Fix.** After the Aurora write succeeds, `patch_me` writes the recomputed
display name back to Cognito — best-effort and non-fatal, but logged at
exception level. A failed attribute write must not fail a profile save; it must
also not be swallowed, because a guard that only speaks when someone is watching
cannot be told from one that never ran. That is `_publish_site_coords`'s
documented contract and it is followed here deliberately.

**The folder is NOT renamed.** 0056's reasoning holds: the person cannot change,
the folder can, and renaming it would orphan every object already written under
it. A display-name change is a label change and nothing more.

## Gap 2 — two of the four identity stores have no writer at all

| store | written by | read by |
|---|---|---|
| Aurora `users`/`memberships` | `create_member`, `patch_me`, `patch_member_folder` | org-api, everything in the VPC |
| Cognito | `create_member` only — never updated (Gap 1) | both frontends, the app |
| DynamoDB `fieldsight-users` | **nothing in this repository** | `lambda_fieldsight_api.py:147` |
| S3 `config/user_mapping.json` | **nothing in this repository** | six lambdas |

The bottom two rows are the answer to "can this information be consistent": no,
because **nobody writes them**. `user_mapping.json` on prod was last written
2026-08-01 and holds 8 people; the directory holds 29. Every account created
since is invisible to it. The nine real customer users at `fcc.co.nz`,
`cassidy.co.nz` and `oceaniadairy.co.nz` appear in **neither** frozen store.

**Fix: do not add writers — remove the readers.** Backfilling a frozen store
would leave four stores that must agree instead of two. Both frozen stores are
consumed by `lambda_fieldsight_api`, which has `VpcConfig=[]`, `Layers=null` and
`PGHOST=null`: it **cannot** reach Aurora, so it cannot be taught to read the
directory. It is retired by not being routed to — `FS_LEGACY_READ_FALLBACK=false`
— once #378 / #381 / #982 have been observed on prod.

Ordering matters and is already recorded: **first** stop the frontend sending a
guessed folder, **then** turn the fallback off. Reversed, all you get is a more
accurate refusal.

## Gap 3 — uniqueness is enforced on the wrong column, and it shows

`users.folder_name` is globally unique (0012). `first_name`/`last_name` and
Cognito `name` are not, and **must not be** — two real people may share a name.

The consequence is the one the owner named:

> 我希望 Ben Lin 永远是名字，但是 `Ben_Lin_test2` 可能是文件夹

Today a collision at onboarding is paid in the customer's own name, because the
suffix `_free_folder_name` allocates is a **path segment that leaks into the
product**. `Ben_Lin_admin` and `Ben_Lin_test2` are not names; they are collision
artefacts, and they are visible.

The structural fix — company in the lake path, so `folder_name` can fall back to
per-company uniqueness (0007's original `idx_users_company_folder`) — is
specified separately in `2026-09-30-tenant-scoped-lake-paths-design.md` (#979)
and is **not** in scope here.

What **is** in scope: **nothing may present a folder as a person's name.**
`lambda_org_api.py:7693` does exactly the inverse and it is worth naming:

```python
"user_name": rows[0]["user_name"] or folder.replace("_", " ")
```

A folder is turned back into a name whenever the stored value is missing, which
produces `Ben Lin test2` *as a person's name*. It should surface the directory's
`first_name`/`last_name` for that folder, or nothing — never a de-underscored
path segment.

## Scope

**In:** Gap 1's Cognito write-back. Gap 3's "never render a folder as a name"
audit across both frontends and the report/email renderers. Recording Gap 2's
retirement order.

**Out:** tenant-scoped lake paths (#979). Dropping the global folder index.
Renaming existing folders — `Ben_Lin_admin` and `Ben_Lin_test2` stay as folders,
which is precisely what the owner asked for. The orphan `Ben_Lin` folder (532
`users/` + 976 `audio_segments/` + 21 `extractions/` objects, 6 report dates, no
directory row) is left where it is and reported, never guessed into a tenant.
The voiceprint identity link's own `full_name`/`first_name` rules (0045) — same
bug class, separately auditable via `linked_on`, low volume today.

## Tests that must go red without the change

- `patch_me` with a changed `last_name` calls `admin_update_user_attributes`
  with the recomputed `name`. Asserted on the Cognito client, not on an
  intermediate object.
- A Cognito attribute write that raises does **not** fail the profile save, and
  **does** log at exception level. Both halves — a swallowed failure and a fatal
  one are each wrong, so each needs its own assertion.
- `patch_me` does **not** change `folder_name`. This is the pin that stops a
  future "keep them in sync" change from orphaning a lake prefix.
- No renderer turns a `folder_name` into a displayed person's name: given a
  directory row the name comes from `first_name`/`last_name`; given none the
  output is empty rather than a de-underscored folder.
- Two users may share a display name in one company, and `_free_folder_name`
  gives the second a distinct folder. Already covered — keep it red-proofed, as
  it is the anchor for this whole design.

## Not yet decided

- Whether Cognito `name` should be written at all, or dropped from the
  frontends in favour of `GET /me`. Dropping it removes this class of bug
  entirely and is the cleaner end state; it is also a larger frontend change,
  and both frontends read it today.
- Whether `lambda_org_api.py:7693`'s fallback should return empty or the email
  local-part. Empty is honest; the local-part may be more useful. Owner's call.
