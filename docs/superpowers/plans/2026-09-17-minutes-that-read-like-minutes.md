# The hand-off reads like minutes a person would send — implementation plan

> **For agentic workers:** REQUIRED SUB-SKILL: use superpowers:subagent-driven-development or
> superpowers:executing-plans. Steps use checkbox (`- [ ]`) syntax.

**Goal:** the day's hand-off is a table a recipient can read without the recording —
`| AGENDA ITEM | ASSIGNED | DUE DATE |`, each line a sentence carrying its own context,
blanks left blank.

**Spec:** `docs/superpowers/specs/2026-09-17-minutes-that-read-like-minutes.md`.

**Two repos:**
- Backend: `C:/Users/camil/Dropbox/wt-pipe-minutes`, branch `feat/minutes-that-read-like-minutes`,
  off `develop` `a73a479`, currently at `9b98fee` (the spec commit). Clean.
- Frontend: `C:/Users/camil/Dropbox/wt-ui-minutes`, branch `feat/handoff-table`, off `dev`
  `e05899b`. Clean.

Run every command from the worktree root. Never `cd` to the original checkout.

## Controller rulings (2026-09-17) — these override the planner where they differ

1. **B4 Step 0 is decided: outcome (a).** The `<= ~8 words` count dies; **"lead with the subject"
   survives**, so the first few words still identify the item where the UI clips it. `action_items`
   feed Today cards, the Tasks list and Timeline rows; turning every card title into a long sentence
   would trade a readable email for an unscannable board. Do **not** delete `:1047-1049` wholesale.
2. **The `why` line is dropped from the stop-recording confirmation email too** (user, 2026-09-17).
   B1 removes shipped behaviour and deletes its test on purpose; the commit message must say so.
3. **There is no separate frontend spec.** The F tasks are driven by the backend spec §4 plus the
   code. Fix the citation in §0 of the spec rather than writing a second document.
4. **Line numbers**: the planner's supersede the spec's — `lambda_session_finalize.py` `folder` at
   `:215`, `summarize(turns)` at `:231`; `lambda_org_api.py` `session_brief_read` `:2728-2799`,
   route `:717-719`.

## Global constraints

- **One commit per task. The tree is green after every task.** Backend:
  `python -m pytest tests/unit -q`. Frontend: `node --test "tests/*.test.js"` plus `node --check` on
  every modified `.js`.
- **Nothing merges to `main` and nothing deploys to prod.** Merging to `develop` deploys TEST; only
  the real-run tasks do that, and only with the owner's explicit approval.
- Stage by path, never `git add -A`; never a bare `git stash`. English only. Commit trailers:
  ```
  Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>
  Claude-Session: https://claude.ai/code/session_012vJF6RLXaUEWuai8RrFknd
  ```
- **Do not undo #861 (`a73a479`).** `decisions` have their own schema
  (`lambda_extract_session.py:939-945`), instruction (`:1081-1082`), empty-array rule (`:1104`) and
  payload narrowing (`lambda_org_api.py:6638`). B4 edits instruction item 2; item 5 is a different
  hunk in the same function — check the diff before committing.
- **`SESSION_BRIEF` on TEST is not assumed.** `deploy.yml:248` passes
  `${{ vars.TEST_ENABLE_SESSION_BRIEF || 'false' }}`. Read the value off the **deployed** function:
  `aws lambda get-function-configuration --function-name fieldsight-test-session-finalize
  --query 'Environment.Variables.SESSION_BRIEF' --region ap-southeast-2`.
- `MSYS_NO_PATHCONV=1` for any AWS CLI call carrying a `/`-prefixed argument (BUG-42).

## The evidence rule for prompt tasks — read before B3 or B4

**"The instruction is present in the prompt" is NOT evidence the model obeyed it.**

`tests/unit/test_extraction_participants_prompt.py` says so itself: *"This test cannot check a
model's output. It checks that the constraint is still in the prompt, because the failure mode is
someone tidying the wording away and nothing going red."* CLAUDE.md states the general form: **a log
line is evidence about the log line.**

