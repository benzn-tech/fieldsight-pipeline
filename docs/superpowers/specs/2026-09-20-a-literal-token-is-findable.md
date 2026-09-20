# A literal token is findable (design)

**Date:** 2026-09-20 · **Status:** design
**Scope:** `src/repositories/search_sql.py:build_search_sql()` and its two callers
(`/search` and Ask retrieval), both of which run the identical query.

---

## 1. The measured gap

Real user session, UCPK2 dev account, 2026-09-03. The records contain a topic titled
`Light pole PS4 and electrical hold-up` and an action item `Provide PS4 for light poles`,
folded into one `chunk_type='topic'` chunk by `chunking.py:_topic_text` (verified in
`extractions/Ben_UCPK2/2026-09-03/sid81a64d850bb34d8bbf01ec11fd2af02f.json`, exact
uppercase "PS4").

- Asking the Ask agent "did we talk about ps4?" returns the correct cited answer.
- Typing `PS4` into the SEARCH box returns nothing at all.

Both surfaces run the same retrieval. `build_search_sql()` is pure vector ANN
(`src/repositories/search_sql.py:5-31`):

```
SELECT c.id, c.chunk_text, ... , c.embedding <=> %(q)s::vector AS distance
FROM report_chunks c
LEFT JOIN topics t ON t.id = c.topic_id
LEFT JOIN sites s ON s.id = c.site_id
WHERE c.site_id = ANY(%(site_ids)s) AND ... AND visible_chunks_predicate("c")
ORDER BY c.embedding <=> %(q)s::vector
LIMIT %(k)s
```

`k` is capped at 30-32 depending on caller (`lambda_fieldsight_api.py:1491 search_topics`
defaults `k=30`; `lambda_ask_agent.py` requests up to 32). There is no distance cutoff in
this SQL and no keyword arm anywhere in it. `git grep -nEi
"ILIKE|tsvector|to_tsquery|websearch_to_tsquery|plainto_tsquery" origin/main -- src/`
returns exactly one hit, a comment in `src/repositories/voiceprints.py:297` about NOT doing
`ILIKE` for name matching — irrelevant to retrieval. There is no lexical retrieval arm in
this codebase today.

The "hybrid" behaviour that exists lives entirely downstream of retrieval, in
`src/lambda_ask_agent.py`:

- `_lexical_terms()` (`:769-786`) tokenizes the *question*, not the corpus, into terms a
  result's title can be checked against.
- `_aggregate_topics()` (`:797-885`) marks each already-retrieved row `"lexical": any(t in
  hay for t in terms)` against the **topic title only** (`hay = derived_title.lower()`,
  `:865`), reorders lexical-matching rows first, and drops non-lexical rows whose distance
  exceeds `_NO_LEX_MAX_DIST = 0.55` (`:753`, applied at `:882`).
- `ps4` passes `_lexical_terms`'s own admission test: `_IDENTIFIER = re.compile(r"[a-z]\d|\d[a-z]")`
  (`:766`) exists specifically to exempt identifiers like this from the 3-character floor.
  This is not a tokenization bug — the term is recognized as lexical. The chunk simply never
  arrives at `_aggregate_topics` to be checked, because it lost the top-*k* ANN race before
  any lexical logic runs.

Call chain, each hop verified: `fieldsight-ui` `scripts/api/search.js` (`origin/dev`,
`POST /search`, project-scoped via `site`) → `lambda_fieldsight_api.py:1491 search_topics()`
→ invokes `AskAgentFunction` with `mode:'search'` → `lambda_ask_agent.py:888
_rag_search_list()` → `RagSearchFunction` (`lambda_rag_search.py:344`
`chunks.search_chunks(...)`) → `repositories/chunks.py:62 search_chunks()` →
`build_search_sql()`.

## 2. The generalization

This is not "short strings are hard" — `_lexical_terms` already special-cases identifiers
correctly. The failure is upstream of anything that string can be sensitive to: **a literal
token present in the indexed text is unreachable through this system unless the chunk that
contains it also happens to land in the top ~30 by cosine distance.** A chunk can contain
the exact string the user typed and still never be considered, because 30+ other chunks
about unrelated topics were closer to the query embedding than this one was.

