# Ask conversation history — design

Date: 2026-10-22. Line: ASR+LLM优化v3 (Ask orchestrator, web, voice).
Status: revision 3, Fable-reviewed twice (verdict: ready for a plan); §12 lists the findings.

## 1. Problem

The search palette's empty state lists "Recent searches": up to five strings in
`sessionStorage['fs.search.recent']` (`fieldsight-ui/scripts/composites/search-palette.js:209,363`).
Ask questions and keyword searches share that list (`doAsk` calls `saveRecent(q)`, 387-389).
Clicking an Ask entry sends the question again: a new model run, new cost, possibly a
different answer, and the conversation's follow-ups are gone. The list dies with the tab
and is not shared between the owner's iPhone and desktop.

The owner asked (2026-10-21) for Ask to keep conversations the way Claude does, starting
with 3–5 topics that roll over.

## 2. Decisions

| # | Decision | Why |
|---|---|---|
| D1 | Ask conversations ("threads") get their own list, separate from keyword searches. Keyword recents stay as they are; Ask questions stop being saved there. | A search is re-run by design; a conversation is re-read. |
| D2 | Stored server-side, per caller, keyed by the authorizer `sub` only. | Same list on every browser; no client-supplied identity. |
| D3 | At most **5 threads** per caller; a sixth evicts the oldest by last activity. A thread expires **30 days** after its last turn. At most **20 turns** per thread. | Owner's 3–5; 30 days bounds how long a stored answer outlives the facts. |
| D4 | Opening a thread **shows the stored turns**; it never re-asks. Each answer shows its date. A follow-up continues the thread with the stored turns as `history`. Each turn has **Ask again**, which runs the question as a new turn. | The point of history is reading, not paying again. |
| D5 | Opening a thread **re-checks every record source the answer was built from** (§6) against the caller's current access. A turn that fails is withheld, with Ask again offered. | A stored answer is a frozen copy of record content; deletion and access changes must reach it. |
| D6 | Storage: S3 prefix `ask_threads/` in the data bucket. **Writes** by the AskVoiceWorker (server truth). **Reads** by **org-api** (`/api/org/ask/threads…`), not the legacy gateway. | The legacy gateway is being retired and phase B removed its data routes; org-api is in-VPC with Aurora and already imports `scope`, `redactions`, `chunks`, so the D5 check runs in process with no new rag-search mode and no cross-line dependency. |
| D7 | Threads are **opt-in per request**: a turn is stored only when the request carries `thread_id` or `new_thread: true`. v1 sends them from the **search palette's AskChat only**. The docked Timeline AskChat, voice/device, and the legacy sync `/api/ask` never store. | The Timeline dock clears history on every day/project change (`ask-chat.js:1096-1124`) and would evict palette conversations under the cap of 5; an old UI must not create threads it cannot show. |
| D8 | Only successful turns are stored: `result` has no `error` and a non-empty `answer`. | `_work_screen` writes `done` for error results too (`ask_voice_turn.py:627-634`), including raw exception strings (`lambda_ask_agent.py:2053-2059`). |
| D9 | Behind `ASK_THREADS` (worker + org-api) and `FS_ASK_THREADS` (UI), default off; TEST first. | Prod also needs Ask v2 (async screen Ask) on prod. |

## 3. Storage

Key: `ask_threads/{thread_owner}/{thread_id}.json`.
- `thread_owner` = a new `thread_prefix(sub)` mirroring `owner_prefix` (16 hex of the sub hash, `ask_voice_turn.py:101-102`). `sub_hash` (12 hex) stays log-only.
- `thread_id` = 32 lowercase hex, minted server-side (`secrets.token_hex(16)`).

```json
{
  "v": 1,
  "thread_id": "…",
  "created_at": 1761000000.0,
  "updated_at": 1761000300.0,
  "scope": { …the first turn's request scope (context / site / everything)… },
  "turns": [
    {"turn_id": "…", "question": "…", "at": 1761000000.0,
     "request": {"offer_web": false, "web_intent": false, "history_len": 0},
     "result":  { …the `result` object `/api/ask/result` returns with status done… },
     "sources": {"record_keys": ["…"], "documents": [{"document_id": "…", "site_id": "…"}],
                 "report_days": [{"folder": "…", "report_date": "YYYY-MM-DD"}]}}
  ]
}
```

