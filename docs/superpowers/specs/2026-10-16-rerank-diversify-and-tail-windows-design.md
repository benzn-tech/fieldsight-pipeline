# Rerank diversify key and short tail windows

Date: 2026-10-16. Line: search & vectors (tool layer owner). Status: draft for review.

## 1. Problem

The prod family comparison (2026-10-16, ruler v1, 39 answerable questions, three
identical runs per family) left the session family **one question short on Ask**
(rerank@32: report 89.7 %, session 87.2 %) and failed spec §2 of the search-index
design ("no category below today") on `programme` and `uncategorised`. The three
questions session lost were read at the time as an ordering problem. Diagnosis says
they are two defects, and the larger one is not session-specific.

### 1a. `_diversify` caps a whole day, not a run of near-duplicates

`lambda_ask_agent._rerank_chunks` keeps at most `RERANK_PER_SESSION_CAP = 4`
chunks per `source_s3_key` before the reranker sees them. The comment beside it
says the cap exists because adjacent transcript windows overlap and positions
6..32 are "neighbours of the same few sessions".

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

So **today's prod Ask (report family) is itself depressed by this cap**, by three
questions (v1-27, v1-30, v1-31). Removing the cap is not shippable: reranker
latency p50 goes 3.0–4.0 s → 8.4–9.9 s (p90 up to 13.7 s), because candidate count
goes from ~8–16 to ~45–63. Ask is also the voice path.

### 1b. Short tail windows in the session index

`session_index._pack_windows` packs a topic's turns into ≤ `TARGET_CHARS` (2600)
windows and emits whatever is left as the last window. The tail can be a few turns
with no context. v1-36 ("flat slab shop drawigns new date") lands in a 639-char,
31-second tail window (11:07:41–11:08:12) of the topic "Program Milestones &
Weather Impacts"; the words "flat slab"/"shop drawings" are in the window before
it. That tail is cosine rank 65 for the question — outside even the 64-row fetch.

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
- G2. The reranker call still fits the prod per-call timeout
  (`PROD_RERANK_TIMEOUT_SECONDS`, unset → 3 s) measured **from the Lambda region**,
  not from a developer machine (§3c).
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

**Instead, bound each search arm separately**, preserving arrival order within
each:

- the first `RERANK_VECTOR_CANDIDATES` vector rows (`lexical_hit` false), then
- the first `RERANK_KEYWORD_CANDIDATES` keyword-only rows (`lexical_hit` true).

Arm-aware because `build_search_sql` appends every keyword-only row after every
vector row: a single total limit in arrival order drops keyword rows first, which
is the defect `_admit_one_lexical_hit` exists for, reproduced one step earlier
(measured: v1-35's evidence is a keyword-only row at position 34 of 57, cut by a
total of 16 or 24).

Starting values 24 + 8 = 32, i.e. the reranker sees at most `RERANK_CANDIDATE_K`
rows, which it was configured to see all along. Both env-overridable with code
defaults; not stack parameters. The per-source cap
`RERANK_PER_SESSION_CAP` is removed (it was never a stack parameter; no repo
variable sets it).

Exact-duplicate texts (same `chunk_text`) are still collapsed to the first, which
is free and keeps what the cap was actually for.

Failure paths are unchanged: reranker failure returns `candidates[:keep]`.

### 3c. Latency and the timeout are coupled config

Prod runs the reranker with a **3 s** per-call timeout (`PROD_RERANK_TIMEOUT_SECONDS`
unset); TEST runs 6 s. On timeout the code silently keeps cosine order. Prod logs
show `rerank: 27 candidates -> 5` succeeding under 3 s; no `rerank: request failed`
warning in 30 days (prod Ask traffic is light, so that is weak evidence).

From a developer machine in NZ the ruler measured p50 3.0–4.0 s at ~8–16
candidates and 8–10 s at ~45–63. Those numbers are not the prod gate: the existing
docstring's measurement ("K=16 → 3.6 s, K=100 → 4.6 s, near-flat") and the prod
log line are both faster. **Before merge, the candidate count is checked from the
Lambda region** (CloudShell probe: the same `rerank()` call at 16/32/48 docs of
~2600 chars, three reps each; read-only). If 32 does not fit 3 s with margin, the
owner chooses between a smaller bound and raising `PROD_RERANK_TIMEOUT_SECONDS`
(TEST already runs 6 s). Parallel batched reranking is the fallback only if
neither is acceptable; it needs per-document scores the current `rerank()`
discards.

