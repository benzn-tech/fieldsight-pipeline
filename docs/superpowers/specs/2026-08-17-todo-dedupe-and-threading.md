# One commitment, said in several recordings

**Status:** proposed. Nothing is built.
**Requested:** 2026-08-17. **Rewritten twice on the same day**, both times because reading the
data contradicted the previous draft. §2 records what was wrong, so nobody re-derives it.

---

## 1. The phenomenon, measured

All figures from the prod database `fieldsight`, 2026-08-17.

On 2026-08-10 Ben_UCPK2 made several recordings. Three of them —
`sid87065733…`, `side58f9bd1…`, `sid100ddb05…` — each produced an action item with
**byte-identical text**:

```
23:37:20  Scaffolding -- inspect before Monday   (topic "Scaffolding Inspection",        sid…afa1ccf)
23:47:58  Scaffolding -- inspect before Monday   (topic "Scaffolding Safety Inspection", sid…6363965)
23:54:05  Scaffolding -- inspect before Monday   (topic "Scaffolding Inspection",        sid…60d6aa5)
```

The same three recordings also each produced a Mitre-10 procurement item. Two days later:

```
2026-08-12  Scaffolding safety inspection
```

**There is no extraction bug here.** Each recording is faithfully reporting what was said in
it. One man mentioned one outstanding job in three recordings that evening, and again two
days later.

Two consequences follow, and the first is the reason this spec was rewritten:

* **"Same-day duplicate" and "cross-day recurrence" are the same phenomenon.** The gap
  between mentions is a continuous quantity that happens to be 0 days sometimes. Designing
  a deduplicator and a threader as two mechanisms would be building one thing twice.
* **Text stability depends on the gap, and it flips.** Within the evening the todo text was
  byte-identical three times while the topic titles differed ("Scaffolding Inspection" vs
  "Scaffolding Safety Inspection"). Across two days the todo text drifted
  ("inspect before Monday" → "safety inspection") while the **topic title was
  byte-identical** ("Scaffolding Safety Inspection" both days). Whichever feature you pick,
  it is the stable one at one time scale and the noisy one at the other.

**Nothing is ever closed.** 2026-08-08 → 08-13: 136 action items, and every day
`count(status='open')` equals the total. Not one has left `open`.

**The existing topic-level queue:** 15 rejected, 4 pending, 0 confirmed.

## 2. What the two earlier drafts got wrong

Recorded so the next reader does not repeat either.

**Draft 1 concluded "title similarity is not the user's signal"** from the rejection of the
0.821 identically-titled pair, without reading the topics' contents. Reading them shows the
pair is the same outstanding job by any plain reading. A label whose subject you have not
read is not evidence about the rule; it is evidence about the labelling.

**Draft 2 proposed same-day deduplication with a `duplicate_of` pointer, and flipping the
threading unit from topic to todo.** Both were wrong:

* `action_items` cascade-delete with their topic, and `write_extraction_items` deletes and
  re-inserts every topic for a source key on **every pass — the live tier re-runs the same
  key on a ~90 s throttle while recording** (BUG-43). The duplicates span three source keys,
  so a `duplicate_of` between them points across two independently-churning lifetimes.
  Depending on an `ON DELETE` clause the draft never specified, that is either a raised FK
  error inside the prod write path *during recording*, a merge that silently un-does itself
  within the hour, or one key's rewrite destroying another key's rows. **There is no durable
  per-todo identity in this store** — id, topic_id and text all churn — so no pointer between
  todo rows can be durable state.
* The draft's survivor rule ("earliest by topic `occurred_at`") is undefined for **100 %** of
  rows: no writer passes `occurred_at`, so it is NULL on every extraction topic.
* Flipping the unit to the todo contradicts a decision this repo made **by measurement** and
  wrote down twice — `0032_topic_threads.sql:10-14` and `thread_match.py:11-16`: matching
  commitments to each other "produced the cross-product of two related topics… because the
  similarity lives in the subject while the promises differ every time", and topic-level
  matching "found ~8 real threads in its top 11". §1 above supplies a fresh example of
  exactly that drift across days. A three-paragraph argument does not overturn a
  measurement.

## 3. The shape that follows

One mechanism, one unit, and the gap decides how much confidence it may claim.

**Unit: the topic**, unchanged, because that is what was measured. The todo gets what it
needs — status carried forward — *through* the confirmed thread rather than by being the
matching key. Inside a confirmed thread, "which todo is which todo" is a small bounded
problem over a handful of rows, and a much easier one than matching across the whole day.

**Two regimes on one axis:**

| gap | text behaviour (measured, §1) | who decides |
|---|---|---|
| 0 days | todo text often byte-identical | **automatic**, exact match only |
| ≥ 1 day | todo text drifts; title steadier | **a person**, via the existing queue |