This generalizes past "PS4": any rare literal — a drawing number, an RFI number, a product
code, a person's surname, a lot number — is exactly the kind of token whose *meaning* to an
embedding model is thin (it's an opaque identifier, not a concept) while its *literal*
recurrence is the entire reason a site team is searching for it. These tokens systematically
lose the top-*k* race against topically-similar chunks, which is the opposite of what a
search box is for.

## 3. Controller's ruling

**Run a keyword arm on every query, unioned with the vector arm before the `k` cap — not
gated behind a "query looks short" heuristic.** This is a ruling, not the only option; the
owner can overturn it, but the reasoning is:

1. **Rarity, not length, is the variable that loses the race.** "PS4" is 3 characters and
   already passes `_lexical_terms`'s length gate; the problem was never that it's short, it's
   that "electrical hold-up" and forty other topic chunks about site issues are semantically
   closer to "did we talk about ps4" than the PS4 chunk itself, given a `k` of 30. A query
   like "who signed off the RFI-0231 variance" is not short either, and would lose the same
   race for the same reason: `RFI-0231` carries almost no embedding signal relative to the
   surrounding prose.
2. **"What counts as short" is exactly the kind of arbitrary number this codebase has
   already been burned by.** The 500-object cap (referenced in project memory as a limit
   with no measurement behind it that silently broke an ordinary day) is the template for
   this mistake: a threshold invented without a distribution behind it, which works until a
   real case sits just past it. There is no principled length below which a query "needs"
   keyword help and above which it doesn't; PS4 already shows a 3-character token is not
   the boundary.
3. **The cost is bounded and cheap.** One additional indexed lookup per query on a column
   that is going to be indexed for exactly this (§4), unioned before the existing `LIMIT
   %(k)s`. It does not change the cost model of the ANN arm at all.

## 4. Mechanism

Two candidates, compared on this schema (`report_chunks.chunk_text text NOT NULL`,
`src/migrations/0004_report_chunks.sql:1-11`, no `pg_trgm` extension currently installed —
`src/migrations/0001_extensions.sql` enables only `vector` and `pgcrypto`):

**`ILIKE '%term%'` + `pg_trgm` GIN index.**
- Needs `CREATE EXTENSION pg_trgm` (a new extension in a prod database — a real change, but
  a one-line, well-understood one) and `CREATE INDEX ... USING gin (chunk_text
  gin_trgm_ops)`.
- Matches inside word boundaries (`"ps4"` would also match inside `"nonps400"`), which is
  wrong for identifier search: it produces false positives on adjacent tokens and drawing
  numbers that share digit runs.
- No backfill needed beyond building the index once — `ILIKE` reads `chunk_text` as it sits,
  there is no derived column to keep in sync.
- Rejected as the sole arm because word-boundary blindness is the wrong failure mode for the
  motivating case: "PS4" should not spuriously match "PS40" or "GPS4x". It is also weaker at
  ranking (no term-frequency notion at all, pure substring test), which matters once results
  are unioned and need a comparable relevance signal.

**`tsvector` column + GIN index + `websearch_to_tsquery`.**
- Needs a migration: `ALTER TABLE report_chunks ADD COLUMN chunk_tsv tsvector`, a
  `GENERATED ALWAYS AS (to_tsvector('english', chunk_text)) STORED` column (Postgres 12+;
  this repo's migrations already assume a modern managed Postgres, given `vector`/`hnsw`
  support in `0004_report_chunks.sql:16`) or a trigger-maintained one, and `CREATE INDEX ...
  USING gin (chunk_tsv)`.
- Word-boundary aware: `to_tsquery`/`websearch_to_tsquery` tokenizes on the same rules as the
  indexed text, so `ps4` matches the token `ps4` and not `ps40`. This is the correct
  semantics for identifier and literal-term search.
- **Every existing row needs the column backfilled** the first time the column is added
  (a `GENERATED ALWAYS ... STORED` column computes automatically on `ALTER TABLE ADD COLUMN`
  for existing rows in modern Postgres, so this is a one-time `ALTER` cost paid at migration
  time, not a separate backfill script — but it must be treated as a backfill event: this
  repo already has a hard rule that **changing embedding or chunking requires a manual
  backfill to rebuild the index** (project convention; see `docs/superpowers/runbooks/2026-07-31-backfill-topic-id.md`
  and `src/backfill_site_coords.py` for the existing backfill pattern). A `tsvector` column
  inherits the identical obligation for a different reason: any future change to the
  tokenizer/dictionary (`'english'` → something else, or adding stemming exceptions for
  identifiers) requires the column to be regenerated, exactly as re-embedding requires
  `reindex-vectors apply` today. If the column is `GENERATED ALWAYS`, Postgres itself keeps
  it in sync with `chunk_text` on every future write — there is no writer to remember, which
  is strictly better than the embedding pipeline's manual-backfill obligation, not merely
  equivalent to it.
- Language config `'english'` will stem "electrical" → "electr" etc., which is fine for
  prose terms but must not stem or mangle alphanumeric identifiers. Postgres's default
  parser already treats `ps4`, drawing numbers like `a-101`, and similar tokens as `asciiword`
  or `numword`/`alphanum` classes and passes them through largely intact; this needs a
  one-time check against a handful of real identifiers from the corpus (not just PS4) before
  shipping, not an assumption.

**Recommendation: `tsvector` + GIN + `websearch_to_tsquery`.** Word-boundary correctness is
the deciding factor — `ILIKE`'s substring matching would make "PS4" match "PS40," which is
the wrong side to be wrong on for a feature whose whole point is precise identifiers. The
cost is one migration (new nullable/generated column + index) and the same backfill
discipline this repo already applies to embeddings; both are one-time and mechanical, not
research questions. `websearch_to_tsquery` also degrades gracefully on natural-language
input (handles quoted phrases, `-exclude`, `OR`) without needing app-side query parsing.

## 5. How the two arms combine

Shape: a `UNION` of the vector arm and the keyword arm, each independently limited, combined
*before* the final `LIMIT %(k)s`, not a post-filter re-rank of one arm's output by the other:

```sql
WITH vec AS (
  SELECT c.*, c.embedding <=> %(q)s::vector AS distance, false AS lexical_hit
  FROM report_chunks c
  WHERE <site/author/date/visibility predicates>
  ORDER BY c.embedding <=> %(q)s::vector
  LIMIT %(k)s
),
lex AS (
  SELECT c.*, 1.0 AS distance, true AS lexical_hit   -- no cosine meaning; see below
  FROM report_chunks c
  WHERE <site/author/date/visibility predicates>
    AND c.chunk_tsv @@ websearch_to_tsquery('english', %(q)s)
  LIMIT %(k)s
)
SELECT DISTINCT ON (id) * FROM (SELECT * FROM vec UNION ALL SELECT * FROM lex) u
ORDER BY id, lexical_hit DESC   -- a row found both ways keeps its real distance, not the placeholder
LIMIT %(k)s * 2  -- generous; final ranking happens in _aggregate_topics as today
```

The lexical arm's rows carry no meaningful cosine distance (they were never scored against
the query embedding), so they must not be assigned a fabricated *good* distance — doing so
would let a keyword hit silently outrank a true semantic top-1. `_aggregate_topics` already
carries a `lexical: bool` per row (`lambda_ask_agent.py:875`, `"lexical": any(t in hay for t
in terms)`); a row produced by the new SQL arm should set this same field from `lexical_hit`
returned by the query rather than recomputing it against the title text, which folds the two
signals (SQL full-text match against the whole `chunk_text`, and the existing title-substring
check) into one boolean without contradicting either. Rows found by both arms are one row
(the `DISTINCT ON (id)` above), keeping the real distance so they still benefit from
ranking.

**The critical wiring point:** `_aggregate_topics` currently does
`rows = [r for r in rows if r["lexical"] or r["score"] <= _NO_LEX_MAX_DIST]`
(`lambda_ask_agent.py:882`). A row that arrives *only* through the new SQL keyword arm has no
real distance (placeholder `1.0` or `NULL`, both `> 0.55`) — it MUST be admitted by the
`r["lexical"]` branch of that `or`, not silently dropped by the distance check on the other
side. This is exactly the failure mode to test for explicitly (§7): a keyword-only match must
carry `lexical=True` all the way to this filter, or the new arm retrieves the row from SQL
and `_NO_LEX_MAX_DIST` throws it away one hop later, reproducing the original bug with extra
steps.

## 6. Scope discipline

`build_search_sql()` is shared by both `/search` (via `search_topics` →
`_rag_search_list`) and Ask (via the RAG retrieval `lambda_rag_search.py:344` /
`:363`) — the file's own comment says so explicitly (`search_sql.py:8-11`, "the Ask path
stays byte-identical when it passes no dates").

**The keyword arm applies to both, because it lives in the shared SQL builder, and this is
correct, not incidental.** Ask already tokenizes the question with the identical
`_lexical_terms()`/`_NO_LEX_MAX_DIST` machinery for its own post-filter ranking
(`lambda_ask_agent.py:753-786`, used by `_aggregate_topics` which both `mode:'search'` and
Ask's synthesis path route through). The PS4 case demonstrates Ask *already* answers "did we
talk about ps4" correctly — because its retrieval budget or luck happened to pull the right
chunk into the top-*k* that day. Adding the keyword arm makes that success reliable instead
of incidental, and there is no reason Ask retrieval should be selectively worse at finding a
literal token than Search is. Ask's synthesis prompt does not need to know a row arrived via
the lexical arm — the LLM answers from chunk text regardless of retrieval path — but Search's
result list surfaces `lexical` per row today (`_aggregate_topics`'s output field), so the
distinction is visible where it already mattered.

No separate flag is needed to turn the keyword arm off for Ask: the arm can only ever *add*
rows that literally contain the query's identifier-like terms, which is a strict
precision-improving addition to what Ask already retrieves, not a behavior change to what
Ask concludes.

## 7. ACL and tombstones — untouched, and this is load-bearing

Both the `vec` and `lex` CTEs above carry the **identical** `WHERE` clause used by
`build_search_sql()` today: `c.site_id = ANY(%(site_ids)s)`, the `author_ids` grading, the
date range, and `visible_chunks_predicate("c")` (`src/deleted_predicates.py:53-63`, both the
topic tombstone arm and the source-key tombstone arm, ANDed). This predicate is deny-by-
default and exists specifically because a deleted recording's chunks are recreated under new
UUIDs by re-ingest and must stay hidden by `source_s3_key` prefix even when no
topic-tombstone names them (`deleted_predicates.py:8-19`, `chunk_archive` migration
`0044_chunk_archive.sql:5-19` documents the exact incident this guards against).

The new arm is a second CTE with the same filter list copy-pasted (or factored into a shared
predicate string, which is the safer implementation — a single Python string builder
function used by both CTEs, so the two can never drift), never a second query that runs
without it and gets OR'd in afterward. **A keyword arm that runs with looser or no ACL/
tombstone filtering and gets unioned into visible results is the worst outcome available
here** — it would resurrect exactly the deleted-recording leak `visible_chunks_predicate`
was built to close (§ per `search_sql.py:26-28`'s own comment: "A recording the customer
deleted must not come back through the search box or through Ask — both run this one
query"). Both arms must run this one query's filter, not just one of them.

## 8. What the user sees

Recommendation: **nothing new, by default.** `_aggregate_topics`'s existing `lexical`
boolean already exists per-row and already reorders lexical matches first
(`lambda_ask_agent.py:797-885`); the new arm feeds that same field, so the search list's
existing lexical-first ordering is the only visible effect — a PS4 query surfaces the PS4
topic at the top, the way any other lexical hit does today. No new badge, label, or "matched
because…" annotation is proposed: the existing product has never surfaced "why" a result
ranked where it did, and inventing that UI is a separate design decision outside this
gap's scope. If the owner wants a "matched on your exact term" indicator later, `lexical`
is already the field to key it off; this spec does not add one.

## 9. Verification

1. **Acceptance test — the PS4 case.** After the migration and backfill, `POST /search
   {"question": "PS4"}` against the UCPK2 corpus (or an equivalent fixture built from the
   same extraction JSON) must return the `Light pole PS4 and electrical hold-up` topic. This
   is a integration/contract test against `_rag_search_list` plus a live check against the
   real UCPK2 data in a non-prod environment, not a unit test with a mocked connection —
   project memory is explicit that a fake connection never executes SQL and has hidden two
   prod crashes before.
2. **A must-not-change control query.** A normal conceptual query with no identifier-shaped
   terms in it (e.g., "what safety issues came up this week", which relies purely on the
   ANN arm and returns cleanly today) must return the same top results before and after —
   the keyword arm should retrieve zero additional rows for a query with no matching literal
   tokens in the corpus, and the vector arm's ordering must be unaffected. This guards
   against the union accidentally injecting noise rows that outrank genuine semantic matches
   for queries the keyword arm has no business touching.
3. **The retrieval ruler.** Project memory (`retrieval-eval-ruler-v1`) refers to a measured
   retrieval evaluation that produced the current `_NO_LEX_MAX_DIST = 0.55` figure — that
   number is cited as ruler-measured in `docs/superpowers/specs/2026-09-17-ask-conversation-memory-design.md:62-64`
   ("That number was measured by the retrieval ruler and must not be relaxed; relaxing it to
   0.65 made every control question return 18–26 rows"). **I could not find a standalone
   ruler document or script in `origin/main`** — `git grep -n "ruler"` across the whole repo
   returns only that one citing spec, and `docs/superpowers/specs/` has no
   `retrieval-eval-ruler` file; the eval tooling that exists under `tools/asr-eval/` and
   `scripts/eval_task_admission.py` is ASR- and extraction-scoped, not retrieval-scoped. This
   spec cannot re-run a ruler it cannot locate. Before shipping the keyword arm, whoever
   implements this should either locate the ruler script (it may live outside this repo, or
   its output-only artifact may be what memory is citing) or construct an equivalent small
   labeled query set and confirm `_NO_LEX_MAX_DIST` and the new arm's row volume don't
   interact badly (e.g., the union should not push volume back toward the "18-26 rows for
   every control question" failure mode noted above, which was caused by loosening the
   distance gate, not by adding a keyword arm — but the two changes should not be shipped
   without checking they don't compound).
4. **Regression on the `_NO_LEX_MAX_DIST` filter itself (§5's critical wiring point).** A
   unit test on `_aggregate_topics` (or its SQL-arm equivalent once combined server-side)
   that constructs a row with `lexical=True` and `distance=1.0` and asserts it survives the
   `r["lexical"] or r["score"] <= _NO_LEX_MAX_DIST` filter. This is cheap, deterministic, and
   catches exactly the reintroduction risk named in §5.

## 10. Rejected alternatives

- **A length threshold ("only run keyword search for queries under N characters").** Rejected
  in the ruling (§3): rarity, not length, is the variable that loses the ANN race, and this
  codebase has already been burned once by an invented threshold (the unmeasured 500-object
  cap) that worked until an ordinary case sat past it. "PS4" is 3 characters and already
  clears any plausible length floor `_lexical_terms` would apply; the failure has nothing to
  do with query length.
- **Fixing this by raising `k`.** Rejected: raising `k` from ~30 to, say, 200 would eventually
  pull the PS4 chunk in, but it does not fix the generalization (§2) — any sufively rare
  literal in a sufficiently large or topically dense corpus can still lose an arbitrarily
  large top-*k* race, it just takes more unrelated chunks to bury it. It also multiplies the
  cost of every query (more rows scored, more rows sent through `_aggregate_topics` and, on
  Ask's path, more candidates into reranking/synthesis) to buy a probabilistic improvement
  instead of a guarantee.
- **`ILIKE` + `pg_trgm` as the sole mechanism.** Rejected in §4: substring matching without
  word-boundary awareness produces false positives on adjacent identifiers (`PS4` matching
  inside `PS40`), which is wrong for exactly the class of query this fix targets.
- **Loosening `_NO_LEX_MAX_DIST`** (e.g., to 0.65) as a cheaper alternative to a new arm.
  Already tried and already rejected by the ruler, per the citing spec: "relaxing it to 0.65
  made every control question return 18–26 rows" (`2026-09-17-ask-conversation-memory-design.md:64`).
  This doesn't fix retrieval either — it only widens what survives the post-filter among rows
  that already made the top-*k*; PS4 still needs to be IN that set first.
- **Doing this only for the search box, not Ask.** Rejected in §6: the two surfaces already
  share the SQL builder and the lexical-term machinery; making Search reliably find literal
  tokens while leaving Ask's retrieval to depend on ANN luck for the same class of query is
  an unjustified asymmetry with no scope boundary to hang it on.

## 11. Risk table

| Risk | Cost if realized | Detects it |
|---|---|---|
| Keyword arm bypasses ACL/tombstone predicate (copy-paste drift between the two CTEs) | Deleted or out-of-scope content becomes searchable again — the exact incident `visible_chunks_predicate` was built to close | A shared predicate-string builder used by both CTEs (§7) rather than duplicated SQL; a test asserting a tombstoned/out-of-site chunk containing a literal term is NOT returned by either arm |
| Lexical-only row gets a fabricated "good" distance and silently outranks true semantic top-1 | Search results feel wrong/untrustworthy for queries with an incidental identifier substring | §5's placeholder-distance handling; a test with a mixed result set asserting vector-arm rows keep their real distance and rank accordingly |
| Lexical-only row is admitted by SQL but then dropped by `_aggregate_topics`'s `_NO_LEX_MAX_DIST` filter because `lexical` wasn't threaded through | Bug reproduces one hop downstream — SQL "fixed" but user still sees nothing | §9.4's unit test on the filter boundary |
| `to_tsvector('english', ...)` stems or mangles an identifier class not yet checked (only PS4 was tested) | Some other identifier shape (e.g., a drawing number with punctuation) still doesn't match even after the fix ships | A pre-ship check against a handful of real identifiers pulled from the corpus (drawing numbers, RFI numbers, product codes), not just PS4, before considering the fix complete |
| Backfill of the generated `tsvector` column is expensive/slow on a large `report_chunks` table, or blocks writes during the `ALTER TABLE` | Migration downtime or a stalled deploy | Test the `ALTER TABLE ADD COLUMN ... GENERATED ALWAYS AS (...) STORED` migration against a copy of the largest known `report_chunks` table size before running it in prod; this repo's existing backfill runbooks (`docs/superpowers/runbooks/2026-07-31-backfill-topic-id.md`) are the pattern to follow for a staged rollout if the single-statement backfill proves too slow |
| Union pushes total candidate volume up and interacts with the `_NO_LEX_MAX_DIST=0.55` ruler-measured threshold in a way nobody re-measured | Silent regression back toward the "18-26 rows per control question" failure the ruler already caught once | §9.3 — locate or reconstruct the ruler and re-run it against the combined-arm SQL before shipping |
| `websearch_to_tsquery` on a purely non-English/CJK query yields zero lexical terms while the vector arm's own Chinese-handling comment (`lambda_ask_agent.py:759-761`) shows this codebase has been burned by asymmetric language handling before | Chinese literal-token queries (e.g., a Chinese product name) still don't benefit from the new arm even though English ones do | Confirm whether `chunk_tsv`'s `'english'` text-search config tokenizes CJK at all (it likely does not meaningfully segment it); if not, note this as a known gap rather than silently shipping asymmetric coverage, and consider whether the existing `_UNSPACED_RUN` shingling logic needs a SQL-side equivalent in a follow-up |
