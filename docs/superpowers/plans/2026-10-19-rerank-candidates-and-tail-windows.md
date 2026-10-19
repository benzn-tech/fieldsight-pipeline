# Rerank Candidate Selection and Tail Windows Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace Ask's per-source rerank cap with an arm-aware candidate selector (keyword-safe on reranker failure), and back-fill short tail windows in the session index.

**Architecture:** `lambda_ask_agent._select_candidates` replaces `_diversify`; `_rerank_chunks` uses it and falls back through `_admit_one_lexical_hit`. `session_index._pack_windows` gets a post-pass that extends a short last window backwards. The retrieval ruler (separate local branch) switches to the new selector so acceptance can be measured.

**Tech Stack:** Python 3.11, pytest. Python is `C:\Users\camil\fspy\Scripts\python.exe` (CI-identical deps).

**Spec:** `docs/superpowers/specs/2026-10-16-rerank-diversify-and-tail-windows-design.md` (commit c0fbf722).

## Global Constraints

- Worktree `C:\Users\camil\wt\fs-divfix`, branch `fix/rerank-diversify-and-tail-windows`, base origin/develop `bd023e58`. Ruler: worktree `C:\Users\camil\wt\fs-eval`, branch `feat/retrieval-eval-baseline` (local, never pushed).
- Total candidates = `RERANK_CANDIDATES` (env `RERANK_CANDIDATE_K`, default 32, a stack parameter — do not change it). Keyword quota `RERANK_KEYWORD_CANDIDATES`, env-overridable, code default **8**, not a stack parameter.
- `RERANK_PER_SESSION_CAP` and `_diversify` are deleted; nothing else may reference them.
- `TARGET_CHARS` (2600), `OVERLAP_TURNS` (2) unchanged; tail threshold is `TARGET_CHARS // 2`.
- No change to search SQL, rerank model, `dashscope_utils.rerank`, the report-family chunker, or any template/workflow parameter.
- Commits: English, end with the two attribution lines; never `git add -A`, never `git stash`.
- PR base must be `develop` (check before merging). Prod writes (release, backfill) are owner-only.

## Review Focus

1. Reranker timeout with keyword rows only after 32 vector rows → the 5 returned rows still include one keyword row (Task 2 test `test_failed_rerank_keeps_a_keyword_row`).
2. Fewer than `keep` candidates after dedup (many identical texts) → no rerank call, rows returned as selected (Task 2 test `test_dedup_below_keep_skips_the_reranker`).
3. A tail whose bucket's earlier turns are each larger than the room left → tail unchanged, not an empty or oversize window (Task 3 test `test_backfill_stops_before_an_oversize_turn`).
4. Hour-bucketed (no topic) windows back-filled → `block.from` of the tail moves earlier but stays later than its predecessor's (Task 3 test `test_backfilled_hour_tail_keeps_a_distinct_block`).
5. `ask_tools.search_records` keeps working through `_rerank_chunks` with the new selector (Task 2 runs `tests/unit/test_ask_tools*.py`).

---

### Task 1: `_select_candidates`

**Files:**
- Modify: `src/lambda_ask_agent.py` (replace the `RERANK_PER_SESSION_CAP` block and `_diversify`, ~lines 270–300)
- Test: `tests/unit/test_ask_rerank.py`

