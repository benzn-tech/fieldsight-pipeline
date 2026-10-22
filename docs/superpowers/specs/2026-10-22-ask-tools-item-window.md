# Ask tools show the part of a chunk that matches the query

Date: 2026-10-22. Line: search & vectors (owner of `ask_tools`). Requested after the Ask v2
ruler run on prod 6a07de4b (read-only). Constraints from the Ask orchestration line
("ASR+LLM优化v3"): their code (`ask_orchestrator`) is not changed. Review: pending (Fable).
All `file:line` refer to origin/develop 5608895a.

## 1. Problem

`ask_tools._text` (src/ask_tools.py:323-328) keeps the first `MAX_TEXT = 600` characters
(:225) of every item. Transcript windows are packed to `TARGET_CHARS = 2600`
(src/chunking.py:24; src/session_index.py:100-125), so they arrive at 2,000-3,300 characters
and the model sees about the first quarter.

Measured (`run_ask_v2.py`, session family, run 1): 19 of 39 answerable questions had the
evidence in a row SQL returned and the tool itemised, past character 600 (`cut_600_cap`); the
judge never saw it. Also 1 `cut_post_search`, 0 `cut_render_budget`. Report family: v1-03.

## 2. Offline experiment (no model, no AWS)

Windows rebuilt from the local 09-10 transcript (`Dropbox/Ben_UCPK2_2026-09-10_transcript.md`
as `[HH:MM:SS] spk_N: text`, packed like `_pack_windows`; 177 windows, median 2,548 chars). For
the 24 answerable 09-10 gold questions: does a window holding the evidence still hold it after
the cut? Query = the gold question.

