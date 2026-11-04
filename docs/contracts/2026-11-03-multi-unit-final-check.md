# Contract: multi-unit final check (Ask, device and phone lines)

Backend: fieldsight-pipeline, branch `feat/multi-unit-inspection`.
Spec: `docs/superpowers/specs/2026-11-03-multi-unit-inspection-design.md`
("The final check", "Not colliding with Ask", "Ownership", "Owner decisions").

The shapes below are copied from the code and its tests:
- `src/check_done.py`, `tests/unit/test_check_done_run.py`;
- `src/walk_confirm.py`, `src/lambda_check_gaps.py`, `tests/unit/test_a_confirmation_answer_is_kept_as_evidence.py`;
- `src/lambda_org_api.py`, `tests/unit/test_phone_settles_a_walk.py`.

If this document and a test disagree, the test is right. Tell the backend line.

**Status:** nothing here is deployed until the PR merges. It goes to TEST first. Prod follows the release.

## Who owns what

| Line | Owns |
|---|---|
| Backend (this line) | Unit list, unit segmentation, the per-unit checklist, the final-check gap and read-back list, writing the confirmation answers back as evidence, and both phone routes. |
| Ask | The `check_done` intent hook, which routes the press to CheckGaps as today. While the device is confirming, it forwards each press as `action: "confirm"` (below). A confirming-state press never goes to general Ask. |
| Device | The confirming state and its exits, TTS of `say` and of each question, push-to-talk answers, and handing the rest to the phone. |
| Phone | The open-items list per unit, Pass / Fail / N/A, a hold-to-record voice note, and "move to another unit" on a No. |

## 1. Final check: the CheckGaps response

The request is unchanged from step A: `{"caller_sub", "check_name", "spoken", "pressed_at"}`.

### Ordinary check (no walk)

The response is exactly what it was. There is **no** `walk`, `questions` or `mode` key (see `test_a_window_of_another_walk_is_an_ordinary_check` and `test_single_unit_check_done_is_unchanged`):

```json
{"status": "ok",
 "say": "1 item open on the Pre-pour Inspection Checklist: Cover to steel checked. And 1 marked No: Edge protection installed.",
 "check": {"name": "Pre-pour Inspection Checklist", "template_id": "t-pp"},
 "not_covered": [{"n": 3, "text": "Cover to steel checked"}],
 "failed": [{"n": 2, "text": "Edge protection installed", "comment": "north side"}],
 "items": 3, "covered_until": 1792443899, "partial": false}
```

`status: "error"` and `status: "no_template"` carry only the base keys (`status, say, check, not_covered, failed, items, covered_until, partial`). Test for `"walk" in response` before you treat a response as a walk. Never assume it is one.

### Multi-unit walk

This is the case when the chosen live window has a `walk_key`. Example from `test_final_check_lists_each_units_open_items_by_text`:

```json
{"status": "ok",
 "say": "5 items open across 2 units — the list is on your phone.",
 "check": {"name": "Pre-pour Inspection Checklist", "template_id": "t-pp"},
 "items": 3,
 "covered_until": 1792443899,
 "partial": false,
 "not_covered": [
   {"n": 2, "text": "Edge protection installed", "unit": "Unit 1"},
   {"n": 3, "text": "Cover to steel checked",    "unit": "Unit 1"},
   {"n": 1, "text": "Formwork braced and plumb", "unit": "Unit 2"},
   {"n": 3, "text": "Cover to steel checked",    "unit": "Unit 2"}],
 "failed": [
   {"n": 2, "text": "Edge protection installed", "comment": "north side", "unit": "Unit 2"}],
 "walk": {"key": "walk:abab…(40 hex)", "units": [
   {"unit": "Unit 1",
    "not_covered": [{"n": 2, "text": "Edge protection installed"},
                    {"n": 3, "text": "Cover to steel checked"}],
    "failed": []},
   {"unit": "Unit 2",
    "not_covered": [{"n": 1, "text": "Formwork braced and plumb"},
                    {"n": 3, "text": "Cover to steel checked"}],
    "failed": [{"n": 2, "text": "Edge protection installed", "comment": "north side"}]}]},
 "questions": [
   {"kind": "not_checked", "unit": "Unit 1", "item_text": "Edge protection installed", "comment": ""},
   {"kind": "not_checked", "unit": "Unit 1", "item_text": "Cover to steel checked",    "comment": ""},
   {"kind": "not_checked", "unit": "Unit 2", "item_text": "Formwork braced and plumb", "comment": ""},
   {"kind": "not_checked", "unit": "Unit 2", "item_text": "Cover to steel checked",    "comment": ""},
   {"kind": "confirm_no",  "unit": "Unit 2", "item_text": "Edge protection installed", "comment": "north side"}],
 "mode": "phone"}
```

