# Multi-unit inspection: one walk, one report, a checklist per unit

Status: design (2026-11-03), from a conversation with the owner. Not built.
Builds on checklist gaps (step B, `2026-10-16-checklist-gaps-design.md`, on
prod) and check done (step A, `2026-10-20-check-done-gaps-design.md`, backend
on TEST, device/Ask hooks not landed).

## Why

Owner, 2026-11-01. Customers such as architects and builders put up a block of
townhouses, 5-20 units. An inspector walks a wall framing inspection through
Unit 1, then Unit 2, and so on. Each unit gets the **same** company checklist:
wall framing, roof truss spacing, and so on.

Today a voice-triggered check is a one-off. One recording yields one report
per check, so a 5-unit walk ends as 5 reports. The weather, project details and
attendees are repeated in each one, and the customer copies the 5 into 1 by
hand.

The report goes to the customer and into their records (owner, 2026-11-03).
It must therefore be a single document that reads as one inspection.

## What the inspector does (owner's words, 2026-11-03)

1. He opens the check by saying which check and which units, for example
   "wall framing inspection today, Lot 150 to 154" or "doing five units today".
   The company's own checklist template for that check is then duplicated once
   per unit.
2. He walks unit by unit. Saying the unit's name ("Unit 2", "Lot 167",
   "Room 102") starts that unit's section. There is **no button** for this:
   speech only. Units are named differently per project and company. The words
   in common are a prefix (unit / lot / room, and the like) plus a number.
3. In later units he usually speaks only about the problems. When a unit is
   fine he **will say so** ("this one's all good"). That statement covers
   **the current unit only**.
4. Before leaving the site there is a **final check**, which settles the gaps
   on site (see below).

## The report

- **One shared header**, written once: project, date, weather, attendees.
  Weather is named by the owner. The others are my call, following the existing
  report header.
- **One section per unit**, in walk order, titled with the unit's name as
  spoken. Each section is the company's checklist, rebuilt by
  `checklist.rebuild` with the customer's own item text and order.
- **A unit-by-item summary table**: rows are units, columns are items, and each
  cell holds Yes / No / N/A / not checked. The owner has no view on it yet:
  build it, and show it to him before deciding whether it stays.
