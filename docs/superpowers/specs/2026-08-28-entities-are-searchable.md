# Spec: the brief already knows what the meeting was about; nothing can look it up

**Status:** proposal, eighth draft after seven reviews.
**Date:** 2026-08-28
**Repo:** `fieldsight-pipeline`.

> Four drafts, and the interesting thing is not that each was wrong. It is that drafts 1–3
> each fixed a defect and the defect **moved**: nothing could write the table → the writer was
> fixed and the immutability broke → that was fixed and "this rule cannot attach to this
> table" reappeared on the deletion predicate → that was fixed and the *value* in the column
> was never stated.
>
> Draft 4 broke the chain differently, and worse: it stated the value, and the reasoning
> behind it was **false**. What followed was the only structural change draft 5 made — **the
> mechanism draft 4 invented was deleted, not repaired.** A resolver that existed to work
> around a problem that does not exist cannot be made correct. Three blocking issues went
> with it, and the fifth review confirmed none of them left a real requirement behind.
>
> And then the defect relocated three more times, always onto the same number. Draft 5 added
> delete-then-insert so retries could not inflate a *"came up in three meetings"* count. Draft
> 6 found a three-device meeting inflating it anyway and answered with a matching delete —
> which turns the inflation into a permanent **zero**, because the merged meeting produces no
> brief and nothing writes a replacement. Draft 7 stopped deleting and moved the count into a
> query over `meeting_session.group_id` — and that column records *joining*, not *one meeting*,
> so it under-counts a group the merge sweep **rejected** and over-counts an offline joiner
> whose adoption window closed. Wrong in both directions, on paths the repo documents as
> routine.
>
> **Seven drafts, five mechanisms, one number — and nobody asked for the number.** The measured
> gap below is a lookup: *"which brands came up yesterday?"*. *"Came up in three meetings"*
> entered in draft 3 as a remark about why rows are not deduplicated, and every draft since has
> been an attempt to defend it. It is not in the motivation, no caller requests it, and making
> it exactly right requires deciding what "one meeting" means in a system where a device can
> join a group that is later rejected and a recording can sync after the meeting it belonged to
> has ended.
>
> **So it is gone.** What replaces it is the thing that is exactly computable and was always
> what the feature needed: which sessions a name appears in. Whether those sessions were one
> meeting is a separate question with its own machinery, and this table is not the place to
> answer it.

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

`GET /api/org/sessions/{id}/brief` reads one brief by key. Nothing searches across them.

### Aliases are not new here (correction, draft 1)

`name_aliases` (migration 0020) already stores wrong → right per company and site, and
**`lambda_rag_search` already reads it** (`lambda_rag_search.py:116`). That does not remove the
motivation — `name_aliases` cannot help when the word is absent from every chunk — but it
decides a question draft 1 never asked. **The brief's aliases should feed `name_aliases`**,
not sit in a second parallel table.

---

## `source_s3_key`: what draft 4 got wrong, and what removing it removes

Draft 4 claimed a constructed `extractions/{folder}/{date}/sid{hex}` would be matched by no
tombstone, because the session base is `{device}_{date}_{time}_sid{hex}`. **That is false.**

```
session_scope.py:199        _CHUNK_SESSION_BASE_RE = r"^sid([0-9a-f]{32})$"
lambda_extract_session.py:570   session_base = f"sid{session_id}"
```

`{device}_{date}_{time}` is the **legacy whole-file** base. A chunk session's base is
`sid{hex}`, its topics carry `extractions/{folder}/{date}/sid{hex}.json`, and the tombstone
`_source_prefixes_for` builds is `extractions/{folder}/{date}/sid{hex}` — which **is** a prefix
of it. Draft 4 confused the chunk *file* name (`{user}_{ts}_sid{id}_c{NNNN}`) with the session
base. The key is constructible, and always was.

Everything draft 4 built on that premise is therefore deleted rather than corrected, and with
it three defects that only existed because the mechanism did:

- a `SELECT … FROM topics WHERE company_id = %s` — **`topics` has no `company_id` column**
  (verified against the live TEST cluster: `column t.company_id does not exist`). Every other
  query in that file reaches the tenant through `users`. It would have raised `UndefinedColumn`
  inside a background lambda, and a connection double cannot see it.
