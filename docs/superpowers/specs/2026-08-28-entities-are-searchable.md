# Spec: the brief already knows what the meeting was about; nothing can look it up

**Status:** proposal, fourth draft after three reviews.
**Date:** 2026-08-28
**Repo:** `fieldsight-pipeline`.

> **Three drafts were found unbuildable and all three failures are kept below.** Not as
> penance: the second draft's defect was the first one's having *moved*, and the third's was
> the second's having moved again. A reader who cannot see that pattern will move it a fourth
> time. The motivation has survived all three. The plan is on its fourth.
>
> This draft differs from the others in one respect that matters more than any individual
> correction: **every claim below about existing code was checked against that code while
> writing it, and one of the third review's own objections did not survive the check.** It is
> marked where it appears.

---

## The measured gap

Asking *"which brands came up yesterday?"* cannot be served. Measured on prod and re-verified:

| check | result |
|---|---|
| `"Plaud"` anywhere in `report_chunks` | **0** |
| keyword indexes anywhere in the schema | **none** |
| `lambda_rag_search` | pure pgvector — one query, `embedding <=> %(q)s` |

Two independent reasons. A rare proper noun is often not in the indexed text at all, because
extraction keeps ~5 % of a transcript. And where it survives, the only instrument is a vector
search, which is a poor one for a token whose whole meaning is that it is unusual.

## What changed

`session_brief` produces the missing half and it is discarded. On the 2026-08-27 meeting:
**22 entities**, each with a kind, a one-line role, and the spelling that actually appears in
the transcript — `FieldSight aka FieldSync`, `Fireflies aka Firefire`.

`session_brief/{folder}/{date}/sid{id}/latest.json` gained a reader today —
`GET /api/org/sessions/{id}/brief` — but that is a keyed lookup: you can read a brief whose
session you already know. Nothing can search across them, which is the gap here.

### Aliases are not new here (correction, draft 1)

`name_aliases` (migration 0020, `repositories/aliases.py`) already stores wrong → right per
company and site, and **`lambda_rag_search` already reads it** (`lambda_rag_search.py:116`) to
correct the display layer.

That does not remove the motivation — `name_aliases` cannot help when the word is absent from
every chunk — but it decides a question draft 1 never asked. **The brief's aliases should feed
`name_aliases`**, not sit in a second parallel table. Two spelling-correction mechanisms that
do not know about each other is the seam this session has spent its time removing.

---

## `source_s3_key`: the value, not the column

Draft 3 added the column and did not say what goes in it. That is not a documentation gap.
**The deletion guard is a string comparison, so a column with the wrong string in it is a
column that silently protects nothing** — and here it is the *only* protection, because
`topic_id` is frequently NULL (see below).

The contract, read out of the code rather than assumed:

```
deleted_predicates.py:30   {alias}.source_s3_key LIKE r.target_key || '%'
redactions.py:142          target_key = source_prefix
lambda_org_api.py:3540     source_prefix = "extractions/{folder}/{date}/{base}"
```

`base` is the **session's filename stem** — `{device}_{date}_{time}_sid{hex}` for a chunk
session, `{device}_{date}_{time}` for a legacy one. The tombstone's key must be a *prefix of*
the row's key.

**And what finalize holds is not that.** `lambda_session_finalize.py:202` builds
`session_brief/{folder}/{date}/sid{session_id}/latest.json` from `artifact["sessionId"]`, which
is the bare hex. `extractions/{folder}/{date}/sid{hex}` **does not start with**
`extractions/{folder}/{date}/{device}_{date}_{time}_sid{hex}`, so a row keyed that way is
matched by no tombstone at all: the customer deletes a recording, every other surface hides it,
and its entities keep answering searches. That is this repo's own note about deleted recordings
returning through search, arrived at from a new direction.

So the value is **not constructed**. `ItemWriterFunction` is in-VPC and holds the connection,
and the string it needs already exists on the session's own topics
(`lambda_item_writer.py:738` wrote it). It resolves:

```sql
SELECT source_s3_key FROM topics
 WHERE company_id = %s AND source_s3_key LIKE %s   -- '%sid' || hex
 ORDER BY created_at DESC LIMIT 1
```

and copies it verbatim. Same string, same tombstone, by construction rather than by agreement.

**If it resolves nothing, the row is not written**, and that is a decision rather than a
fallback. An entity row no tombstone can name is a permanent hole in deletion, and one that
appears only when somebody exercises deletion months later. A missing row is a feature gap;
an unreachable row is a promise broken. The skip is logged with the sid, because "no entities
for that session" and "entities were refused" must not look alike.

