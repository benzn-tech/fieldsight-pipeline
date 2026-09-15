# Retrieval evaluation baseline — design

Status: draft · 2026-09-15 · tool: `tools/retrieval-eval/`

## Why this exists

Changes to how FieldSight finds things have been argued from single queries and from
vendor benchmarks. Three were on the table at once when this was written: switching the
embedding model to `qwen3.7-text-embedding`, turning on the reranker that is already
wired but disabled in prod, and re-chunking transcripts so search stops depending on the
daily report. None of them can be decided without a fixed yardstick, and the project's own
measurement history says why: this repo has published conclusions that reversed once the
same configuration was run a second time.

The owner's requirements for the yardstick were explicit: it must outlive the current
pipeline, be reusable as the baseline for future content changes, and be portable.

## What was measured before building it

These are the facts the design rests on, each checked against production on 2026-09-15.

- **The search list hides three quarters of transcript content.** Of 561 transcript-window
  chunks from the last 30 days, 419 carry no `topic_id`, and `_aggregate_topics` drops every
  such window from the search list. 210 fall outside every report topic's time range; 209
  sit inside one but the report topic could not be matched to its Aurora topic.
- **Ask is not affected by that filter.** The answer path takes the top 5 chunks by distance
  and never calls `_aggregate_topics`. An earlier write-up said otherwise and was corrected.
- **Worked example.** "Why did the roofing subcontractor pull out?" returns the phone call
  that answers it at ranks 1–4 of the raw vector search. Ask receives it. The search list
  shows six rows and the evidence is in none of them.
- **Search is not the latency.** Prod `rag-search` is p50 60 ms / p90 219 ms; Ask as a whole
  is p50 1.45 s / p90 8.6 s, dominated by the model.
- **The source transcripts expire.** Bucket rule `DeleteOldTranscripts` removes
  `transcripts/` objects 90 days after creation: the 2 Sep corpus from 2026-11-30, the
  10 Sep corpus from 2026-12-08.

## Asset layout

| Piece | Format | Why that format |
|---|---|---|
| Gold questions | JSON Lines, one question per line | Diffable, appendable, readable without tooling |
| Corpus manifest | JSON: S3 key, byte size, sha256 per file, per-day digest, expiry window | Proves a later run used the same words |
| Runner | One Python file importing production modules | The thing under test is the production path, not a copy |
| Results | One JSON per run, with a configuration fingerprint | Comparable six months later without memory of the setup |

## Rules

1. **Evidence is anchored to words.** A gold item names the sentence that answers it via
   `match_any` patterns matched against chunk text, plus the verbatim `quote` and its time
   for a human reader. It never names a chunk id, topic id or report field: every one of
   those is regenerated when the pipeline re-runs, and the report format has changed three
   times this month. This is what makes the set survive embedding swaps, re-chunking and
   prompt rewrites.
2. **Versions are appended, never edited.** A published gold file or manifest is immutable.
   Corrections produce `v2`. A result records the sha256 of the gold file it used.
3. **Every configuration is run at least twice.** `run.py --repeat` reports whether the ranks
   were identical across repeats. A difference between configurations smaller than a
   configuration's own movement is not a finding.
4. **Controls stay in.** `expect: "unanswerable"` items have no evidence, so a system that
   returns something plausible for every question cannot score well by doing so.
5. **Drafts are marked.** Every item starts `status: "draft"`. It becomes `confirmed` only
   when someone who was on site has checked the question and its answer.
6. **The runner uses production code or fails.** The search SQL is imported and re-spelled
   for the Data API with each rewrite asserted; the list uses the production aggregator.
   If the production SQL changes shape, the runner stops rather than measuring a different
   search.

## Metrics (v1)

Per question, the 1-based rank at which the evidence first appears, or null:

- `raw_top30_rank` — returned by vector search at all (the ceiling),
- `ask_context_rank` — among the 5 chunks given to the model,
- `search_list_rank` — visible in the search list rows.

Per run: the hit rate of each, split by question language; median embedding and query
time. The gap between `raw` and `search_list` isolates loss caused by aggregation from
loss caused by retrieval.