- **Item states:**
  - **Yes / No / N/A**, each with evidence.
  - **No** carries the comment ("east side two studs short"), as today.
  - **Not checked**: nothing in the recording addressed it for this unit and no
    "all good" covers it. Its Answer is empty and its Comment says it was not
    checked, as `NOT_COVERED` does today. **Silence is never Yes** (owner: "even
    if all passed he will say all passed").
- **"All good" for a unit** answers Yes for every item of **that unit** that
  was not otherwise answered. The evidence for each of those items is the
  "all good" quote. This is a deliberate exception to step B's rule of one quote
  per item. It must be visible in the report, for example a comment "covered by
  'this one's all good' (10:42)", so the customer can see the basis.
- A unit named during the walk that was **not** in the opening list gets its
  own section anyway. It is flagged in that section for the inspector to
  confirm: a slip of the tongue, or a unit added on the day.

## Unit boundaries

The unit list and names come from the opening statement when he gives them.
The owner recommended speech, "more practical from experience".

A "prefix + number" mention switches the current unit **only when he is
walking into it**. It does not switch when he merely refers to another unit:

- "back to Unit 2, forgot to say the lintel..." -> a finding for Unit 2 while
  he stands in Unit 4. The current section stays Unit 4. The finding goes to
  Unit 2.
- "next door Unit 5 is the same" -> applies to Unit 5. It does **not** start
  Unit 5.

Telling the two apart is a model judgement on the transcript, with the quote as
evidence. Code checks that each assignment names a unit from the list (or a
flagged new one), and that each item has at most one answer per unit.

`inspection_match.GENERIC` already drops "room", "level", number words and
digits when matching a check to its template. It must also drop unit / units /
lot / lots / today (see "Builder-check answers"), or a unit name can push a
check below `MIN_SCORE`. A unit mention must also **not** close the inspection
window: `end_source='next_check'` is for a different check, not a different
unit.

## The final check (on site)

The inspector holds the device's Ask key and says the check is done, as in
step A. The device then reads out what is open. It reads **the item's text,
never its number** (owner): "Unit 3: wall bracing, lintel size not mentioned."

It reads two kinds of item:

1. **Not checked**, per unit.
2. **Answered No**, per unit, read back for confirmation: "Unit 3, wall bracing
   failed, east side two short -- right?" This catches the error the gap list
   cannot catch, which is a finding filed under the **wrong unit**. A misfiled
   finding looks "checked", so only a read-back exposes it. My call; it answers
   the risk raised in the conversation.

How the answers come back depends on how many items are open:

- **Few open** (my guess: about 3 or fewer, tune on real walks): the device
  asks one item at a time, "Unit 3, wall bracing -- pass?". He **holds the Ask
  key** and answers ("pass", "fail, two short on the east side", "not
  applicable"). Releasing the key ends the answer, and the device asks the next
  item. Anything not understood is skipped and goes to the phone.
- **Many open:** the device says one line, "Unit 3 has 8 items not mentioned,
  the list is on your phone". The phone shows the open items grouped by unit.
  Each item has Pass / Fail / N/A buttons, and a hold-to-record short voice
  note for a reason (no typing in capture, owner rule).
- Every answer, from the device or the phone, is written to the same report,
  on that unit's item, with its quote or voice note as evidence. Whatever is
  still unanswered stays "not checked" in the report. Settling it later on the
  web is the next phase (see "Owner decisions").
- **Wrong unit, fixed on the spot (owner, 2026-11-03).** When a No is read back
  and he answers "no, that was Unit 2", the finding **moves** to Unit 2. If
  Unit 2 had that item as Yes, the moved No replaces it. The device confirms
  the move ("moved to Unit 2"). On the phone, every No has a "move to another
  unit" option. A move keeps its quote, and the report shows the finding was
  moved and by which answer.

**Why not a live voice model (GPT-live style):** sites are loud, and the hard
part is open-mic endpointing. Push-to-talk on the existing Ask key avoids it.
The device already has push-to-talk, cloud STT, TTS playback and a two-phase
Ask with follow-up turns (on TEST). There is no free button, and none is
needed.

### Not colliding with Ask

The **device state** decides, not a backend guess:

- When the device has just asked a confirmation question, it is in
  *confirming* state. An Ask-key press then goes to the confirmation endpoint,
  carrying `{inspection, unit, item}`, and never to general Ask.
- Confirming state ends when:
  - the list is done;
  - he long-presses (today's interrupt);
  - nobody presses for a while (my guess: about 30 s).
  When it ends, the remaining items go to the phone.
- An answer that is not pass / fail / N/A (he asks something else) gets "skipping
  this one" and that item is skipped. A confirmation answer is never routed into
  Ask.
- **Evidence:** step A treats the Ask clip as private and keeps it out of the
  recording, because the mic is borrowed. A confirmation answer, however, **is**
  report evidence. Its transcript must be stored with the item it answers. It is
  not left to the main recording, which does not contain it.

## Risks and how this design meets them

1. **Finding filed under the wrong unit.** Met by the read-back of every No in
   the final check, and by quoting the evidence in each unit's rows.
2. **Mentioning a unit is not entering it.** Met by the boundary rule above.
3. **"All good" scope.** It applies to the current unit only (owner).
4. **Not every item applies to every unit.** For example, an end unit has a
   firewall and a middle unit does not. Such an item must read N/A, not "not
   checked". *Left open:* whether the template marks per-unit applicability, or
   the inspector says N/A. Until decided, he answers N/A in the final check.
5. **Photos.** A photo goes to the unit whose section was current at the
   photo's time. Today photos bind per session by time
   (`photo-bound-once-per-session-not-per-day`).
6. **Recording split into several sessions** (the known chunk-session
   fragmentation). The multi-unit inspection must still be **one** report. Unit
   sections are keyed by the inspection, not by the session.
   `inspection_windows.segments` already models one check across several
   stretches.
7. **Scale.** 20 units x 20 items is 400 rows. Extract per unit (one model call
   per unit section, in parallel) rather than one call over the whole walk. The
   final check must stay within the step A budget (35 s function timeout).
   *Measure on a real walk before promising a number.*

## Goals

- One report per multi-unit walk: shared header once, a checklist per unit, and
  a summary table.
- Silence is never Yes. A per-unit "all good" is an explicit, visible Yes.
- Gaps and No answers (including a wrong unit) are settled on site before he leaves, by voice on the
  device for a few items and on the phone for many.

## Non-goals

- New hardware buttons, or a live/open-mic voice agent.
- Changing single-unit checks. A check with no unit list behaves as today.
- Auto-detecting **which** template to use. That is the existing
  `inspection_match`.

## Anti-goals (failure even if it "works")

- An item marked Yes because nobody spoke about it.
- A finding silently filed under another unit.
- Five reports again, or one report the customer still has to edit into shape.

## Ownership (cross-line)

- **Backend / extraction / report (this line):**
  - the unit list from the opening statement;
  - unit segmentation;
  - per-unit `checklist.rebuild`;
  - the "all good" rule;
  - the summary table;
  - the final-check gap and read-back list;
  - the confirmation-answer write-back and its evidence.
- **Ask line:** the `check_done` intent hook (from step A), plus routing a
  confirming-state press to the confirmation endpoint.
- **Device line:**
  - the confirming state and its exits;
  - TTS of each question;
  - push-to-talk answers;
  - handing off to the phone.
- **Phone line:** the open-items list per unit, the buttons, and the voice note.
  Whether the existing phone voice notes (migration 0089) can carry a per-item
  note is to be confirmed with that line.

## Left to the builder

- Data model: units as rows under an inspection window, versus a jsonb unit list
  on `inspection_windows` with per-unit item answers.
- How the opening statement is parsed: a range ("150 to 154"), a count ("five
  units"), or a list.
- The exact few-vs-many threshold, and the confirming-state timeout.
- The layout and export of the summary table (shown to the owner before it is
  final).

## Builder-check answers (2026-11-03)

A fresh reviewer read this design. Its questions are settled here: by the
owner's words where he gave them, otherwise as **my calls** for the owner to
check.

- **When a walk is multi-unit (my call).** A walk is multi-unit when either:
  - the opening names units or a count; or
  - he walks into two or more distinct unit names.

  A single mention of "room 102" in an ordinary check does not make it
  multi-unit. With a count ("five units") the names come from the walk and are
  not flagged. Flagging applies only against a named opening list. Findings
  spoken before the first unit, or that name no unit, go to a "General" section
  after the shared header. They are never dropped and never guessed into a
  unit.
- **Final check is once per walk** (owner: "one reckoning before he leaves").
  It lists every unit's open items. Whether step A computes the per-unit state
  on demand, or the live extraction persists it and step A reads it, is left to
  the builder. The constraint: the answer must reach the device within step A's
  budget, and must say how far its evidence reaches, as step A does.
- **Identity across sessions (my call).** A walk is keyed by (company, user
  folder, day, template) plus the opening statement's time, not by session or
  `inspection_windows.id`. That id is regenerated on every re-extraction, so it
  must not be the confirmation key. A later session without an opening
  statement continues the open walk of that day and template. A new opening
  statement starts a new walk. The report is regenerated when any of its
  sessions is re-extracted.
- **`inspection_match.GENERIC` must gain** unit / units / lot / lots / today.
  Otherwise "wall framing inspection today, Lot 150 to 154" scores exactly
  `MIN_SCORE` against "Wall Framing Inspection" (reviewer's finding). One more
  spoken word ("units") would drop it to 0.4 and the match fails. Words such as
  townhouse / house are to be measured on real openings before adding.

## Owner decisions (2026-11-03, "go with your recommendations")

- **Wrong unit:** moved by voice on the spot, and from the phone (see "The
  final check").
- **Web "settle later": next phase, not this one.** On-site settling should
  leave few items. When built, it needs a write endpoint, per-item controls on
  the web, and a record of who answered, when and on what basis, so that
  on-site evidence and later answers stay distinct in a customer record. Who may
  answer (the inspector, or also the PM/reviewer) is decided then. My lean: both,
  with the answerer named on the item.
- **On-site answer vs the full recording.** The on-site check sees only the
  part of the recording already transcribed. When the full recording is
  extracted:
  - an item settled on site as "not checked", which the recording answers, takes
    the recording's answer;
  - an item where the on-site answer and the recording **disagree** (Yes on site,
    "size looks wrong" at 10:05 in the recording) keeps **both**. It is flagged
    for the inspector to choose. Neither one silently overwrites the other.
