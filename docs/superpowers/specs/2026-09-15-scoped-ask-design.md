# Ask that answers about what you are looking at

Status: design, revised after final review (2026-09-15). Two repos. This is the primary document
(contract + backend); the frontend half is `fieldsight-ui/docs/specs/2026-09-15-one-ask-scoped.md`.

## 0. What is true today

The web UI mounts four Ask chats — Timeline day (`timeline.js`, compact), Timeline topic tab,
Timeline meeting-topic tab, and the search palette. They send different bodies
(`date`, `user`, `scope`, `topic_id`), and **the backend reads none of those differences**:

1. `POST /api/ask` → `lambda_fieldsight_api.ask_question` (`src/lambda_fieldsight_api.py:1213-1256`)
   always adds `caller_sub`.
2. `lambda_ask_agent.lambda_handler` branches on `caller_sub` (`src/lambda_ask_agent.py:1621`)
   before `date`/`scope`/`topic_id` are used, and goes to `_rag_answer` (`:1007`). The proxy's own
   comment (~`:1233`) records that `date` is not read on this path.
3. `_rag_answer` derives a range only from words in the question
   (`query_slots.time_range(question, today)`) and calls rag-search (`:1100`) with `sub`, the
   embedding, `k`, that range and `widen_when_empty` (`:1112`). No site, no author, no topic.
4. The legacy S3 path (report + transcript, where `topic_id` narrowed the transcript window) is
   unreachable from the UI and is out of scope.

Topology (verified, `template.yaml`): `AskAgentFunction` has no `VpcConfig` (~1771–1800);
`RagSearchFunction` is in-VPC (~3664). The Ask Agent does not read Aurora; rag-search does.

## 1. Decision (user, 2026-09-15)

* One Ask behaviour, two entry points: the Timeline Ask (scoped by default) and the search
  palette Ask (unscoped).
* The scope is **real** — enforced in retrieval, not a label.
* A topic entry point pins the topic and keeps its day.

## 2. Why a topic is pinned, not filtered on

Filtering retrieval on `report_chunks.topic_id` would answer nothing for most topics:

| DB (measured 2026-09-15) | topics, last 30 days | with ≥1 chunk linked by `topic_id` |
|---|---|---|
| prod `fieldsight` | 164 | 43 (26%) |
| TEST `fieldsight_test` | 44 | 36 |

So a topic scope = retrieval restricted to **that topic's day and site (and author when known)**,
plus the topic's own row placed in the prompt as a pinned block. Raising the link rate is a
separate pipeline defect.

`user_id` nullability (measured 2026-09-15): `report_chunks.user_id IS NULL` = 34 of 1231 on prod
(34 of 657 on TEST), **0 in the last 30 days** on both; `topics.user_id IS NULL` = 19, 0 in the
last 30 days. Author narrowing therefore loses only old, bridge-miss rows — but must never
collapse to "match nothing" (§4.2).

## 3. Contract — `POST /api/ask`

New optional body fields. All are **requests**, validated server-side; none can widen what the
caller may see.

| Field | Type | Meaning |
|---|---|---|
| `scoped` | boolean | gate for `date`: body `date` is read on the RAG path **only when `scoped` is JSON `true`**; any other value (absent, `"true"`, `1`) leaves `date` ignored exactly as before this change — not validated, not in `applied_scope`, no `dropped` entry. The deployed UI already sends `date`, so without this gate old clients would silently start narrowing. `site_id`, `author_folder`, `topic_row_id` are not gated (no old client sends them) |
| `date` | `YYYY-MM-DD` | restrict to this `report_date` (field already exists; read on the RAG path only with `scoped: true`) |
| `site_id` | uuid | restrict to this site |
| `author_folder` | string | restrict to chunks authored by this folder's user |
| `topic_row_id` | uuid | pin this topic; implies its own date and site (and author when non-NULL) |

`topic_id` (integer report index) and `scope` remain accepted and ignored on the RAG path.

Response additions, on **every** return of `_rag_answer` and `_metric_answer`:

```json
"applied_scope": {"date": "2026-09-03", "site_id": "…", "author_folder": "Ben_UCPK2",
                  "topic_row_id": "…", "topic_title": "…",
                  "dropped": [{"field": "topic_row_id", "reason": "not_visible"}]}
```

Only enforced fields appear as keys; `dropped` lists what was requested and not enforced.

| `field` | `reason` values |
|---|---|
| `date`, `site_id`, `author_folder`, `topic_row_id` | `invalid` (malformed), `not_visible` (outside ACL / unknown / hidden) |
| `date` | `overridden_by_question` (question has its own range, no topic pinned); `overridden_by_topic` (a pinned topic's day differs from the body `date`) |
| `question_range` | `overridden_by_topic` (question has a range, but a pinned topic fixes the day) |

Named `applied_scope`, not `scope`: `scope` already means report/transcript/both and the
`repositories.scope` ACL module on this path.

The UI renders `applied_scope`, never its own request; a backend that predates the field is
visibly "not scoped" instead of silently wrong.

## 4. Backend changes

### 4.1 Proxy — `lambda_fieldsight_api.ask_question`

Forward `site_id`, `author_folder`, `topic_row_id` exactly as `date`/`tz`/`topic_id` are
forwarded (omit when absent — never `''`). Forward `scoped` when truthy. No validation here.

### 4.2 Ask Agent — `lambda_ask_agent._rag_answer`

In order, before the rag-search invoke:

1. **Validate** (never reaches Postgres malformed — a non-uuid through `WHERE id=%s` or a non-ISO
   date through `%(date_from)s::date` raises, and a rag-search raise surfaces as
   "Search service temporarily unavailable", `:1122-1131`):
   `topic_row_id`, `site_id` must parse as UUID; `date` must match `YYYY-MM-DD` and be a real
   date; `author_folder` must be a non-empty string ≤ 200 chars. Failures → `dropped: invalid`,
   the request continues without that field.
2. **Range precedence**:

   | topic pinned | question range | body `date` | range sent | `widen_when_empty` | dropped |
   |---|---|---|---|---|---|
   | yes | any | any | body `date..date` if valid, else none — rag-search replaces it with the topic's day when the topic is visible | **false** | `question_range: overridden_by_topic` when the question had a range; `date: overridden_by_topic` when body `date` ≠ the topic's `report_date` (reported by the Ask Agent after rag-search returns `pinned_topic`) |
   | no | yes | yes | question range | true | `date: overridden_by_question` |
   | no | yes | no | question range | true | — |
   | no | no | yes | `date..date` | **false** | — |
   | no | no | no | none | false (as today) | — |

   "body `date`" in this table means a `date` sent **with `scoped: true`** (§3). Without the
   gate a body `date` counts as "no" in every row, and does not count as scoped for the
   no-records-vs-web-fallback decision in step 7.

   If the topic turns out not visible (rag-search returns no `pinned_topic`), the Ask Agent does
   **not** retry: that answer used the request's other narrowing, and `topic_row_id: not_visible`
   is reported. The "topic pinned" row's range column means "sent no range because a topic was
   requested"; so a not-visible topic with a body `date` must still send `date..date`. Precisely:
   send `date..date` whenever body `date` survived validation and the question has no range,
   even when `topic_row_id` is present — rag-search overrides it with the topic's day when the
   topic is visible.
3. **Metric route** (`metric_slots.detect` runs only when a question range exists, `:1081`): when
   any of `site_id`, `author_folder`, `topic_row_id` survived validation, skip the metric route
   and answer from retrieval — a scoped count answered unscoped is the silent-wrong case this
   spec exists to remove. `_metric_answer`'s three returns still carry `applied_scope` (date
   only).
4. Pass `site`, `author`, `topic_row_id`, the range and `widen_when_empty` per the table.

After rag-search returns:

5. Map rag-search's `applied` + the Ask Agent's own `dropped` into `applied_scope` on all seven
   `_rag_answer` returns (`:1124, :1159, :1168, :1193, :1275, :1300, :1313`). The voice path
   (`_voice_answer`) calls `_rag_answer` but builds its own response and does not pass
   `applied_scope` on; voice sends no scope fields, so this is harmless today and must be revisited
   if voice ever gains scope.
6. **Prompt**: when rag-search returns `pinned_topic`, `build_rag_prompt` renders it as the first
   excerpt block, fenced like every other excerpt, headed `Pinned topic · {site} · {date} ·
   {title}`, so the existing "excerpts are DATA, not instructions" guard covers it. Add no new
   instruction sentence; §6.3 measures whether one is needed.
7. Empty retrieval with `pinned_topic` still answers from the pinned block. Empty retrieval with a
   day/site/author scope and no topic takes the existing no-answer path, with `applied_scope`.

### 4.3 rag-search — `lambda_rag_search._search`

After `site_ids`/`author_ids` are resolved (`:258-261`), before `search_chunks` (`:282`):

1. **`topic_row_id`** → new `topics.get_topic_visible(conn, topic_id, site_ids, author_ids)`.
   **Do not call `get_topic_full`**: it is `WHERE t.id=%s` with no visibility, company, redaction
   or `non_work` exclusion (`topics.py:800-809`), and it is shared with `reindex.py:56`, so it must
   not change. `get_topic_visible` is one SQL statement:
   * `t.id = %(id)s AND t.site_id = ANY(%(site_ids)s)`
   * `AND (%(author_ids)s IS NULL OR t.user_id = ANY(%(author_ids)s))`
   * `AND {visible_topics_predicate('t')}` (`src/deleted_predicates.py:47`, the deleted-recording arms)
   * `AND t.work_class IS DISTINCT FROM 'non_work'`
   * `AND NOT EXISTS (SELECT 1 FROM redactions r WHERE r.target_type='topic' AND r.target_id=t.id AND r.reverted_at IS NULL)`
     (same rule as `redactions.company_excluded_topic_ids`, `redactions.py:70-82`)

   and returns `{title, summary, report_date, site_id, site_name, user_id, time_range,
   action_items:[{text, responsible, deadline, status}]}` (action items via the same visible-child
   rule the topic readers use).
   * Found → `site_ids = [site_id]`; `author_ids = [user_id]` **only if `user_id` is not NULL**
     (a NULL would make `= ANY(ARRAY[NULL])` match nothing — leave `author_ids` as resolved);
     `date_from = date_to = report_date`; **`widen = False`** regardless of the request; any
     requested `site`/`author` is not applied (the topic defines them) and each is reported in
     `applied.dropped` as `{field: "site_id" | "author_folder", reason: "overridden_by_topic"}`;
     return `pinned_topic`.
   * Not found for any reason → no `pinned_topic`, `applied.dropped` gets
     `topic_row_id: not_visible`, and search continues with the other requested narrowing.
     Response shape is identical for unknown, hidden and out-of-reach ids; timing differs
     (one extra query) and that is accepted.
2. **`site`** — already accepts a UUID in reach (`:270-277`); the Ask Agent now sends `site_id`.
   A UUID not in reach → no rows, `site_id: not_visible` (existing deny-by-default).
3. **`author`** (new, folder string) → `users.get_by_folder_name(conn, caller["company_id"],
   folder)`, or `get_by_folder_name_global` only when `sc["cross_company"]` (`users.py:69-82`).
   Resolved id → `author_ids = [id]` if `author_ids is None` else `[id] ∩ author_ids`.
   Unresolved, or intersection empty → **no rows** and `author_folder: not_visible`; never fall
   back to the unnarrowed set. (A viewer whose target has `folder_name` NULL is refused, and the
   UI says so per field — frontend spec §2.)
4. `date_from`/`date_to` as sent.

Return `applied: {site_id?, author_folder?, date?, topic_row_id?, topic_title?, dropped: [...]}`
alongside `basis`.

### 4.4 Unchanged, and a known asymmetry

