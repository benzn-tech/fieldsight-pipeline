# Rerank candidate selection and short tail windows

Date: 2026-10-16, revised 2026-10-19 after review. Line: search & vectors (tool
layer owner). Status: reviewed (Fable, 2026-10-19), fixes applied.

## 1. Problem

The prod family comparison (2026-10-16, ruler v1, 39 answerable questions, three
runs per family) left the session family **one question short on Ask**
(rerank@32: report 89.7 %, session 87.2 %) and failed spec §2 of the search-index
design ("no category below today") on `programme` and `uncategorised`. The three
questions session lost were read at the time as an ordering problem. Diagnosis says
they are two defects, and the larger one is not session-specific.

The three runs agree on the percentage but not on the miss-set: report run 3
hits v1-27 and misses v1-32, runs 1–2 the reverse. So the run-to-run floor on
report is at least one question, and §5 counts a question as gained or lost only
when it flips in every run.

### 1a. `_diversify` caps a whole day, not a run of near-duplicates

`lambda_ask_agent._rerank_chunks` keeps at most `RERANK_PER_SESSION_CAP = 4`
chunks per key before the reranker sees them; the key is
`source_s3_key or topic_id or id(c)` (`_diversify`), which for every indexed prod
row is `source_s3_key`. The comment beside the cap says it exists because adjacent
transcript windows overlap and positions 6..32 are "neighbours of the same few
sessions".

The key does not match that intent:

| family  | key                                  | chunks per key (prod) |
|---------|--------------------------------------|-----------------------|
| report  | `reports/{date}/{user}/daily_report.json` — one per day | mean 17.1, max 171 |
| session | `extractions/{user}/{date}/sid{id}.json` — one per recording session | mean 9.2, max 200 |

A long day or a long session is one key, so the reranker is shown the first four
cosine-ordered chunks of the whole day/session — hours of unrelated talk collapse
into four slots. On the ruler days both families have exactly one key per day.
v1-16 (vector rank 18) and v1-19 (vector rank 10) under session are cut here: the
evidence is in the fetched rows and never reaches the reranker.

Measured, prod data, prod code (`31939b78`), cap raised to 64 (= off):

| Ask hit, rerank@32 | cap 4 (prod today) | cap off |
|---|---|---|
| report  | 89.7 % | 94.9 % |
| session | 87.2 % | **97.4 %** |

Prod runs the reranker (repo variable `PROD_ENABLE_RERANK=true`; the workflow
default is `false`), so **today's prod Ask (report family) is itself depressed by
this cap**, by v1-27, v1-30, v1-31 (v1-27 is inside the noise floor above).
Removing the cap outright is not shippable: candidate count goes from ~8–16 to
up to 64 (32 vector + 32 keyword rows, see §3a), and developer-machine rerank
latency p50 goes 3.0–4.0 s → 8.4–9.9 s. Ask is also the voice path.

### 1b. Short tail windows in the session index

`session_index._pack_windows` packs a topic's turns into windows of about
`TARGET_CHARS` (2600) and emits whatever is left as the last window. The tail can be
a few turns with no context. v1-36 ("flat slab shop drawigns new date") lands in a
639-char, 31-second tail window (11:07:41–11:08:12) of the topic "Program
Milestones & Weather Impacts"; the words "flat slab"/"shop drawings" are in the
window before it. That tail is cosine rank 65 for the question; Ask's vector arm
fetches `RERANK_CANDIDATE_K` = 32 rows (`search_sql.py` `LIMIT %(k)s`), so it
never arrives.

Prevalence (prod `report_chunks`, `transcript_window` rows):

| family  | windows | < 800 chars | < 1300 chars | p50 |
|---|---|---|---|---|
| report  | 1229 | 133 (11 %) | 177 | 3385 |
| session |  928 | 190 (20 %) | 258 | 2440 |

Probe (read-only, same question): back-filling the tail with the previous window's
turns to ~2600 chars moves its distance 0.772 → 0.570 and its rank 65 → **20**.

## 2. Goals

- G1. Ask (and `ask_tools.search_records`, which calls the same `_rerank_chunks`)
  reranks over distinct passages instead of the first four of a day.
