# Review: Ask tools show the part of a chunk that matches the query

Date: 2026-10-22. Reviewer: Fable (adversarial). Spec:
`docs/superpowers/specs/2026-10-22-ask-tools-item-window.md`. All `file:line` refer to
origin/develop 5608895a (fs-itemwin worktree) and fs-eval ce6bb1ff (`tools/retrieval-eval`).
Offline checks run with `C:\Users\camil\fspy\Scripts\python.exe`; no AWS, model or network
call was made. The main agent's decisions on Q1-Q6 were taken as the default and reviewed.

## Verdict

Sound idea, correctly placed (one pure function in `ask_tools._item`, no orchestrator
change), and the offline experiment reproduces exactly (`winsim.py`, 24 questions: head
9/11/14/15/19, centred 20/20/21/21/21 at 600/800/1,000/1,200/1,500, stem on or off). Three
blockers, all cheap: the choice rule is incomplete at two edges, the replay that fixes W
cannot see the items the old budget truncated, and the `time_start` override has no
fallback. Fix those and the plan can start.

## Citations checked

Every `file:line` in the spec was opened. All resolve to the code they describe:
`MAX_RESULT_CHARS`/`MAX_TEXT` (ask_tools.py:224-225), `_text` (:323-328), `_item` (:353-403,
branches :355/:366/:370/:376/:391), `_size`/`_envelope` (:406-443, truncation :427-430),
`DEFAULT_K` (:230), `TARGET_CHARS` (chunking.py:24), `_pack_windows`/`_window_line`
(session_index.py:100-146), topic lines (:41-97), digest (:231-242), documents
(project_doc_chunks.py:11-12, `_SEP = "\n\n"` :15), `query_terms`/`ASKING_WORDS`/
`subject_terms` (lexical_terms.py:104-165, shingles :49-50), `lexical_hit` (lambda_ask_agent.py:
315-318), `_citation_time_start` (:729-741), `_rerank_chunks` (:377-410), `_budgeted`/
`RENDER_MAX_CHARS` (ask_orchestrator.py:221-245, :41-42), `sanitize`/`_FORGED_LEAD` (:52-73),
`_item_line` (:197-214), `_card` (:286-297), fallback (:1119-1143), latest_activity coverage
(rag_tools_dispatch.py:316-321), `--keep-renders` guard (run_ask_v2.py:171-173), `cut_600_cap`
(funnel.py:124-128). One imprecision: §7 says refs are at ":421-436"; `RefAssigner` is
:259-279 and the ref assignment inside `_envelope` is :431-436. Harmless.

## Findings

### BLOCKER 1 — the choice rule (§4) is incomplete at two edges

(a) **Run length is not decided.** "Most distinct terms, then most hits, then earliest
start" never separates a run `[i..j]` from `[i..j+1]` when the extra line adds no hit. The
simulation (`winsim.py:53-66`, strict `>`) therefore returns the *shortest* earliest run
that attains the maximum. Measured on the 38 windowed choices of the 24 questions: 3 are a
single line, median chosen length 919 of a 1,000 cap. So when the evidence is one line and
nothing else in the window names a term, the model gets one 60-120 character line and no
context, which is worse than today's 600-character head for "why"/"what happened" questions.
The §8 tests ("evidence at ~2,000 kept, on whole lines") would not catch it.

Fix: after choosing `(i, j)` by coverage/hits/earliest, **extend** it deterministically:
add the unit before, then the unit after, alternately, while the joined length plus
markers stays ≤ cap; stop on the first side that does not fit and keep trying the other.
The extension cannot lower evidence-in-item (it only adds text), so the §2 counts stay
valid as lower bounds. Test: a window whose only hit is line 20 of 40 returns ~cap
characters around line 20, not line 20 alone.

(b) **An over-cap unit is never a candidate.** §4 scores only "runs whose joined length is
≤ W". A transcript turn or document paragraph longer than W (project_doc_chunks `_split_long`
cuts at TARGET 1,800, so a 1,500-character paragraph is normal; long monologue turns exist)
is excluded from every run, so a chunk whose only hit is inside that unit scores 0 and
falls back to the head. §5 then says "a single unit longer than the cap is cut by
characters around its first term hit", which can only apply when the whole chunk is one
unit. `winsim.py:60` has the opposite bug: it lets `j == i` through the cap check and
returns the oversize line whole, exceeding the cap.

