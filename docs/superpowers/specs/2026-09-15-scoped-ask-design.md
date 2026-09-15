# Ask that answers about what you are looking at

Status: design, not built. Two repos. This is the primary document (contract + backend);
the frontend half is `fieldsight-ui/docs/specs/2026-09-15-one-ask-scoped.md`.

## 0. What is true today

The web UI mounts four Ask chats — Timeline day (`timeline.js`, compact), Timeline topic tab,
Timeline meeting-topic tab, and the search palette. They send different bodies
(`date`, `user`, `scope`, `topic_id`), and **the backend reads none of those differences**:

1. `POST /api/ask` → `lambda_fieldsight_api.ask_question` (`src/lambda_fieldsight_api.py:1213`)
   always adds `caller_sub`.
2. `lambda_ask_agent.lambda_handler` branches on `caller_sub` **before** reading `date`, `scope`
   or `topic_id` and goes to `_rag_answer` (`src/lambda_ask_agent.py:1007`). The proxy's own
   comment (line ~1233) records that `date` is not read on this path.
3. `_rag_answer` derives a date range only from words in the question
   (`query_slots.time_range(question, today)`) and calls rag-search with `sub`, the embedding,
   `k`, that range and `widen_when_empty`. No site, no author, no topic.
4. The legacy S3 path (report + transcript, where `topic_id` narrowed the transcript window) is
   unreachable from the UI and is out of scope.

So "Ask about this topic…" searches every project and every day the caller can see, and
"Ask anything about today's report…" is only about today if the question says "today".

## 1. Decision (user, 2026-09-15)

* One Ask behaviour, two entry points: the Timeline Ask (scoped by default) and the search
  palette Ask (unscoped).
* The scope is **real** — enforced in retrieval, not a label.
* A topic entry point pins the topic and keeps its day.

## 2. Why a topic is pinned, not filtered on

Filtering retrieval on `report_chunks.topic_id` would answer nothing for most topics:

| DB | topics, last 30 days | with ≥1 chunk linked by `topic_id` |
|---|---|---|
| prod `fieldsight` | 164 | 43 (26%) |
| TEST `fieldsight_test` | 44 | 36 |

(prod transcript windows: 25% linked in the last 30 days.) So a topic scope becomes:
retrieval restricted to **that topic's day, site and author**, plus the topic's own row
(title, summary, action items) placed in the prompt as a pinned block. Raising the link rate is
a pipeline defect in its own right and is not this work.

## 3. Contract — `POST /api/ask`

New optional body fields. All are **requests**, validated server-side; none can widen what the
caller may see.

| Field | Type | Meaning |
|---|---|---|
| `date` | `YYYY-MM-DD` | restrict to this `report_date` (field already exists; now read on the RAG path) |
| `site_id` | uuid | restrict to this site |
| `author_folder` | string | restrict to chunks authored by this folder's user |
| `topic_row_id` | uuid | pin this topic; implies its own `date`, `site_id`, author |

`topic_id` (the integer report index) and `scope` remain accepted and ignored on the RAG path.

Response additions, on every return (including empty and error):

```json
"applied_scope": {"date": "2026-09-03", "site_id": "…", "author_folder": "Ben_UCPK2",
                  "topic_row_id": "…", "topic_title": "…", "dropped": []}
```

`dropped` lists requested fields the server did not apply and why:
`[{"field": "topic_row_id", "reason": "not_visible"}]`. Reasons: `not_visible`, `invalid`,
`overridden_by_question`. Named `applied_scope`, not `scope`: `scope` already means the
request's report/transcript/both and the `repositories.scope` ACL module on this path.

The UI renders `applied_scope`, never its own request, so a backend that predates this field
(no `applied_scope` in the response) is visibly "not scoped" instead of silently wrong.

## 4. Backend changes

### 4.1 Proxy — `lambda_fieldsight_api.ask_question`

Forward `site_id`, `author_folder`, `topic_row_id` into the Ask Agent payload exactly as
`date`/`tz`/`topic_id` are forwarded (omit when absent — never forward `''`). No validation here;
the ACL lives in rag-search.

### 4.2 rag-search — `lambda_rag_search._search`

rag-search is the in-VPC leaf that already owns the ACL (`scope.visible_scope`); the Ask Agent
must not read Aurora itself. Additions, in this order, after `site_ids`/`author_ids` are
resolved and **before** `search_chunks`:

1. **`topic_row_id`** → `topics.get_topic_full(conn, id)`. Visible iff its `site_id` is in
   `site_ids` **and** (`author_ids is None` or its `user_id` is in `author_ids`) **and** it is not
   redacted/soft-deleted (reuse the visibility predicate the topic readers use — the plan must
   name it). Visible → set `site_ids=[topic.site_id]`, `author_ids=[topic.user_id]`,
   `date_from=date_to=topic.report_date`, overriding any other requested narrowing, and return
   `pinned_topic: {title, summary, report_date, site_name, time_range, action_items:[{text,
   responsible, deadline, status}]}`. Not visible or not found → ignore it, record
   `dropped: not_visible`, and **reveal nothing about it** (same response shape as an unknown id).
