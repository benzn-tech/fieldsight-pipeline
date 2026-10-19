# latest_activity(query) picks a day that names the subject

Date: 2026-10-20. Line: search & vectors (owner of `ask_tools` / `rag_tools_dispatch` /
repo SQL). Requested by the Ask orchestration line ("ASR+LLM优化v3").

## 1. Problem

`latest_activity` with a `query` answers "when did we last X". It chooses the day
with `chunks.latest_matching_date`: the latest `report_date` in a 92-day window
with a visible chunk where

    cosine distance <= ASK_TOOLS_MATCH_GATE (0.55)  OR  keyword hit on or_query(query_terms(q))

then keeps that day's search rows by the same rule.

Both halves are loose for this question shape:

- **Vector**: at 0.55 almost any site chunk is "near" a construction question, so
  the latest recording day wins.
- **Keyword**: `query_terms` keeps recency and discourse words ("last", "time",
  "talk", "mentioned"), and `or_query` ORs them, so "last" alone selects a day.

Measured (read-only, prod `fieldsight`, ruler scope site `4326dbc7…`, session
family, on_or_before 2026-10-16, probe `fspy-runs/latest_probe.py`): for 13 English
"when did we last X" questions, today's rule picked the latest recording day
(09-21 / 09-22 / 09-23) for 12. For 12 of the 13 days it picked, no chunk on
that day contains all of the subject words. Example: "when did we last talk about
drainage" → 09-22; the drainage discussion is on 08-08.

The orchestrator then cites a day unrelated to the question. Its judge rule 7
(`ask_orchestrator.py:861`) treats the result as a nearest match and falls back to
`search_records`, but only after the model has been shown the wrong day.

## 2. Goal

With `query` set, `latest_activity` returns the latest day on which a visible chunk
names every subject word of the query, and only that day's chunks that name them.
When no such day exists, it returns `[]` with `empty_reason` `no_match`
(`nothing_visible` when the caller sees no sites — unchanged). The result shape,
`meta.matched_date`, and the no-query path are unchanged.

Contract confirmed by the orchestration line (2026-10-20): it does not depend on
vector-only days; `[]` + `no_match` is already handled (the judge falls through
to `search_records`, then "nothing recorded mentions it").

## 3. Design

### 3.1 Subject terms

`lexical_terms.subject_terms(question) -> list[str]`:

    [t for t in query_terms(question) if t.isascii() and t not in RECENCY_WORDS]

- `RECENCY_WORDS`: words that describe the asking, not the subject. Exact set:
  `last latest recent recently time times when talked talk talking discussed discuss
  discussion mentioned mention said say came come ever previous previously date day
  happened someone anyone something anything`.
  (`did`, `was`, `when` are already in `QUERY_STOPWORDS`; listing a word in both is
  harmless.)
- CJK bigrams are dropped (`isascii()`): `lexical_terms` emits overlapping bigrams
  for CJK text ("上次浇混凝土…" → 上次, 次浇, 浇混, …), which an AND can never satisfy
  against English speech and which includes recency bigrams (上次, 时候). A purely
  Chinese question therefore has no subject terms → `no_match` → the judge's
  `search_records` (vector) path, which is what serves cross-language questions.
  A mixed question keeps its Latin words ("上次讨论 crane 是哪天" → `crane`).

Empty subject terms → `[]`, `no_match`, no SQL.

### 3.2 Day selection

`chunks.latest_matching_date(conn, accessible_site_ids, *, on_or_before, window_days=92,
subject_text, author_ids=None, source_family=None)` (`query_embedding` and `gate`
removed; its one caller is `_latest_activity`) keeps the search's own scope predicate
(unchanged) and requires

    <lexical vector> @@ (websearch_to_tsquery('english', %(subject_text)s)
                         || websearch_to_tsquery('simple', %(subject_text)s))

with `subject_text = " ".join(subject_terms)`. `websearch_to_tsquery` ANDs
space-separated terms. The terms are lowercase alphanumeric runs, so no escaping
is needed (same argument as `or_query`). The `english` half matches stems
(scaffolding ~ scaffold, panels ~ panel). The `simple` half matches the exact
token, which covers identifiers the `english` parser would split.
`<lexical vector>` is `_lexical_vector("c")`, unchanged, so the GIN index still
serves the match.