B3 and B4 each ship two things, and the second is not optional:
1. a prompt test, whose only job is to stop a future edit deleting the wording; and
2. **a real run on TEST, N ≥ 2 on the same session, whose output a person reads.**

N > 1 because this repo has measured its own run-to-run variance: *"93.5% of the word-level change
attributed to keyterms turned out to be run-to-run jitter, and only a fourth run revealed it."*
A single good sample is a coin landing heads.

---

## Task B0 — Baseline on TEST (NO COMMIT)

Must run **before** B3/B4 touch any wording. "The item count did not drop" is unprovable afterwards.

- [ ] Confirm the deployed `SESSION_BRIEF` value. If `false`, stop and have the owner set
      `TEST_ENABLE_SESSION_BRIEF=true` and redeploy — there is no brief to compare.
- [ ] Choose **two** real TEST sessions, deliberately different: one solo site walk, one multi-voice
      meeting. The meeting is where attribution (§3.2) can go wrong; the solo one is where naming the
      owner is sound.
- [ ] Archive current artifacts from `fieldsight-data-test-509194952652` into the scratchpad (**not**
      the repo): `extractions/{folder}/{date}/{session_base}.json` and
      `session_brief/{folder}/{date}/sid{session_id}/latest.json`.
- [ ] Record per session: `len(action_items)` summed across topics; `len(tasks)` in the brief; how
      many items have a non-null `responsible` and how many a non-null `deadline`; and the verbatim
      text of every item.
- [ ] `scripts/compare_extraction_vs_brief.py {folder} {date} --env test --json` already produces the
      per-session item-set diff — use it rather than writing a new one.

**Pass:** two baselines recorded, each with an item count and assignee/date fill. Nothing to make green.

---

## Task B1 — The code stops carrying `why` and `basis`

**Files:** `src/session_brief.py`, `src/lambda_session_finalize.py`, `src/lambda_org_api.py`
(docstring only), `tests/unit/test_session_brief.py`, `tests/unit/test_session_finalize.py`.

Pure code, fully unit-provable. The prompt still asks for `why`/`basis` after this task — the code
simply stops reading them; B3 removes the ask.

**Tests first**, each naming the mutation that must turn it red:

- [ ] `test_a_todo_carries_only_text_responsible_due_and_at` — `to_session_summary` emits no `why`.
      **Mutation:** restore `"why"` at `session_brief.py:342-347` → red.
- [ ] `test_a_task_is_anchored_from_its_own_sentence` — `reanchor` still corrects `at` with no `why`.
      `session_brief.py:292` currently anchors on `f"{task['text']} {task['why']}"`; the spec is silent
      about this. **Mutation:** feed `_snap_to_terms` an empty `text` → red.
- [ ] `test_the_email_renders_no_context_line_under_an_item` — both halves of
      `build_confirmation_email`. **Mutation:** restore the `why` line at
      `lambda_session_finalize.py:110-111` → red.
- [ ] Delete `test_the_reason_a_todo_exists_reaches_both_halves_of_the_email`
      (`tests/unit/test_session_finalize.py:241-268`). It pins the behaviour being removed. Deleting a
      test is a decision: the commit message must say §3.3 retires the field and the email loses its
      context line as a direct consequence (ruling 2).
- [ ] Keep `test_a_todo_without_a_reason_renders_exactly_as_before` — it now describes every to-do.

**Implementation:**

- [ ] `session_brief.py:342-347` — emit `{text, responsible, due, at}`.
- [ ] `session_brief.py:292` — anchor from `task.get('text','')` alone.
- [ ] `lambda_session_finalize.py:66` — drop `"why"` from `_clean_todos`; keep `at`.
- [ ] `lambda_session_finalize.py:105-111` and `:127-133` — remove the context line from the text
      renderer and the `<div>` from the HTML row. Leave the `—` blanks at `:139-142` alone: "blanks
      blank" is §4, about the frontend table, and does not reach this email.
- [ ] `lambda_org_api.py:2745` — the docstring calling `why` *"the field the to-do list was missing"*
      becomes false in this commit. Fix it. **Comment only.**

**Verify:** `python -m pytest tests/unit -q`, then replay each deleted behaviour by hand and watch it
go red (CLAUDE.md: *replay the actual defect against the new test*).