Fix: candidates are (1) every run of whole units with joined length ≤ cap, and (2) every
single unit longer than cap, scored on its own. If (2) wins, cut it by characters: the
unit's first term hit (earliest position in the unit of any term) at the centre, snapped
outward to spaces (Latin) or hard (no space within 40 characters), prefix `[HH:MM:SS]
spk: ` kept when present, `…` at each cut end, total ≤ cap. Test: a 2,600-character
chunk of 12 lines where line 7 is 1,400 characters and holds the only hit.

### BLOCKER 2 — the replay cannot see what the old budget truncated

§6.1 maps each baseline item to its row by id and rebuilds. Two records exist per turn:
`rec.invokes` holds the dispatcher's **pre-rerank** rows (harness.py:171-175; up to
`candidate_k` 32/64), `rec.batches` holds the **post-truncation** envelopes (:375-379).
The post-search output (`_post_search`, ask_tools.py:553-559, the reranker's ordered k rows)
is recorded nowhere. When a baseline envelope has `truncated=True` (ask_tools.py:428-430),
the rows that would fit at 12,500 are unknown without re-running DashScope rerank, so the
replay under-counts items, rendered characters and render drops for exactly the envelopes
the raise is meant to grow. `day_overview` (no post-search) is reconstructible from
`invokes`; `search_records`/`latest_activity` are not. How often a baseline envelope is
truncated is unknown (the ruler reports no truncation count; only the 40-topic test at
test_ask_tools_execute.py:273-279 demonstrates the path), which is itself the reason the
dump must record the post-search output rather than assume truncation never happened.

Fix: `--keep-rows` also records what `ask_tools._post_search` returned per call (row ids in
order). The harness already patches module attributes (`_Patches.set`, harness.py:181-199;
tests do the same at test_ask_tools_execute.py:46). The replay then rebuilds every envelope
through the branch's `_envelope` from the post-search rows, at 10,000 and at 12,500.

### BLOCKER 3 — `time_start` override has no fallback and no kind restriction

§7: "`_item` sets `time_start` to the first kept line's `[HH:MM:SS]`". Nothing says what
happens when the first kept unit carries no timestamp: a turn whose text contains `\n`
(the window splits on `\n`, so the second half is a unit with no prefix), a digest line
(`- [HH:MM] title`, session_index.py:236, four-digit time), a topic body line, a document
paragraph. Today `time_start` is `None` for every non-`transcript_window` chunk
(`_citation_time_start`, lambda_ask_agent.py:729-741) and the card's anchor logic in the UI
relies on that. A parse that yields `None` or `"HH:MM"` for a transcript window would blank
or mis-anchor the card.

Fix: override only for `chunk_type == "transcript_window"`; parse `^\[(\d\d:\d\d:\d\d)\]`
on the first kept unit; on no match keep the chunk's `window_span` start (today's value).
Tests: embedded-newline turn, digest, topic, document each keep today's value.

### SHOULD 1 — 12,500 is asserted, not derived; the 8-items test must use a real key

§3: "Item JSON costs about 539 characters before the text". 8 × (539 + 1,000 + 2 markers)
+ separators ≈ 12,350, leaving ~18 characters per item for variance in `source`
(`extractions/<folder>/<date>/<file>.json`, lake_keys.py:16; folder is a name or a UUID,
the file name carries a session id), `site`, `who`, `title`. One long title and the 8th
item is silently dropped with `truncated=True`. The spec's own fixture risk is the memory
rule "verified the wrong thing": a test with `source="s.json"` passes at 11,000.

Fix: the replay reports per-item overhead (JSON size minus text) max/p95 over the real
rows; the constant is set from that (8 × (overhead_p95 + W + 2) + 2, rounded up to 500) and
the unit test's fixture uses the longest observed `source`/`title` shape. If the number
lands above ~13,000, prefer the third option of Q2 (stop counting `source` and ids, which
the model never sees) over a larger constant; it is a two-line change to `_size`'s input
and the contract (§3.2 of the tools spec) is already being rewritten for 12,500.