- a LIKE pattern `'%sid' || hex` with no trailing wildcard, against keys that all end `.json`.
  It matches **nothing, for every session**. Combined with draft 4's "if it resolves nothing,
  do not write the row", the feature would have shipped writing zero rows, forever, reporting
  success. That is draft 3's defect relocated one more time: *"a column with the wrong string
  protects nothing"* had become *"a resolver that never resolves writes nothing"*.
- an ordering race: the brief is produced by non-VPC finalize, the topics by the separate
  extraction path. On an offline bulk upload the topic may not exist yet, so "refuse when
  unresolved" would permanently skip that session.

**So the key is built, not looked up — and the builder already exists.** Draft 5 proposed
adding `extraction_key(folder, date, session_base)`; the fifth review found it at
`lambda_extract_session.py:1589`, with that signature, used in exactly one place (`:1698`).
So this is a **move**, not an invention — the same move `EXTRACTION_KEY_RE` already made into
`session_scope`, for the reason that file states: *"so the writer and the readers share ONE
definition."* No key changes value.

Two things travel with it, and both are the kind that get lost in a move:

- the existing docstring carries load-bearing semantics (*live and final passes deliberately
  collide on one key*). It moves verbatim; the collision is the idempotency of the extraction
  path and is not this feature's to reinterpret.
- **`lambda_finalize_claim.py:267` `group_merged_key` is a second formatter and must stay
  one.** Its docstring says why it is not `extraction_key(...)`. A test that says "one place
  defines the key shape" would sweep it in and delete a distinction somebody made on purpose,
  so the test asserts round-tripping (`parse_extraction_key(extraction_key(x)) == x`) and that
  `group_merged_key`'s output still parses — the grp/sid difference is a `session_base`
  spelling, not a key shape.

**`topic_id` is dropped from the table entirely.** It existed only as a second deletion arm,
and setting it requires exactly the lookup this section just deleted. `DELETED_SOURCE_PREDICATE`
alone is the guard, which is what draft 3's review said the source arm was for.

### Legacy sessions

Whole-file recordings never reach finalize — there is no `meeting_session` and no sid — so they
produce no brief and no entities. Stated because "legacy resolves nothing" would otherwise
read as a gap rather than as the absence of an input.

---

## The table

```
session_entities
  id            uuid PK
  company_id    uuid NOT NULL REFERENCES companies(id) ON DELETE CASCADE
  site_id       uuid REFERENCES sites(id)
  user_id       uuid REFERENCES users(id)     -- the recorder; the ACL needs it
  session_id    text NOT NULL                 -- BARE hex; joins meeting_session
  source_s3_key text NOT NULL                 -- session_scope.extraction_key(...)
  occurred_on   date NOT NULL                 -- the device's local day, as topics use
  name          text NOT NULL
  kind          text
  note          text
  aliases       text[] NOT NULL DEFAULT '{}'  -- stored ALREADY LOWERCASED
  created_at    timestamptz NOT NULL DEFAULT now()
```

**`session_id` is the bare hex and never the `sid{hex}` spelling.** Three spellings of one
thing are already in play — the artifact's `sessionId` is bare, the brief's S3 key is
`sid{hex}`, topics use `sid{hex}` — and two spellings of a session are equal as sessions and
not as strings, which in this repo produced the `label_map` and `_SOURCE_RANK` failures. The
bare form is chosen because it is what `meeting_session.session_id` holds, and that is the
join the count depends on. The writer normalises once and a test pins it.

**`name_lower` is gone** (fourth review): `lower(name)` is IMMUTABLE and legal inside the
generated column, so a stored copy is only a drift surface. Only the array genuinely needs
writer-side lowering.

### One row per session, and no claim beyond that

Nothing is deleted at merge time. Three member sessions produced three briefs; three rows are
an honest record of that.

Retries are handled the way the topics beside them are: **delete by `source_s3_key`, then
insert, under `pg_advisory_xact_lock(hashtext(key))`** (`lambda_item_writer.py:572`, whose
comment states delete-then-insert is not concurrency-safe without it). Every writer touches
only its own key, so same-key locking is exactly the guarantee needed — which was **not** true
in draft 6, where a merge holding `hashtext(grp_key)` was expected to serialise against a
member holding `hashtext(member_key)`.