- G2. The reranker call still fits the prod per-call timeout measured **from the
  Lambda region**, not from a developer machine (§3c).
- G3. No session-family transcript window shorter than half a target window when
  its topic/hour bucket has more text available.
- G4. With both changes, session family meets search-index spec §2 against
  today's prod report family: no category lower on Ask, list unchanged or better.

Non-goals: changing the report-family chunker (`chunking.py`) — the report family
is the one being retired by `SEARCH_INDEX_READ=session`; changing rerank model,
`RERANK_CANDIDATE_K`, or the search SQL.

## 3. Design

### 3a. Bound the candidates per arm, not per source

**The per-source cap goes.** Its premise — "one session's windows are
near-identical" — is false for this index: adjacent windows share
`OVERLAP_TURNS = 2` turns out of ~25, and one key spans a whole day or session.
Every finer grouping measured still cut real evidence (grid in §5): two windows
of one topic are not near-duplicates either (v1-19: evidence at fetch positions
10 and 26, a sibling window of the same topic at 2).

**Instead, bound the two search arms**, preserving arrival order within each.
With `fetch_k = RERANK_CANDIDATE_K` (32), `build_search_sql` returns up to 32
vector rows followed by up to 32 keyword-only rows (`lexical_hit` true) — every
keyword-only row after every vector row. So a single total limit in arrival
order drops keyword rows first, which is the defect `_admit_one_lexical_hit`
exists for, reproduced one step earlier (measured: v1-35's evidence is a
keyword-only row at position 34 of 57, cut by a total of 16 or 24).

New function `_select_candidates(chunks)`:

1. Take the first `RERANK_KEYWORD_CANDIDATES` (K, default 8) keyword-only rows.
2. Fill with vector rows in arrival order up to a total of
   `RERANK_CANDIDATE_K` (32). **Unused keyword slots go to vector rows**: with
   no keyword rows the reranker sees 32 vector rows, not 24.
3. Return vector rows first, then keyword rows (arrival order), so the
   cosine-ordered prefix stays a prefix.
4. Collapse exact-duplicate non-empty `chunk_text` to the first occurrence,
   OR-ing `lexical_hit` into the kept row (`_basis` reads it). Empty texts are
   not deduplicated. SQL already returns distinct ids, so this only catches the
   same text indexed twice.

K is env-overridable with a code default, not a stack parameter. The total stays
`RERANK_CANDIDATE_K`, which is a stack parameter and was the configured
candidate count all along, so §3c's latency bound is the one already deployed.
`RERANK_PER_SESSION_CAP` and `_diversify` are removed (the cap was never a stack
parameter; no repo variable sets it).

The measured variant (§5) was a fixed 24 + 8 without spill. Spill only changes
questions with fewer than 8 keyword rows, by adding vector ranks 25–32 (v1-19's
second evidence window is rank 26); it is measured in the `--repeat 3` run
before merge.

**Fallback.** Today the reranker-failure path returns `candidates[:keep]`. With
the keyword rows after the vector rows that would drop every keyword row on a
timeout. The fallback becomes `_admit_one_lexical_hit(candidates, keep)` — the
same function the rerank-off path already uses — so a failed rerank returns
what Ask returned before reranking existed. Timeouts are not rare from NZ (see
`template.yaml` beside `RerankTimeoutSeconds`), and the ruler runs the reranker
with a 20 s timeout and never takes this path, so the unit tests are its only
evidence.

The `RERANK_ENABLED=false` path and the `len(chunks) <= keep` short-circuit are
unchanged.

### 3c. Latency and the timeout are coupled config

The Lambda reads `DASHSCOPE_RERANK_TIMEOUT_SECONDS`, set from the stack parameter
`RerankTimeoutSeconds`. Prod: repo variable `PROD_RERANK_TIMEOUT_SECONDS` unset →
workflow default **3 s**. TEST: `TEST_RERANK_TIMEOUT_SECONDS=6` (both verified on
the deployed functions, 2026-10-19). urllib3 applies the float timeout to connect
and to read separately, so the worst-case wall time is about twice the setting.
On timeout the code silently falls back (§3a). Prod logs show
`rerank: 27 candidates -> 5` succeeding under 3 s; no `rerank: request failed`
warning in 30 days (prod Ask traffic is light, so that is weak evidence).