### SHOULD 2 — the W gate cannot be met and has no floor

§6.3: "lines dropped by `_budgeted` ... not above 5608895a's" with a baseline of 0
`cut_render_budget`. Any single dropped line fails W = 1,000; at 900 four calls × 8 × ~998
characters is 32k on the nose, so 900 fails the same turns, and the spec stops there.
Two independent causes, only one of which W controls: (1) windowed items (W); (2) the
raised `MAX_RESULT_CHARS` lets `day_overview` return more topics (ask_tools.py:223:
15 English topics at 10k → ~19 at 12.5k), ~700 rendered characters each.

Fix: gate on **evidence-bearing** drops (a dropped line whose date is the gold date and
whose text matches `match_any`, i.e. the replay's `cut_render_budget`), require it to be 0,
and *report* raw dropped lines, turns with a drop and rendered p90. Ladder: 1,000 → 900 →
800. If drops come from `day_overview` at 12,500, keep `MAX_RESULT_CHARS` at 10,000 for
non-windowed tools (a per-tool budget is a one-line table) rather than lowering W.

### SHOULD 3 — judge-ms gate has no noise floor; prompt tokens are fine

§6.3 "judge ms ≤ 15%" from two repeats per arm. Judge latency is dominated by the model
vendor; the ruler already defines a same-config noise pair (run_ask_v2.py:57-58,
`FULL_RUN_TURNS` comment) and reports `min_detectable_delta` as flips (:327). Make
`seen`/`cited` gains count only above the flip floor, keep prompt tokens as a gate (near
deterministic given fixed calls), and make judge ms report-only.

### SHOULD 4 — state what the card now shows, and check the fallback prompt size

`_card` (ask_orchestrator.py:295) snippets `text[:200]`: a windowed item's snippet starts
with `…\n[08:24:10] spk_1: …` and differs from `chunk_text[:200]`. The UI card shows that
snippet; say so in §7 and in the TEST e2e check. `test_chunk_item_carries_every_field_
build_citations_reads` (test_ask_tools_execute.py:196-218) asserts `item["text"][:200] ==
chunk_text[:200]` with a 900-character chunk; it still passes (≤ cap) and should stay, with a
new test where they differ by design. `_fallback` (:1125-1132) feeds up to
`FALLBACK_MAX_CHUNKS = 12` item texts to `build_rag_prompt`: 12 × 1,000 instead of 12 × 600.
No cap inside `build_rag_prompt` (lambda_ask_agent.py:186-215 fences `chunk_text` whole);
+4,800 characters is within any model window used, but note it in §10.

### SHOULD 5 — log how often the window fired

Nothing in prod will say whether items are windowed or head-fallback after deploy. Add
counts to the existing per-tool log line (ask_tools.py:569-573): `windowed=n head=n`. No
text, consistent with the module's logging rule.

### SHOULD 6 — the spec's per-tool query table is right; write it down

Checked what `args["query"]` is at `_item` time (ask_tools.py:567 → :353):
- `search_records`: round 1 = `asked` (ask_orchestrator.py:570-578; the rewritten standalone
  question when history exists, :1278; in the ruler, the gold question); judge rounds =
  model-written (:956).
- `latest_activity`: the raw question (ASKING_WORDS matter); the dispatcher's
  `" ".join(terms)` rewrite is on its own copy (rag_tools_dispatch.py:305) and never reaches
  `_item`. Without a query it returns `_overview` topic rows (:265-272): never windowed.
- `day_overview`: no `query` in its schema; topic rows only.
- `search_project_documents`: the question or model query; document rows, `\n\n` units.
- `find_people`, `count_metric`: no text to window.
Put this table in §4; it is the contract the tests pin.

### NIT 1 — CJK and normalisation: clean, with two notes

No ASCII normaliser is on this path: `ask_tools`, `ask_orchestrator.sanitize`,
`lexical_terms` and `text_normalize.normalize` (alias pairs only, :31-44) leave CJK intact;
`str.lower()` is CJK-safe. Two notes. (1) For the character cut in BLOCKER 1(b), find the
hit with `re.search(re.escape(term), unit, re.IGNORECASE)` on the original unit, not by
offset into `unit.lower()`: `lower()` can change length (U+0130), and the offset would
drift. (2) CJK asking bigrams (`什么`, `多少`, `怎么`, `一个`) are not in `ASKING_WORDS` and
match generic speech; coverage-first usually still favours the subject bigrams (`分包`,
`包商`, `退出` outnumber `为什`, `什么`), but the replay's flip list must be read for the 7
zh/mixed questions before calling it fine. Alias normalisation rewrites `chunk_text`, not the
query (rag_tools_dispatch.py:81-93): a query that spells the `wrong_term` will not match the
normalised text. Pre-existing for the keyword arm too; note, do not fix here.

### NIT 2 — substring vs word boundary

Left-boundary matching (`(?<![a-z0-9])term`) gave the same 21/24 as substring on this
gold (`winsim2.py`). Keep substring (Q3's spirit: no stemming) and let the replay decide.

### NIT 3 — units, separators, `\r`

Split transcript/topic/digest on `"\n"` only (not `splitlines()`, which also splits on
`\x1c`, `\u2028`, etc. and would change the re-join); a trailing `\r` stays on its unit and
round-trips. Documents split on `"\n\n"` (`_SEP`, project_doc_chunks.py:15). Cap and
`_size` both count code points (`ensure_ascii=False`), so multi-byte text costs the same in
both; JSON escapes of `"`/`\` are counted by `_size`, not the cap, which is why the cap is
not the budget and the 8-items test must go through `_envelope`.

### NIT 4 — figures and names to update

- ask_tools.py:220-223 comment (2,500 tokens) and the tools design spec
  `2026-10-15-ask-retrieval-tools-design.md:192` ("≤ 600 chars"), `:221`, `:356` (2,500
  tokens ≈ 10 KB).
- Keep `MAX_TEXT = 600` for topic/person/metric; add `WINDOW_CHARS = 1000` rather than
  overloading `MAX_TEXT` (test_ask_tools_execute.py:226-230 pins the topic cap).
- fs-eval rename sites: funnel.py:128, tests/askv2/test_funnel.py:88-90
  (`test_600_cap`), docs/2026-10-22-ask-v2-mode-spec.md:38, docs/2026-10-22-ask-v2-mode-
  plan.md:181. The spec lists only funnel.py.
- `--src-ref` exports `src` with `git archive` (srcref.py:16-31): the after-arm needs the
  branch **committed**; `worktree` means fs-eval's own src, not the branch.

### Over-built / missing

- Not over-built: one pure function, two constants, a dump flag and a replay script.
- Missing: the sequencing. No existing `results/*.json` holds rows (run_ask_v2.py:100-125
  stores call args and judge summaries only), so the §6.1 replay needs a live run first.
  The baseline run at 5608895a with `--keep-rows` **is** the §6.2 before arm; the ladder is
  offline build → [gated] before-run with rows → offline replay fixes W → [gated] after-run.
  Two live steps, both owner-run, not one.
- Missing: the replay must load two `ask_tools` versions whose lazy imports
  (`ask_tools_log`, `query_slots`, `lambda_ask_agent`) resolve from `sys.path`. Run one
  subprocess per src path (reuse `harness.prepare_imports`) and diff JSON, instead of
  importlib renaming in one process.
- Missing: reuse `funnel._patterns` and its `hit` rule by import in the replay; do not
  re-spell the regex rule.

## Decisions Q1-Q6, reviewed

- Q1 (W = 1,000, replay decides 900/1,000/1,200): agree, with SHOULD 2's gate and ladder.
  The orchestration line's 1.2-1.5k is refuted by §2 (no gain above 1,000) and §3 (31k of
  32k at 1,200 with three calls).
- Q2 (12,500): agree in direction; derive the number (SHOULD 1); keep the `source`-free
  measure as the fallback if it lands high.
- Q3 (no stemming): agree; NIT 2 supports it.
- Q4 (repeated-ref re-render is theirs): agree. Note it in the PR for the orchestration line.
- Q5 (documents now): agree; the paragraph unit is trivial and BLOCKER 1(b) covers the
  long-paragraph case. No gold covers it: say "untested on gold" in the PR.
- Q6 (eval pieces in fs-eval worktree): agree; four rename sites, not one.