`topic_id` is stored when a topic resolved and is `ON DELETE SET NULL` — the topic arm is a
bonus, and a `CASCADE` here would let a routine topic rebuild delete entity rows the customer
never deleted. **The source arm carries this feature alone.**

---

## The table

```
session_entities
  id            uuid PK
  company_id    uuid NOT NULL REFERENCES companies(id) ON DELETE CASCADE
  site_id       uuid REFERENCES sites(id)
  user_id       uuid REFERENCES users(id)     -- the recorder; the ACL needs it
  session_base  text NOT NULL                 -- the sid, for the brief join
  source_s3_key text NOT NULL                 -- copied from topics; see above
  topic_id      uuid REFERENCES topics(id) ON DELETE SET NULL
  occurred_on   date NOT NULL                 -- the device's local day, as topics use
  name          text NOT NULL
  name_lower    text NOT NULL                 -- see the index
  kind          text
  note          text
  aliases       text[] NOT NULL DEFAULT '{}'  -- stored ALREADY LOWERCASED
  created_at    timestamptz NOT NULL DEFAULT now()
```

One row per entity per session. Not deduplicated across sessions: *"PB Tech came up in three
meetings"* is a count worth having.

---

## The ACL, written out rather than described

Draft 2 proposed `(site_id IS NULL OR site_id = ANY(...)) AND user_id = ANY(author_ids)`. That
rule is wrong twice, in opposite directions, and draft 3's repair introduced a third.

