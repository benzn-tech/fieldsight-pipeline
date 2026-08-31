# Spec: the questions whose answer is a number

**Status:** proposal, third draft.

* The **first** had eight blocking findings and they held up against the code:
  two of its four data sources would have computed **wrong numbers**, and its
  deletion predicate could not match a row.
* The **second** fixed those, and was then falsified by measurement — §4 rested
  on a risk that does not exist, and on a number this document invented.

**Date:** 2026-08-31, measured 2026-09-01.
**Repo:** `fieldsight-pipeline`.

> Ask can find what was said. It cannot say how much there was of it. Three
> questions from the customer, all refused today:
>
> * *"昨天我录制了多长时间？"* — how long did I record yesterday
> * *"我前天在 XX 会议上拍照片了吗？"* — did I take photos at that meeting
> * *"昨天有多少 QA 的问题？多少 safety 的问题？"* — how many quality / safety issues

---

## Read this first: measured on prod, 2026-09-01

The first draft ended with a measurement that had to run before any code, and
warned the answer might kill the feature. It ran, over the RDS Data API against
the prod database. It did not kill it, and it falsified one of this document's
own sections.

| | measured | consequence |
|---|---|---|
| days-with-topics that have `recordings` rows | **33 / 39 = 84.6%** | viable. But 15.4% have none, so the **third kind of zero** (§7) is real, not theoretical |
| sessions with a usable duration | **286 / 287 = 99.7%** (281 from `duration_s`, 5 from the span, 1 unmeasurable) | `duration` is answerable |
| chunk rows → sessions | **2823 → 287, a fold of 9.8×** | counting rows would have reported nearly ten times the recordings a person made |
| | | *(3127 is every `kind`; 2823 is audio+video, which is what a session count is over. The first pass mixed the photos in.)* |
| `findings.domain` unlabelled | **0 / 189** | **§4's original justification was false — see below** |
| findings on NULL-author topics | **5 / 189 = 2.6%** | small, real, and named rather than dropped |
| topics from the nightly report path | **25 / 267 = 9.4%** | the fallback in §3 is not theoretical |

And the one that settles §3 outright — the two paths are **disjoint**:

| topic source | topics | with `findings` | with `safety_observations` |
|---|---|---|---|
| live extraction | 242 | 139 | **0** |
| nightly report | 25 | **0** | 4 |

A findings-only count reports **zero** for every report-path topic, and four of
them genuinely carry safety items.

## Why the questions are refused, and why better retrieval would not help

Ask retrieves **text chunks** and asks a model to answer from them alone. None of
the three has a textual answer anywhere: a duration is a number in a column,
nobody says it in the meeting; a photo count is a row count; a safety total is an
aggregate. Retrieval returns the semantically nearest chunks — about anything —
and the model correctly reports the excerpts do not contain the answer.

**So this is a routing problem, not a retrieval problem.** The precedent is the
*alerts* route in `fieldsight-ui`'s `scripts/composites/ask-chat.js` (routing spec
3.5), answered client-side without the agent, with the answer stating which route
produced it. That file is in the **UI repo, not this one** — an implementer will
not find it here.

## The rule this spec exists to enforce

> **The number is computed. A model may never produce one.**

Identical to `basis` (2026-08-30): a model asked "how long did I record" will say
"about two hours" with the fluency of a fact, and nothing downstream can tell.
Every figure comes from a query; the sentence around it comes from a template.

---

## 1. Where the route runs

Aurora is in-VPC. `lambda_ask_agent` is non-VPC and holds no connection;
`lambda_rag_search` is in-VPC and holds the pooled one. BUG-36 forbids the
in-VPC side from calling out, so the only workable topology is the one Ask
already uses:

**intent detection in `lambda_ask_agent` → invoke `lambda_rag_search` with a new
`mode` → SQL there → the numbers come back down the existing channel.**

No new function, no new IAM, no new endpoint. The first draft left this unsaid,
which would have let a plan propose something BUG-36 forbids.

## 2. Intent detection: rules, and a safe failure direction

Same discipline as `query_slots`, for the same reason: a rule that misses returns
nothing and the question falls through to RAG — today's behaviour, so a miss
costs nothing new. A classifier that misfires routes a retrieval question to a
counter and answers the wrong question confidently.

A metric question is a **quantity interrogative over a countable noun**:

| metric | EN | ZH |
|---|---|---|
| `duration` | how long, how much time, total time | 多长时间, 多久, 录了多久 |
| `count_photos` | how many photos/pictures | 多少张照片, 几张照片, 拍了几张 |
| `count_sessions` | how many recordings/sessions | 几段录音, 录了几次 |
| `count_findings` | how many safety/quality issues | 多少安全问题, 多少质量问题, 几个 QA 问题 |

The range comes from `query_slots.time_range`, already shipped. A metric question
with no resolvable range is answered over the caller's whole visible history and
**says so**, exactly as an unfiltered Ask does today.

## 3. Where each number lives — and the two the first draft got wrong

### `duration` and `count_sessions` — reuse `recordings.day_stats`, do not write SQL

