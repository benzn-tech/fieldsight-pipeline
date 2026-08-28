# Plan: make the brief's entities searchable

**Spec:** `docs/superpowers/specs/2026-08-28-entities-are-searchable.md` (eighth draft, first
one with no blocking findings).
**Date:** 2026-08-28.

Eight drafts, and seven of them died on a *"came up in N meetings"* count that was never in the
motivation. That history is why this plan states, for every task, **what it must assert** — the
spec is now correct on paper, and every failure this feature has had was a correct paper
design meeting a seam nobody tested.

Two rules from this repository apply to every task below and are not repeated in each:

- **A guard that passes must still log.** "It ran and found nothing" and "it never ran" are
  otherwise the same observation. 1078 uploads produced zero log lines once.
- **After the fix, put the defect back and watch the test go red.** A test written against
  already-correct code has never been shown to fail.

---

## 1. Migration — the table and its index

`src/migrations/00XX_session_entities.sql`. **The number is chosen at merge time**: two `0041_*`
and two `0044_*` already exist, so picking one now guarantees a collision with whichever branch
lands first.

Table as specced, plus the generated `search` column, the GIN index, and
`CHECK (session_id ~ '^[0-9a-f]{32}$')`.

**Asserts** — integration, through the RDS Data API inside a rolled-back transaction, because
the connection doubles record SQL without parsing it and three defects this week lived exactly
there:

- a row with a mixed-case `name` and lowercased `aliases` is found by
  `plainto_tsquery('simple', 'firefire')`. This is the one the whole feature turns on.
- an `aliases` array containing `''` **or** a NULL element raises. That proves the writer must
  filter, rather than the column tolerating it.
- `session_id` rejects a `sid`-prefixed value.

## 2. Move `extraction_key` into `session_scope`

It exists at `lambda_extract_session.py:1589` with one call site. This is a move, not a new
function: `session_scope` already owns `EXTRACTION_KEY_RE` and `parse_extraction_key` for the
stated reason that the writer and readers must share one definition.

**The docstring moves verbatim** — it carries the live/final collision semantics.

**Asserts:**

- `parse_extraction_key(extraction_key(x)) == x`.
- `group_merged_key`'s output still parses. **`group_merged_key` (`lambda_finalize_claim.py:267`)
  is deliberately a second formatter** and its docstring says why; a test phrased as "one place
  defines the key shape" would delete that distinction.

## 3. Repository + ItemWriter's `{"entities": …}` branch

Routed **before** the `Records` loop (`lambda_item_writer.py:927`), so the two callers stay
distinguishable in the code and in the logs. A fabricated `Records` envelope would be a lie
about provenance.

The branch reuses, in order: the same company and site rungs as `write_extraction_items`, and
`_source_is_deleted`. It normalises `session_id` to bare hex, lowercases aliases and drops
empty/NULL elements, and does delete-then-insert under
`pg_advisory_xact_lock(hashtext(extraction_key))`.

**A site-resolution miss writes nothing** — not `site_id NULL`. The ACL makes NULL-site rows
company-wide visible, so writing on a miss silently widens who sees them; NULL stays reserved
for a session that genuinely has no site (BUG-43).

**Asserts:**

- re-invoking with the same payload leaves `COUNT(DISTINCT session_id)` unchanged.
- a `sid{hex}` input is stored bare.
- a site-resolution miss writes zero rows **and logs**.
- a deleted source writes zero rows **and logs**.

## 4. Finalize hands off, and is allowed to

After `_store_brief` (`lambda_session_finalize.py:189`), invoke ItemWriter with the entities
payload — best-effort, exactly like the brief store, because the confirmation email must not
depend on it.

`SessionFinalizeFunction` has **no `lambda:InvokeFunction`** today (`template.yaml:2528-2557`
carries S3 and SES only). Add one statement scoped to ItemWriter's ARN.

**Asserts:**

- a template test that the statement exists.
- **after deployment, `simulate-principal-policy` against the live role.** A missing permission
  here surfaces as an `AccessDeniedException` inside a background lambda, and three silent
  failures this week were exactly that. The brief endpoint shipped last night with this same
  gap and answered 500 for every session.

## 5. The search arm

In `lambda_rag_search`: the entity query with the four-clause ACL, sets converted to lists as
at `:88-91` (psycopg does not adapt a set), `plainto_tsquery('simple', %s)`, and
`DELETED_SOURCE_PREDICATE`.

**Placed so it does not sit behind the `if not site_ids` short-circuit at `:109`** — the ACL
makes site-less entities visible to exactly the callers that short-circuit hides. Every return
path carries an `entities` key, empty list included. A missing `query_text` returns
`entities: []` with an `error` note and **never raises**: `lambda_ask_agent` turns a
`FunctionError` into "search backend failed" for the whole search.

A site filter narrows entities to that site plus the NULL-site arm.

**Asserts:** the zero-sites caller and the not-provisioned caller both get an `entities` key; a
site-less entity is visible to a caller with an empty site set.

## 6. Both callers send `query_text`

`lambda_ask_agent:701` and `:789`, in the same change. Without this the arm compiles, deploys,
and returns an empty list forever.

**Assert a seam test**, in the shape of `test_embedder_writer_contract.py`: the payload
ask-agent builds is the payload rag-search reads. *Twenty tests monkeypatched `invoke_writer`
and none called it* — that is the failure this assert exists for.

## 7. End to end on TEST, then break it on purpose

Run one real briefed session, confirm rows exist and search returns them. Then revert the
finalize hand-off locally and confirm the seam test goes red.

A brief already exists to test against:
`session_brief/Ben_UCPK2/2026-08-27/sid93396a6ac8434fdf908c25a50cc7e167/latest.json` — 6
sections, 18 entities, including `FieldSight aka FieldSync/FieldSight Visual/FieldSight Record`,
which is precisely the alias case the index exists to find.

---

## Not in this plan

- **The UI.** The API gains a key; presenting it is a separate spec.
- **Backfill.** `EnableSessionBrief` has been on for TEST since 2026-08-27 and there is one
  brief. Backfill is worth building when there is a corpus, not now.
- **Feeding the brief's aliases into `name_aliases`.** The spec asks for this decision; it
  improves an existing production surface and should be its own change, after this one.

## The decision still open

**How wide site-less entities should be visible.** As written they are company-wide, which is
wider than the chunk search's precedent of requiring a site match. BUG-43 is the reason — all
three site sources can miss at once, and a strict filter makes those rows invisible to
everybody. It ships behind a default-false flag either way, so it is safe to start as written
and narrow later; it is listed because it is a visibility decision, not an implementation one.