From a developer machine in NZ the ruler measured p50 3.0–4.0 s at ~8–16
candidates and 8–10 s at ~45–63. Those numbers are not the prod gate: the
`rerank()` docstring's measurement ("K=16 → 3.6 s, K=100 → 4.6 s, near-flat") and
the prod log line are both faster, and on 2026-10-14 the whole retrieval stage on
TEST (embed + search + rerank, ~8 candidates) took 1.2–1.9 s in-region.
**Before merge, the candidate count is checked from the Lambda region**
(CloudShell probe: the same request as `rerank()` — endpoint, model, a new
connection per call — at 8/16/24/32/48 docs of ~2600 chars, five reps each;
read-only). If 32 does not fit 3 s with margin, the owner chooses between a
smaller `RERANK_CANDIDATE_K` and raising `PROD_RERANK_TIMEOUT_SECONDS` (TEST
already runs 6 s). Parallel batched reranking is the fallback only if neither is
acceptable; it needs per-document scores the current `rerank()` discards.

### 3b. Back-fill a short tail window

In `session_index._pack_windows`, after packing, when there are at least two
windows and the last window's text is shorter than `TARGET_CHARS // 2`:

- Let `i` be the position in the bucket's `turns` of the tail's first turn (the
  tail already starts with the `OVERLAP_TURNS` carried turns).
- Prepend `turns[i-1]`, `turns[i-2]`, … one at a time, stopping **before** the
  first turn that would take the window over `TARGET_CHARS`, or at the start of
  the bucket.

Properties:

- Turn boundaries only; earlier windows unchanged; window count unchanged.
- Back-fill never makes a window exceed `TARGET_CHARS`. (Packing itself can:
  a single turn longer than `TARGET_CHARS` is admitted alone today. That is
  unchanged; back-fill does not add to such a window.)
- The tail cannot absorb its whole predecessor: the predecessor was emitted
  because it plus the tail's first fresh turn exceeded `TARGET_CHARS`. So two
  windows never become identical.
- Single-window buckets untouched (nothing to borrow).

Downstream effects, accepted:

- The tail's `window_span` and `turns` metadata cover the borrowed turns, so its
  citation start (`_citation_time_start`) moves earlier, to its true first turn.
- For hour-bucketed (no-topic) windows, `block.from` moves earlier too. The list
  groups windows by `block:<source>|<from>`; since the tail cannot start where
  its predecessor starts, no two windows merge into one group.
- More duplicated text between the last two windows of a bucket, so both can
  reach the reranker. The reranker then picks the better of the two.

Re-index is delete-and-replace per topic/source with fresh chunk ids
(`reindex.py`, `repositories/chunks.py`), so applying 3b is idempotent.

## 4. Rollout

- 3a is code-only; it takes effect on deploy. TEST via develop, prod via release
  (owner).
- 3a removes `_diversify`, which the ruler calls (`run.py` `rerank_all`). The
  ruler changes in the same PR or before it: its rerank systems call
  `_select_candidates` (taking `RERANK_CANDIDATE_K` from the module) and its
  fingerprint records K and the total instead of `rerank_per_session_cap`.
  Without that the `--repeat 3` acceptance run cannot run on the merged code.
- 3b changes chunk text, so existing session-family chunks keep the short tails
  until re-indexed. TEST: emit `index_requests/` for TEST keys (the existing
  backfill script, `--family session`). Prod: the same backfill the owner ran on
  2026-10-16 (169 keys, ~170 embed calls; idempotent replace per key),
  **executed by the owner** after release. Until it runs, prod still holds the
  old windows and the ruler measures them, so "3b changed nothing" before the
  backfill is expected.
- `PROD_SEARCH_INDEX_READ` stays `report` until §5's final measurement passes.
- Rollback: revert and redeploy. For 3b also re-run the backfill (owner-executed,
  same ~170 embed calls) to restore the old windows.

## 5. Measurement

Ruler v1, prod database, `--repeat 3`, both families, per-category table, against
prod `31939b78` cap-4 as the baseline. A question is gained or lost only if it
flips in all three runs (§1).

