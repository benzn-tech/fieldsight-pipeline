# latest_activity(query) picks a day that names the subject

Date: 2026-10-20. Line: search & vectors (owner of `ask_tools` / `rag_tools_dispatch` /
repo SQL). Requested by the Ask orchestration line ("ASR+LLM优化v3"). Reviewed by
Fable 2026-10-20; findings applied (§7).

## 1. Problem

`latest_activity` with a `query` answers "when did we last X". It chooses the day
with `chunks.latest_matching_date`: the latest `report_date` in a 92-day window
with a visible chunk where

    cosine distance <= ASK_TOOLS_MATCH_GATE (0.55)  OR  keyword hit on or_query(query_terms(q))

then searches that day with the whole question and keeps rows by the same rule.

Both halves are loose for this question shape:

- **Vector**: at 0.55 almost any site chunk is "near" a construction question, so
  the latest recording day wins.
- **Keyword**: `query_terms` keeps words that describe the asking ("last",
  "time", "talk", "mentioned"), and `or_query` ORs them, so "last" alone selects
  a day.

Measured (read-only probe, prod `fieldsight`, ruler scope site `4326dbc7…`, session
family, on_or_before 2026-10-16; probe SQL in Appendix A). 14 English "when did we
last X" questions: today's rule picked 09-21, 09-22 or 09-23 (the latest recording
days) for 13 of them. For 13 of the 14 days it picked, no chunk on that day contains
every subject word. Example: "when did we last talk about drainage" → 09-22, but
the drainage discussion is on 08-08.

The orchestrator then shows the model a day unrelated to the question. Judge rule 7
(`ask_orchestrator.py:861`) treats the result as a nearest match and falls back to
`search_records`, but only after that.

## 2. Goal

With `query` set, `latest_activity` returns the latest day on which a visible
chunk contains every subject word of the query (stemmed or exact), and only
that day's chunks that contain them all.

When there are no subject words, or no such day, it returns `[]` with
`empty_reason` `no_match` (`nothing_visible` when the caller sees no sites — as
today). If a day is found but none of its rows survive (§3.3), it also returns
`[]` with `no_match`, and `meta.matched_date` is still set.

Unchanged: the result shape, the `meta` keys, and the no-query path.

Contract confirmed by the orchestration line (2026-10-20): it does not depend on
vector-only days. `[]` with `no_match` is already handled: the judge falls
through to `search_records`, then to "nothing recorded mentions it".
`matched_date` is not read by the orchestrator (`ask_orchestrator.py:130-160`).

## 3. Design

### 3.1 Subject terms

`lexical_terms.subject_terms(question) -> list[str]`:

    [t for t in query_terms(question) if t.isascii() and t not in ASKING_WORDS][:MAX_SUBJECT_TERMS]

`MAX_SUBJECT_TERMS` = 6.

- **`ASKING_WORDS`** are words that describe the asking, not the subject. This is
  Ask-tool policy, not a list pinned by Postgres the way `QUERY_STOPWORDS` is.
  The exact set:

  `last latest recent recently lately time times when ever ago earlier previous
  previously before date day week month talked talk talking spoke spoken speak
  discussed discuss discussion mentioned mention said say told asked heard raised
  brought came come happened someone anyone something anything`

  A word missing from this list causes a false `no_match`, which is the costlier
  failure. A real subject word that is on the list ("completion date", "lead
  time", "day works") only loosens the AND by one word; it never causes a false
  `no_match`.
- **CJK bigrams are dropped** (`isascii()`). `lexical_terms` emits overlapping
  bigrams for CJK text ("上次浇混凝土…" → 上次, 次浇, 浇混, …). An AND over these
  can never match English speech, and they include asking bigrams (上次, 时候).
  - A purely Chinese question therefore has no subject terms → `no_match` → the
    judge's `search_records` (vector) path, which is what serves cross-language
    questions.
  - A mixed question keeps its Latin words ("上次讨论 crane 是哪天" → `crane`).
- **More than 6 subject words** keeps the first 6. A model-written query of
  four or more words is likely to over-constrain the AND, so the tool
  description (§3.5) asks for one to three words.

With no subject terms the result is `[]` with `no_match`, and no SQL runs. The
`_need_embedding` check still runs first, so a call without an embedding stays
`bad_args`.

### 3.2 The subject match

`search_sql.subject_tsquery(n) -> str` builds an AND across terms, with each term
matching either its english stem or its exact token:

    (to_tsquery('english', %(st0)s) || to_tsquery('simple', %(st0)s))
    && (to_tsquery('english', %(st1)s) || to_tsquery('simple', %(st1)s)) ...

Parameters `st0`…`st{n-1}` take one term each. Terms are `[a-z0-9]+`
(`lexical_terms` output), so `to_tsquery` syntax is safe.

This is a fold per term, not `websearch_to_tsquery('english', all) ||
websearch_to_tsquery('simple', all)`. The latter ORs two whole AND-queries, and
the english config drops english stopwords. Fable's probe:
`'crane' | 'don' & 'crane'`, which matches "the crane arrived" for the subject
"don crane". With the per-term fold, `to_tsquery('english', 'don')` is empty, so
that term reduces to the simple `'don'` and the AND holds.

- The english half matches stems: scaffolding ~ scaffold, panels ~ panel.
- The simple half matches exact tokens, including what the english parser
  tokenises differently. For example "RFI-108": english gives `'rfi' '-108'`,
  simple gives `'108'`.
- Known `no_match`: spacing variants (`rfi108` vs "RFI 108") match neither half.

The left side is `_lexical_vector("c")`, unchanged, so the existing GIN
expression index serves any tsquery shape.

`chunks.latest_matching_date(conn, accessible_site_ids, *, on_or_before,
subject_terms, window_days=92, author_ids=None, source_family=None)`:

- `query_embedding` and `gate` are removed. Its only caller is
  `_latest_activity`.
- WHERE is the search's own `_scope_predicate` (unchanged) AND
  `_lexical_vector("c") @@ subject_tsquery(len(subject_terms))`.

The vector arm and `ASK_TOOLS_MATCH_GATE` are removed from day selection. The
env var is read nowhere else and is not in the template.

### 3.3 Rows kept on the chosen day

The day's search is `_run_search` as today (`date_from = date_to = day`,
`search_k = 64`, same embedding), with one change: the question passed to it is
`" ".join(subject_terms)` instead of the whole question.

