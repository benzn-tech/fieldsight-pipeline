# latest_activity names the subject: Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** `latest_activity(query)` chooses the latest day where a chunk contains
every subject word, and returns only that day's chunks that do.

**Architecture:** There are three parts:

1. A pure term extractor in `lexical_terms`.
2. One SQL fragment, `subject_tsquery(n)`, shared by the day query and the row
   recheck in `search_sql` / `chunks`.
3. The dispatch change in `rag_tools_dispatch._latest_activity`. It passes the
   subject words to the day search and keeps only rechecked rows.

**Tech Stack:** Python 3.11, psycopg 3, PostgreSQL full-text search (GIN), and pytest
(unit with fake conns; integration on local Postgres via `fswork/run-tests.sh`).

**Spec:** `docs/superpowers/specs/2026-10-20-latest-activity-names-the-subject-design.md`

## Global Constraints

- Python: `C:\Users\camil\fspy\Scripts\python.exe` only. Run integration tests one
  pytest at a time through `fswork/run-tests.sh`.
- `build_search_sql` must not change. The golden hashes in
  `tests/unit/test_search_sql.py:352-363` stay as they are.
- Do not change the result shape, the meta keys, or the `empty_reason` values
  (`no_match` / `nothing_visible`).
- `MAX_SUBJECT_TERMS = 6`. `ASKING_WORDS` is exactly the set in spec §3.1.
- Do not touch the template or any workflow.

## Review Focus

1. A subject word that is an english stopword (`don`): the AND must still
   require it. Covered by the Task 2 integration test `test_don_crane_needs_both_words`.
2. A long day where 64 chunks say "last"/"time": the naming chunk must still be
   returned. Covered by the Task 3 integration test `test_naming_chunk_survives_a_crowded_day`.
3. A purely Chinese question: `no_match`, with no SQL and no search call.
   Covered by the Task 3 unit test `test_cjk_only_query_is_no_match_without_sql`.
4. A call without an embedding: still `bad_args`, checked before the
   empty-subject return. Covered by the Task 3 unit test
   `test_missing_embedding_is_bad_args_before_subject_check`.
5. A day is found but no row rechecks: `[]` with `no_match`, and
   `matched_date` still set. Covered by the Task 3 unit test
   `test_day_without_rechecked_rows_is_no_match_with_date`.

---

### Task 1: `subject_terms`

**Files:**
- Modify: `src/lexical_terms.py` (after `query_terms`)
- Test: `tests/unit/test_lexical_terms.py`. Append to it, or create it if it is
  missing (`git grep -l query_terms tests/unit` finds the existing file).

**Interfaces:**
- Produces: `ASKING_WORDS: frozenset[str]`, `MAX_SUBJECT_TERMS = 6`,
  `subject_terms(question: str) -> list[str]`

- [ ] **Step 1: Failing tests**

```python
def test_subject_terms_drop_asking_words():
    assert subject_terms("when did we last talk about drainage") == ["drainage"]
    assert subject_terms("last time someone mentioned the site safe course") == ["site", "safe", "course"]

def test_subject_terms_keep_identifiers():
    assert subject_terms("what was the latest on RFI 108") == ["rfi", "108"]

def test_subject_terms_drop_cjk_bigrams_keep_latin():
    assert subject_terms("上次讨论 crane 是哪天") == ["crane"]
    assert subject_terms("上次浇混凝土是什么时候") == []

def test_subject_terms_all_asking_is_empty():
    assert subject_terms("when did we last talk") == []

def test_subject_terms_cap_at_six():
    assert subject_terms("alpha bravo charlie delta foxtrot hotel india") == \
        ["alpha", "bravo", "charlie", "delta", "foxtrot", "hotel"]
```

- [ ] **Step 2: Run them.** `C:\Users\camil\fspy\Scripts\python.exe -m pytest tests/unit/test_lexical_terms.py -q -k subject` → FAIL (ImportError).
- [ ] **Step 3: Implement.** One comprehension over `query_terms(question)`, as
  in spec §3.1. The docstring states that this is Ask-tool policy and that a
  missing word causes `no_match`.
- [ ] **Step 4: Run them.** The same command → PASS.
- [ ] **Step 5: Commit** `feat(ask-tools): subject_terms for latest_activity`

### Task 2: subject tsquery, day query and recheck (SQL)

