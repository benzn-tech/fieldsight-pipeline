# The hand-off reads like minutes a person would send

Status: design, 2026-09-17. Two repos. This is the primary document (extraction + brief);
the frontend half is `fieldsight-ui/docs/specs/2026-09-17-handoff-table.md`.

## 0. What the user rejected, in their words

The "Preview & copy" hand-off produces this:

```
Rainwater tank install Old Smith (11:01 – 11:02)
* Rainwater tank front position -- discuss with Paul Smith
```

> 你认为邮件或者会议纪要会这么写吗？收件人完全不知道我们在讲什么。

Their own minutes read like this — every line carries context, an owner and a date:

```
Arborist report catching the cut and fill for link bridge. IA and Civix to catch up.
Cut and Fill plan still in Dev Design … — JV, 2-Sept-26
Modular drop ceiling 100mm to send details to Ignite — SH, 15-Sept-26
```

## 1. Decisions (user, 2026-09-17)

| Decision | |
|---|---|
| The `why` field | **Dropped.** Not a separate line. |
| The `basis` field | **Folded into the sentence, with the person's name**: "Ben confirmed QA pour checks start once the cure ends", "Ben is going to the Icehouse office today to meet KCD Constructions". Never a `committed` / `inferred` label in the output. |
| `"keep the whole thing to a handful of words (aim <= ~8)"` | **Rejected.** The prompt structure is reworked (§3). |
| A missing assignee or date | **Left blank. Never invented.** "有就有，没有就没有。" |
| Output shape | A table with a header row: `| AGENDA ITEM | ASSIGNED | DUE DATE |`, blanks blank. |
| Rollout | Verified on TEST with a real run before anything is pushed. |

The purpose the user stated, which decides every ambiguous case: **the reader pastes the
table into an email and can reconstruct the rest themselves.** A line succeeds when the
person who was there recognises what it was about. It fails when it needs the recording.

## 2. Why this is not only a rendering change

Measured, not assumed (2026-09-17):

* **The per-item context sentence does not exist anywhere.** `lambda_extract_session.py:1051`
  instructs: *"keep the whole thing to a handful of words (aim <= ~8) … cut rationale/filler
  ('in order to…', 'to discuss…')"*, and the action-item schema (`:922-928`) has exactly four
  fields — `action`, `responsible`, `deadline`, `priority`. There is no field a context
  sentence could land in. The `--` in the user's example is this prompt's house style.
* **Two prompts in this repo contradict each other.** `session_brief.py:107-108` names
  `'Procurement strategy -- productize as standard IT'` as a **failure** ("the reader cannot
  tell what to do with it") — the exact register `lambda_extract_session.py` holds up as
  **good**. This spec resolves the contradiction in favour of the brief.
* **The brief prompt cannot name anyone.** `build_brief_prompt(turns)` (`session_brief.py:50`)
  receives only `[HH:MM:SS] speaker: text` lines, and the prompt states speaker labels are not
  names (`:86`). Nothing passes the recording's owner in. "Ben confirmed…" is therefore
  unproducible today — §3.2 is what makes it possible.
* **The brief is real and runs.** TEST `SESSION_BRIEF=true` (prod `false`), 10 artifacts in
  `fieldsight-data-test-509194952652`, the most recent 17 KB with 4 sections, 9 bullets,
  16 entities and 6 tasks. It is served by `GET /api/org/sessions/{id}/brief` and no client
  has ever called it (`lambda_org_api.py:2368`).

## 3. Backend changes

### 3.1 The action item gets a sentence (`lambda_extract_session.py`)

Replace the ≤8-word rule and the "cut rationale" instruction. The action text becomes **one
or two short clauses a reader who was in the room can act on**, carrying the subject, what is to happen,
and the context that makes it make sense. The upper bound is the half that does the work:
this is not licence for a paragraph. (Corrected 2026-09-17: this line originally said "one sentence",
which contradicted the register sample quoted in §0 — it is two grammatical sentences. The
upper bound is the half that matters.)

* Keep `responsible` / `deadline` as their own fields and keep **"do NOT guess"** — the user
  explicitly accepts blanks.
* The name goes in the sentence **only when the transcript supports it** (§3.2).
* Retire the "Good:" example `"Damaged doors -- replace, floors 1-3, PK building"` and replace
  it with worked examples in the target register, plus a **bad** example in the old telegraphic
  style so the contrast is explicit.
* `decisions` (now kept, #861) are a **separate list** — they are not tasks and must not be
  rewritten into imperatives.

### 3.2 The brief learns who was recording (`session_brief.py`, `lambda_session_finalize.py`)

`brief_from_turns` / `build_brief_prompt` take the recording owner's display name, derived
from the artifact's `folder` (`lambda_session_finalize.py:167`, e.g. `Ben_Lin` → `Ben Lin`).
The prompt then says, in substance:

> This recording belongs to **{owner}**. When a turn is plainly {owner} speaking about what
> they will do, write the sentence with their name as the subject: "Ben confirmed QA pour
> checks start once the cure ends." When you cannot tell who spoke, write the sentence without
> a name and never attribute it to {owner}.

**The constraint that makes this safe:** the transcript's speaker labels are not names. On a
solo recording the owner is the only voice, so naming them is sound. In a meeting it is not,
and a wrong attribution ("Ben committed to X" when Paul did) is worse than no name. The prompt
must be given the owner *and* the instruction not to use it when the speaker is unclear.

### 3.3 `why` and `basis` leave the output

`why` is dropped from the task object. `basis` stops being an output label: it survives only
as the thing that decides the sentence's verb — a commitment reads "Ben confirmed…" /
"Ben will…", an inference reads "…to be confirmed" or carries no name. The task object becomes
`{text, at, assignee, due}`.

## 4. What the frontend gets

`GET /api/org/sessions/{id}/brief` already serves the whole artifact. **Preview & copy is
rendered per DAY** (`timeline.js:2949`, `:2977`) while briefs are written **per session**, so
a day with several recordings needs its briefs merged into one table, ordered by `at`.

Fallback is required, not optional: prod has `SESSION_BRIEF=false` and no artifacts, and no
historical day has one. With no brief the table is built from `action_items` as today — the
same three columns, just thinner text. The frontend must never show an empty table where the
old output had rows.

## 5. Verification (TEST, before anything is pushed — user's condition)

1. Re-run a real session through `fieldsight-test-session-finalize` and read the stored
   `latest.json`: tasks are sentences, no `why`, no `basis`, the owner's name appears only
   where the transcript supports it.
2. Re-run an extraction and confirm action items are sentences, `responsible`/`deadline` still
   blank when unstated, and `decisions` unchanged in shape.
3. Paste the rendered table into a mail client and read it as the recipient: can someone who
   was in the room reconstruct each line without the recording?
4. Confirm the count of items did not drop — a prompt that writes longer text must not write
   fewer items. Compare item counts before and after on the same session.

## 6. Non-goals

Speaker identification; inventing assignees or dates; changing `action_items`' four-field
schema beyond the text register; touching the nightly report generator or meeting-minutes
templates (`config/prompt_templates*.json`), which are a different path.