---

## Task B2 — The owner's name reaches the brief prompt

**Files:** `src/session_brief.py`, `src/lambda_session_finalize.py`, and their unit tests.

Plumbing only. **No wording change here** — that is B3. Splitting them is what makes B3's real run
interpretable: if the name is wrong, this task is where it broke.

Today `build_brief_prompt(turns)` (`session_brief.py:50`) receives only `[HH:MM:SS] speaker: text`
and the prompt states at `:86` that speaker labels are not names, so "Ben confirmed…" is
unproducible. `folder` is in hand at `lambda_session_finalize.py:215`.

**Tests first:**

- [ ] `test_the_owner_name_is_derived_from_the_folder` — `Ben_Lin` → `Ben Lin`; `Ben_UCPK2` →
      `Ben UCPK2`; `""` → `None`. **Mutation:** return the folder unchanged → red.
- [ ] `test_the_prompt_names_the_owner_when_one_is_known` — `build_brief_prompt(turns,
      owner_name="Ben Lin")` contains `Ben Lin`. **Mutation:** drop it from the f-string → red.
- [ ] `test_the_prompt_says_nothing_about_an_owner_when_none_is_known` — no owner clause, no `None`,
      no `{owner}` placeholder. **Mutation:** always emit the clause → red.
- [ ] `test_the_rolling_summariser_is_still_called_with_turns_alone` — with `SESSION_BRIEF` off,
      `_complete_summary` calls `summarize_turns(turns)` unchanged. **Mutation:** pass `owner_name`
      down the rolling branch → red.
- [ ] `test_the_brief_is_called_with_the_folders_display_name` — with `SESSION_BRIEF` on and
      `folder="Ben_Lin"`, the injected summariser observes `owner_name == "Ben Lin"`.
      **Mutation:** pass `folder` raw → red.

**Implementation:**

- [ ] `session_brief.py:50` → `build_brief_prompt(turns, owner_name=None)`.
- [ ] `session_brief.py:351` → `brief_from_turns(turns, call_llm=None, owner_name=None)`, forwarded at
      `:371`. The keyword-only default keeps it a drop-in for `summarize_turns` — the contract this
      module was built to honour (module docstring `:11-13`).
- [ ] `lambda_session_finalize.py` — add `_display_name(folder)`; bind it into the brief branch only
      (`:225-227`). The call at `:231` stays `summarize(turns)`; the rolling branch is untouched.

---

## Task B3 — The brief prompt writes minutes  ⚠ PROMPT TASK

**Files:** `src/session_brief.py` (prompt text only), `tests/unit/test_session_brief_prompt.py` (new).

**Step 0 — the register is already decided here.** `session_brief.py:107-108` names
`'Procurement strategy -- productize as standard IT'` a **failure** — the exact style
`lambda_extract_session.py:1055-1056` holds up as **good**. The spec resolves this in favour of the
brief. Rule 2 stays as written; do not "improve" it.

**Prompt tests** (their only claim is that the wording still exists):

- [ ] `test_the_task_object_has_no_why_and_no_basis`. **Mutation:** restore either at `:84`/`:88` → red.
- [ ] `test_the_prompt_forbids_attributing_to_the_owner_when_the_speaker_is_unclear` — asserts the
      **negative** half verbatim, because that is the half the model gets wrong.
      **Mutation:** delete the "when you cannot tell who spoke" sentence → red.
- [ ] `test_the_telegraphic_style_is_still_called_a_failure` — rule 2 survives.
      **Mutation:** delete rule 2 → red.

**Implementation (`:81-89` and the "How to write it" list):**

- [ ] Task object becomes `{text, at, assignee, due}`. Keep `assignee`'s "speaker labels are NOT
      names … do not guess" wording at `:86` verbatim — it is load-bearing and `_real_name` (`:306`)
      backs it.
- [ ] `text`: one sentence a reader who was in the room can act on, carrying the subject, what is to
      happen, and the context that makes it make sense.
- [ ] `basis` survives only as the rule that picks the verb: a commitment reads "Ben confirmed…" /
      "Ben will…"; an inference reads "…to be confirmed" or carries no name. **Never a
      `committed`/`inferred` label in the output.**
