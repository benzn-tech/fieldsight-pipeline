# Self-introduction → name suggestion (design, 2026-09-29)

**Owner request (2026-09-29, starred in the voiceprint brainstorm):** when somebody says
"Hi, this is Petros from Cassidy", offer that voice as Petros through the bell. Zero-click
enrolment proposals from something people already do on site every day.

**Line:** voiceprint. **Customer rule:** plain words only, no scores.

## Why

A company's library fills only through renames, and a company cannot get a calibrated
floor until 20 human corrections exist. People introduce themselves constantly — at the
start of a walk, at a meeting, on a call. Each introduction is a named, single-speaker
passage: the best enrolment material there is, currently thrown away.

## What it is not

- **Not automatic naming.** A heard name is a suggestion a person confirms. The
  transcriber gets names wrong (Petros Pan → "Petrus Pang", 2026-09-22), and a wrong name
  written into a biometric store is the failure this system prices highest.
- **Not introductions of other people.** "This is Benny from Performance" said while
  pointing at Benny names the wrong voice. Only first-person phrasing is detected, and
  "this is X" only at the start of a turn after a greeting.

## Data path

The transcript text exists only in `lambda_extract_session` (no database). The database
side (`lambda_item_writer`, in-VPC) has the company, the tombstones and the session, and
already receives `speaker_turns` on the extraction artifact. So the pattern runs where
the text is, and the result rides the artifact — the same route `speaker_turns` took.

1. **`src/self_introduction.py` (new, pure, no I/O).** `find(turns) -> [intro]` over the
   normalised turns. Each intro: `source_filename, speaker_label, start_sec, end_sec,
   heard_name, company (optional), quote`.
   - English: `my name is X`, `my name's X`, `I'm X` / `I am X` **followed by** `from|with|
     at|here|,|.` or preceded within the turn by `introduce myself`; `this is X` only when
     the turn starts with `hi|hello|hey|morning|g'day|good (morning|afternoon)`.
   - Mandarin (spaces stripped first — ASR emits spaced characters): `我叫X`,
     `我的名字(是|叫)X`. `我是X` only with a following `来自|从|，|。` — bare `我是` is too
     common ("我是说…").
   - X: 1–3 capitalised Latin tokens or 2–4 Han characters; a stop list rejects common
     words that follow "I'm" ("I'm going", "I'm sure", "I'm here").
   - Turns shorter than 3 s are skipped (too short for the enrolment the confirm triggers).
   - **Final extraction only**, like `speaker_turns`.
2. **Artifact field** `self_introductions: [...]` beside `speaker_turns`.
3. **Migration `00NN_speaker_intro_suggestions.sql`** (next free number at merge):
   `speaker_intro_suggestions(id, company_id, session_base, source_filename,
   speaker_label, user_folder, session_date, start_sec, end_sec, heard_name, company_name,
   quote, state pending|confirmed|rejected, decided_by, decided_at, created_at,
   UNIQUE(company_id, session_base, source_filename, speaker_label))`.
   Separate from `speaker_name_proposals`: that table asks "is this <existing person>?"
   and requires a voiceprint; this one asks "who is this new voice?" and has none.
4. **`lambda_item_writer`** inserts pending rows (`ON CONFLICT DO NOTHING`), **skipping** a
   cluster whose file already carries a live turn name (same `turn_ref` stem rule as the
   candidate search) — asking "is this Petros?" about a passage already named Petros is a
   question with no information in it.
5. **org-api**
   - `GET /name-proposals` (badge shape, no transcript read) gains `introductions: n`.
   - `GET /name-suggestions` lists pending introductions with `heardName, quote, date,
     userFolder, sourceFilename, startSec, endSec`.
   - `POST /name-suggestions/{id}` `{decision: confirmed|rejected, display_name?}`.
     `confirmed` delegates to `speaker_corrections` with the suggestion's own turn and the
     (possibly edited) name, **in one transaction with the state flip** — the same
     lesson as #939. Session id = the passage filename (it carries the date).
   - Same roles as naming (`_CORRECTION_ROLES`), company from the caller only.
6. **Frontend (fieldsight-ui)**
   - Bell: a "New voices" row per suggestion: "Someone introduced themselves as Petros —
     save their voice?" → opens a dialog.
   - Dialog: the quote, Listen (same audio key rule), a name field **prefilled with the
     closest roster name** when one is close to the heard name (heard name shown beneath:
     "heard as: Petrus Pang"), buttons **Save as <name>**, **Not a name**, **Decide later**.
   - After save, the existing enrolment-outcome notice reports saved / not saved.

## Tests that must go red without the change

- Pattern: positives and the named negatives above (introducing someone else, "I'm going",
  bare `我是说`), spaced Mandarin.
- Writer: a named cluster produces no suggestion; a re-run produces no duplicate.
- org-api: confirmed → the queued artifact equals a rename of that turn with the edited
  name; a refused correction leaves the row pending (transaction).
- Real database: the migration and the insert run on `fieldsight_test` in a rolled-back
  transaction before merge.

## Out of scope

Roster integration (SignOnSite etc.), automatic acceptance at any confidence, and
introductions heard in live (non-final) extractions.

## Review outcome (Fable review, 2026-09-29) — supersedes the sections above where they differ

Defects found against the code, all adopted by the plan
(`docs/superpowers/plans/2026-09-29-self-introduction-name-suggestions.md`):

1. Normalised turns carry `speaker`, not `speaker_label`; the detector reads `speaker` and
   emits `speaker_label`, tested on real `normalize_transcript` output.
2. The writer insert goes INSIDE the `get_connection()` block (beside
   `location_markers.replace_for_day`), not beside `_request_rebind`, which runs after the
   connection closed.
3. Confirming needs a `sid<32hex>` in the session id; recordings without one are skipped at
   write time, since they could never be confirmed.
4. "Already named" is judged per FILE (`speaker_turn_names` has no label) — a known,
   accepted over-suppression, the same as the candidate search.
5. The confirm enrols on the cluster's LONGEST turn (as `_apply_confirmed_proposal` does);
   the introduction itself is often under the ~10 s enrolment needs.
6. Migration is 0071 (re-check at merge).
7. Site-speech false positives ("Hi, this is Level 2", "this is Block C", "this is
   Cassidy", "I'm Gib fixing", "我叫他过来"): place/thing stop list, reject a following
   digit, reject pronouns after 我叫, and a precision gate (>= 0.8 measured on 30 days of
   TEST transcripts) before shipping.

Owner decisions, taken on the owner's standing instruction to choose the long-term option:

1. **Gate on `SPEAKER_IDENTITY_MODE`: the CONFIRM path only.** Suggestions are stored even
   while identity is off (they hold text and offsets, no biometric data), so the queue is
   ready the day identity is switched on; confirming them 404s while it is off.
2. **Detect at 3 s, enrol on the cluster's longest turn** — catches short "Hi, I'm Petros"
   intros without asking the guard to judge 3 s of audio.
3. **Reject company-word suffixes** ("Cassidy Construction", "Ltd", "Group", "Services")
   as names — a wrong suggestion costs the customer a click and some trust.
4. **List cap 20**, env `SUGGESTION_PAGE`.