**The API returns rows and counts sessions, not meetings.** *"In how many sessions does this
name appear"* is `COUNT(DISTINCT session_id)` — exact, no join, no group semantics. Every
attempt to report **meetings** instead has failed, and the seventh review shows why it is not
a wording problem:

| case | what the system says | what `group_id` says |
|---|---|---|
| merge sweep **rejects** the group (>12h span, cross-company) | two meetings — each member's own topics stand (`lambda_item_writer.py:212`) | one, and `group_id` is never cleared (`repositories/meeting_session.py:39`) |
| offline joiner syncs after the lead pressed **End** | adoption refused, stays solo (`lambda_org_api.py:802`) | `group_id` NULL, forever — the convergence draft 7 promised never arrives |

Collapsing correctly means consulting `session_group.merge_result` and stating an adoption
boundary. Both are real and neither belongs here: the table would be carrying a second opinion
about meeting identity, next to the one the merge machinery already owns.

**Stated as a cost, because it is one.** A caller asking *"how often did PB Tech come up"* gets
sessions, and a three-device meeting counts as three. That number is defensible in a sentence
— *"three recordings mentioned it"* — and no draft has managed a defensible sentence about
meetings.

**The stored key is bare hex** rather than the `sid{hex}` spelling: it is what
`meeting_session.session_id` holds, with a `^[0-9a-f]{32}$` check on it (migration 0026), so
anything that later does want the group can join without a `substring()`. The `sid` prefix is
added where an S3 key is built and nowhere else.

### Deletion of a merged meeting, written down

Per-session deletion works: the tombstone `_source_prefixes_for` builds
(`lambda_org_api.py:3555`) prefixes the entity's `source_s3_key`, so deleting a recording hides
its entities. **When one member deletes, the other members' entities remain** — visible in
their own folders, still answering searches.

That follows from deletion being per-recording, and it is the same shape as the known `grp{gid}`
topics leak rather than a new one. It is written here because this repository has a note about
a deletion that says "deleted" and means something narrower, and because entities are a new
surface where the difference becomes visible.

**Inherited, not introduced: a session straddling NZ midnight** puts its transcripts — and so
its topics — under two date folders, i.e. two extraction keys, while finalize's brief carries
one date. The entity's `source_s3_key` matches only one of them, so a delete issued from the
other date's listing does not hide it. Topics already split the same way and the tombstone
already covers one date, so entities inherit the gap rather than widening it. Noted so the
next reader does not rediscover it as new — and so that if it is ever fixed, it is fixed for
both.

---

## The ACL, written out rather than described

Draft 2 proposed `(site_id IS NULL OR ...) AND user_id = ANY(author_ids)`. Wrong twice, in
opposite directions; draft 3's repair introduced a third.