The first draft proposed counting `recordings` rows filtered on `started_at`.
**Both halves are wrong, and this repository has already written down why**
(`repositories/recordings.py:78-118`):

1. **A row is a CHUNK, not a recording.** Under the chunk-session contract one
   session arrives as N ~30s chunks, each its own row
   (`…_sid{32hex}_c{NNNN}.wav`) — "a single 9-minute meeting is 21 rows.
   Reporting 21 would tell the user they made 21 recordings." `day_stats` folds
   on the sid parsed from the key, with the key itself as the fold value when
   there is no sid, so legacy one-row recordings are unchanged.
2. **`started_at` is the wrong clock.** It is `timestamptz` (UTC) while "yesterday"
   is the device's local day — "filtering by UTC would move an evening recording
   to the next day (the BUG-37/finalize-timezone family)". The shipped match is
   the **date segment of the s3_key**, which is the clock the topics and the
   timeline are on, and it is the same clock `query_slots.time_range` produces.

The first draft cited `idx_recordings_user_started` as evidence the query was
"already indexed". That index serves precisely the query this repo rejected.

**So: extend `day_stats` to a range rather than one date, and use it.** It
already returns `{sessions, duration_s}`, already scopes by `company_id`, and
already restricts to `kind IN ('audio','video')`.

### `count_photos` — `recordings`, not `topic_photos`

| source | answers |
|---|---|
| `recordings` where `kind='photo'`, matched by s3_key date segment | "how many photos were taken that day" |
| `topic_photos` | "which photos are attached to this subject" |

*"Did I take photos"* is the first. The binder for the second is
`photo_binding.py` (shared by item_writer and ingest) with
`PHOTOS_PER_TOPIC_CAP = 10` and **cascade to the next-nearest eligible topic**
when a topic is full — the first draft claimed a cap of 5 that silently drops
photos, citing `lambda_report_generator.py:422`, which its own docstring marks
retired. The real reasons `topic_photos` under-reports are narrower: the ±2min
window tolerance, and topics with no parseable `time_range` never binding at all.

### `count_findings` — findings first, `safety_observations` as fallback

The first draft said "`findings`, and ONLY `findings`". **That would report zero
on every day the nightly report path ran**, and three facts make it so:

- `lambda_ingest.py:687` still passes `safety=_map_safety(...)` to `upsert_topic`,
  so the nightly report path **still writes `safety_observations`**. The
  "frozen legacy" claim is true only of the item_writer path
  (`lambda_item_writer.py:791-801`).
- `lambda_ingest.py:662` calls `delete_topics_for_source_prefix(extraction_prefix)`
  — the nightly report supersedes that day's live-extraction topics — and
  `findings.topic_id` is `ON DELETE CASCADE`. **The day's findings are deleted by
  the nightly run**, and ingest never writes findings.
- The shipped read path already knows this. `repositories/topics.py:369-378`
  sources the safety slot from safety-domain findings first and falls back to raw
  `safety_observations` rows **only when a topic has zero of them**, keeping the
  legacy query "precisely to preserve that legacy-topic fallback".

**So the count mirrors the shipped read semantics per topic — findings first,
fallback second — rather than inventing a third opinion about which table is
true.** Measured on prod: extraction topics carry findings and **zero**
safety_observations; report topics carry safety_observations and **zero**
findings. The two paths are disjoint, so a findings-only count does not
under-report by a margin — it reports nothing at all for one of them. A dashboard showing safety items beside an Ask answer saying "zero" is
the failure this paragraph exists to prevent.

`observations` (0006, `author_sub NOT NULL`, `status open/closed`) stays out: it
is **filed by a person**, and "how many did we raise" is a different question
from "how many did the system find". Merging them silently is the error this
section is about.

### The join the first draft never wrote out

`findings` has **no date and no user**: only `topic_id`. So "yesterday" is
`topics.report_date` and "I" is `topics.user_id` — **and `topics.user_id` is
nullable** (migration 0003). `repositories/findings.py:116-119` warns about
exactly this shape: reaching the tenant through `users` "would also drop every
NULL-author row silently."

A worker on SELF scope asking "how many safety issues did I have yesterday" must
not silently lose the NULL-author rows. They are **counted separately and named**,
under the same rule as §4.

## 4. Every count carries its denominator — for the reasons that are true

A hard requirement. But the first draft justified it with a risk that does not
exist, and the correction matters more than the rule.

**What the first draft claimed.** `findings.domain` is nullable and
model-produced, so a safety count would silently omit whatever the extractor
failed to label; the answer should therefore read *"3 labelled safety, 7
unclassified"*.

**What is true.** `domain` is NULL on **0 of 189** findings on prod. The
extractor labels every one. **The "7 unclassified" was invented** — a number
written to make the rule look necessary, which is precisely the failure this
repository has documented at length and which the review of this spec was asked
to hunt for. The author produced one anyway.

The rule survives, because three things do make a count mean less than it looks:

1. **The third zero.** 15.4% of days with topics have no `recordings` rows at
   all (§7). "You recorded nothing yesterday" and "there are no rows for
   yesterday" are different facts and this route must not merge them.