**The 0-day case needs no new storage.** It is a *render-time* collapse, not a write-time
merge: when several open todos in one `(site, date, user)` have identical normalised text,
show one, with a count. Nothing is written, so nothing can be destroyed by the next
re-extraction, and the FK problem in §2 does not arise. Recomputed on every read from rows
that are recreated on every write — the only design that matches how this store actually
behaves.

**Normalisation for the 0-day collapse:** lowercase, collapse internal whitespace, strip
trailing punctuation. **Exact after that, nothing fuzzier.** A missed collapse leaves a
visible duplicate somebody can complain about; a wrong collapse hides a commitment nobody
knows is gone.

⚠ Do **not** normalise by stripping to `[a-z0-9]`. That deletes every CJK character and
makes unrelated Chinese todos compare equal — this codebase has shipped that bug three times
(`memory:fieldsight-ascii-norm-erased-chinese`). Lowercase and whitespace only.

## 4. Where the collapse has to happen — all of it, or none

`duplicate_of IS NULL`-style filtering in one place and not another is this repo's standing
failure. A render-time collapse has the same exposure. Consumers of `action_items`:

| surface | collapse? |
|---|---|
| `topics.py:415` → `/timeline`, `/live-items`, org-api serialisation, session `open_action_count` | yes |
| `topics.py:568` → authority-flip timeline shim (**the prod customer read path**) | yes |
| `topics.py:647` `get_topic_full` → reindex → RAG chunk text | yes |
| `rollup.py:109` site open-action counts | yes |
| `threads.py:34/84/104` candidate corpus, thread facts, "raised N times" | **yes — or the recurrence count is inflated by the very duplicates the UI just hid** |
| `action_items.py` get/update, `content.py:60` correction | **no** — a hidden row must stay editable |

**And one surface that is not in the database at all:** the stop-recording confirmation
email builds its todo list from the S3 extraction artifact
(`_todos_from_topics`, `lambda_item_writer.py:385-403`), and RAG text comes from the same
JSON (`chunking.py:71`). A DB-side collapse leaves the email listing the scaffolding item
three times. Either collapse in artifact space too, or say in writing that email is out of
scope — do not discover it from a customer.

## 5. Status carried through a confirmed thread

What the customer asked for: confirming a recurrence should let a todo's status follow.

On confirm, the thread gains: first raised, times raised, and whether the earliest linked
todo is still `open`. That is enough to render "outstanding since Monday, raised twice" —
which is the actual product, and which the 136-open-items figure says nobody can see today.

Escalation (auto-raising priority after N recurrences or M days) is **not** in this spec. It
needs the thread data to exist and a rule nobody has stated.

## 6. Before any of this is built

**One question decides §3's second row.** The 0.821 pair — identical title, two days apart,
the same scaffolding inspection before the same Monday — was rejected. If that genuinely is
two separate jobs to this customer, then cross-day threading has no true positives to find
and only the 0-day collapse should be built. If it was swept out with the noise (13 items
cleared in 13 minutes, most of them cross-products of the 0-day duplicates), then the queue
needs the 0-day collapse *first* and re-asking afterwards.

Either way **the 0-day collapse comes first**, and it is the part that needs no labels, no
human, and no new storage.

## 7. Verification

1. **Answer §6 before writing code.**
2. The 0-day collapse, on a TEST day with induced duplicates — **not** by replaying
   2026-08-10, whose artifacts and rows live in the prod bucket and prod DB and are not
   reachable from TEST (`fieldsight_test` + `fieldsight-data-test`, BUG-38/PR #114).
3. **A collapse that collapses nothing must say so.** Silence is indistinguishable from a
   path that never ran — the failure this repo keeps recording.
4. Every surface in §4's "yes" column shows one row where three existed, **and**
   `rollup` / `threads` counts drop by the same amount. A UI that collapses while the count
   still says 3 is worse than not collapsing.
5. The count in "raised N times" must equal what a person counts by hand from that day's
   recordings. The count is the product.
6. Flag wiring: `ENABLE_TODO_COLLAPSE`, default off, repo variable → **both** `deploy.yml`
   and `deploy-prod.yml` → template → the org-api and item-writer envs, in one commit with a
   test reading the actual YAML (`memory:fieldsight-unwired-toggle-trap`).

## 8. Out of scope, as decisions

* Escalation rules (§5).
* Cross-site threading.
* Closing todos automatically. 136 open items is a symptom this makes visible, not one it fixes.
* Retiring `topic_thread_suggestions` — it stays until cross-day threading has produced
  confirmations, or there is nothing to fall back to.