- `result` is stored as returned (`_terminal_body` gives `{status:'done', result}`, `lambda_fieldsight_api.py:467-472`), so the UI renders a stored turn with the code that renders a live one (§7).
- `sources` is the **retrieved-source manifest** (built as in §4 "Manifest"): every record source key, document and report day the answer was built from, not only the cited cards. The orchestrator can use evidence it did not cite (`shown` ≠ `citations`, `ask_orchestrator.py:1474-1478`), and web turns also carry record cards (`lambda_ask_agent.py:1842-1850`, `ask_orchestrator.py:423-432`). The manifest is server-side only and is never returned to the client.
- Size cap 256 KB per object. A turn that would cross it is stored with `result.answer` only and `result.truncated: true`.
- Offer turns (`web_offer: true`) and web-failed turns (`web.status: 'unverified'`) are stored; they pass D8 when `answer` is non-empty.
- No index object: the list is `ListObjectsV2` on the caller's prefix (≤ 5 keys + in-flight), objects read in parallel.

**Eviction and expiry.** After the worker creates a new thread object, if the caller has more than 5 it deletes the oldest by `LastModified` until 5 remain. The org-api list route skips (and deletes, bounded like `sweep_old`) objects older than 30 days. Backstop: an S3 lifecycle rule `ask-threads-expiry` on `ask_threads/` at 31 days. The data bucket is created outside the stack (`template.yaml:175-178`), so the rule goes into `scripts/wire-bucket-lifecycle.sh` (and its exclusion list) for TEST; prod is a numbered owner step, because the script refuses prod.

## 4. Write path

**Request.** `POST /api/ask/start` (screen) accepts `thread_id` (must match `^[0-9a-f]{32}$`, else 400) or `new_thread: true`. Plumbing that must change:
- gateway `_build_ask_payload` is a whitelist (`lambda_fieldsight_api.py:128-209`): add both fields;
- `ask_voice_turn._SCREEN_DROP` gets both, so they reach the turn state but not the RAG body (`ask_voice_turn.py:516,544,606`).

The client-supplied `thread_id` never confers identity: the object key is built from the authorizer `sub`, so it only ever addresses the caller's own prefix.

**Manifest.** Neither answer path hands the worker its retrieved sources today: `_rag_answer` returns the client dict only (`chunks`, `lambda_ask_agent.py:1415`, never leaves it) and the orchestrator's `shown` (`ask_orchestrator.py:1361,1417`) is local. Both paths gain an internal `result["_sources"]`: `_rag_answer` builds it from `chunks`, the orchestrator from `_card_items(shown)` plus its `documents`; each item gives `source_s3_key`, `site_id`, `document_id` as applicable (`site_id` is on chunk rows, `search_sql.py:169-170`, and on `_doc_hit`, `lambda_ask_agent.py:606-612`). `reports/{date}/{folder}/daily_report.json` keys also yield a `report_days` entry. The worker lifts `_sources` into the thread turn and **strips it from `result` before writing `done`**, always (flag on or off), so it never reaches a client; the legacy sync `/api/ask` path strips it too.

**Worker.** In `_work_screen`, when `ASK_THREADS` is on, the request asked for a thread, the turn is not cancelled and D8 holds, the worker appends the turn **before** it writes `done`:
1. `new_thread` → mint an id and create the object. `thread_id` given → read it. A **miss** (evicted or expired) or a thread already at 20 turns → mint a new id, create, and run eviction.
2. Put the object (read, append, put; two tabs racing on one thread can lose a turn — accepted, same as `write_convo`).
3. Write `done` with `result.thread = {"thread_id": …, "rolled_over": bool}` so the UI learns the id, including a new one after a miss or the 20-turn limit.
4. Eviction (list + deletes beyond 5) runs **after** `done`, off the user's path.

Added latency before the user sees the answer: one S3 read + one put on the worker client (`read_timeout=2`, one retry, `ask_voice_turn.py:66-67`), about 0.2–1 s; a timeout follows the failure branch below.

Appending before `done` means a cancel that lands after the append still leaves the turn stored while the user saw "cancelled". This is accepted and stated: the thread then shows a turn the user abandoned, which they can delete. The reverse (cancel after `done`, before the append) can no longer happen.

A failed append logs the exception type only; the turn still completes, with no `result.thread`.