The reason is the keyword arm of `build_search_sql`. It takes the top 64 rows by
`ts_rank` of `or_query(query_terms(question))`. Fed the whole question, that OR
includes "last", "time" and "talk", and on a long day 64 chunks containing those
words can outrank the one chunk that names every subject word. Fed only the
subject words, `ts_rank` puts the chunks that match more of the subject words
first.

The vector arm still ranks by the question's embedding, which is unchanged.

Rows kept: those that `chunks.subject_matched_ids(conn, ids, subject_terms)`
reports. That function replaces `keyword_matched_ids` and uses
`build_subject_recheck_sql(n)`, which has the same `subject_tsquery(n)`
fragment as the day query. So "matched" means the same thing when the day is
chosen and when rows are kept. Rows that pass only on distance are no longer
kept. The kept rows are then cut to `_candidate_pool`.

`keyword_matched_ids` and `build_keyword_recheck_sql` are deleted. They have no
other callers.

### 3.4 Known limitations, stated rather than fixed

- **Alias variants.** `_normalize_rows` rewrites `chunk_text` from
  `name_aliases` after SQL, so a stored ASR variant ("geotek", "Egger") does not
  match the subject "geotech" or "eggar". The result is `no_match`, then the
  judge falls back to `search_records`, which is vector-led. Expanding each
  subject term with its active `wrong_term`s (OR within a term, AND across
  terms) fits the per-term fold. It is deferred until a measured miss shows it
  is needed.
- **Typos** in the question fail the AND and return `no_match`, with the same
  fallback.

### 3.5 Text and comments that change

- `ask_tools.py` `latest_activity` description (around line 72) becomes: "The
  most recent day on which every word of `query` was recorded (stemmed or
  exact). Give the subject in one to three words, e.g. "concrete pour",
  "drainage"."
- `rag_tools_dispatch.py:44-48` (`LATEST_QUERY_MIN_START_S`): the exact-scan
  rationale is stale. The day query is now served by the GIN index. Keep the 5 s
  guard, because the day search still costs a full search, and rewrite the
  comment to say so.

### 3.6 Unchanged

- No-query `latest_activity` (latest visible day → day overview).
- `search_records`, `build_search_sql`, `or_query`, `query_terms`, the Ask
  (non-tool) path, the report/session family switch, ACL, scope and visibility.
- The 92-day window.

## 4. Measured effect

Same probe. The new rule here is 3.1 (an earlier draft of the asking-word list)
plus `websearch_to_tsquery` english||simple. That differs from 3.2 only for
english stopwords, and none occur in these questions. In the "names it" column,
"yes" means a chunk on that day contains every subject word. By construction,
the new rule's day is the latest such day in the window.

