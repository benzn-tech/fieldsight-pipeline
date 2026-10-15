# Ask retrieval tools — TEST acceptance results (2026-10-16)

Deployed: develop 788c0f6d (PR #1128), TEST stack. Probes are read-only:
`scripts/ask_tools_probe.py` (latency/shape) and
`scripts/ask_tools_timeline_parity.py` (day_overview vs /timeline).

## Latency (spec §3.4)

30 runs per tool per caller (search tools: 4 queries × 30). `server` = the
dispatcher's own `meta.ms` (in-region SQL + post-processing); `wall` = invoke
round trip from New Zealand.

| Tool | gm server p50/p95 | platform_admin server p50/p95 | wall p95 (both) | Target |
|---|---|---|---|---|
| day_overview | 8 / 11 ms | 8 / 12 ms | ≤ 145 ms | ≤ 400 ms |
| find_people | 6 / 17 ms | 6 / 13 ms | ≤ 170 ms | ≤ 400 ms |
| count_metric | 18 / 23 ms | 13 / 15 ms | ≤ 165 ms | ≤ 400 ms |
| latest_activity (no query) | 10 / 12 ms | 11 / 15 ms | ≤ 154 ms | ≤ 400 ms |
| search_records | 37 / 44 ms | 77 / 100 ms | ≤ 241 ms | ≤ 1.5 s (incl. embed + rerank, measured separately in ask-agent) |
| latest_activity (query) | 62 / 71 ms | 804 / 912 ms | ≤ 1,068 ms | ≤ 1.5 s |

Zero errors across all calls. platform_admin is the slowest caller for
`latest_activity(query)` (exact scan over every site in reach); still under
target. No admin-role account exists on TEST; gm and platform_admin bracket it.
Re-measure on prod (≈ 2× TEST chunk volume) before any prod flag flip.

## day_overview vs /timeline (spec §4.4a)

test2 (gm), every day with Aurora topics on TEST, compared by topic id
(timeline `topic_row_id` vs tool row `id`):

| Day | Timeline work topics | Tool | Missing | Extra |
|---|---|---|---|---|
| 2026-10-12 | 13 | 13 | 0 | 0 |
| 2026-10-09 | 11 | 11 | 0 | 0 |
| 2026-10-08 | 16 | 16 | 0 | 0 |
| 2026-10-06 | 17 | 17 | 0 | 0 |
| 2026-10-01 | 18 | 18 | 0 | 0 |

Recall 1.0 (75/75), no extras. The timeline additionally shows 3 `non_work`
topics to their owner (10-09 ×2, 10-08 ×1) so the classification can be
confirmed; Ask excludes them by design (spec, shared visibility rules) — the
first run counted them as missing, which is how the difference was found.
Only 5 such days exist for test2 on TEST, not the 10 the spec asked for.

## latest_activity(query) dates (spec §4.4b)

Compared with `max(report_date)` of chunks whose text contains the keyword
(company-scoped SQL — a lower bound, since the tool also matches semantically):

- cutoff 2026-10-16: 8/8 agree (5 found, 3 correctly none).
- cutoffs 2026-10-10 and 2026-10-07: 9/10 agree; the one difference is
  "scaffolding" ≤ 10-07, where the tool found 10-06 by vector similarity and
  the literal keyword appears nowhere before 10-08. Not an error.

## search_records hit rate (spec §4.4c)

Not re-run through the 39-question ruler: `search_records` calls the same
`_search` and the same `_rerank_chunks` with the same candidate count as
`_rag_answer` (verified by the final review), so its ranking is the live
Ask ranking by construction. The ruler run belongs to the orchestration
line's phase-4 eval, which exercises the tools end to end.