**`author_ids` is None for ALL and SITE scopes** (`repositories/scope.py:66` — *"no per-author
filter"*). `ANY(NULL)` is NULL, so that clause is never true and **every admin, gm, pm and
site-scoped caller sees zero entities**. Opened by an admin on day one, the feature looks
entirely dead.

**Repairing only that opens the other hole.** The chunk query needs no `company_id` because
`site_id = ANY(site_ids)` is already company-pinned. `site_id IS NULL OR ...` bypasses that, so
a site-less row **from another company** passes both clauses.

**And draft 3's unconditional `company_id` pin breaks platform_admin — the third review was
right, and the code says why explicitly.** `visible_scope` has one branch that is *"the SOLE
branch NOT pinned to caller.company_id"* (`scope.py:47-49`): for a cross-company caller,
`site_ids` is every site in every company. A hard company pin would intersect that back down to
one company and make the cross-company role quietly narrower here than everywhere else.

`visible_scope` already publishes the flag. The rule uses it:

```sql
(%(cross_company)s OR e.company_id = %(company_id)s)
AND (e.site_id IS NULL OR e.site_id = ANY(%(site_ids)s))
AND (%(author_ids)s::uuid[] IS NULL OR e.user_id = ANY(%(author_ids)s::uuid[]))
AND <deletion predicate>
```

For an ordinary caller the company pin holds and the site-less row from another company is
gone. For a cross-company caller the pin lifts, which is that role's whole definition.

`site_id IS NULL` is deliberate: chunk sessions with no resolvable site exist (BUG-43 — all
three site sources can miss at once), and a strict site filter makes those invisible to
everybody, which is a blind spot rather than a safeguard.

**`user_id` nullable is a real state**, not an oversight: `resolve_user` can miss. A NULL author
is then invisible to SELF scopes and visible to managers, which is the correct direction and is
asserted rather than assumed.

---

## The index, and the case problem that would have made it useless

Draft 1's expression does not compile: `array_to_string()` is **STABLE, not IMMUTABLE**.
Draft 2 fixed that with `array_to_tsvector` — and introduced a worse failure, because
**`array_to_tsvector` inserts lexemes verbatim and does not fold case.**

`{"Firefire"}` becomes the lexeme `Firefire`, while any query-side `plainto_tsquery` lowers its
terms. A mixed-case alias — and a mangled proper noun is *always* mixed case, that is what it
is — silently never matches. **The feature's one purpose, defeated by its own index, with no
error anywhere.** Nor is it fixable in the expression: a generated column may not contain a
subquery, so `lower()` over the array happens in the writer.

```sql
ALTER TABLE session_entities ADD COLUMN search tsvector
  GENERATED ALWAYS AS (
      to_tsvector('simple', coalesce(name_lower, '') || ' ' || coalesce(lower(note), ''))
      || array_to_tsvector(coalesce(aliases, '{}'))
  ) STORED;
CREATE INDEX session_entities_search ON session_entities USING GIN (search);
```

The writer lowercases both and **rejects empty-string elements**: `array_to_tsvector` raises on
`''`, and `coalesce(aliases,'{}')` guards the NULL array and not an empty element inside it.
One blank alias would fail the whole INSERT inside a background lambda, where nobody is watching.

**The query side is pinned too, and it is half of the same bug.** `plainto_tsquery(%s)` without
a configuration uses `default_text_search_config`, which is a *server setting* — so the index
would be built with `'simple'` and queried with whatever the cluster happens to be set to, and
a later parameter-group change would break search with no deploy and no error. It is
`plainto_tsquery('simple', %s)`, and a test asserts both halves name the same configuration,
because they live in two files that never mention each other.

A generated column rather than an expression index, so the value is visible when debugging why
a row did not match. `simple`, not `english`: these are proper nouns, and a stemmer hurts some
while helping none.

Known and accepted: array lexemes carry no positions, so `ts_rank` scores an alias-only match
near zero. Ranking is not what this arm is for — finding the row at all is.

---

## The search arm, and the payload that does not carry a query

`lambda_rag_search` gains a keyword arm. RagSearch is already in-VPC, already resolves the
caller, already applies `visible_scope`; a new function would solve database reachability a
second time.

**The arm has no input today, and both earlier drafts missed it.** RagSearch receives
`{"sub", "query_embedding", "k"}` and refuses anything else — *"missing sub or
query_embedding"*. The raw query string never reaches the function, and both invokers in
`lambda_ask_agent` build exactly that payload. A keyword arm added without changing them
compiles, deploys, returns an empty list forever, and looks like "no entities matched".

That is this repository's own green-over-a-dead-path shape, so it is a listed step and not an
implementation detail: **every caller passes `query_text` alongside the embedding, in the same
change**, and the arm asserts it is present rather than defaulting to empty.

**The arm does not merge.** The vector arm returns chunk rows scored by cosine distance; this
one returns entity rows scored by `ts_rank`. The scales are not comparable, the shapes are not
the same, and `{"chunks": [...]}` is consumed by `lambda_ask_agent` in two places plus the
legacy frontend. The response gains a separate `"entities": [...]` key.

---

## The wiring, and the correction the third review got wrong

Draft 3 said "a direct invoke removes items 1–3". Two of the three objections to that hold and
one does not, so the list is restated with what is actually true:

1. **ItemWriter does accept a direct invoke.** It iterates `event.get("Records", [])`
   (`lambda_item_writer.py:928`) and warns *"skipping non-extraction S3 key"* on anything else.
   The third review read that as a blocker. **It is not** — but the fix is not to hand it a
   *synthetic* S3 event either: a fabricated `Records` envelope is a lie about provenance that
   the next reader has to decode. ItemWriter gains an explicit second entry shape,
   `{"entities": {...}}`, routed **before** the Records loop, so the two callers stay
   distinguishable in the code and in the logs.
2. **Finalize has no `lambda:InvokeFunction` and the third review was right.** Its `Policies`
   block does not carry the action. One statement, scoped to ItemWriter's ARN — but it is a
   listed step, because a missing permission here surfaces as an `AccessDeniedException` inside
   a background lambda, which is the shape that produced three silent failures this session.
3. **No retry, and that is the real cost.** An S3 artifact gets Lambda's async retry ladder; a
   synchronous invoke gets one attempt. The brief is regenerable — finalize can be re-run for a
   session — so a lost invoke is recoverable and not a data loss. **Stated so it is a choice.**
4. BUG-36 permits this direction. Non-VPC → invoke → in-VPC is explicitly allowed; CLAUDE.md
   warns that mis-banning it *"forces building an S3 hop that should have been a direct
   invoke, and BUG-33 means every new S3 trigger is hand-wired outside the template."*
   Draft 3 cited this backwards, and then draft 3's reviewer cited the correction backwards.

The S3-artifact alternative remains available if retry semantics later justify it. It costs a
hand-wired notification (BUG-33), a routing branch, and two IAM prefixes — items 1–3 of the
draft-3 list, which the direct invoke genuinely does avoid.

---

## Sequencing

`EnableSessionBrief` is **default false** and drives whether any of this has input at all.
Backfill can only reach sessions briefed while it was on — currently TEST, one day. That is
worth knowing before promising the feature, and it is why the write path and the backfill are
separate tasks: the first is worth building now, the second when there is a corpus.

## What this does not do

- No UI. The API gains a key; presenting it is a frontend change and a separate spec.
- No entity resolution across sessions or companies. Two meetings, two rows, deliberately.
- No change to extraction.

## The decision I need

**Whether the brief's aliases should be written into `name_aliases` as well as
`session_entities`.** It would improve the existing display-layer correction immediately and
for every surface, and it is the only part of this that touches something already in
production. I lean yes, separately from this change and after it.
