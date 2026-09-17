# One hand-off table, two surfaces

Status: plan, 2026-09-18. Two repos: fieldsight-pipeline (confirmation email) and
fieldsight-ui ("Preview & copy"). Follows PR #864 / #866 (backend) and #315 (frontend).

## 0. Decisions (owner, 2026-09-18)

| Question | Decision |
|---|---|
| Sync the email and Preview & copy | **Yes — one table shape on both surfaces.** |
| Header | `AGENDA ITEM` / `ASSIGNED` / `DUE DATE` on both. The email's `Items / Assignee / Due` goes. |
| Blank cells | As the email already does: a **topic row** shows `N/A` in both cells; an **action row** missing an owner or a date shows `—`. |
| Topics with no action | Sink to the bottom as rows of their own: `<title> — <first sentence of summary>` (the #866 template). |
| Brief vs extraction for action rows | **If a brief exists, use it.** The row-count floor goes. Fall back to extraction action items only when there is no usable brief. |

The purpose that decides ambiguous cases: the recipient reads the table without the recording.

## 1. The shared row contract (both surfaces MUST produce exactly this)

1. **Columns:** `AGENDA ITEM | ASSIGNED | DUE DATE`.
2. **Order:** every action row, then every topic row. Nothing interleaved; no topic label rows.
3. **Action rows, source:** the session brief's `tasks[]` when a brief with **at least one task** is
   available (ordered by `at`, then original order); otherwise the extraction's action items
   (Preview & copy: still excluding items marked done).
   - A brief with zero tasks is treated as no brief. An empty table where the extraction had items
     would read as broken.
4. **Action row cells:** `text` as written. ASSIGNED = assignee/responsible, else `—`. DUE DATE = due,
   else `—`. A raw speaker label (`spk_0`, `spk_12`, case-insensitive) is not a name and renders `—`.
5. **Topic rows:** one per topic whose extraction produced **no action items at all**. Text is built
   exactly as `lambda_item_writer._topic_rows` builds it today:
   - title = `topic_title` (or `title`), trimmed;
   - summary whitespace-collapsed; `first` = the part before the first `". "`, trimmed; if `first`
     is non-empty, differs from the whole summary and does not end with `.`, append `.`;
   - text = `title — first` (join only the non-empty parts with ` — `);
   - if longer than **180** characters: keep the first 179, `rstrip`, append `…`.
   ASSIGNED and DUE DATE are `N/A`. HTML renders the row greyed (`color:#666`).
6. **No time suffix** in any cell — no `(at 09:05:00)`, no `(09:00 – 09:20)`.
7. **Plain-text flavour** is a pipe table with the same three columns and header separator; cells
   escape `|` and collapse newlines to a space.

## 2. Backend (fieldsight-pipeline, branch `feat/handoff-sync`)

### 2.1 The brief is actually produced on the live path — in parallel

Today only `_complete_summary` writes a brief, and it is skipped for `kind in ("final","rolling",
"updated")`. Every real session arrives as `kind: "final"` (item-writer), so **no brief has been
generated on TEST since #856–#858**. "Use the brief if it exists" is dead without this.

- `lambda_finalize_claim._request_extraction` additionally enqueues
  `session_finalize_requests/brief-<sessionId>.json` = `{"kind": "brief", "sessionId", "folder",
  "date"}`, conditional (`IfNoneMatch="*"`), at the same moment it requests the final extraction —
  so the brief and the final extraction run concurrently and the email is not delayed by a serial LLM
  call. That prefix already triggers `SessionFinalizeFunction`; no new trigger.
- `process_finalize_request` handles `kind == "brief"` FIRST and returns: no recipient required, no
  email, no `session_finalize_results/` write, no `_already_sent` check. If `SESSION_BRIEF` is off →
  return skipped. If the session was deleted → skipped. Otherwise `_complete_summary(artifact)` (which
  stores the brief).

### 2.2 The final email waits briefly for the brief

- For `kind == "final"` with `SESSION_BRIEF` on: poll
  `session_brief/<folder>/<date>/sid<sessionId>/latest.json` every `BRIEF_POLL_SECONDS` (default 10)
  up to `BRIEF_WAIT_SECONDS` (default 90). Both env-tunable.
- If a brief with ≥1 task appears: action rows = brief tasks mapped to
  `{text, responsible: assignee (speaker labels → None), due, at, kind: "action"}`; **topic rows from
  the request's `openTodos` (kind `topic`) are kept**. Otherwise the request's rows are used unchanged.
- Log one line saying which source was used and why (brief found / timed out / flag off / no tasks).

### 2.3 IAM — without this the wait is silently dead

`SessionFinalizeFunction` has `s3:PutObject` on `session_brief/*` but **no `s3:GetObject`** on it.
A read would be AccessDenied, which the code would read as "no brief yet", and every email would fall
back to extraction while every test stays green. Add `session_brief/*` to its GetObject statement in
`src/template.yaml`. Verify on TEST after deploy with `simulate-principal-policy` or a real read.

### 2.4 The email renderer

`build_confirmation_email`: header `AGENDA ITEM / ASSIGNED / DUE DATE`; drop the `<h3>Items</h3>`
heading (the header row carries it); action blanks `—`, topic rows `N/A` greyed; the text flavour
becomes the §1.7 pipe table (no `Unassigned`, no bullets). Intro, Site/Date lines and the
empty-state note stay.

## 3. Frontend (fieldsight-ui, branch `feat/handoff-sync`)

`scripts/composites/email-preview-modal.js`:

- `buildPreviewModel`: **remove the row-count floor**. Rows = action rows (§1.3) + topic rows (§1.5).
  `rowsSource` stays (`brief` / `action_items`). Topic rows need each topic's `summary`
  (present on daily-report topics).
- Renderers + on-screen preview: one flat table per §1; the per-topic layout and `laysOutPerTopic`
  go, together with the tests that pinned them (deleted deliberately, not left failing). Photos move
  **below the table**, grouped under their topic title.
- Parity test: pin §1.5 with the **same input/output cases** the backend tests use for
  `_topic_rows`, so the two implementations cannot drift silently.
- Cache busters in both preview HTMLs.

## 4. Verification

- Unit: every new test watched RED with its change reverted (mutation), controls first.
- TEST, backend, after CI deploy: (a) confirm the deployed role can read `session_brief/`;
  (b) enqueue a `kind:"brief"` request for a real session and confirm a brief is written;
  (c) enqueue a `kind:"final"` request carrying real rows, recipient = `bounce@simulator.amazonses.com`,
  and read the logs for which source was used; (d) render the email body locally from the same inputs.
- Frontend: render both paths in a real browser (minimal page, no Babel) and read the DOM.
- Prod: untouched. `SESSION_BRIEF=false` there, so prod emails keep extraction rows, now in the new
  table shape.

---

# Part 2 — the brief and the extraction stop talking past each other

Status: plan, 2026-09-18, after reading the first REAL email this produced
(session `d740cd5f…`, 2026-09-11) against the extraction it replaced.

## 5. What the real output showed

Two defects, both consequences of §1.3 substituting brief tasks for extraction
action items while topic rows were still decided by the extraction alone.

**5.1 The same thing appears twice.** The brief wrote "Visit MPI site with
DeAndre to see comprehensive open-space usage pattern." as a task; the extraction
had no action item for that topic, so the topic ALSO sank to the bottom as
"Ormiston College 360 Inspections — Speaker hopes to visit the MPI site with
DeAndre." Three of the five topic rows in that email duplicate an action row.

**5.2 A dated commitment the extraction caught was lost.** The extraction had
"PS4 for the Port Com SR study to be signed by January next year" (deadline
resolved). The brief has no such task, and because the brief won, the commitment
is not in the email at all.

## 6. Decisions (owner, 2026-09-18)

| Question | Decision |
|---|---|
| 5.1 duplicates | **A topic that a brief task already covers is not a topic row.** Coverage is decided by TIME, not by text. |
| 5.2 lost commitment | **Back-fill it.** An extraction action item that carries a due date and is not represented in the brief is appended to the action rows. |

## 7. The rule, for both surfaces

Both surfaces already build one table (§1). Part 2 changes only how the two
sources are merged.

1. **Coverage by time.** A topic is *covered* when at least one brief task's `at`
   (HH:MM:SS) falls inside that topic's own time range, inclusive of both ends.
   A topic with no parsable range is never covered. A brief task with no `at` covers nothing.
2. **Topic rows** (§1.5) are emitted for topics that produced no action item
   **and are not covered**.
3. **Back-fill.** When action rows come from a session's brief, every extraction
   action item of that session that (a) carries a non-empty due date and
   (b) is not represented in the brief is appended after the brief's rows, in the
   extraction's own order.
4. **"Represented" is a deliberately conservative text test.** Lower-case both
   texts, keep `[a-z0-9]+` tokens of 3+ characters, drop the stop list
   (`the a an and or to of for on in at is are be by with from that this it as`),
   and compute Jaccard overlap. **Represented iff overlap >= 0.30.** A tie or an
   empty token set counts as NOT represented, so the bias is towards carrying a
   commitment twice rather than losing it — losing one is the failure this plan
   exists to prevent.
5. The thresholds and the stop list are **one shared constant per surface, named
   identically**, and both sides pin the same worked examples (§9).

## 8. Backend: the request has to carry more than it does

`session_finalize` sees only `{text, responsible, due, kind}` rows — no time
ranges, no per-row provenance — so neither rule is computable there today.
`lambda_item_writer._final_email_context` must send, per row:

* action rows: `topic_range` (the topic's `time_range` string, unparsed) — needed
  so a back-filled row can be attributed, and to keep the shapes uniform;
* topic rows: `topic_range` likewise.

Nothing else changes shape: `_topic_rows` / `_final_email_rows` keep their text
rules (§1.5), and `_clean_todos` must carry the new key through rather than
rebuild the row without it (it rebuilds each row from scratch — the same trap
that lost `kind` once already).

`_rows_from_brief_or_request` then applies §7 and logs, in its existing single
line, how many topic rows were suppressed and how many items were back-filled.

## 9. Worked examples both surfaces must agree on

From the real 2026-09-11 session:

* Brief task `at 13:40:43` + topic "Ormiston College 360 Inspections"
  `13:40 – 13:41` → **covered** → no topic row.
* Extraction item "PS4 for the Port Com SR study to be signed by January next
  year", due `2027-01-31`, against the brief's five tasks → best overlap is with
  the QA task ("port", "com") ≈ 0.1 → **not represented** → **back-filled**.
* Extraction item "Concrete QA pour checks and report to be started three weeks
  later after cure" vs brief "Start QA pour checks and create report on Port Com
  concrete once 28-day cure ends in about three weeks" → overlap ≈ 0.5 →
  **represented** → not back-filled.
* An extraction item with NO due date and no match → **not** back-filled (§7.3a).

## 10. Stated plainly: this can double

The Jaccard test is a judgement, and at 0.30 it will sometimes carry an item the
brief did already say in different words. That is the chosen direction: a
duplicate is visible and annoying; a dropped commitment is invisible and is what
the owner objected to. Any future tightening needs a real run behind it.
