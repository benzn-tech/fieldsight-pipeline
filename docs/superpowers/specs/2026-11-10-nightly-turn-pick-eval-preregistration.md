# Nightly turn pick (J2) measurement: pre-registration

Date: 2026-11-10. Spec: `2026-11-10-nightly-rematch-agent-design.md` §3 J2 and §7 (phase N0).
Plan: `docs/superpowers/plans/2026-11-10-nightly-rematch-agent.md` Task 2.
Code under test: `src/nightly_turn_pick.py` (Task 1). Harness: `scripts/nightly_turn_pick_eval/`.
Scoring functions reused from Track C: `scripts/item_support_eval/score.py`.

This document is written and committed, on its own, before any harness output exists.
- **Freeze point:** it is frozen at the **first fit result row**. After that, only the wording of
  `nightly_turn_pick.INSTRUCTION` may change, and only as §5 allows.
- **Hashes:** every run records the sha256 of `src/nightly_turn_pick.py`, `src/item_support.py`,
  `src/item_text_match.py`, `labels/done.agent.jsonl` and `data/manifest.json`. A held-out run
  refuses to start when any of them other than `nightly_turn_pick.py` differs from the last fit
  run. `nightly_turn_pick.py` may differ only in `INSTRUCTION`; the harness checks this by
  comparing `PROMPT_HASH` with `WORDING.md`.
- **Later changes** go in a dated addendum at the end, and the addendum says whether it was written
  before or after the result it might affect.

## 1. What is measured

For each final item (action, finding, decision, question) of a session, one model call per session
picks the single transcript turn, inside the item's topic `time_range`, that says the item, or null.
Code guards then accept, reject or keep the abstention (§4). An accepted pick is the link J2 would
write as `event_links.supported_by` with `method='nightly_turn_pick_v1'`. The measurement decides
whether J2 may run `on` anywhere, and whether it may write cross-language links.

## 2. Set, split, gold (all from Track C, unchanged)

- **Set:** Track C's TEST set, built 2026-11-09 by `scripts.item_support_eval.build`: 59 eligible
  final sessions with evidence, since 2026-08-01 (Track C amendment A12). Manifest digest
  `38e50e20da1202fccdd29a9a819e08c51e6b644a514d513f380cb8b861eec2e3`. It lives only in the
  gitignored `C:\Users\camil\wt\track-c-eval\scripts\item_support_eval\data` on the machine that
  built it.
- **Split:** Track C's seeded split: fit 30 sessions / 306 items, held-out 29 sessions / 252 items.
- **Item reference:** Track C's `store.iter_items` / `store.item_ref` (session ref, topic index,
  list name, child index). Before any call, the harness asserts that the refs it builds equal the
  gold refs for each split.
- **Gold:** Track C's blind agent labels, `labels/done.agent.jsonl`, read with Track C's
  `gold_by_ref`. The file has 569 rows for 558 unique refs; 11 refs repeat and 4 of those conflict.
  `gold_by_ref` keeps the last row, which gives turn 489 / several 69 / none 0, the same counts as
  Track C's run notes. Owner answers, when they exist, override the agent's through Track C's
  `merge_gold` (adjudication, else the owner's blind answer, else the agent's).
- **Correct link:** Track C's `is_link_correct`.
  - The linked turn is the gold turn or one of the `several` turns; or
  - it is the turn just before or after one of them, with the same speaker, and holds every item
    token the gold turn holds.
  - A link on an item whose gold is `none` is wrong.

## 3. Model and runs

- **Model:** `claude-sonnet-5-5`, through the Claude API (`ANTHROPIC_API_KEY`, the `anthropic` SDK),
  never OpenRouter. This is the model the registry will deploy.
- **Parameters:**
  - No sampling parameters. This model rejects a non-default `temperature` with a 400, so the
    spec's "temperature 0" cannot be sent.
  - Thinking is adaptive (the model default). `output_config.effort = "high"`, stated explicitly.
  - `max_tokens = 16000`. The SDK's `max_retries = 2`. Both are recorded in `meta.json`.
- **No server-side fallbacks.** The spec's §4.2 registry keeps a fallback chain. The verdict here
  covers this model only, and a fallback model's picks are not covered by it.
- **Failed call:** a call that ends in `refusal` or `max_tokens`, whose answer fails
  `parse_answer`, or that errors after the SDK's retries. Its items count as not linked (for
  coverage) and are reported by count. They are not `abstained`.
- **Prompt:** `nightly_turn_pick.INSTRUCTION`, hashed into `PROMPT_HASH`. Item text and turn text
  are fenced as quoted data. Every run records `PROMPT_HASH`.
- **Reps:** 3 per session per run.
- **Calls:** one per session per rep. A session with no eligible item (every item `no_window`) makes
  no call. `run.py --dry-run` prints the exact counts.
  - Expected: about 30×3 + 29×3 = 177, plus at most 2 fit wording revisions of 1 rep each (about 30
    calls each).
  - Ceiling: 237 calls.
  - The count goes to the owner first. No paid call is made before the owner approves it.