The rules:
- **`not_covered` / `failed`** keep their step-A meaning across the whole walk. Every entry carries `unit`, so the same `n` repeats from unit to unit. Read `n` as the template's item number. It is not unique.
- **`walk.key`** is `walk:` followed by 40 hex characters. It is the confirmation key and the phone route's `{walkKey}`. It stays the same when the walk is re-extracted.
- **`walk.units`** are in walk order: the units the opening named, then the units he entered, then any other unit the recording has findings for (R44). A unit he walked into and said nothing about is listed, with every item not checked.
- **One "all good" covers one unit** (R45). The final check has no unit stretches, so an "all good" counts for a unit only when that unit has its own line saying it. Otherwise that unit's items stay open and are asked.
- **`questions`** is everything still open, per unit in walk order, **only for units the walk opened with or entered** (R51). A unit seen only in the recording's checklist rows is still a flagged section of `walk.units`, `not_covered` and `failed`, but it gets no questions: the device's hop and the phone route accept answers only for the opened and entered units. Within a unit the not-checked items come first, then the Nos, each in template order. `kind` is one of:
  - `not_checked`, asked as "pass?";
  - `confirm_no`, a No read back for confirmation, with its `comment`.

  An item already decided on site is not asked again. When the site and the recording disagree, the item is asked by what the recording said: a recording No becomes `confirm_no`, anything else `not_checked`.
- **`mode`** is `"device"` when `len(questions) <= 3` (`check_done.DEVICE_MAX_QUESTIONS`), including **0**, and `"phone"` otherwise.
- **`say`** depends on `mode`:
  - `device` with questions: the first question only, e.g. `"Unit 2: Cover to steel checked — pass?"` or `"Unit 2, Edge protection installed failed, north side — right?"`. It reads the item's text and never its number.
  - `phone`: one line, `"<N> items open across <K> units — the list is on your phone."`
  - Nothing open: `"All 3 items on the Pre-pour Inspection Checklist are covered in both units."`. If the opening counted more units than were heard, it adds `"…; 2 of the 4 units were heard."`. If no unit was heard at all: `"No unit of … has been heard yet."`.
- **`partial` / `covered_until`** mean what they do in step A. `covered_until` is the epoch seconds the evidence reaches. A walk is also partial when it has more than 5 recordings (only the newest 5 are read), or when time ran out before every recording could be read.
  - The `"As of a minute ago, "` prefix is added to `say` **only** on the phone line and the nothing-open line. The device's first-question line never has it, even when `partial` is true.
- The whole run stays inside `check_done.RUN_BUDGET_S` (30 s).