**IAM.** AskVoiceWorker gets `s3:GetObject`/`PutObject`/`DeleteObject` on `ask_threads/*` and `ListBucket` with `s3:prefix` `ask_threads/*`.

**Logging.** No question, answer, source key or document id text. Lines carry `sub_hash`, `thread_id`, turn count, byte size.

## 5. Read routes (org-api, authorizer `sub` only)

| Route | Returns |
|---|---|
| `GET /api/org/ask/threads` | `{"threads": [{"thread_id", "title", "updated_at", "turns"}]}`, newest first. `title` = first question, cut to 80 chars. |
| `GET /api/org/ask/threads/{id}` | `{thread_id, scope, turns: [{turn_id, question, at, request, result \| withheld: true}]}`. `sources` is never returned. 404 when absent. |
| `DELETE /api/org/ask/threads/{id}` | 204, idempotent. |

- Flag off: 404.
- An **archived** caller is already refused before dispatch (`lambda_org_api.py:573`, every route but `GET /me`); inherited, not new, and tested.
- org-api is one `/api/org/{proxy+}` ANY event (`template.yaml:3374-3379`): no new API event. The wiring is the S3 grant: `s3:GetObject`/`DeleteObject` on `ask_threads/*` and `ListBucket` with `s3:prefix` `ask_threads/*`, verified with `aws iam simulate-principal-policy` on TEST before it is called working. The handler never swallows `ClientError`: an AccessDenied must surface as 5xx, not read as an empty list.
- Budget: list = 1 ListObjectsV2 + ≤ 5 GETs; thread GET = 1 GET + the §6 SQL (one query per kind, batched); both well inside org-api's timeout.

## 6. Re-check on open (D5)

For each stored turn, org-api evaluates its `sources` against the caller now, with the same primitives every search mode uses (`scope.visible_scope`, `redactions`, the live predicates in `search_sql.py`). A turn is **withheld** when any of the following holds:

1. A `record_keys` entry is tombstoned (`redactions.is_source_deleted(key)`, which prefix-matches, `redactions.py:284-292`).
2. A `record_keys` entry has no live `report_chunks` row with `site_id ∈ visible.site_ids` (and `user_id ∈ visible.author_ids` when the caller's grade restricts authors, `search_sql.py:59-60`). Checked by source key, never by chunk id: nightly re-ingest deletes and re-inserts a key's rows (`lambda_ingest.py:750`), so ids churn.
3. A `documents` entry no longer exists or its `site_id` is not in `visible.site_ids`.
4. A `report_days` entry has any recording tombstone for that folder and date: `redactions.deleted_source_prefixes(folder, date)` is non-empty (`redactions.py:212-235`; tombstones carry `extractions/{folder}/{date}/sid…`, not a site, 138-158). `reports/{date}/{folder}/daily_report.json` keys are day-level, so test 1 alone would let one deleted session inside a day survive (the deletion-leak pattern). No "after the turn" filter: stricter and simpler.

Manifest entries with no `source_s3_key` (the report family can store NULL, `search_sql.py:24`) are skipped by rule 2, not counted as missing.

If the check itself fails (DB error), every turn with any record or document source is withheld for that response (fail closed). Web-only sources (`web.sources`) are not checked.

Withheld turns keep the question and show: "A record behind this answer is no longer available to you." with Ask again.

## 7. UI (fieldsight-ui, `dev`)

- Palette empty state, flag on: **Recent conversations** (from `GET /api/org/ask/threads`, up to 5, with Delete) above **Recent searches**. `doAsk` stops calling `saveRecent(q)` for Ask; keyword searches still save.
- Clicking a conversation opens the palette's inline AskChat seeded with the stored turns, without calling `/ask/start`. New `AskChat` prop `initialMessages` (today it has only `initialQuestion`, which auto-sends, `ask-chat.js:1033-1035`), and a shared `messageFromResult(question, result, requestFacts)` used by both the live `send()` path and the seeded path. Today `send()` derives `zh`, `webOffer`, `sentHistory`, `scopeResponse` from request-side facts (`ask-chat.js:1341-1388`); those come from the stored `request` and `scope`.
- A stored offer turn's "Check the web" button re-asks as a new turn, exactly like a live one (it uses the stored history).
- A follow-up sends `thread_id` and the history from the seeded messages (`historyFromMessages`). The first palette question sends `new_thread: true`. When `result.thread.thread_id` differs from the current id (miss or 20-turn rollover), the chat switches to it and says "Started a new conversation".
- Plumbing: `api/ask.js` `ask()` rebuilds the body as a whitelist (198-257, the ui#316 trap) — add `thread_id`/`new_thread`; `_askAsyncTurn` returns `res.result`, which now carries `result.thread`, so no other change.
- The org-api base URL is the one the UI already uses (`orgBaseUrl`).
- Each stored answer shows "Answered {date}". Withheld turns render the §6 sentence and Ask again.
- Scope: a thread reopens in its stored scope; changing the scope starts a new thread (the existing "scope change clears history" rule).
- The docked Timeline AskChat sends neither field (D7): a new `AskChat` prop `threads: true` is set only by the palette instance (`search-palette.js:534-543`); without it the component sends no thread fields.

## 8. Security and privacy

- Identity from the authorizer `sub` only; `thread_id` addresses within the caller's prefix.
- Deletion is: user Delete, eviction at 5, 30-day expiry on read, the 31-day lifecycle rule. Member archive does **not** delete threads (archive is reversible); it blocks reads (§5). Account removal beyond archive does not exist today; when it does, it must delete `ask_threads/{thread_owner}/` and `ask_turns/{owner}/`.
- Recording deletion and access loss reach stored answers through §6, not a cascade.
- The source manifest stays server-side.

## 9. Compatibility

- Device / GrandTime, voice Ask, legacy sync `/api/ask`, docked Timeline Ask: unchanged; they never store (D7).
- New UI, backend without `ASK_THREADS`: org-api routes 404 → section hidden; `/ask/start` drops the fields (whitelist) → no `result.thread` → chat works as today.
- Old UI, backend on: no fields sent → nothing stored.
- Search line's round-1 contract (`round1_calls`, `_scope_args`) and `rag_tools_dispatch` untouched; no new rag-search mode.
- Prod order: Ask v2 on prod → `ASK_THREADS` on prod (owner) → lifecycle rule on prod (owner) → UI flag on `main`.

## 10. Tests

- Unit (pipe): `_sources` built by both answer paths and stripped before `done` and from sync `/api/ask`; thread create/append/evict/expire/20-turn rollover/size cap on a fake S3; D8 (error and empty results are not stored); append-before-done ordering; `thread_id` validation and whitelist pass-through; `_SCREEN_DROP` keeps the fields out of the RAG body; org-api routes 404 off, 403 archived, list/get/delete; each §6 rule withholds and fail-closed; manifest never returned; caplog proves no question/answer/key text is logged.
- Integration (local Postgres): the §6 SQL against real `report_chunks`/redactions rows, including a re-ingested key (still visible) and a tombstoned session inside a day report (withheld).
- UI (`node --test`): two separate sections; Ask no longer saved to recents; opening renders stored turns with no `/ask/start`; follow-up sends `thread_id` + history; rollover switches id; withheld rendering; docked chat sends no thread fields; flag off = today.
- TEST live (model calls stated first): ask, follow up, open on another browser, reopen without a model call, delete a cited recording and see the turn withheld.

## 11. Out of scope

Voice/device threads, Timeline-dock threads, search inside history, sharing, renaming, more than 5 threads, deletion on account removal (no such path yet).

## 12. Review log (Fable, revision 1 → 2)

1 routes moved to org-api; 2 D8 success-only; 3 deletion stated as it is, archived callers refused; 4 manifest + §6 rules incl. web record cards, documents, day-level keys, no chunk ids; 5 `thread_id` plumbing named; 6 `result` stored as-is, `initialMessages` + `messageFromResult`; 7 `thread_prefix` (16 hex); 8 lifecycle via script, prod owner step; 9 opt-in threads, docked chat excluded, miss mints a new id; 10 append before `done`, cancel race stated; 11 off the legacy gateway; 12 `saveRecent` removed for Ask; 13 budget stated; 14 worker-write kept over a client POST so the manifest is server truth.

Re-review (rev 2 → 3): rule 4 keyed on folder+date via `deleted_source_prefixes`; `_sources` manifest hand-off named in both answer paths; archived refusal inherited; single proxy event + IAM simulate + no swallowed ClientError; eviction after `done` and latency stated; `threads` prop; NULL keys skipped.