2. **`recordings.site_id` is nullable.** A `SITE`-scoped PM filtering on
   `site_id` silently excludes untagged recordings; a worker on `user_id = self`
   sees their own untagged rows. **The same question has a different denominator
   for the two roles** (BUG-43 is why untagged rows exist), and the answer must
   say which it used.
3. **NULL-author findings** — 2.6% on prod. A SELF-scoped worker asking about
   their own safety issues cannot see them through `topics.user_id`, and
   `repositories/findings.py` already warns that reaching the tenant through
   `users` "would drop every NULL-author row silently". They are counted and
   named, not dropped.

Where a denominator is zero, it is not printed. A line reading "and 0
unclassified" on every answer is noise, and noise is how a caveat stops being
read.

## 5. Scope, and the denominators that differ by role

`repositories/scope.visible_scope` already resolves `ALL | SITE | SELF+WORKERS |
SELF`, is the primitive `lambda_rag_search.py:102` uses, and matches
`acl.visible_user_scope`. This route makes the same call, so what a person can
count is what they can already read. It also has a **platform_admin
cross-company** branch the first draft omitted.

- **PM / regional** → `SITE` — the whole project, which is what was asked for.
- **site_manager** → `SELF+WORKERS`.
- **worker** → `SELF`.
- **admin / gm** → `ALL`; **platform_admin** → cross-company.

**"我" defaults to the caller alone** even when their scope is wider.

**One asymmetry must be stated in the answer, not hidden:** `recordings.site_id`
is nullable. A `SITE`-scoped PM filtering on `site_id` silently excludes
un-tagged recordings, while a worker on `user_id = self` sees their own untagged
rows. The same question therefore has a different denominator for the two roles.
BUG-43 is why untagged rows exist; this route may not pretend they do not.

## 6. Deleted recordings: the predicate does not exist yet

The first draft said "a join or a predicate against the tombstones". Written
literally, **it matches nothing**:

- tombstones carry `target_key` in the **extraction key space** —
  `extractions/{folder}/{date}/{base}` (`lambda_org_api.py:_source_prefixes_for`);
- `deleted_predicates` matches `{alias}.source_s3_key LIKE r.target_key || '%'`;
- `recordings` has **no `source_s3_key` column at all**, and its `s3_key` lives in
  `users/{folder}/…/{date}/{base}…`.

So the predicate never matches a row, and a count would silently include deleted
recordings — the disclosure §6 exists to prevent, and the same key-space mismatch
this repo has already shipped once.

**The translation must be written, not assumed**: parse `folder`, `date` and
`session_base` from both sides and compare those, or reuse the deletion mirror,
which is keyed on exactly `(folder, date, sessionBase)` and is already read by
non-VPC and in-VPC callers alike. **The plan must carry a test that a count goes
DOWN after a delete** — that is the only assertion which proves the translation
rather than the intention.

`findings` needs no new work: `visible_topics_predicate` already covers it.

## 7. Three kinds of zero, not two

- **"you recorded nothing"** — a true zero.
- **"you can see nothing"** — an empty `visible_scope`.
- **"there are no rows for this day, and that does not mean there was no
  recording"** — the RealPTT path never registers rows, days before migration
  0009 have none, and lake-fed environments have none.
  `lambda_org_api.py:5785-5795` deliberately emits **no** zero in that case.

This route must distinguish all three. Reporting the third as the first is the
misleading zero the shipped KPI was changed to stop producing.

## 8. What must be measured before any code

One query, in-VPC, carried by a script like the ones already in `scripts/`:

1. **Row coverage** — of the days that have topics, what fraction have any
   `recordings` rows? This is the question that decides whether the feature is
   viable at all (see the top of this document).
2. **`duration_s` population** — of the session-folded recordings, what fraction
   can produce a duration from `duration_s` or from `ended_at - started_at`?
3. **`findings.domain` population** — what fraction is NULL? That decides whether
   §4's denominator is a footnote or the headline.

If (1) is poor, stop: it is a collection problem and this spec is premature.

## 9. What this does not do

- **No new table, no migration, no new IAM, no new endpoint.**
- **No model call** on this route.
- **No metric beyond the four.** "How many action items", "how many people spoke"
  are the same machinery and can follow.
- **No comparison arithmetic.** *"How much longer than last week"* is out.
- **No meeting-name resolution.** *"At the XX meeting"* needs a name → session
  time-span resolver that does not exist; `query_slots` yields calendar days
  only. **v1 answers the whole day and says so**, which is honest and is not what
  was asked for. Building the resolver is its own spec.
- **No mobile change.**

## 10. Decisions still open

1. **Does "我" ever widen?** Default is the caller. Whether *"how long did the
   team record"* ships in v1 is a product call; the ACL supports either.
2. **Does `count_findings` also report `observations`** — the human-filed ones —
   as a separate line? They answer a neighbouring question, and merging them
   silently is the error §3 warns about.
3. **How loudly to report the third zero.** "No rows for that day" is accurate and
   unhelpful; naming the capture path that produced it is more useful and leaks
   an implementation detail into an answer.