| question | today: day / names it | new: day |
|---|---|---|
| when did we last talk about drainage | 09-22 / no | 08-08 "Drainage Outlet Discrepancy" |
| when did we last discuss the precast panels | 09-23 / no | 09-02 "…Discussed precast panels, AquaHeat…" |
| last time the crane was mentioned | 09-22 / no | 09-10 "Crane CAA and Vehicle Access Safety" |
| when did Hector Eggar come up | 09-22 / no | 09-02 "…Hector Eggar is developing the scaffold methodology" |
| airport height restriction | 09-22 / no | 09-02 (same topic as above) |
| scaffolding / insulation / roofers / flat slab shop drawings | 09-22 / no | 09-10 (four questions; the topic chunks or windows on 09-10 name each) |
| when did we last pour concrete | 09-22 / yes | 09-22 (same day, now with only the naming rows) |
| what was the latest on RFI 108 | 09-10 / no | no_match |
| flat slab shop drawigns new date (typo) | 09-21 / no | no_match |
| last time someone mentioned the site safe course | 09-23 / no | no_match |
| when did we last talk about the geotech report | 09-23 / no | no_match (corpus spells "geotek", §3.4) |
| 上次讨论 crane 是哪天 | 09-10 / yes | 09-10 |
| 上次浇混凝土是什么时候 | 09-02 / no | no_match → search_records |

Of 14 English questions, today's rule returns a day that names the subject for 1.
The new rule does so for 10 and returns `no_match` for 4. A `no_match` is
handled by the orchestrator. A wrong day is shown to the model.

The day-row step (3.3) was not part of the probe. Its acceptance is the TEST
check in §6.

## 5. Tests

**Unit**
- `subject_terms`:
  - drops asking words and CJK bigrams;
  - keeps identifiers (`rfi`, `108`);
  - a mixed question keeps its Latin words;
  - an all-asking question gives `[]`;
  - caps at 6 terms.
- `subject_tsquery(n)`:
  - shape for n = 1 and n = 3;
  - parameter names `st0..`;
  - `build_latest_match_date_sql` contains `_scope_predicate`, `_lexical_vector`
    and `subject_tsquery`, and no `<=>`;
  - the recheck SQL contains the identical `subject_tsquery(n)` (string
    equality, like the existing index-expression test).
- Dispatch (`_latest_activity`, fake conn):
  - empty subject terms → `[]`, `no_match`, and neither `latest_matching_date`
    nor `search_fn` is called;
  - missing embedding is still `bad_args` (checked first);
  - no day → `no_match`;
  - day found → `matched_date` is set, `search_fn` receives
    `question == "concrete pour"`, and only rechecked rows are returned (a near
    row that is not rechecked is dropped);
  - day found but nothing rechecked → `[]`, `no_match`, and `matched_date` is
    still set.
- `test_live_topic_predicate_everywhere`: the recheck builder is renamed. The
  day builder keeps `_scope_predicate`.

**Integration** (local Postgres, `fswork/run-tests.sh`)
- Day choice over three days with chunks "concrete pour", "pour" only, and
  "talked last time": "when did we last pour concrete" picks the first.
- Stemming: "scaffolding" in the question finds a chunk that says "scaffold".
- The `don crane` case: a chunk "the crane arrived" does not match; a chunk
  "Don moved the crane" does.
- `EXPLAIN` of the day query on a seeded table with `enable_seqscan = off` uses
  `idx_report_chunks_tsv_multi_config`.
- A day with more than 64 chunks containing "last"/"time" plus one chunk naming
  the subject. Through `lambda_rag_search` with the subject-only question, that
  chunk is in the 64 and is kept.

**Updated existing tests** (enumerated in the plan, from the review):
- `test_rag_tools_dispatch.py`: the gate and query_text tests, and the
  "kept by distance" tests;
- `test_rag_tools_dispatch_sql.py:243-259`;
- every call in `test_ask_tools_latest_match_sql.py`;
- `test_search_sql.py:327-340` and `:366-379`.

The golden hashes at `test_search_sql.py:352-363` must not change, because
`build_search_sql` is untouched.

## 6. Rollout

- develop → TEST. `TEST_ASK_AGENTIC=screen,voice`, so TEST Ask exercises it.
- No flag: the old behaviour is the defect. No template change.
- No prod change until a release PR, which the owner merges. `latest_activity`
  is reachable on prod only once `ASK_AGENTIC` is on there.
- TEST check after deploy: invoke rag-search `mode: tools` with `latest_activity`
  for three subjects on a TEST site. Each returned row names the subject.
  `matched_date` is the latest such day; a direct SQL count on
  `fieldsight_test` confirms it.

## 8. Amendment A (2026-10-20): coverage first, not ALL

**Why.** The main caller is not the model. `ask_orchestrator.round1_calls`
(`ask_orchestrator.py:576`) runs deterministically on every question matching
`_LATEST` (last|latest|most recent|when did). It sends the raw question as `query`,
so the tool description in §3.5 never reaches it.

A natural question yields 4–6 subject terms. For example, "When did we last pour
concrete for the B2 slab on level 2?" gives `pour concrete b2 slab level`. Under
an AND over all terms, one missing word produces `no_match`. The judge then
spends a model round on `search_records` (+2–3 s), and the "latest" ordering is
lost. The orchestration line asked that the tool handle full questions.