**Question wording.** Only the first question is in `say`. The device speaks questions 2..n itself, built from `questions[i]` (the backend's form is `check_done.say_question`):
- `not_checked`: `"<unit>: <item_text first clause> — pass?"`
- `confirm_no`: `"<unit>, <item_text first clause> failed[, <comment first clause>] — right?"`

## 2. Device confirmation: `action: "confirm"`

This goes to the same CheckGaps Lambda. Its `lambda_handler` sends `action == "confirm"` to `walk_confirm.handle`, never to general Ask.

### Request

```json
{"action": "confirm",
 "caller_sub": "sub-1",
 "walk_key": "walk:aaaa…(40 hex)",
 "unit": "Unit 3",
 "item_text": "Wall bracing",
 "kind": "not_checked",
 "text": "pass",
 "pressed_at": 1}
```

- **`unit`, `item_text`, `kind`** are copied from the question being answered. `kind` is `not_checked` or `confirm_no`.
- **`text` or `audio_b64`.** Send `audio_b64` (base64 WAV) and the backend's STT hears it. A non-blank `text` wins over audio.
- **`pressed_at`** is carried but not read. The answer's time is server time (NZ, `HH:MM:SS`).
- Company, folder and walk facts come from the caller's own record (the in-VPC hop), never from the event.

### Response

Always `{"status", "say", "next": null}`.

| status | say | Meaning |
|---|---|---|
| `saved` | `"Got it."` | The answer is stored as report evidence (its transcript is the quote) and the walk report is re-queued. |
| `saved` | `"Moved to Unit 2."` | A move is stored. |
| `skipped` | `"Skipping this one — it's on your phone."` | The answer was not understood, or nothing was heard. Nothing is written. |
| `error` | `"Skipping this one — it's on your phone."` | Bad request, STT missing or failed, or the hop refused or failed (e.g. walk not found). Nothing is written (ruling R33). |

`skipped` and `error` have the same `say`, so only `status` tells them apart. The device handles both the same way: speak `say`, move to the next question, and the item stays on the phone. `next` is always `null`: the device holds the list and moves through it. A re-queue failure never fails a `saved` answer.

### Parse rules

The first rule that applies wins. Text is lower-cased and punctuation is ignored.

1. **Move** (only on `confirm_no`, and only when the whole answer is a move). The form is: [no | nope | wrong (one)] [that was | that's | that is | it was | it's | it is | should be | belongs to | it belongs to] <unit>, with nothing else besides "actually" or "sorry". The unit is normalized ("unit two" → "Unit 2"; also Lot, Room, House, Apartment, U3…).
   - A unit mentioned in passing is not a move. "No, the nogging in Unit 2 is missing" is a No with that comment.
   - A move to the current unit is not a move. It falls through to rule 3.
   - A move to a unit the hop says is not one of the walk's is parsed again by the next rule. It is not a skip.
   - A move that names two or more units ("no, unit 2 and 3", "units 2 and 3", "unit 2 unit 3") is **skipped** to the phone (R48). "One hundred and five" is one unit.
   - On `not_checked`, "no, that was Unit 2" is a No with the comment "that was Unit 2".
2. **Pass.**
   - On `not_checked` (R46): **Yes** only when the **whole** answer, filler ("um", "uh", "actually") aside, is one of pass / passed / yes / yep / good / all good / ok / okay / fine. An acknowledgement followed by more words ("okay hang on", "right, let me check", "pass, n/a for the lintel") is **skipped**, and so are "right" and "correct" alone.
   - On `confirm_no` (R32/R34):
     - an acknowledgement (right / correct / yes / yeah / yep / ok / okay / good / that's right) **confirms the No**;
     - only an explicit pass ("pass", "passed", "it passes", "actually it passes") gives **Yes**;
     - both together ("yes it passes", "that's right, pass") are ambiguous, so the item is **skipped** to the phone.
3. **Fail.** An answer that starts with fail / failed / no (on `confirm_no` also fails) is **No**. The rest of the answer is its comment ("Fail, the nogging is missing." → comment "the nogging is missing.").
   - On `not_checked` a hedge is never a No (R46): "no idea", "no wait", or "not sure" / "don't know" anywhere in the answer is **skipped**.
4. **N/A.** An answer that starts with not applicable / N/A / n.a. / na / doesn't apply (optionally after "it's"/"that's") is **N/A**. This rule is anchored to the start, so "the lintel is na" is a skip.
5. **Anything else is skipped** (it goes to the phone). It is never routed to Ask.

Before these rules, heard text that is not speech is **skipped** (R46): a bracketed audio-event tag ("(wind)", "[music]", "[inaudible]"), "recording started/stopped", or no letters at all ("..."). The repo has no ASR hallucination classifier; this marker list is the one `lambda_extraction_backlog` uses.

## 3. Phone routes (org-api)

Both routes read the walk the way `GET /api/org/days/{date}/inspections` reads the day's checks, with the same access checks.
- `{walkKey}` is `walk:` + 40 hex. The route URL-decodes it, so a raw or encoded colon both work.
- `?user=` is the walker's folder. When it is omitted, it defaults to the caller's own folder.

### GET `/api/org/days/{date}/inspections/walks/{walkKey}/open?user=`

200:
```json
{"walkKey": "walk:…",
 "source": "report",
 "units": [
   {"unit": "Unit 1", "items": [
     {"item_text": "Insulation",       "kind": "not_checked", "comment": ""},
     {"item_text": "Window flashings", "kind": "not_checked", "comment": ""},
     {"item_text": "Building wrap",    "kind": "confirm_no",  "comment": "torn at the corner"}]},
   {"unit": "Unit 2", "items": []},
   {"unit": "Unit 3", "items": [
     {"item_text": "Building wrap", "kind": "confirm_no", "comment": ""}]}]}
```

- **Shape.** This list is grouped by unit, and its key is camelCase `walkKey`. The final-check `questions` is a **flat** list with `unit` on each entry. The kinds and the order are the same in both, because both are built by `check_done.walk_questions`.
- **Source.** `source` says where the list came from. The route serves the newer of two lists (R41):
  - `"report"`: the latest **finished** walk report. While a re-queued report is still being written, or if it failed, this is the newest finished report it replaced (up to 5 back, R39).
  - `"final_check"`: the walk's latest final check. Every final check stores its questions, an empty list when nothing is open (the `questions` of the device's response, as the device got them; `walk_final_checks`). A question that no longer fits the walk is left out: for example, the template was edited mid-day. This list is served when it is newer than the chosen report's queue time, or when no report has finished. A final check's time is when it was pressed (the request's `pressed_at`), not when the asynchronous store landed, so a late retried store never outranks a newer list. So **right after the final check the list is there**, before any walk report exists. Every unit of the walk is listed in walk order, and units with nothing open get `"items": []`.

  Either way, every answer given after that list was made is laid over it:
  - an answer drops its question, a read-back's included;
  - a move drops the target's question and re-asks the origin as `not_checked`.

  The phone should still keep what it answered and drop it locally, because a server list can lag a fresh POST for a moment.
- **Items.** For `report`, the item wording is from the template version the report was written with, or else from the current version. For `final_check`, it is the template's wording as the final check stored it.

Errors:

| Status | Body | Meaning |
|---|---|---|
| 400 | `{"error": "date required (YYYY-MM-DD)"}` | The date is malformed. |
| 400 | `{"error": "user required"}` | An all-scope caller sent no `user` and has no folder. |
| 403 | `{"error": "not permitted to view this user's inspections"}` / `"you may only view your own inspections"` / `"no folder mapping for your account"` | The caller may not read this folder. |
| 404 | `{"error": "user not found in your company"}` | An all-scope caller named an unknown folder. |
| 404 | `{"error": "walk not found"}` | The key is malformed, the walk is not this folder's on this date, or it is not visible to the caller. |
| 404 | `{"error": "not ready"}` | There is no final check, and either no walk report row yet or its result is not under the folder's day (never read). Right after a final check this can briefly be the case, because the store is asynchronous (R42), or after a store that failed. |
| 404 | `{"error": "not ready", "status": "pending" \| "error" \| "done"}` | There is no final check and no finished walk report. `status` is the newest result's: `pending` means it has not been written yet, `error` means it failed, and `done` means the newest result is an ordinary report with no walk. |
| 500 | — | An S3 refusal (a missing IAM grant). It is raised, never served as "nothing open". |

### POST `/api/org/days/{date}/inspections/walks/{walkKey}/answers?user=`

Body:
```json
{"unit": "Unit 1",
 "item_text": "Insulation",
 "answer": "Yes",
 "move_to_unit": null,
 "voice_note_id": "55555555-5555-5555-5555-555555555555",
 "asked_kind": "not_checked"}
```

- **`answer`** is `"Yes"`, `"No"`, `"N/A"` or `null`.
- **A move** is `answer: null` with `move_to_unit: "<unit>"`. A body must carry exactly one of an answer or a move, never both and never neither.
- **`unit` and `move_to_unit`** must be units of the walk. They are normalized, so "unit one" becomes "Unit 1".
- **`item_text`** must be an item of the checklist, in either the current template version or the version the latest finished walk report used. The match ignores case and whitespace, and the template's wording is stored.
- **`voice_note_id`** is optional and must be a UUID. The route checks only that it is a UUID, not that the note exists.
- **`asked_kind`** is optional: `not_checked` (the default) or `confirm_no`. Send `confirm_no` when answering a read-back No (R36), because that answer is the inspector's choice over the recording.
- **There is no `comment` and no `quote`.** Both are stored as `""`, so the reason for a Fail lives in the voice note.
- Any other key in the body (company_id, user_folder, source, walk_key…) is ignored.
- The row is stored with source `phone`, the caller as the answerer, the server time (NZ) and the walk's company. It is committed, and then the walk report is re-queued.

201:
```json
{"status": "saved", "id": "<uuid>", "walkKey": "walk:…", "requeued": true}
```
`requeued: false` means the answer is saved but the report was not re-queued. That is not a failure for the phone.

**Only the walker may POST (R38).** Being able to read the day, as an admin, GM or platform_admin, does not let anyone answer for the walker.

Errors (GET's 400/403/404 access and walk-not-found errors apply first):

| Status | Body `error` | Meaning |
|---|---|---|
| 403 | `only the person who did the walk can answer for it` | The caller is not the walker. |
| 400 | `malformed JSON body` | |
| 400 | `unit invalid` / `item_text invalid` / `move_to_unit invalid` / `voice_note_id invalid` / `asked_kind invalid` | A field has the wrong type or value. |
| 400 | `answer invalid` / `answer or move_to_unit required` / `answer with a move_to_unit` | The answer/move rule is broken. |
| 400 | `unit not in walk` / `move_to_unit not in walk` | |
| 400 | `item not in checklist` | |
| 409 | `walk company unclear` | The walk's windows span companies and there is no opening to decide which company it belongs to. Nothing is written. |

## 4. The confirming state (device line)

```
final-check response
  ├─ no "walk" key      → as today (speak say)
  ├─ mode "phone"       → speak say; the phone shows the list
  ├─ mode "device", questions == []   → speak say; do NOT enter confirming
  └─ mode "device", questions 1..3    → speak say (= question 1) → CONFIRMING
CONFIRMING
  Ask-key press → Ask line forwards {action:"confirm", walk_key, unit, item_text, kind, audio_b64, pressed_at}
               → speak the response's say → ask the next question (TTS built from questions[i])
  exits: the list is done | long-press (today's interrupt) | 30 s with no press
  on exit: the remaining questions go to the phone
```

- While the device is confirming, a press is **never** general Ask, whatever was said.
- An answer that comes back `skipped` or `error` leaves that item on the phone. The device moves on to the next question.

## 5. Phone line

- **The list.** Take it from GET `/open`. Normally GET serves the final check's questions at once (`source: "final_check"`), including an empty list when the final check left nothing open. The store is asynchronous (R42), so a GET within a second or two of the final check, or after a store that failed, can still get `not ready`. **Retry briefly** in that case (for example a few times over about five seconds). If the device's `questions` are in hand, they are the same list, in a flat shape (see above).
- **The buttons.** Each item has Pass / Fail / N/A, which POST `answer`. Send `asked_kind: "confirm_no"` for a read-back No.
- **The voice note.** A hold-to-record voice note gives the reason for a Fail, sent as `voice_note_id`. The id is meant to come from the existing phone voice notes (migration 0089). **The phone line still has to confirm** that a 0089 note can carry a per-item note.
- **Moves.** Every No offers "move to another unit", which POSTs `answer: null` with `move_to_unit`.
- **Local state.** Keep each answered item and drop it locally. Do not wait for GET to stop listing it.

## Not built / open

- **Web "settle later"** is the next phase. Whatever is unanswered on site stays "not checked" in the report.
- **There is no push from the backend to the phone.** The phone pulls GET `/open`. Since R41 every final check stores its questions, including an empty list, so GET normally has the list right after the final check, before any walk report exists. The store is fire-and-forget (R42): an asynchronous invoke the device's answer never waits on. It lands within about a second, and a send failure is only logged. Until it lands, or if it failed, GET answers `not ready` until the walk report finishes, so the phone retries briefly (section 5).
- **The voice_note_id from migration 0089** still has to be confirmed with the phone line (above).
- **The few-vs-many threshold (3) and the 30 s idle timeout** are first guesses. They are to be tuned on real TEST walks.