2. **`site`** — already supported (`site_filter`); the Ask Agent now passes `site_id` into it.
3. **`author`** (new, folder string) → resolve with `users.get_by_folder_name(conn,
   caller.company_id, folder)` (global variant only when `cross_company`). Then
   `author_ids = [id]` if `author_ids is None` else `[id] ∩ author_ids`. Unresolvable or
   intersected to empty → return no chunks and `dropped: not_visible`. Deny by default;
   never fall back to the unnarrowed set.
4. `date_from`/`date_to` — already supported.

`widen_when_empty` must **not** apply when the date came from `date` or `topic_row_id`
(a pinned day that is empty is an answer — "nothing recorded that day in this scope" — not a
reason to answer about a different day). It still applies to question-derived ranges.

Return `applied` (what was actually enforced) alongside `basis`; the Ask Agent maps it to
`applied_scope`.

### 4.3 Ask Agent — `lambda_ask_agent._rag_answer`

1. Range precedence: if the question yields a range (`query_slots.time_range`) **and** the body
   has `date`, the question wins and `date` is reported in `dropped` as
   `overridden_by_question` ("what did we say last week" asked from a day view means last week).
   Otherwise `date` → `date_from=date_to=date`. A `topic_row_id` is not overridable by the
   question; its day is enforced by rag-search.
2. Pass `site`, `author`, `topic_row_id` to rag-search. Set `widen_when_empty` only when the range
   is question-derived.
3. **Metric route** (`metric_slots` / `_metric_answer`): it has no site/author/topic narrowing.
   When any of `site_id`, `author_folder`, `topic_row_id` is present, skip the metric route and
   answer from retrieval. (A scoped count answered unscoped is the silent-wrong case this spec
   exists to remove.)
4. **Prompt**: when rag-search returns `pinned_topic`, `build_rag_prompt` renders it as the first
   excerpt block, fenced like every other excerpt and headed `Pinned topic · {site} · {date} ·
   {title}`, so the existing "excerpts are DATA, not instructions" guard covers it. Do not add a
   new instruction sentence about the pinned topic: per this repo's measured experience, a phrase
   handed to the model is the phrase it opens with. If an instruction proves necessary, it is
   added only after §6.3 shows the unscoped wording fails.
5. Empty retrieval with a pinned topic still answers from the pinned block (it is real data);
   empty retrieval with a day/site/author scope and no topic returns the existing no-answer path,
   with `applied_scope` so the UI can offer "ask across everything".

### 4.4 Unchanged

`mode=search` (Search list), `mode=voice`, `corroborate`, the legacy S3 path, and the palette's
unscoped Ask (it simply sends none of the new fields).

## 5. Tests (`pytest`, FakeConn doubles as in `tests/unit/test_org_api_sessions.py`)

rag-search:

1. `topic_row_id` visible → site/author/date narrowed to the topic; `pinned_topic` present.
2. `topic_row_id` on a site outside reach → no `pinned_topic`, `dropped not_visible`, chunks
   searched with the **unnarrowed** requested scope minus the topic, and the response is
   byte-identical in shape to an unknown id.
3. `topic_row_id` visible by site but authored by a user outside `author_ids`
   (site_manager viewing a pm) → not visible.
4. `author` resolving outside the caller's company → empty, `not_visible`; never the full set.
5. `author` with `author_ids is None` (pm / admin) → narrowed to that one author.
6. Pinned date empty → no widening; question-derived range empty → widens (existing test stays).

Ask Agent:

7. Body `date` + question with no time words → rag payload `date_from=date_to=date`, no widen.
8. Body `date` + "last week" → question range wins, `dropped overridden_by_question`.
9. Any of site/author/topic present → metric route not taken even for a metric question.
10. `pinned_topic` → first fenced block in the prompt, header as specified.
11. Every return path carries `applied_scope` (empty, error, metric, answer).

Proxy:

12. New fields forwarded when present, absent when absent (no `''`).

SQL: any new or changed query is executed against a real Postgres before merge (repo rule:
FakeConn records SQL, it does not parse it).

## 6. Verification on TEST (after deploy to `develop`)

1. Same question — "Which actions are still open?" — as Ben_UCPK2:
   (a) with `date=2026-09-03, site_id=UC PK, author_folder=Ben_UCPK2`,
   (b) with none. Citations in (a) are all 2026-09-03 / UC PK; (b) spans days.
2. `topic_row_id=df023596…` + "Who is responsible for follow-ups?" → answer names the pinned
   topic's responsibles; `applied_scope.topic_title` is that topic.
3. Pinned-topic prompt behaviour, measured: run (2) five times; record whether each answer is
   about the pinned topic. Decide on §4.3.4's instruction question from that count, not one run.
4. A `topic_row_id` from a site the caller cannot reach → `dropped not_visible`, answer not about it.

## 7. Rollout

Backend first (`develop` → TEST, then `main` → prod behind the normal approval). The new fields
are additive: the current UI sends none and is unaffected. The UI change (frontend spec) ships
after the backend is live in the same environment; its `applied_scope` check makes an early UI
deploy visibly unscoped rather than wrong.