**Interfaces:**
- Produces: `RERANK_KEYWORD_CANDIDATES: int` (module constant, `int(os.environ.get("RERANK_KEYWORD_CANDIDATES", "8"))`); `_select_candidates(chunks: list[dict], total: int | None = None) -> list[dict]` — `total` defaults to `RERANK_CANDIDATES`. Returns new list; rows are the input dicts except a deduplicated keyword copy whose `lexical_hit` was OR-ed into a kept row (mutating the kept dict's `lexical_hit` to True is acceptable; document it).

- [ ] **Step 1: Write the failing tests** (helper `_row(text, lex=False, key="one-session")` returning `{"chunk_text", "source_s3_key", "topic_id": None, "lexical_hit"}`)

```python
def test_select_spills_unused_keyword_slots_to_vector_rows():
    vec = [_row("v%d" % i) for i in range(40)]
    out = agent._select_candidates(vec, total=32)
    assert [c["chunk_text"] for c in out] == ["v%d" % i for i in range(32)]

def test_select_caps_keyword_rows_and_puts_vector_rows_first(monkeypatch):
    monkeypatch.setattr(agent, "RERANK_KEYWORD_CANDIDATES", 8)
    rows = [_row("v%d" % i) for i in range(32)] + [_row("k%d" % i, lex=True) for i in range(20)]
    out = [c["chunk_text"] for c in agent._select_candidates(rows, total=32)]
    assert out == ["v%d" % i for i in range(24)] + ["k%d" % i for i in range(8)]

def test_select_reaches_a_keyword_row_past_position_32(monkeypatch):
    monkeypatch.setattr(agent, "RERANK_KEYWORD_CANDIDATES", 8)
    rows = [_row("v%d" % i) for i in range(33)] + [_row("kw", lex=True)]
    out = [c["chunk_text"] for c in agent._select_candidates(rows, total=32)]
    assert out[-1] == "kw" and len(out) == 32

def test_select_keeps_many_rows_from_one_source():
    rows = [_row("s%d" % i, key="same") for i in range(10)]
    assert len(agent._select_candidates(rows, total=32)) == 10

def test_select_collapses_duplicate_text_and_keeps_the_keyword_flag():
    rows = [_row("same"), _row("other"), _row("same", lex=True)]
    out = agent._select_candidates(rows, total=32)
    assert [c["chunk_text"] for c in out] == ["same", "other"]
    assert out[0]["lexical_hit"] is True

def test_select_does_not_collapse_empty_texts():
    rows = [_row(""), _row(""), _row("")]
    assert len(agent._select_candidates(rows, total=32)) == 3
```

- [ ] **Step 2: Run** `C:/Users/camil/fspy/Scripts/python.exe -m pytest tests/unit/test_ask_rerank.py -k select -q` — Expected: FAIL (`_select_candidates` missing).

- [ ] **Step 3: Implement** `_select_candidates` per spec §3a steps 1–4: dedup first (single pass, first occurrence wins, OR `lexical_hit`), then take up to `RERANK_KEYWORD_CANDIDATES` keyword rows, fill vector rows to `total - len(keyword_taken)`, return vector rows then keyword rows. Replace the cap's comment block with a short comment pointing at the spec (why per arm, why spill). Delete `RERANK_PER_SESSION_CAP` and `_diversify`; delete the four cap tests (`test_one_recording_cannot_fill_the_candidate_list`, `test_diversify_keeps_the_incoming_order`, `test_chunks_with_no_source_key_are_not_collapsed_together`, `test_the_cap_is_applied_before_the_reranker_sees_anything`) and their section header. `_rerank_chunks` temporarily calls `_select_candidates(chunks)` so the module imports.

- [ ] **Step 4: Run** `... -m pytest tests/unit/test_ask_rerank.py -q` — Expected: all pass. `grep -rn "_diversify\|RERANK_PER_SESSION_CAP" src tests` — Expected: no output.

- [ ] **Step 5: Commit** `git add src/lambda_ask_agent.py tests/unit/test_ask_rerank.py` — "Ask rerank: select candidates per search arm instead of capping per source".

### Task 2: `_rerank_chunks` wiring and keyword-safe fallback

**Files:**
- Modify: `src/lambda_ask_agent.py` `_rerank_chunks`
- Test: `tests/unit/test_ask_rerank.py`

**Interfaces:**
- Consumes: `_select_candidates(chunks)` (Task 1), existing `_admit_one_lexical_hit(chunks, keep)`.
- Produces: `_rerank_chunks(question, chunks, keep)` — signature unchanged.

- [ ] **Step 1: Write the failing tests**

```python
def test_failed_rerank_keeps_a_keyword_row(monkeypatch):
    monkeypatch.setattr(agent, "RERANK_ENABLED", True)
    monkeypatch.setattr(dashscope_utils, "rerank", lambda *a, **k: None)
    rows = [_row("v%d" % i) for i in range(32)] + [_row("kw", lex=True)]
    out = agent._rerank_chunks("q", rows, 5)
    assert len(out) == 5 and out[-1]["chunk_text"] == "kw"
    assert [c["chunk_text"] for c in out[:4]] == ["v0", "v1", "v2", "v3"]

def test_reranker_sees_at_most_the_configured_total(monkeypatch):
    seen = {}
    monkeypatch.setattr(agent, "RERANK_ENABLED", True)
    monkeypatch.setattr(agent, "RERANK_CANDIDATES", 32)
    monkeypatch.setattr(dashscope_utils, "rerank",
                        lambda q, docs, n: seen.update(n=len(docs)) or list(range(n)))
    rows = [_row("v%d" % i) for i in range(32)] + [_row("k%d" % i, lex=True) for i in range(32)]
    agent._rerank_chunks("q", rows, 5)
    assert seen["n"] == 32

def test_dedup_below_keep_skips_the_reranker(monkeypatch):
    monkeypatch.setattr(agent, "RERANK_ENABLED", True)
    monkeypatch.setattr(dashscope_utils, "rerank",
                        lambda *a, **k: (_ for _ in ()).throw(AssertionError("reranked")))
    rows = [_row("same") for _ in range(9)] + [_row("b")]
    assert [c["chunk_text"] for c in agent._rerank_chunks("q", rows, 5)] == ["same", "b"]
```

Update the existing failure-path tests in the file whose expectation was `candidates[:keep]` only if they now differ (they use no keyword rows, so `_admit_one_lexical_hit` returns the same prefix — expect no change).

- [ ] **Step 2: Run** `... -m pytest tests/unit/test_ask_rerank.py -q` — Expected: the three new tests FAIL (fallback drops the keyword row; dedup case calls the reranker).

- [ ] **Step 3: Implement** in `_rerank_chunks`: after the existing `len(chunks) <= keep` check, `candidates = _select_candidates(chunks)`; if `len(candidates) <= keep` return `candidates`; on `not order` return `_admit_one_lexical_hit(candidates, keep)`. Update the docstring's failure sentence (failure returns the pre-rerank Ask selection, keyword row included).

- [ ] **Step 4: Run** `... -m pytest tests/unit/test_ask_rerank.py tests/unit/ -k "ask_tools or rerank or ask_agent" -q` — Expected: PASS.

- [ ] **Step 5: Commit** — "Ask rerank: a failed rerank keeps the keyword row, as the rerank-off path does".

### Task 3: back-fill short tail windows

**Files:**
- Modify: `src/session_index.py` `_pack_windows` (docstring + post-pass)
- Test: `tests/unit/test_session_index.py`

**Interfaces:**
- Produces: `_pack_windows(turns) -> list[list[dict]]` — same signature; may return a last window that overlaps its predecessor by more than `OVERLAP_TURNS`.

- [ ] **Step 1: Write the failing tests** (use `si._pack_windows` directly with `turn(...)`; size via `len(si._window_line(t)) + 1`)

- `test_short_tail_is_backfilled`: 4 turns of ~1000 chars then 1 turn of ~200 → windows count unchanged vs. today (compute expected = 3 from the packing rule in a comment); last window's text length ≥ `TARGET_CHARS // 2`, ≤ `TARGET_CHARS`; its turns are a contiguous slice of the input ending at the last turn.
- `test_tail_already_long_enough_is_unchanged`: the existing 10×700 shape — the windows equal what packing alone gives (assert overlap `a[-2:] == b[:2]` on every consecutive pair, i.e. today's behaviour).
- `test_single_window_bucket_is_unchanged`: 3 short turns → one window equal to the input.
- `test_backfill_stops_before_an_oversize_turn`: predecessor turns of 3000 chars each, tail 300 chars → tail unchanged (no borrowed turn), all windows non-empty.
- `test_backfill_never_absorbs_the_whole_predecessor`: for each shape above, no two windows are equal and the last window's first turn is after the previous window's first turn.
- `test_backfilled_hour_tail_keeps_a_distinct_block` (via `si.build_chunks` with turns outside any topic so they bucket by hour): the last two windows' `metadata["block"]["from"]` differ and the tail's `from` ≤ its last turn's time.

- [ ] **Step 2: Run** `... -m pytest tests/unit/test_session_index.py -q` — Expected: the back-fill tests FAIL; the unchanged-shape tests pass.

- [ ] **Step 3: Implement** the post-pass per spec §3b: if `len(out) >= 2` and the last window's text size `< TARGET_CHARS // 2`, find `i` = index of the last window's first turn in `turns` (identity, `is`, not equality), then prepend `turns[i-1], turns[i-2], …` while the size stays `<= TARGET_CHARS`, stopping at the first turn that would exceed it. Update the docstring.

- [ ] **Step 4: Run** `... -m pytest tests/unit/test_session_index.py tests/unit/ -k "session_index or reindex or chunks" -q` — Expected: PASS.

- [ ] **Step 5: Commit** — "Session index: back-fill a short last window from the turns before it".

### Task 4: ruler uses the production selector

**Files (ruler worktree `C:\Users\camil\wt\fs-eval`):**
- Modify: `tools/retrieval-eval/run.py` (`rerank_all` ~line 356, docstring ~line 27, fingerprint ~line 582)

**Interfaces:**
- Consumes: `lambda_ask_agent._select_candidates(rows, total)` and `RERANK_KEYWORD_CANDIDATES` when the imported source has them; otherwise the old `_diversify(rows, RERANK_PER_SESSION_CAP)` (so `EVAL_SRC_REF=origin/main` baselines still run).

- [ ] **Step 1:** In `rerank_all`, take the rerank system's candidate count as a new parameter `total` and select with `_select_candidates(rows, total)` if `hasattr(lambda_ask_agent, "_select_candidates")`, else the old call. Fingerprint: replace `rerank_per_session_cap` with `"candidate_selector": "per-arm" | "per-source-cap"`, `"rerank_keyword_candidates"` (or None), and keep `rerank_per_session_cap` only on the old path (`getattr(..., None)`).
- [ ] **Step 2: Verify** both paths import and fingerprint without running the eval: `EVAL_SRC_REF=fix/rerank-diversify-and-tail-windows` and `EVAL_SRC_REF=origin/main`, each with a `--dry-run`-style smoke if run.py has one; if not, a 1-question gold file in the scratchpad with `--repeat 1` (costs ~2 embed + 2 rerank calls; read-only). Expected: the result JSON's fingerprint shows `per-arm` / `per-source-cap` respectively.
- [ ] **Step 3: Commit** on `feat/retrieval-eval-baseline` — "Ruler: measure Ask with the production candidate selector".

### Task 5: acceptance run, PR, TEST re-index

- [ ] **Step 1: In-region latency.** Owner runs the CloudShell probe (spec §3c). If 32 candidates do not fit 3 s with margin, stop and put the owner's choice (smaller `RERANK_CANDIDATE_K` or `PROD_RERANK_TIMEOUT_SECONDS=6`) into the PR description before merging.
- [ ] **Step 2: Measurement.** `--repeat 3`, both families, `EVAL_SRC_REF=fix/rerank-diversify-and-tail-windows`, prod database, one family at a time. This host kills background evals under memory pressure: hand the command to the owner to run in their own terminal. Record the per-category table and all-three-runs gains/losses (spec §5) in the spec's §5 and commit.
- [ ] **Step 3: Full unit suite** `... -m pytest tests/unit -q` — Expected: 0 failed (compare skips with the `test.yml` pip line; no new skips).
- [ ] **Step 4: PR** — push the branch, `gh pr create --base develop`, description: summary, §5 table, latency result, owner actions (release; prod backfill 169 keys; read switch decision). Confirm `baseRefName == develop` before `gh pr merge` after CI is green.
- [ ] **Step 5: TEST re-index** after the develop deploy: emit `index_requests/` for TEST session keys with the existing backfill script (`--family session`), then re-run the §1b window-length query on `fieldsight_test`. Expected: no session window under 1300 chars except in buckets whose total text is under 1300.
- [ ] **Step 6:** Update memory (`retrieval-eval-ruler-v1.md`) with the acceptance numbers and the owner actions still pending.