The vector arm and `ASK_TOOLS_MATCH_GATE` are removed from day selection (the env
var is read nowhere else and is not in the template). The embedding is still
required: it is used by the day's search (3.3).

### 3.3 Rows kept on the chosen day

The day's search is unchanged (`_run_search` with `date_from = date_to = day`,
`search_k = 64`). The rows kept are those that `chunks.keyword_matched_ids`
reports as matching the same AND tsquery, re-checked in SQL with the same
expression, then cut to `_candidate_pool`. Rows passing only the distance gate
are no longer kept: every row returned names the subject, which is what judge
rule 7 requires.

If the chosen day's search returns no matching row, the result is `[]` with
`no_match`. This happens only if the matching chunk falls outside the 64-row
search, which needs more than 64 visible chunks on that day ranked above it.
It is not filled from another day.

`keyword_matched_ids` and `build_keyword_recheck_sql` take the same
`subject_text` and the same two-config tsquery. A shared SQL fragment
(`_SUBJECT_QUERY`) is used by both builders, so "matched" means the same thing
when the day is chosen and when rows are kept.

### 3.4 Unchanged

- No-query `latest_activity` (latest visible day → day overview).
- `search_records`, `build_search_sql`, `or_query`, `query_terms`, the Ask
  (non-tool) path, the report/session family switch, ACL and scope.
- The 92-day window and `LATEST_QUERY_MIN_START_S`.

## 4. Measured effect (same probe, rule E = 3.1 + 3.2)

| question | today | new |
|---|---|---|
| when did we last talk about drainage | 09-22 | 08-08 (Drainage Outlet Discrepancy) |
| when did we last discuss the precast panels | 09-23 | 09-02 (names precast panels) |
| last time the crane was mentioned | 09-22 | 09-10 (Crane CAA …) |
| when did Hector Eggar come up | 09-22 | 09-02 |
| airport height restriction | 09-22 | 09-02 |
| scaffolding / insulation / roofers / flat slab shop drawings | 09-22 | 09-10 |
| when did we last pour concrete | 09-22 | 09-22 (a chunk names pour + concrete) |
| 上次讨论 crane 是哪天 | 09-10 | 09-10 |
| 上次浇混凝土是什么时候 | 09-02 | no_match → search_records |
| flat slab shop drawigns new date (typo) | 09-21 | no_match → search_records |
| what was the latest on RFI 108 | 09-10 | no_match |
| site safe course / geotech report | 09-23 | no_match |

The new rule picks a day that names the subject for 9 of 13 English questions.
The other 4 return `no_match`, against 0 of 13 for today's rule. A typo or a
corpus spelling variant (geotek) becomes `no_match` instead of a wrong day.
That trade is the one the orchestrator asked for.

## 5. Tests

- `subject_terms`: drops recency words and CJK bigrams; keeps identifiers (`rfi`,
  `108`); mixed question keeps Latin words; all-recency question → `[]`.
- SQL builder: day SQL contains the scope predicate and the two-config AND tsquery,
  and no `<=>`; recheck SQL uses the same `_SUBJECT_QUERY` fragment (string
  equality test, like the existing index-expression test).
- Dispatch (`_latest_activity`, fake conn): empty subject terms → `[]`, `no_match`,
  no SQL issued; no day → `no_match`; day found → `matched_date` set and only
  rechecked rows returned, a gate-near but unmatched row dropped; day found but no
  rechecked row → `[]`, `no_match`.
- Integration (local Postgres, `fswork/run-tests.sh`, the existing
  `test_search_family_pg`-style fixture): three days with chunks "concrete pour",
  "pour" only, "talked last time"; query "when did we last pour concrete" picks
  the first; stemmed match ("scaffolding" vs chunk "scaffold") is found.
- Existing `latest_activity` tests asserting distance-gate behaviour are updated
  to the new rule (listed in the plan).

## 6. Rollout

develop → TEST (`TEST_ASK_AGENTIC=screen,voice`, so TEST Ask exercises it).
No flag: the old behaviour is the defect. No template change. No prod change until
a release PR, which the owner merges; `latest_activity` is reachable on prod only
once `ASK_AGENTIC` is on there.