- **Crashes:** an interrupted run resumes only the (session, rep) pairs that have no recorded row.
  A recorded outcome, a failed call included, is never re-called. A run is never voided or re-run
  for its failure count.

## 4. Guards (Task 1), with the controller's rulings

The guards run in order, and the first failure names the drop. A null pick is `abstained`, and so
is an item missing from a parsed answer.

1. `shape`: the alias is known, and the turn is a number sent for that item's topic (turns are
   numbered per topic, T1.., over `item_support._window_turns`).
2. `window`: the turn lies inside the topic's `time_range` by `_window_turns` (start to end + 1 min).
3. `twin`. Two items in one request are a **twin pair** when:
   - both are in the same known script (zh–zh or en–en);
   - their place values (`item_text_match.place_values`) differ; and
   - their token sets are equal once place, digit and direction tokens are removed.

   The guard rejects:
   - (a) both items of a twin pair picking one turn, **whatever the turn's script** (both
     rejected);
   - (b) when item and turn are not cross-script: an item picking a turn that carries its twin's
     distinguishing place value;
   - (c) when item and turn are not cross-script: a pick where `item_text_match.twin_flags(item,
     turn)` is non-empty.
4. `same_language_floor` (item and turn not cross-script): `item_support._distinguishing_ok(item,
   turn)` holds, and every place value of the item is among the turn's place values. This applies
   whether or not a sibling item exists.

**Cross-script and unknown script.**
- A pick is **cross-script** only when both scripts are known and differ. It skips guards 3(b),
  3(c) and 4, is accepted, and is flagged `cross_language`.
- **Script** is decided by majority: CJK characters against Latin letters, in each text. Equal
  counts or no letters means unknown.
- An unknown-script pick is not cross-script. It gets every guard, and its count is reported.

**Language** (coverage strata only) follows Track C: an item with any CJK character is `zh`, else
`en`.

**Not linked without a call slot.** Items with no or unparsable `time_range` count as `no_window`.
Items beyond 60 in a session count as `over_cap`. Items whose text is empty or blank count as `no_text`.

The writer's `already_linked` recheck needs a database and is out of scope offline.

## 5. Fit (fit half only): prompt wording

J2 has no constants. The fit half fixes the wording of `INSTRUCTION` only.
- **Run v0:** 3 reps on the fit half, scored against fit gold with the same bars.
- **Revisions:** at most **2 wording revisions**. Each is logged in
  `scripts/nightly_turn_pick_eval/WORDING.md` with:
  - the new `PROMPT_HASH`;
  - the reason;
  - the fit numbers that prompted it.

  Each revision is then run on the fit half with 1 rep. A 1-rep revision is not judged on bar 4.
  Going back to an earlier wording counts as a revision.
- **Held-out wording:** the held-out half uses the last wording run on the fit half, whatever the
  fit numbers are. That wording is committed, and its hash logged, before the held-out run starts.
- The wording may not name sessions, sites, people or any text from the set.

## 6. Held-out bars

**Verdict order** is `fail` > `pending` > `insufficient` > `pass`; "worst" means the first in that
order. Bars 1–3 are judged **per rep**, and each bar's verdict is the worst rep. Reps are never
pooled: they are not independent, and pooling would triple n and fake a tighter bound.

1. **Link precision.** Compute wrong / accepted and its one-sided 95% Clopper–Pearson upper bound
   (Track C `cp_upper`).
   - Fewer than 59 accepted links → `insufficient` (59 is the smallest n at which 0 errors clears
     5%).
   - Otherwise, pass iff the bound ≤ 0.05.
   - An accepted link with no gold makes the bar `insufficient`.
2. **Cross-language stratum.** Accepted links flagged `cross_language`, with their own `cp_upper`.
   - n < 30 → `insufficient`.
   - n ≥ 30: pass iff the bound ≤ 0.05.

   Do not reuse Track C's `MIN_LINKS = 59` here. **Known cost, stated before the result:** with
   30–58 such links and 0 wrong, the bound is still above 5%, so the bar fails. The threshold is not
   moved.
3. **Twin hard negatives = 0.** Three counts over accepted links. All must be 0, except that (a)
   counts only owner-confirmed flags.
   - (a) **Mechanical:** `item_text_match.twin_flags(item, turn)` is non-empty. It is counted over
     **all** accepted links, cross-language ones included, since the guard does not check those.
     Each flag goes to the owner, who answers with a JSON boolean (Track C A14). An unanswered flag
     makes the bar `pending`. **Known cost:** `twin_flags` also fires on a correct turn that names
     both places, such as "3楼和4楼". That is why a flag is confirmed and does not fail the bar on
     its own.
   - (b) **By gold:** an accepted link of a twin-pair item that lands on a turn index that is
     exactly in the twin's gold turns (`turn` or `several`) and is not correct for the item itself
     by `is_link_correct`.
   - (c) **Pair:** both items of a twin pair accepted on the same turn. This should be 0 by
     construction, and is checked anyway.