**`author_ids` is None for ALL and SITE scopes** (`repositories/scope.py:66` — *"no per-author
filter"*). `ANY(NULL)` is NULL, so every admin, gm, pm and site-scoped caller sees zero
entities. Opened by an admin on day one, the feature looks entirely dead.

**Repairing only that opens the other hole**: `site_id IS NULL OR ...` bypasses the company
pinning that `site_id = ANY(site_ids)` provides for chunks, so a site-less row from another
company passes.

**And draft 3's unconditional company pin breaks platform_admin.** `visible_scope` has one
branch that is *"the SOLE branch NOT pinned to caller.company_id"* (`scope.py:47-49`). A hard
pin would make the cross-company role quietly narrower here than everywhere else.

```sql
(%(cross_company)s OR e.company_id = %(company_id)s)
AND (e.site_id IS NULL OR e.site_id = ANY(%(site_ids)s))
AND (%(author_ids)s::uuid[] IS NULL OR e.user_id = ANY(%(author_ids)s::uuid[]))
AND <DELETED_SOURCE_PREDICATE>
```

**`visible_scope` returns sets, and psycopg does not adapt a set.** `lambda_rag_search.py:88-90`
converts to lists; the new arm must too. A listed step, not a detail — it is a `TypeError` at
runtime that no double reproduces.

**The `site_id IS NULL` arm is wider than it looks**, and the fourth review is right that
draft 4 disclosed the choice without its consequence: a pm pinned to one site will see
site-less entities from meetings on sites they cannot otherwise reach. That is accepted
deliberately — BUG-43 shows all three site sources can miss at once, and the alternative makes
those rows invisible to everybody, which is a blind spot rather than a safeguard — but it is
**wider than the chunk-search precedent**, which requires a site match, and it should be
decided rather than inherited.

**`user_id` nullable is a real state**: `resolve_user` can miss. A NULL author is invisible to
SELF scopes and visible to managers, which is the correct direction and is asserted.

---

## The index, and the case problem that would have made it useless

Draft 1's expression does not compile: `array_to_string()` is **STABLE, not IMMUTABLE**.
Draft 2 fixed that with `array_to_tsvector` — and introduced a worse failure, because
**`array_to_tsvector` inserts lexemes verbatim and does not fold case.** `{"Firefire"}` becomes
the lexeme `Firefire` while `plainto_tsquery` lowers its terms, so a mixed-case alias — and a
mangled proper noun is *always* mixed case — silently never matches. **The feature's one
purpose, defeated by its own index, with no error anywhere.**

```sql
ALTER TABLE session_entities ADD COLUMN search tsvector
  GENERATED ALWAYS AS (
      to_tsvector('simple', lower(coalesce(name, '')) || ' ' || lower(coalesce(note, '')))
      || array_to_tsvector(coalesce(aliases, '{}'))
  ) STORED;
CREATE INDEX session_entities_search ON session_entities USING GIN (search);
```

The writer lowercases `aliases` and **drops both empty strings and NULLs**:
`array_to_tsvector` raises on `''` (*lexeme array may not contain empty strings*) and
separately on a NULL element (*may not contain nulls*), and `coalesce(aliases,'{}')` guards the
NULL **array** and not a null **inside** it. Drafts 1–5 named only the empty string. Either one
fails the whole INSERT in a background lambda, and an LLM-produced alias list is exactly where
a null shows up.

**The query side is pinned too, and it is half of the same bug.** `plainto_tsquery(%s)` without
a configuration reads `default_text_search_config`, a *server setting* — the index would be
built with `'simple'` and queried with whatever the cluster is set to, and a later parameter
group change would break search with no deploy and no error. It is
`plainto_tsquery('simple', %s)`, and a test asserts both halves name the same configuration.

`simple`, not `english`: these are proper nouns, and a stemmer hurts some while helping none.
Array lexemes carry no positions, so `ts_rank` scores an alias-only match near zero; finding
the row at all is what this arm is for.

---

## The search arm, and the payload that does not carry a query

`lambda_rag_search` gains a keyword arm — already in-VPC, already resolves the caller, already
applies `visible_scope`.

**The arm has no input today, and drafts 1–2 missed it.** RagSearch receives
`{"sub", "query_embedding", "k"}`, and both invokers in `lambda_ask_agent` build exactly that.
A keyword arm added without changing them compiles, deploys, returns an empty list forever,
and looks like "no entities matched" — this repo's green-over-a-dead-path shape. **Every
caller passes `query_text` alongside the embedding, in the same change.**

**It does not assert on a missing one.** Drafts 5–6 said it should, and that contradicts this
function's stated contract: soft failures return `{"chunks": [], "error": ...}`, and
`lambda_ask_agent` turns a `FunctionError` into *"search backend failed"* for the **whole**
search (`lambda_ask_agent.py:706`). In a deploy window where new rag-search serves old
ask-agent, an assert would take chunk search down with it. The arm returns `entities: []` with
an `error` note — visible in the response, harmless to the other half.

(A claim in drafts 5–6 was simply wrong: rag-search does not "refuse anything else". It
ignores unknown keys and already accepts `date_from`/`date_to`/`site`
(`lambda_rag_search.py:66`), which makes adding `query_text` safer than stated.)

**The arm does not merge.** Cosine distance and `ts_rank` are not comparable scales, and
`{"chunks": [...]}` has three consumers. The response gains a separate `"entities": [...]` key.

**And it must not be added in the natural place.** `_search` returns early three times — at
`:72` (no `sub`/`query_embedding`), `:81` (caller not provisioned), and **`:109` (`if not
site_ids: return {"chunks": [], "site_count": 0}`)**. An arm written after the chunk query
never runs for a caller whose site set is empty or whose `site_filter` missed — and the ACL
above says `site_id IS NULL` entities are visible to *exactly those callers*. Worse, the
`entities` key is absent from every early return, so the consumer's `.get("entities")` reads as
"nothing matched".

That is this file's own green-over-a-dead-path shape for the third time, so both halves are
pinned: **the entity query does not depend on the chunk side's `site_ids` short-circuit**, and
**every return path carries an `entities` key**, empty list included. A test drives the
zero-sites caller and asserts the key is present.

**When a caller scopes to one site, entities use the narrowed set** (`lambda_rag_search.py:96`)
plus the `site_id IS NULL` arm — not the full accessible set. A site filter is the caller
saying which site they mean, and answering it with entities from elsewhere would make the
filter mean something different on one half of the response than on the other.

---

## The wiring

1. **ItemWriter accepts a direct invoke.** It iterates `event.get("Records", [])`
   (`lambda_item_writer.py:928`). The fix is not a *synthetic* S3 envelope — a fabricated
   `Records` is a lie about provenance — so it gains an explicit `{"entities": {...}}` shape
   routed **before** the Records loop.

   That branch **bypasses everything `write_extraction_items` does on the way in**: company
   resolution (`:593`), site attribution, the write-side deleted-source check (`:676`). It
   reuses the first two by calling the same rungs in the same order — the entity row's
   `company_id`, `site_id` and `user_id` are not a second attribution scheme.

   It **also runs the write-side deleted-source check**. Draft 6 said the topics path skips it
   and cited `:914`; that is a misreading — the topics path *runs* `_source_is_deleted`
   (`:679`) and fails open only when the check itself errors (`:905`). Copying the real
   behaviour is free, and inventing a laxer one for entities would be a second deletion
   policy, which is what this spec spent three drafts avoiding.

   **A site-resolution miss skips the entity write**, matching the topics path's "zero writes"
   rather than writing `site_id NULL`. The two are not equivalent: the ACL makes NULL-site
   rows company-wide visible, so writing on a miss would silently widen who sees them.
   `site_id NULL` stays reserved for a session that genuinely has no site (BUG-43), which is a
   different fact from a resolution that failed.
2. **Finalize has no `lambda:InvokeFunction`** (`template.yaml:2517-2548` carries only S3 and
   SES). One statement scoped to ItemWriter's ARN — listed, because a missing permission here
   surfaces as an `AccessDeniedException` inside a background lambda.
3. **No retry**: an S3 artifact gets Lambda's async ladder, a synchronous invoke gets one
   attempt. The brief is regenerable, so a lost invoke is recoverable — and with delete-then-
   insert, re-running is safe. Stated so it is a choice.
4. BUG-36 permits this direction (non-VPC → in-VPC), with `AskAgent → RagSearch` as precedent.

The S3-artifact alternative stays available if retry semantics later justify it; it costs a
hand-wired notification (BUG-33), a routing branch, and two IAM prefixes.

## Migration numbering

`src/migrations/` already carries two `0041_*` and two `0044_*` files. The number is chosen at
merge time against the branch that lands first, not written into this spec.

## Sequencing

`EnableSessionBrief` is **default false** and drives whether any of this has input. Backfill can
only reach sessions briefed while it was on — currently TEST, one day. The write path is worth
building now; the backfill when there is a corpus.

## What this does not do

- No UI. The API gains a key; presenting it is a separate spec.
- No entity resolution across sessions or companies. Two sessions, two rows, deliberately.
- **No meeting-level count.** Sessions are counted; whether several were one meeting is
  the merge machinery's question and is answered there or not at all.
- No change to extraction.

## The two decisions I need

1. **Whether the brief's aliases should also be written into `name_aliases`.** It would improve
   the existing display-layer correction immediately and for every surface, and it is the only
   part of this that touches production. I lean yes, separately and after.
2. **Whether site-less entities should be company-wide visible**, as written, or restricted to
   a site match like the chunk search. As written they are wider; BUG-43 is the reason, and it
   is a real one, but it is a visibility decision rather than an implementation one.