Measured so far (prod DB, prod code `31939b78`, rerank@32 Ask hit, `--repeat 1`
unless noted; latency from a developer machine):

| variant | report | session |
|---|---|---|
| per-source cap 4 (prod today, `--repeat 3`) | 89.7 | 87.2 |
| no cap (cap 64) | 94.9 | 97.4 |
| group key, cap 2, total 16 | 84.6 | 89.7 |
| group key, cap 2, total 24 | 82.1 | 92.3 |
| group key, cap 3, total 16 | 84.6 | 89.7 |
| group key, cap 3, total 24 | 84.6 | 92.3 |
| **per arm, 24 vector + 8 keyword, no spill** | **92.3** | **94.9** |

Every group-key variant lost questions the uncapped run kept, for three reasons
confirmed row by row: keyword-only rows cut by the arrival-order total (v1-35),
report-family NULL-topic windows of one day collapsing into one group (v1-11), and
sibling windows of one topic sharing a group (v1-19). Hence §3a.

Per arm 24 + 8 (experiment commit `902c09b1`), rerank@32 Ask, against prod today:

- session: gains v1-06, v1-16, v1-31; loses nothing; still misses v1-19 and
  v1-36. v1-36 is the §1b tail window, outside the 32-row vector fetch, which
  no candidate rule can reach. v1-19 is the one question 24 + 8 gives up
  against no cap; spill (§3a) may recover it.
- report: gains v1-27, v1-30, v1-31; loses v1-32; still misses v1-06, v1-36.
  v1-27 and v1-32 are inside the report noise floor (§1).

Developer-machine rerank p50 (comparable with each other, not with prod):
cap 4 3.0–4.0 s, 24 + 8 5.1 s (session) / 7.0 s (report), no cap 8.4–9.9 s.

Remaining before merge: the in-region latency check of §3c at 32 candidates,
and `--repeat 3` of the final §3a selector (with spill) on both families.

3b changes indexed text, and the ruler corpus lives in the prod database, which
only the owner writes. So before merge 3b is evidenced by the read-only probe in
§1b and, after the TEST re-index, by the window-length table of §1b re-run on
`fieldsight_test` (no session window under `TARGET_CHARS // 2` unless its bucket
holds less than that). Its effect on the ruler is measured after the owner's prod
backfill, and that measurement — not the pre-merge one — is the one the read
switch is decided on.

Acceptance for the read switch (owner decision): session family with 3a + 3b
≥ report family with 3a in every category on Ask, and ≥ today's prod in every
category on both Ask and list, by the all-three-runs rule above.

## 6. Tests

Removed: the four cap tests in `tests/unit/test_ask_rerank.py` that reference
`RERANK_PER_SESSION_CAP` / `_diversify`, including
`test_chunks_with_no_source_key_are_not_collapsed_together` (its key fallback
goes with `_diversify`).

- `_select_candidates`: at most K keyword-only rows and at most
  `RERANK_CANDIDATE_K` in total; unused keyword slots filled by vector rows; each
  arm in arrival order, vector rows first; a keyword-only row at arrival position
  > 32 still selected when fewer than K keyword rows precede it; many rows from
  one `source_s3_key` all pass (the regression this spec fixes); duplicate
  non-empty texts collapse to the first with `lexical_hit` OR-ed in; empty texts
  are kept.
- `_rerank_chunks`: the reranker is called with at most `RERANK_CANDIDATE_K`
  texts; **on reranker failure with keyword rows present, the result contains a
  keyword row** (equals `_admit_one_lexical_hit(candidates, keep)`).
- `_pack_windows`: a short tail is back-filled from the turn before its first
  turn, stopping before `TARGET_CHARS`; turn boundaries; window count unchanged;
  single-window bucket unchanged; a tail already ≥ `TARGET_CHARS // 2`
  unchanged; an oversize single turn is not added to. In a two-window bucket
  with a back-filled tail, the overlap is more than `OVERLAP_TURNS`, so
  `test_two_turn_overlap_between_windows` is narrowed to windows that are not a
  back-filled tail, and a new test asserts the tail's turns are a contiguous
  suffix of the bucket ending at its last turn.
- Ruler: `run.py` imports `_select_candidates` and fingerprints K and the total
  (no `_diversify` reference left).