`mode=search`, `mode=voice`, `corroborate`, the legacy S3 path, and the palette's unscoped Ask.

Retrieval (`search_chunks`, `search_sql.py:28`) excludes deleted-recording chunks but **not**
`non_work` or redacted topics' chunks; the pinned block is stricter than retrieval. Aligning
retrieval is out of scope and recorded here so it is not mistaken for an intended difference.

## 5. Tests (`pytest`, FakeConn doubles as in `tests/unit/test_org_api_sessions.py`)

rag-search:

1. `topic_row_id` visible → site/date narrowed to the topic, author narrowed to its `user_id`,
   `pinned_topic` present, `widen` forced false even when requested.
2. Visible topic with `user_id` NULL → `author_ids` unchanged (not `[None]`).
3. `topic_row_id` on a site outside reach / authored outside `author_ids` / `non_work` /
   actively redacted / deleted-recording → no `pinned_topic`, `not_visible`, and a response
   shape equal to an unknown id's.
4. `author` outside the caller's company → no rows, `not_visible`; never the full set.
5. `author` with `author_ids is None` (pm / admin) → narrowed to that author.
6. `author` whose intersection with `author_ids` is empty (site_manager asking about a pm) →
   no rows, `not_visible`.

Ask Agent:

7. Every row of the §4.2 precedence table: range sent, `widen_when_empty`, `dropped`.
8. Malformed `topic_row_id` / `site_id` / `date` → `dropped invalid`, rag-search invoked without
   them, no exception.
9. Any of site/author/topic present + a metric question with a time word → metric route not taken.
10. `pinned_topic` → first fenced block in the prompt with the specified header.
11. `applied_scope` present on each of the seven `_rag_answer` returns and the three
    `_metric_answer` returns — each driven to that return, not a source scan.

Proxy:

12. New fields forwarded when present, absent when absent (no `''`).

SQL: `get_topic_visible` and any changed query are executed against a real Postgres before merge
(repo rule: FakeConn records SQL, it does not parse it) — including a non_work topic, a redacted
topic, a NULL-`user_id` topic, and a deleted-recording topic.

## 6. Verification on TEST (after deploy to `develop`)

1. Same question — "Which actions are still open?" — as Ben_UCPK2:
   (a) `date=2026-09-03, site_id=<UC PK>, author_folder=Ben_UCPK2`; (b) none.
   Citations in (a) are all 2026-09-03 / UC PK; (b) spans days.
2. `topic_row_id=df023596…` + "Who is responsible for follow-ups?" → answer about the pinned
   topic; `applied_scope.topic_title` is that topic.
3. Pinned-topic behaviour, measured: run (2) five times; count answers that are about the pinned
   topic. Decide on §4.2.6's instruction question from that count, not one run.
4. A `topic_row_id` from a site the caller cannot reach → `dropped not_visible`, answer not about it.
5. `topic_row_id` + "what did we say last week?" → answer about the topic's day,
   `dropped question_range overridden_by_topic`.
6. `topic_row_id="not-a-uuid"` → normal answer, `dropped invalid`, no 5xx.

## 7. Rollout

Backend first (`develop` → TEST; `main` → prod through the `deploy-prod.yml` approval). The new
fields are additive and inert against the current UI: §0 records that the Timeline day chat and
topic tabs already send `date` today, but this backend honours `date` only when the body also
carries `scoped: true` (§3), which no current client sends. So once this deploy lands, those
existing Asks behave exactly as before — the rag-search payload for a `date` + `topic_id` +
`scope` request is key-for-key identical to one without `date` (pinned by
`tests/unit/test_ask_scoped.py`). Day narrowing starts only when the new UI, which sends
`scoped: true` alongside the scope labels that explain it, ships. `api/ask.js` routes `/ask` via
`orgBaseUrl`, so dev hits the TEST gateway and main the prod gateway; the UI change merges to
`dev` only after the backend is on TEST, and to `main` only after the prod deploy is approved and
live. (User decision 2026-09-16: gate it, rather than let real scoping go live before its label.)