**Rule.** This replaces "every subject word" in §2, §3.2 and §3.3:

- The coverage of a chunk is the number of subject terms it matches. Each term
  is matched by the per-term fold of §3.2.
- `k*` is the largest coverage of any visible chunk in the window. The floor is
  `min(2, n)`: with `n ≤ 2` all terms are still required, and a single generic
  word never selects a day once there are two or more terms.
- The day is the latest `report_date` that has a chunk with coverage `k*`. This
  is the orchestration line's "AND over all, then drop terms until a day
  matches", done as one query.
- The rows kept on that day are the rows with coverage `≥ k*`.
- `meta.subject_match = {"matched": k*, "of": n}`. This is a new key; the
  existing keys and the result shape are unchanged.
- The result is `no_match` only when no chunk reaches the floor.

**Trade-off, accepted by the orchestration line.** `k*` is the window maximum,
so an older day naming 5 terms beats a newer day naming 4. Judge rule 7 still
checks the text, so the risk is a less recent day, not a wrong one.

**SQL.** The day query becomes:

    WITH m AS (
      SELECT c.report_date, <cover> AS k FROM report_chunks c
      WHERE <scope> AND <lexical vector> @@ (<t0> || <t1> || …)
    )
    SELECT report_date, k FROM m
    WHERE k >= %(floor)s
    ORDER BY k DESC, report_date DESC
    LIMIT 1

- `<ti>` is term i's fold from §3.2.
- `<cover>` is `(<lexical vector> @@ <t0>)::int + (<lexical vector> @@ <t1>)::int + …`.
- The OR prefilter is GIN-served. The coverage sum runs only on the rows that
  pass it.
- The recheck returns `(id, k)` for the given ids by the same `<cover>`
  expression.

`chunks.latest_matching_date` returns `(day, k)` or `None`.
`chunks.subject_matched_ids` becomes `subject_coverage(conn, ids, terms) ->
dict[id, k]`.

**Asking words added:** `hear conversation conversations update updates news
status`. Measured: "When did we last hear from Hector Eggar…" had kept `hear`,
and "most recent conversation about the crane…" had kept `conversation`.

**Measured** (read-only, prod, the same scope, 10 round-1-shaped questions,
probe `fspy-runs/latest_probe2.py`):

- All 10 chose a day, and none returned `no_match`.
- Drainage: 08-08 "Drainage Outlet Discrepancy" (2/3).
- Hector Eggar scaffold methodology: 09-02 "Scaffold Methodology & Crane
  Restrictions" (4/5).
- Flat slab shop drawings with Jesse: 09-10 (4/5).
- Insulation R-value: 09-02 (2/2).
- The weak cases are 2/3 or 2/4 matches on generic words: "Dave … arriving on
  site", and "geotech report for the retaining wall", where the corpus spells it
  "geotek". The judge's rule 7 text check handles these.

**Tests in round-1 shape** (raw question plus site/person, as `round1_calls`
sends it):

- Full question where no chunk has all terms but one has 4 of 5: that day is
  returned and `subject_match == {"matched": 4, "of": 5}`.
- An older 5/5 day beats a newer 4/5 day.
- A single-term match with `n ≥ 2` returns `no_match`.
- With `n == 2`, both terms are required.
- On the chosen day, rows below `k*` are dropped.

## 7. Review log (Fable, 2026-10-20)

Applied:
1. Per-term fold instead of two whole-query halves (the `don` case). The
   "identifiers" sentence was corrected to the hyphen case. Spacing variants
   are noted as a known `no_match`.
2. The day search now gets the subject words. The alternative the review
   proposed, a direct day query, would need a second row shape next to
   rag-search's; the subject-only keyword arm removes the crowding the review
   identified, and the crowding case gets an integration test.
3. Alias variants stated as a limitation (§3.4).
4. Asking-word list extended, and the asymmetry stated.
5. §4 recounted (14 English questions). Every row has a "names it" column and
   a snippet. The probe SQL is in Appendix A.
6. `matched_date` behaviour on the no-rows path stated; tool description text
   fixed (§3.5).
7. Stale exact-scan comment (§3.5).
8. Test plan enumerated (§5).

## Appendix A: probe SQL (read-only)

Scope: `c.site_id = '4326dbc7-…' AND c.source_s3_key LIKE 'extractions/%' AND
c.report_date <= '2026-10-16'`.

- Today's rule: `max(report_date)` where `(embedding <=> q) <= 0.55` OR
  `_lexical_vector @@ websearch_to_tsquery('simple', or_query(query_terms(q)))`.
- New rule: `max(report_date)` where `_lexical_vector @@
  (websearch_to_tsquery('english', t) || websearch_to_tsquery('simple', t))`, with
  `t = " ".join(subject_terms)`.
- Snippet: the first chunk on the chosen day that matches the new rule's tsquery.