| cap W | head (today's rule) | centred (§4) |
|---|---|---|
| 600 | 9/24 | 20/24 |
| 800 | 11/24 | 20/24 |
| 1,000 | 14/24 | 21/24 |
| 1,200 | 15/24 | 21/24 |
| 1,500 | 19/24 | 21/24 |

Centring helps far more than a larger cap. Exact substring and a light stem (-s/-es/-ed/-ing)
gave identical counts. The three misses: v1-13 and v1-23 (Chinese questions about English
speech, so CJK bigrams cannot match and the window falls back to the head, as today) and v1-27
(the query misspells the names).
Limits: prod groups turns by topic, and only round 1 passes the raw question as `query`
(later rounds: model-written, e.g. "roofing subcontractor withdraw Xtreme"). §6 uses real ones.

## 3. Budgets the window must fit

Two caps, in this order:

1. **Tool layer, ours.** `MAX_RESULT_CHARS = 10_000` (:224) per envelope, measured as item JSON
   (:406-408) and enforced in `_envelope` (:427-430, `truncated` and stop). Item JSON costs
   about 539 characters before the text, because of ids and the S3 `source` key that the
   model never sees. At 10k, 8 items (`DEFAULT_K`, :230) fit only if W ≤ ~700.
2. **Orchestrator, theirs.** `_budgeted` (src/ask_orchestrator.py:221-245) holds
   `RENDER_MAX_CHARS = 32,000` (:41-42) across all rounds. When over, it drops older rounds'
   lines first. A rendered item line is ~98 characters of `ref | date time | site | who |
   title — ` plus the text (:195-215).

| W | items per 10k JSON | render per call (8 items) | 3 calls | 4 calls |
|---|---|---|---|---|
| 600 (today) | 8 | 5.6k | 16.8k | 22.4k |
| 900 | 6 | 8.0k | 24.0k | 32.0k |
| 1,000 | 6 | 8.8k | 26.4k | 35.2k |
| 1,200 | 5 | 10.4k | 31.2k | 41.6k |

Calls per turn (log): 1 ×9, 2 ×10, 3 ×27, 4 ×4, 5 ×5. Worst case: topics, people, metrics
and short windows come in far under W.

**Decision:** W = 1,000 for windowed types (§5), and `MAX_RESULT_CHARS` goes up to 12,000
(≈3,000 tokens per result; the :220 comment and spec §3.2 figure are updated) so that 8
items still fit. (Provisionally 12,500; the plan Task 10 replay measured the per-item
overhead p95 at 477, so 8 x (477 + 1,000 + 2) + 2 rounds up to 12,000. Replayed at both
budgets, evidence-in-item is identical: session 72/78, report 65/78.) W is fixed by the rule in §6.3 (1,000 if the replay shows no new render
drops, else 900). We do not adopt the 1.2-1.5k the orchestration line asked for. §2 shows
no gain above 1,000, and at 1,200 three calls already use 31k of the 32k.

## 4. Where the match comes from

At `_item` time (:353) the tool knows `args` (validated `query`, :164-168), the row
(`chunk_text`, `chunk_type`, `metadata`, `lexical_hit`) and nothing else. `lexical_hit` is
only a keyword-only flag (src/lambda_ask_agent.py:315-318) and carries no span, and
rag-search returns no offsets. So the tool computes the match itself, the same way for
vector and keyword rows:

- **Terms** = `lexical_terms.query_terms(args["query"])` (src/lexical_terms.py:104-126) minus
  `ASKING_WORDS` (:135-145), deduplicated in order. Latin runs ≥3 characters, identifiers
  such as `b2`/`p3`, and CJK 2-character shingles (:46-51) are kept. Unlike `subject_terms`
  (:149-165), CJK is kept. Only `.lower()` is applied: no ASCII normalisation, no stemming.
  A line matches a term when the lowercased line contains it as a substring.
- **Choice**: of the contiguous runs of units whose joined length is ≤ W, take the one
  covering the most distinct terms. Ties go to more total term hits, then to the earliest
  start. This is deterministic and needs no model or I/O. The search is O(units²), with
  ≤ ~60 units per chunk.
- **No term hits** (vector-only row, Chinese over English, no `query`): line-aligned head,
  which is today's behaviour at the new size.
- No `query` (`day_overview`, `latest_activity` without one): topic rows, never windowed.
- `latest_activity` with a query already keeps only rows that cover the subject words
  (src/rag_tools_dispatch.py:316-321), so centring on the same terms puts the item on them.

## 5. Window shape, per type (one pure function `_window(text, terms, cap, unit)`)

| item source | branch | unit | cap | always kept |
|---|---|---|---|---|
| `transcript_window` chunk | :376 | line (`\n`) | W | — |
| `topic` chunk (`chunk_text` up to 4,500; session_index.py:68-90,182) | :376 | line | W | line 1 (`[time] title (category)`) |
| `session_digest` chunk (session_index.py:234-240) | :376 | line | W | line 1 (`Session hh:mm–hh:mm`) |
| document chunk (project_doc_chunks.py: TARGET 1,800, HARD_CAP 3,000, `\n\n` paragraphs) | :355 | paragraph | W | — |
| topic row (`summary`, e.g. `day_overview`) | :391 | — | 600 head (unchanged) | — |
| person, metric | :366, :370 | — | 600 (unchanged) | — |

- Text ≤ cap is returned unchanged, byte for byte.
- Elision: a line `…` before the first kept unit when units were skipped above, and after
  the last when units were skipped below. The cap includes the markers.
- A single unit longer than the cap is cut by characters around its first term hit and
  snapped to spaces (CJK: hard). It keeps a transcript line's `[HH:MM:SS] spk_N:` prefix.
  `…` marks each cut end.
- `sanitize` (ask_orchestrator.py:56-73) joins lines with spaces, so the model reads
  `… [08:24:10] spk_1: …`; `_FORGED_LEAD` (:52) does not strip that lead.

## 6. Measurement

### 6.1 Offline replay (no model, no Data API)
fs-eval adds `--keep-rows DIR` to `run_ask_v2.py`, which dumps per turn: gold id, each
batch's `calls` with their args (per-round `query`), round index, the envelopes' item
ids, and the raw `rec.invokes` rows. It writes to scratch only, never `results/`, with the
same guard as `--keep-renders` (run_ask_v2.py:171-173), because rows hold transcript text.
`replay_window.py` then:
1. maps each item to its row by `chunk_id`/`id` (keeps the reranker's choice; no DashScope),
2. rebuilds it with the branch's `_item` and with 5608895a's, using that round's args.
3. **evidence-in-item**: per answerable question, does an item whose date is the gold
   date match a `match_any` regex (funnel.py `_patterns`/`hit` rule)? It reports
   before/after counts and lists every question that flips either way.
4. **render drops**: rebuilds envelopes with their `round`, calls `_budgeted`, counts lines
   dropped (`total - shown`), turns with a drop, rendered chars (median/p90), truncations.
Calls are held fixed; a turn ending earlier is §6.2's to see.

### 6.2 Live ruler (later, gated, run by the owner's session)
`run_ask_v2.py` on both families, before (5608895a) and after (branch). The harness runs
`ask_orchestrator`/`ask_tools` locally from the given src ref against the read-only Data
API, so this needs no deploy. Repeat 2. Report the funnel stages, judge rounds per turn,
prompt tokens, and judge ms. fs-eval renames `cut_600_cap` to `cut_text_cap`
(funnel.py:124-128).

### 6.3 Success
- Replay: evidence-in-item for the session family's `cut_600_cap` questions goes from 0 to
  ≥ 15 of 19, and no question loses evidence it had (0 regressions, or each one explained).
  The report family recovers v1-03.
- Replay: lines dropped by `_budgeted` and `_envelope` truncations are not above
  5608895a's. W = 1,000 if both hold, else 900, then the replay is re-run.
- Ruler: `seen` and `cited` go up on both families. `cut_render_budget` stays 0 (baseline
  0). Median prompt tokens rise ≤ 35% and judge ms ≤ 15%. Controls do not gain
  citations.

## 7. Citations
- `time_start` (`_time_start`, :309-320) is the window's first turn today. When the shown
  text does not start at the chunk's first line, `_item` sets `time_start` to the first
  kept line's `[HH:MM:SS]`. The card then jumps to what the model read. `time` stays the
  chunk's `window_span` (:378-382).
- `_card` (ask_orchestrator.py:287-297) snippets `text[:200]` and the fallback (:1128-1143)
  takes `text` and `time_start` from the item: both follow the window. Refs (:421-436) unchanged.

## 8. Tests (unit, offline; tests/unit/test_ask_tools_item_window.py)
- ≤ cap unchanged; no hit → line-aligned head + `…`; cap never exceeded (markers included).
- Evidence at char ~2,000 of a 2,600-char window is kept, on whole lines; full tie → earliest.
- Chinese query over Chinese text centres on the shingle hit. A mixed query (`P3 方案`) hits
  through its Latin token. A Chinese query over English text gives the head (no
  regression).
- A topic chunk keeps line 1. A digest keeps line 1. A document centres on a paragraph. A
  single over-long line keeps its speaker prefix and is cut at both ends with `…`.
- `time_start` = first kept line's time when windowed, else the chunk start.
- Updated: test_ask_tools_execute.py:279/289/419 hold at `MAX_RESULT_CHARS` 12,500. New:
  8 items at the maximum window fit in one envelope.
- Fixtures use session_index's `_window_line`, so a format change breaks the test, not prod.

## 9. Rollout and flag
develop (PR, CI green, merged by me) → TEST deploy (CI) → one TEST Ask e2e in English and
in Chinese, citation click lands on the shown line → release PR → prod (15-minute gate).
§6.1 and §6.2 come before the release PR, and their numbers go in it.

**No flag.** A pure function and two constants, fully measured on prod data before deploy
by running the harness from the branch (§6.2). Rollback is a one-commit revert (no data,
schema or index change); a flag would only leave an untested twin path in `_item`.

## 10. Risks
- **Wrong region.** A run dense in generic terms (`site`, `pour`) can beat the evidence
  run. Coverage-first scoring limits this. The replay's flip list shows it, and the
  per-question regression check catches it.
- **Budget pressure.** With 4-5 calls per turn (9/55 turns) and W = 1,000 the render can
  exceed 32k, and older evidence drops. §6.3 rules this out or moves W to 900.
- **Cost and latency.** Up to +57% rendered chars per full-size item (1,099 vs 699); bounded by §6.3.
- **Chinese / documents.** No measured gain (zh/mixed = 7 of 39; zh over English speech
  falls back to the head). No gold covers documents.

## 11. Open questions
1. W = 1,000 (data) vs 1.2-1.5k (orchestration line's request). Is the line content with
   §6.3 deciding?
2. Raise `MAX_RESULT_CHARS` to 12,500, or keep 10k and accept 6 items per call? A third
   option is to measure only rendered fields, so `source` and ids stop counting. That
   changes the §3.2 contract.
3. Stemming: §2 shows no difference. Leave it out unless the replay's flips show a stem
   miss.
4. The orchestrator renders a repeated ref again in every envelope (no cross-envelope
   dedupe in `_budgeted`). Rendering "R7 (above)" would free more budget than any W choice.
   That change belongs to the orchestration line.
5. Should documents be held back to a later change, given no gold covers them?
6. fs-eval work (`--keep-rows`, `replay_window.py`, label rename) goes on the fs-eval
   branch. Who owns it?