4. **Rep agreement.** Counted over items that got a prompt slot (not `no_window`, `over_cap` or `no_text`).
   - An item agrees when its **raw model pick** is the same in all 3 reps: the same turn index, or
     null in all 3. This is before the guards.
   - A failed call is its own value: it agrees only with another failed call.
   - Pass iff the share of agreeing items ≥ 0.90.
5. **Agent–owner agreement.** Track C's `score_agreement` on the owner's blind sample ≥ 0.85.
   - The sample **is** Track C's `labels/todo.owner.jsonl` (45 held-out items, seed
     `item-support-owner-2026-11-09`), so the owner labels once for both lines.
   - The bar is `pending` until all 45 are answered.
   - The owner answers the 45 blind first. The blind view is the item and the topic's turns, with no
     agent label and no J2 output.
   - Then the harness exports, for owner decision, the items in that sample where an accepted J2
     link in any of the 3 reps is not correct against gold. The owner sees the item, the gold turn
     and the J2 turn, and decides. The owner always wins.

**Overall verdict.**
- If bar 2 is `pass`, the verdict is the worst of bars 1, 3, 4 and 5, and cross-language links may
  be written (`allow_cross_language = True`).
- If bar 2 is not `pass`, cross-language links are off. Bars 1, 3 and 4, and coverage, are judged
  again with cross-language accepted picks counted as not linked. The worst of those and bar 5 is
  the verdict.
- **Deviation from spec §7**, stated here: the spec lists bar 2 among "all must pass". Here a bar-2
  `fail` turns cross-language off instead of failing J2 as a whole. The spec already treats
  `insufficient` that way, and with cross-language off the failing stratum is never written.

**Scoring once.**
- The held-out run is made once and scored once. The first scoring uses agent gold, writes
  `summary.json`, and marks itself provisional.
- When the owner's 45 blind answers, the adjudication and the bar-3(a) flag answers all exist,
  **one** re-scoring with `--addendum <date>` is authorised here in advance. It uses gold =
  `merge_gold(agent, owner, adjudicated)`, and it is the final verdict.
- The harness refuses any other second scoring of a held-out run, and refuses to write into an
  existing run directory.

## 7. Reported, not gated (held-out half, J2's own result, not a Track C result)

- **Coverage** is accepted links / all final items, against the owner's 60% line. It is reported
  per rep (min and median):
  - overall;
  - per kind;
  - per language (`zh` / `en`);
  - for the mixed stratum (cross-language accepted links / items).
- **Counts:** outcomes per guard; `no_window`, `over_cap`, `no_text`, failed calls, unknown-script picks.
- **Lexical-unlinked split.** The lexical linker has no fitted constants, because Track C's fit had
  no feasible point.
  - "Left unlinked by the lexical linker" therefore means unlinked by
    `item_support.attach_support` at Track C's most permissive grid point: floor 0.30, margin 0.05,
    nothing_close 0.10, paths quote + window. Any grid point links a subset of what that point
    links.
  - It is computed on the held-out items.
  - Among those items: how many J2 links (per rep, min and median), split by cross-language and same
    language, and how many of those are correct.
- **Tokens** in and out per run, from `usage`.

## 8. Caveats stated before any result

- **Repeated recordings.** The same recordings recur under several session refs, some across the
  two halves (replayed scripts, sid copies). Of the 59 sessions, 37 come from one folder. So fit and
  held-out are not fully independent (Track C Task 10 notes).
- **Gold has 0 `none` labels.** The agent labeller found a supporting turn for every item. Bar 1 can
  therefore only see wrong-turn errors, and an abstention is never "right". Bar 5's owner sample is
  the check on this.
- **The gold view is wider than J2's.** The labeller saw the window widened by 2 minutes on each
  side, plus quote-cited turns. J2 sees `[start, end + 1 min)`.
  - When the gold turn lies outside J2's window, J2 either abstains (a coverage cost) or picks
    another in-window turn. That turn counts as wrong unless the adjacency rule saves it (a
    precision cost).
- **Numeral form.** `same_language_floor` compares characters, so a same-script pick where only the
  numeral form differs (item 三楼, turn 3楼) is rejected. This is a coverage cost, not a wrong link.
- **The prompt names each item's topic** (`I<n> (topic <k>) <kind>: <text>`), and each topic block
  has its own heading. Turn numbers restart per topic, so without the topic label the model could
  not tell which topic's T1 an item means.
- **Unknown aliases** in an answer are dropped and counted, and give no row.
- **Rebuilt turns.** Turns were rebuilt from the transcripts as they were on 2026-11-09. The
  labeller and J2 see the same rebuilt turns.
- **No writes.** The runs make no write to S3 or a database. All outputs stay in the gitignored
  `scripts/nightly_turn_pick_eval/runs/`. The only committed output is
  `scripts/nightly_turn_pick_eval/results/summary.json` (counts, rates, verdicts).

## Addenda

(none)
