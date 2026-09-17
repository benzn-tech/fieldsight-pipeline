# The brief says which section each task came from

Status: design, 2026-09-18. Step 2 of the owner's ruling ("按你的这两步走"). Step 1
(suppression by text instead of by time) is a separate, smaller change already in flight.

## 0. What this removes

The hand-off table mixes two documents written by two different models under two
different ideas of what counts as a task:

* the **extraction** decided "go to the Icehouse office to meet KCD" is a topic that
  produced **no** action item;
* the **brief** decided the same sentence is a **task**.

So the table showed it twice — once as an action row from the brief, once as a sunk
topic row from the extraction. Every attempt to fix that at RENDER time is a guess about
whether two sentences from two documents mean the same thing:

* **by time** — measured on TEST 2026-09-18 and rejected: the brief's 8 tasks carried
  only 3 distinct `at` values, and the extraction's topics are overlapping 2-minute
  windows, so "Papakura is mid-program and progressing well" was suppressed by a task
  about meeting KCD. Content disappeared with no trace.
* **by text** (Jaccard) — shipping as step 1, and strictly better, but still a guess.

The model that wrote the brief already knows the answer. This spec asks it.

## 1. The change

`session_brief`'s task schema gains one field:

```
tasks: [{ text, at, assignee, due, section }]
```

`section` is the **exact title of the section this task came from**, or `null` when the
task does not belong to one. The prompt states it in those words, and says the title must
be copied verbatim from the `sections` it just wrote.

Validation in `session_brief` (not in the prompt — an instruction cannot be relied on and
this can): after parsing, a `section` that does not match any section title exactly is
set to `null`. The count of tasks whose `section` survived is logged and put in `stats`,
so a prompt regression is visible without reading an artifact by hand.

## 2. What the table then does

When a session has a usable brief, **the whole table comes from the brief**:

* action rows = the brief's tasks (unchanged);
* sunk rows = the brief's **sections that produced no task**, rendered as
  `title — first bullet`, truncated exactly as `_topic_rows` truncates today (180
  codepoints, `…`);
* the extraction contributes **nothing** to the table except the existing safety net:
  a dated action item the brief does not mention is still back-filled (step 1's rule,
  unchanged).

No cross-document matching survives anywhere in the render path. The text test stays for
back-fill only, where its bias ("when unsure, carry it twice") is the safe direction.

With no usable brief the table is built from the extraction exactly as it is today.

## 3. Why the linkage is trustworthy where the render-time guess was not

It is written by the model that produced both halves in one pass, with both in front of
it, rather than inferred afterwards from two documents by a third party that has neither's
reasoning. It is also **checkable**: §1's validation rejects a title that does not exist,
which is the failure mode a hallucinated linkage would take.

## 4. Verification — this is a PROMPT change, so a green suite proves nothing

Per this repo's own rule (`prompt-instruction-present-is-not-obeyed`), the instruction
being present is not evidence the model obeyed it.

1. **Unit**: schema, validation (a bogus section title becomes `null`), the sunk-row
   rule, and that the extraction no longer reaches the table when a brief is used.
2. **TEST, real runs, N >= 2 per session on the B0 pair** (`Ben_Lin/2026-09-11` solo and
   `Ben_UCPK2/2026-08-27` multi-voice). For each run record: how many tasks carried a
   valid `section`, how many sections produced no task, and the rendered table.
3. **Pass conditions, judged by a person:**
   * every task carries a section that exists, or `null` for a genuine orphan — a
     hallucinated title is a FAIL;
   * no section that plainly produced a commitment is listed as a sunk row (that is the
     duplicate this whole change removes);
   * **nothing the recipient needs disappears**: the Papakura case is the canonical
     example — it must appear, as a sunk row, in every run;
   * the row count does not drop below what the same session renders today.
4. **Byte parity** between the email and Preview & copy on one fixture, as for steps 1
   and 2 — the check that has caught four divergences so far.

## 5. Risks, stated

* **The model may attach a task to the wrong section.** It is one more thing to get wrong,
  and validation only catches titles that do not exist, not titles that are wrong. §4.3's
  human read is the only guard, which is why it is a pass condition and not a footnote.
* **Sections are coarser than topics.** Four sections covered eight extraction topics on
  the measured session, so sunk rows will be fewer and broader than today's topic rows.
  That is a product change, not a bug — but it is the owner's call if it reads as thin.
* Prod is unaffected until `SESSION_BRIEF` is turned on there; today prod renders the
  extraction path only.

---

## 11. Measured on TEST, and the owner's ruling (2026-09-18)

Three real runs after deploy, two sessions:

| run | sections | tasks | tasks naming a section that exists | hallucinated | sunk rows |
|---|---|---|---|---|---|
| solo, 1 | 4 | 7 | 7 | 0 | **0** |
| solo, 2 | 4 | 6 | 6 | 0 | 1 |
| multi, 1 | 7 | 6 | 6 | 0 | 3 |

**The instruction is obeyed**: 19 of 19 tasks across three runs named a section that
exists in their own brief; validation nulled nothing. So §4.3's first pass condition
holds, and the render-time guessing is gone for good.

**§5's first risk is real and was accepted anyway.** Sections are coarser than topics, so
a section that produced ANY task is never sunk, and the discussion inside it is not shown.
Solo run 1 is the extreme: every section was claimed, sunk rows were 0, and
"Papakura is mid-program and progressing well" — a row the extraction path would have
carried — appeared nowhere in the email.

The alternative offered was bullet-level linkage (ask the model which BULLET each task came
from, sink the unclaimed bullets), costing a longer table: roughly 6 rows to 11 on the
measured session. **The owner chose to keep section granularity**, with the consequence
above quoted back to them in those words. Do not "fix" this later as if it were an
oversight; reopening it needs the owner, not a reviewer.

What the table is, after this ruling: **what is owed, plus whatever the brief discussed and
nobody took on**. Not a transcript of the day.