- [ ] The owner clause, only when `owner_name` is set: this recording belongs to `{owner}`; when a
      turn is plainly `{owner}` speaking about what they will do, use their name as the subject;
      **when you cannot tell who spoke, write the sentence without a name and never attribute it to
      `{owner}`.**

**Step N — REAL RUN ON TEST. The prompt test above is not evidence of any of this.**

- [ ] Deploy to TEST (owner approval required). Confirm the deploy carries the change — `gh run list`
      right after a merge returns the run for the PREVIOUS commit; match on `headSha` (BUG-22).
- [ ] **Read `process_finalize_request` first** (`lambda_session_finalize.py:330-400`): `_already_sent`
      (`:165`) can short-circuit before `_complete_summary` (`:390`), and you would read a stale
      artifact believing you triggered a fresh one. Pick a session whose
      `session_finalize_results/{id}.json` is absent, or clear it.
- [ ] Invoke `fieldsight-test-session-finalize` for **both** B0 sessions, **twice each**, keeping
      every artifact (`session_brief/{folder}/{date}/sid{session_id}/latest.json`, `:250`).
- [ ] Read `tasks[].text`, `tasks[].assignee`, `tasks[].due`; confirm no `why`/`basis` key anywhere.

**Pass requires all of:**
1. every `tasks[].text` is a sentence naming its own subject — a reader who was in the room can tell
   what it is about without the recording;
2. on the **solo** session, where the owner's name appears it is the owner, and it reads naturally;
3. on the **meeting** session, **zero** commitments attributed to the recording owner where the
   transcript does not plainly show them saying it. **One wrong attribution is a FAIL** — the spec:
   *a wrong attribution is worse than no name*;
4. no `committed`/`inferred` label leaks into any `text`;
5. **`len(tasks)` is not lower than B0's**, either session, either run.

**Fail** on: a telegraphic `--` line surviving; an invented assignee or date (blanks must stay blank);
a count drop; an owner name on an unclear speaker. If the two runs disagree on any of the five, that
is an unstable prompt, not a pass with noise — run a third; if it still disagrees, the wording is the
defect.

---

## Task B4 — The extraction prompt writes a sentence  ⚠ PROMPT TASK

**Files:** `src/lambda_extract_session.py` (`_instructions_block()` only),
`tests/unit/test_extraction_action_register_prompt.py` (new).

**Step 0 is already decided (ruling 1): outcome (a).** Keep the lead-with-the-subject requirement at
`:1047-1049` and the truncation rationale; delete only the word count. Record this in the commit
message.

**Blast radius the spec does not mention:** `_instructions_block()` (`:984`) is reused **verbatim** by
`build_group_prompt` (`:1593`) as well as `build_extraction_prompt` (`:1148`), so this changes merged
multi-device reports too. Intended — one contract, two callers (`:988-991`).

**Prompt tests:**

- [ ] `test_the_action_is_one_sentence_not_eight_words` — no `<= ~8`, no "handful of words".
      **Mutation:** restore the clause at `:1050-1051` → red.
- [ ] `test_the_telegraphic_example_is_gone` — `"Damaged doors -- replace, floors 1-3, PK building"`
      (`:1056`) absent. **Mutation:** restore it → red.
- [ ] `test_a_bad_example_in_the_old_style_is_present`. **Mutation:** delete it → red.
- [ ] `test_the_subject_still_comes_first` — the lead-with-the-subject wording survives (ruling 1).
      **Mutation:** delete `:1047-1049` → red.
- [ ] `test_responsible_and_deadline_are_still_never_guessed` — `:1054` survives.
- [ ] `test_decisions_are_still_a_separate_list` — item 5 (`:1081-1082`) and the empty-array rule
      (`:1104`) untouched. This test exists so this task cannot quietly undo #861.

**Implementation, `:1047-1060` only:**

- [ ] Keep `:1040-1046` **exactly** — "FIRST decide whether there is an action at all", the verb test,
      and "a discussion that reached no act produces NO action_items". That paragraph holds the count
      up, and B4's largest risk is a longer-text instruction quietly suppressing items.