### 3b. Back-fill a short tail window

In `session_index._pack_windows`, after packing: if there are at least two windows
and the last window is shorter than `TARGET_CHARS // 2`, rebuild the last window
by prepending turns from before it (walking backwards through the bucket's turns)
until adding the next turn would exceed `TARGET_CHARS`. Earlier windows are not
changed; the tail simply overlaps its predecessor by more than `OVERLAP_TURNS`.

Properties kept: every window ≤ `TARGET_CHARS`; turn boundaries only; no window
added or removed (window count is unchanged, so the chunk-count side of
re-indexing is unchanged); single-window buckets untouched (nothing to borrow).

Cost: more duplicated text between the last two windows of a bucket, so both can
reach the reranker. Accepted: the reranker then picks the better of the two.

## 4. Rollout

- 3a is code-only; it takes effect on deploy. TEST via develop, prod via release
  (owner).
- 3b changes chunk text, so existing session-family chunks keep the short tails
  until re-indexed. TEST: emit `index_requests/` for TEST keys (the existing
  backfill script, `--family session`). Prod: the same backfill the owner ran on
  2026-10-16 (169 keys, ~170 embed calls; idempotent replace per key), run by the
  owner after release.
- `PROD_SEARCH_INDEX_READ` stays `report` until §5's final measurement passes.
  Rollback for either change: revert and redeploy; for 3b also re-run the backfill.

## 5. Measurement

Ruler v1, prod database, `--repeat 3`, both families, per-category table, against
prod `31939b78` cap-4 as the baseline.

Measured so far (prod DB, prod code `31939b78`, rerank@32 Ask hit, `--repeat 1`
unless noted; latency from a developer machine):

| variant | report | session |
|---|---|---|
| per-source cap 4 (prod today, `--repeat 3`) | 89.7 | 87.2 |
| no cap (cap 64) | 94.9 | 97.4 |
| group key, cap 2, total 16 | 84.6 | 89.7 |
| group key, cap 2, total 24 | 82.1 | 92.3 |
| group key, cap 3, total 16 | 84.6 | 89.7 |

(The cap-3/total-24 runs were stopped by the host for memory and are absent.)
Every bounded variant lost questions the uncapped run kept, for three reasons
confirmed row by row: keyword-only rows cut by the arrival-order total (v1-35),
report-family NULL-topic windows of one day collapsing into one group (v1-11), and
sibling windows of one topic sharing a group (v1-19). Hence §3a.

Next: the arm-aware 24 + 8 variant on both families, then the final run.

3b changes indexed text, and the ruler corpus lives in the prod database, which
only the owner writes. So before merge 3b is evidenced by the read-only probe in
§1b and, after the TEST re-index, by the window-length table of §1b re-run on
`fieldsight_test` (no session window under `TARGET_CHARS // 2` unless its bucket
holds less than that). Its effect on the ruler is measured after the owner's prod
backfill, and that measurement — not the pre-merge one — is the one the read
switch is decided on.

Acceptance for the read switch (owner decision): session family with 3a + 3b
≥ report family with 3a in every category on Ask, and ≥ today's prod in every
category on both Ask and list.

## 6. Tests

- Candidate selection: at most V vector rows and K keyword-only rows, each in
  arrival order; a keyword-only row past position V+K of the arrival list still
  reaches the reranker when fewer than K keyword rows precede it; many rows from
  one `source_s3_key` all pass (the regression this spec fixes); exact-duplicate
  texts collapse to the first.
- `_rerank_chunks`: the reranker is called with at most V+K texts; on failure the
  returned rows are the selected candidates in arrival order.
- `_pack_windows`: a short tail is back-filled to ≥ `TARGET_CHARS // 2` when
  material exists, stays ≤ `TARGET_CHARS`, starts and ends on turn boundaries,
  window count unchanged; single-window bucket unchanged; a tail already long
  enough unchanged.