## Deliberately out of scope for v1

- **Answer correctness.** Needs each answer judged against the gold answer; v2.
- **Access control.** Scope is pinned to one author on one site. Production resolves a
  wider SELF+WORKERS scope; ACL is not what this measures.
- **In-region latency.** Timings are from the machine running the tool. Compare runs from
  the same machine with each other, never with CloudWatch.

## Open decisions

1. ~~Where the frozen corpus lives.~~ Resolved 2026-09-15 without a copy: the owner
   removed the prod bucket's hand-made `DeleteOldTranscripts` rule (90-day expiry on
   `transcripts/`, found in no template or script, first deletions due ~2026-09-22). The
   corpus is the original files, pinned by the manifest's sha256.
2. ~~How a copy honours deletion.~~ No copy exists, so there is nothing extra to delete;
   the originals follow the product's deletion behaviour like any other transcript.
3. ~~Who confirms the gold set.~~ Confirmed by the owner on 2026-09-15 and published as
   `gold/v1.jsonl` (45 items). Result files dated earlier that day name
   `gold/v1.draft.jsonl` when it held 20, 40 or 45 items; every item in them is the
   same item in v1, apart from `status`.

## First results (2026-09-15, draft gold set, 45 items)

39 answerable items (19 clean, 20 deliberately typed like a phone on site: typos, no
punctuation, broken grammar, 4 in Chinese or mixed), 6 controls. Every configuration ran
twice. Result files: `results/20260915T111826Z-v1-45q-threshold.json` and earlier.

| System | Ask top-5 | List: any row | List: top 5 rows | List rows p50 | Rows on the 6 controls |
|---|---|---|---|---|---|
| prod | 82.1 % | 8 / 39 | 7 | 4 | 10, 3, 3, 1, 3, 14 |
| rerank (qwen3-rerank) | 84.6–87.2 % | 8 / 39 | 7 | 4 | unchanged |
| link to extraction topic | 82.1 % | 20 / 39 | 16 | 6 | 11, 3, 4, 1, 3, 16 |
| link + hour-block rollup | 82.1 % | **29 / 39** | **23** | 8 | 14, 3, 5, 2, 6, 20 |
| link + rollup, cut-off 0.65 | 82.1 % | 35 / 39 | 26 | **20** | **22, 23, 22, 18, 22, 26** |

What each result rests on:

- **The list loses evidence in three places, in order:** windows with no `topic_id` are
  dropped (fixed by linking); windows that overlap no extracted topic are still dropped
  (fixed by rollup — commercial questions go 0/5 → 5/5); and every remaining miss whose
  evidence *was* retrieved sits at cosine distance 0.57–0.65, just past
  `_NO_LEX_MAX_DIST = 0.55`, including two at rank 1.
- **The cut-off is not the next lever.** At 0.65 the median list is 20 rows and every
  control question returns 18–26 rows — the list stops meaning "these match". Keep 0.55
  until something other than a single distance decides relevance.
- **The first 19 questions could not see rollup at all**: their evidence happened to sit
  in linkable windows. A question set must include evidence that no topic claims.
- **Rerank is within its own noise.** Ask moved 84.6 % in one repeat and 87.2 % in the
  other; it rescued "big-ticket items" (rank 8 → 1) and "geotek inspection" (9 → 1) and
  dropped two others out of the top 5, and 8–11 of 45 calls took longer than production's
  3 s budget from NZ, where production silently falls back to distance order.
- **Typing costs retrieval, not language.** The 20 messy questions against clean rewrites
  with identical evidence: top-30 18 vs 20, Ask 15 vs 17; a typo can push evidence from
  rank 3 to 17. The Chinese and mixed questions over English speech all reached Ask.
- **Recall, not aggregation, limits quality and subcontractor questions** (Ask 2/5 and 4/5).

## Next systems to compare

- link + rollup built on the write side (ingest), measured by this runner against a
  re-indexed copy rather than simulated at read time;
- `qwen3.7-text-embedding` at 1024 dimensions (full re-embed) — the messy-question gap
  and the recall-limited categories are what it would have to move;
- reranker, only once its latency is measured in-region.