- [ ] Replace the word-count clause with: one sentence a reader who was in the room can act on,
      carrying the subject, what is to happen, and the context that makes it make sense. **One
      sentence — not licence for a paragraph.** Keep "lead with the subject".
- [ ] Keep `responsible`/`deadline` in their own fields, keep "do NOT guess", keep "never a vague
      placeholder".
- [ ] Replace the Good examples with two in the target register (the user's own minutes are the model:
      *"Arborist report catching the cut and fill for link bridge. IA and Civix to catch up."*) and
      **add a Bad example in the old telegraphic style**.
- [ ] Do not touch the four-field schema at `:922-928` (§6 non-goal) or `:1081-1082`.

**Step N — REAL RUN ON TEST.**

- [ ] Deploy and confirm the deployed code carries the change (BUG-22).
- [ ] `lambda_handler` dispatches on the S3 key: `extraction_requests/` runs the **final** pass
      (thinking on, authoritative); anything else runs the **live** pass. Judge the register on a
      **final** pass. Invoke `fieldsight-test-extract-session` directly with a synthetic `Records`
      event rather than re-copying a transcript, which would re-drive the whole chain (BUG-13/BUG-43).
- [ ] **Twice per session**, both B0 sessions. Output overwrites
      `extractions/{folder}/{date}/{session_base}.json` — copy each run out before the next.

**Pass requires all of:** every `action` is a sentence carrying its own context; **item count ≥ B0's**
per topic and in total; `responsible`/`deadline` still null wherever B0 had them null; `decisions`
unchanged in shape and not rewritten into imperatives; the first few words of each `action` still
identify it. **Fail** on a count drop, an invented assignee/date, a decision turned into a task, or a
paragraph where a sentence was asked for.

---

## Task F1 — `org.getSessionBrief`

**Files:** `scripts/api/org.js`, `tests/org-session-brief.test.js` (new), `app-shell-preview.html`
(cache-buster only).

`GET /api/org/sessions/{id}/brief?date=&user=` exists and **no client has ever called it**. It returns
the stored artifact **whole** (`lambda_org_api.py:2796-2798`) with `status` ∈ `ready|pending|removed`.
**No backend change is needed.**

**The load-order trap, sidestepped rather than fought:** `scripts/api/index.js:87` assigns
`window.FS.api = { … }` wholesale, so anything registering onto `FS.api` before that line is silently
wiped. `api/org.js` already loads after it. Put the function there — **no new script tag**.

**Tests first** (source-level, the style `tests/a-control-that-vanishes-reads-as-removed.test.js` uses):

- [ ] `getSessionBrief is registered on FS.api.org` — the name appears inside the export block at
      `:851`. **Mutation:** define it but leave it out of the block → red (the exact defect the
      wholesale-assign trap produces, and it throws no error at load).
- [ ] `getSessionBrief is gated on the same predicate as getSessions`.
- [ ] `org.js still loads after api/index.js` — assert tag order in `app-shell-preview.html`.
- [ ] `the mock serves a brief for a fixture session rather than an empty one` — an empty read stub is
      a claim that the feature is finished and the data is absent; this repo has paid for that four
      times (CLAUDE.md). **Mutation:** return `{tasks: []}` → red.

**Implementation:** `getSessionBrief(opts)` → `orgRequest('/sessions/{id}/brief', {params:{date,user}})`
behind the live gate; otherwise a mock derived from the day's fixture. Callers treat
`status !== 'ready'` and any `_accessDenied`/`_notFound` envelope as **no brief**, never an error.
Bump `api/org.js?v=24` → `?v=25`.

---

## Task F2 — `buildPreviewModel` learns the brief

**Files:** `scripts/composites/email-preview-modal.js` (`buildPreviewModel` only),
`tests/email-preview-modal.test.js`.

**Tests first:**

- [ ] `with no brief the table is built from action_items exactly as today` — **prod has
      `SESSION_BRIEF=false` and no artifacts; the fallback is the normal case.**
      **Mutation:** make the brief path unconditional → red.
- [ ] `a day with two sessions produces ONE table carrying both briefs` — Preview & copy is per DAY,
      briefs are per SESSION.