**Files:**
- Modify: `src/repositories/search_sql.py`
  - add `subject_tsquery`;
  - rewrite `build_latest_match_date_sql(n)`;
  - replace `build_keyword_recheck_sql` with `build_subject_recheck_sql(n)`.
- Modify: `src/repositories/chunks.py`
  - `latest_matching_date` gets its new signature;
  - `keyword_matched_ids` is replaced by `subject_matched_ids`;
  - update the re-export and `__all__`.
- Test, unit: `tests/unit/test_search_sql.py:327-340` and `:366-379` (rewrite),
  and `tests/unit/test_live_topic_predicate_everywhere.py:137,153,181,185`
  (rename the recheck builder; the day builder is now called with `n=1`).
- Test, integration: `tests/integration/test_ask_tools_latest_match_sql.py`
  (rewrite every call; replace the `gate=2.0` case with "a word present in
  every row").

**Interfaces:**
- Consumes: `subject_terms` (Task 1) in tests only.
- Produces:
  - `subject_tsquery(n: int) -> str`, with parameters `%(st0)s…%(st{n-1})s`;
  - `subject_params(terms: list[str]) -> dict` (`{"st0": t0, …}`);
  - `build_latest_match_date_sql(n: int) -> str`;
  - `build_subject_recheck_sql(n: int) -> str` (bound `%(ids)s` plus the `st*`
    parameters);
  - `chunks.latest_matching_date(conn, accessible_site_ids, *, on_or_before,
    subject_terms, window_days=92, author_ids=None, source_family=None) ->
    str | None` (returns None when `subject_terms` is empty);
  - `chunks.subject_matched_ids(conn, chunk_ids, subject_terms) -> set[str]`
    (lower-case ids; returns an empty set when there are no ids or no terms).

- [ ] **Step 1: Failing unit tests (`test_search_sql.py`)**
  - `test_subject_tsquery_folds_each_term`:
    `subject_tsquery(2) == "((to_tsquery('english', %(st0)s) || to_tsquery('simple', %(st0)s)) && (to_tsquery('english', %(st1)s) || to_tsquery('simple', %(st1)s)))"`
  - `test_latest_match_date_sql_is_subject_only`: the SQL contains
    `_lexical_vector("c") + " @@ " + subject_tsquery(3)` and
    `_scope_predicate("c")`. It does not contain `<=>`, `%(gate)s` or `q_text`.
  - `test_recheck_uses_the_same_subject_fragment`: `subject_tsquery(2)` is in
    `build_subject_recheck_sql(2)` and in `build_latest_match_date_sql(2)`.
  - `subject_tsquery(0)` raises `ValueError`.
- [ ] **Step 2: Run them** → FAIL.
- [ ] **Step 3: Implement the builders, `subject_params`, and the two `chunks`
  functions.** Keep the docstrings' disclosure note ("a date is a disclosure
  too"). Delete the old recheck builder and `keyword_matched_ids`; `git grep`
  must show no remaining users.
- [ ] **Step 4: Integration tests** in `test_ask_tools_latest_match_sql.py`:
  update the existing scope/visibility/window/family tests to the new signature
  (subject terms are the words of the seeded chunk text), then add:
  - `test_picks_the_latest_day_naming_every_word`: three days with chunk text
    "concrete pour on level 2" (10-12), "pour only" (10-13), and "we talked last
    time" (10-14). `subject_terms=["pour", "concrete"]` gives `"2026-10-12"`.
  - `test_stem_matches`: the chunk "scaffold up tomorrow" and terms
    `["scaffolding"]` give that chunk's date.
  - `test_don_crane_needs_both_words`: the chunk "the crane arrived" gives None
    for `["don", "crane"]`. Adding "Don moved the crane" on a later day gives
    that day.
  - `test_day_query_uses_the_text_index`: seed 200 chunks, run `SET LOCAL
    enable_seqscan = off`, and check that `EXPLAIN` of
    `build_latest_match_date_sql(1)` mentions `idx_report_chunks_tsv_multi_config`.
  - `test_subject_matched_ids_returns_only_naming_rows`.
- [ ] **Step 5: Run them.** Unit: `python -m pytest tests/unit/test_search_sql.py tests/unit/test_live_topic_predicate_everywhere.py -q` → PASS. Integration: `bash fswork/run-tests.sh tests/integration/test_ask_tools_latest_match_sql.py` → PASS.
- [ ] **Step 6: Commit** `feat(search-sql): subject tsquery for latest_activity day choice`

### Task 3: dispatch, tool description, comment

**Files:**
- Modify: `src/rag_tools_dispatch.py`
  - `_latest_activity`, the query branch;
  - the comment at `:44-48`;
  - remove the `ASK_TOOLS_MATCH_GATE` read and the `or_query` import if it
    becomes unused.
- Modify: `src/ask_tools.py:72-78`: the description, verbatim from spec §3.5.
- Test, unit: `tests/unit/test_rag_tools_dispatch.py`
  - rewrite `:358`, `:395-427`, `:429-446`, `:540-551`, `:571-583` and `:594`
    to the new interfaces;
  - add the new tests below.
- Test, integration: `tests/integration/test_rag_tools_dispatch_sql.py:222-259`
  (invert it: a distance-near day without the words is not chosen). Add
  `test_naming_chunk_survives_a_crowded_day`.

**Interfaces:**
- Consumes:
  - `subject_terms` (Task 1);
  - `chunks.latest_matching_date(..., subject_terms=...)` and
    `chunks.subject_matched_ids` (Task 2).

- [ ] **Step 1: Failing unit tests**
  - `test_missing_embedding_is_bad_args_before_subject_check`: a query with
    only asking words and no `query_embedding` gives `error == "bad_args"`.
  - `test_cjk_only_query_is_no_match_without_sql`: "上次浇混凝土是什么时候" with an
    embedding gives `[]` and `empty_reason == "no_match"`; the
    `latest_matching_date` and `search_fn` spies are never called.
  - `test_day_search_gets_only_subject_words`: "when did we last pour concrete"
    → the `search_fn` event has `question == "pour concrete"`, `date_from ==
    date_to == "2026-10-11"` and `k == 64`; `meta["matched_date"] == "2026-10-11"`.
  - `test_only_rechecked_rows_are_kept`: the search returns rows a (distance
    0.2) and b (distance 0.6), and the recheck returns `{b}`. The result is
    `[b]`.
  - `test_day_without_rechecked_rows_is_no_match_with_date`: the recheck
    returns `set()`. The result is `[]`, `empty_reason == "no_match"`, and
    `matched_date` is set.
  - `test_latest_activity_description_asks_for_a_short_subject`: the offered
    tool's description contains "every word" and "one to three words".
- [ ] **Step 2: Run them** → FAIL.
- [ ] **Step 3: Implement.** Order inside the query branch:
  1. `_need_embedding`;
  2. `terms = subject_terms(a["query"])`; if empty, set `no_match` and return
     `[]`;
  3. site/person/folder resolution as today;
  4. `latest_matching_date(..., subject_terms=terms)`;
  5. `_run_search` with `dict(a, query=" ".join(terms), date_from=day,
     date_to=day)` and `search_k=LATEST_DAY_SEARCH_K`;
  6. `subject_matched_ids`;
  7. cut to `_candidate_pool`.

  Rewrite the `:44-48` comment so it says the guard covers the day search,
  because the day query is index-served.
- [ ] **Step 4: Integration test** `test_naming_chunk_survives_a_crowded_day`:
  - seed one day with 70 chunks "we talked about it last time, time and again"
    and one chunk "precast panels arrive Friday";
  - run the dispatch through the real `lambda_rag_search` handler, as the
    neighbouring tests do, with "when did we last discuss the precast panels";
  - expect exactly the precast chunk.
- [ ] **Step 5: Run.**
  - `python -m pytest tests/unit/test_rag_tools_dispatch.py tests/unit/test_ask_tools.py tests/unit/test_ask_orchestrator.py -q` → PASS.
  - `bash fswork/run-tests.sh tests/integration/test_rag_tools_dispatch_sql.py` → PASS.
- [ ] **Step 6: Commit** `fix(ask-tools): latest_activity(query) returns a day that names the subject`

### Task 4: PR and TEST check

- [ ] Run the full unit suite in shards (`tests/unit -q -p no:cacheprovider`, split
  by `-k` halves if memory-killed). Expected: no failures.
- [ ] Push and run `gh pr create --base develop`. Before merging, confirm
  `baseRefName == develop`. Merge when CI is green. Send the PR number to the
  orchestration line.
- [ ] After the TEST deploy, run the spec §6 check: invoke
  `fieldsight-test-rag-search` `mode: tools` with three subjects. Every row
  names the subject, and `matched_date` equals `max(report_date)` from a
  direct SQL query on `fieldsight_test`.