- [ ] `rows are ordered by at, and two sessions sharing a clock time keep a stable order` — `at` is
      `HH:MM:SS` with no date and no session component. Order on `(at, sessionIndex, taskIndex)`.
- [ ] `the fallback path has no at and falls back to the topic time_range`.
- [ ] `the row count never drops below the action_items count for the same day`.
- [ ] `a done item is still excluded and a topic with nothing open is still dropped`.

**Implementation:** `buildPreviewModel(opts)` accepts `opts.briefs` = `[{sessionId, brief}]` in session
order; rows come from `tasks[]` when present, else from `action_items` exactly as today. Blank stays
blank — no `—`, no "Unassigned", no invented date. Keep `groups`/`photos` intact; add `rows` alongside.
**No cache-buster here** — F3 edits the same file and carries the bump.

---

## Task F3 — The renderers emit the three-column table

**Files:** `scripts/composites/email-preview-modal.js` (`actionLine`, `renderEmailHtml`,
`renderEmailText`), its test, `app-shell-preview.html`, `components-preview.html`.

**Tests first:** a header row of `AGENDA ITEM / ASSIGNED / DUE DATE`; the plain-text half is a
readable table too (every client picks the richest flavour it supports); an empty assignee renders as
an **empty cell, not a dash**; the sentence is never folded together with its owner and date (the old
`actionLine` join is exactly the register §1 rejects); nothing truncates.

**Implementation:** `renderEmailHtml` emits a `<table>` with the header row, photos still inside their
topic block; `renderEmailText` emits the same three columns as a pipe table. Keep `actionLine`
exported (other callers use it) but stop using it for table rows. Bump
`email-preview-modal.js?v=7` → `?v=8` in **both** HTMLs.

---

## Task F4 — timeline.js fetches and passes the day's briefs

**Files:** `scripts/pages/timeline.js`, `tests/timeline-handoff-briefs.test.js` (new),
`app-shell-preview.html` (cache-buster only).

Two mount sites, and **both** must be fed or the feature is half-shipped: the aggregated per-person day
section and the single-person view. `PreviewEmailButton` forwards props to the modal; add `briefs`.
`org.getSessionsCached(date, folder)` already exists so a day's sessions are fetched once and shared.

**Tests first:** both mounts pass `briefs` (**mutation:** feed only one → red — the shape of a real
defect this repo already hit, a link wired to one of three mounts); a failed brief fetch renders the
`action_items` table, **never an empty one**; briefs are fetched per session for the scoped day; a
report-scoped topic with no `session_id` still reaches the table.

Bump `pages/timeline.js?v=68` → `?v=69`.

---

## Task V — Acceptance (NO COMMIT)

Judgements no test can make.

- [ ] **Read a real table as the recipient**, in one HTML client and one plain-text client, for a day
      with **several sessions**. *Can someone who was in the room reconstruct each line without the
      recording?* That decides every ambiguous case.
- [ ] **Line by line: does each line carry its own context?** Rejected:
      `Rainwater tank front position -- discuss with Paul Smith`. Target:
      `Modular drop ceiling 100mm to send details to Ignite`.
- [ ] **Is any commitment attributed to the wrong person?** Check the meeting session against what the
      owner remembers. **One wrong attribution fails the feature**, not the row.
- [ ] **Does a blank read as absent rather than broken?**
- [ ] **Does it still look like minutes at length?** Twenty rows of sentences is a different object
      from four.
- [ ] **Confirm on prod-shaped data** that the fallback renders the old rows.

**Only when all six pass** does this go to the owner for the push decision.

## Known risks, stated plainly

1. **A prompt that writes longer text writes fewer items.** Guarded three times (B3 rule 5, B4 rule 2,
   F2's row-count test) because the output looks better while quietly saying less.
2. **A wrong owner attribution has no code-side backstop.** `_real_name` exists because the prompt
   could not be trusted about `assignee`; a name inside `text` has no equivalent. The prompt is the
   only guard — which is why B3 is judged on real output and one error fails it.
3. **#861 landed one commit ago** and B4 edits the same function. Diff before committing.
