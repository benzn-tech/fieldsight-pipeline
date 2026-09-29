# Who is on site today — roster narrowing, implementation plan (backend)

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking. Every task is test-first, and every task ends with the **revert-to-red check**: put the production change back the way it was and confirm the new test fails before moving on (CLAUDE.md, "Replay the actual defect").

**Goal:** A per-(site, NZ day) roster that the voice matcher narrows against. With a roster, an on-site person wins ties and an off-roster name is never `confirmed`; without one, matching is byte-for-byte today's behaviour.

**Spec:** `docs/superpowers/specs/2026-09-30-on-site-roster-design.md`. This plan corrects the spec where the code contradicts it (§Spec corrections) and the corrections win.

**Tech stack:** Python 3.12 Lambdas, psycopg3 + Aurora Postgres, SAM `src/template.yaml`. Phase 1 needs **one migration, one repository, one new writer op field, one org-api route family; no new S3 prefix, no new trigger, no new IAM.**

Verified against worktree `e4934aa` (branch `feat/self-introduction-suggestions`, 2026-09-30). Migration `0071` is taken by that branch; this plan uses `0072`. Re-check at merge (`test_migrate_ordering.py::test_two_files_sharing_a_version_have_a_defined_order` exists because collisions have shipped twice).

---

## Spec corrections (read before Task 1)

1. **Site and date are per SESSION, not per turn, and both already reach the matcher.** `lambda_item_writer.py:949-953` resolves one `site` per extraction (the BUG-41 ladder) and `:1309` passes it as `site_id` into `_request_match`; `speaker_match_request.build` (`:118`, `:127`) stamps `date` and `site_id` on every part of the request. `_from_match_artifact` (`lambda_speaker_embed.py:1213,1221`) reads both, but forwards **only `site_id`** to the writer's `profiles` op. The roster lookup therefore needs one extra key (`date`) on that invoke — nothing else has to be plumbed. A session that crosses NZ midnight is dated by its S3 key (`extractions/{folder}/{date}/…`, `session_scope.parse_extraction_key`), i.e. the device wall clock at record start; the roster is looked up for that one day. Accepted.

2. **Narrowing belongs in the WRITER, not the embedder.** The embedder is non-VPC, cp312, has no psycopg (`template.yaml:3772-3778`) and receives profiles on the invoke result (`lambda_voiceprint_writer.py:376-401`). The writer's `_profiles` is the only place that has a connection and already takes `site_id`. So: the writer reads the roster and returns **one boolean per profile, `on_roster`**; the embedder decides. `_profiles` keeps returning the full consented list (spec consumer 1: "narrows, never blocks").

3. **"Margin computed against the subset only" is unsafe as written, and here is the rule that is safe.** If `decide_name` were run over the on-roster subset only, an unsigned visitor whose voice is nearest to an on-roster profile would be **confirmed as that person** — the exact wrong-confident-name this layer exists to refuse, and it contradicts the spec's own consumer 2. The rule this plan implements in `_match` (`lambda_speaker_embed.py:642-651`, beside the existing `tentative`-profile cap):
   - Score every profile (unchanged). Take the full-pool winner `w`.
   - If `w` is **off-roster**: keep `decide_name`'s answer but cap the status at `tentative` (consumer 2). `effective_margin` is unchanged because `len(scores)` is unchanged.
   - If `w` is **on-roster**: re-run `decide_name` over the **on-roster subset**; that result stands. The subset is small so `effective_margin(k)` returns `DEFAULT_MIN_MARGIN` (`voiceprint_utils.py:159-161`) — which is the whole benefit: the runner-up that used to be an absent colleague is no longer counted. The full-pool duration check, the `DEFAULT_ABSENT_FLOOR` check and the company floor all still run inside that second call.
   - If the roster is empty/absent (`on_roster` missing on every profile): exactly today's code path. The test for this asserts the *same* `Decision` object fields, not merely the same status.
   - A single on-roster profile with `w` on-roster is the 1:1 case: `decide_name` returns `tentative` with `margin=None`, and `_from_match_artifact:1271-1282` then **drops the turn** (`skipped_no_runner_up`). Accepted and documented: a one-person roster narrows nothing; the full-pool result (with its runner-up) is used instead when the subset has fewer than 2 profiles.

4. **Attendance is keyed by profile, resolved at lookup time, not at write time.** The spec stores `voiceprint_id`/`user_id` on the attendance row. A profile enrolled *after* the roster was written (the roadmap's "sign in at the gate = enrol") would never be on the roster that day. Rows keep `user_id`/`voiceprint_id` as **hints**, but the writer's lookup joins by three arms: `p.id = a.voiceprint_id` OR `p.user_id = a.user_id` OR `lower(p.display_name) = lower(a.display_name)`. The name arm is what makes `manual` useful on day one, since most profiles today are subcontractors with `user_id IS NULL` (`repositories/voiceprints.py:193-200`).

5. **Consumer 3 (intro-dialog prefill) is a frontend concern with one backend field.** `list_name_suggestions` (`lambda_org_api.py:2599-2625`) returns `heardName`; the dialog lives in the fieldsight-ui repo. Phase 1 adds `rosterNames: [...]` (the day's names for that session's site) to that response and stops. No fuzzy matching server-side: "Petrus Pang" → "Petros Pan" is a UI choice.

6. **The connector hop does not need an S3 prefix.** The spec's `attendance_requests/{company}/{date}/{source}.json` + "the existing in-VPC writer pattern" would be a **new hand-wired S3 notification** (BUG-33; `scripts/wire-s3-events.sh` has one block per prefix, and both deploy workflows run it) plus `s3:GetObject` grants on a writer that has none today (`VoiceprintWriterFunction` policies: `VPCAccessPolicy` only, `template.yaml:3874-3875`). The permitted and precedented direction is **non-VPC connector → direct `lambda:InvokeFunction` on the in-VPC writer** (BUG-43 note 4; exactly how `SpeakerEmbedFunction` reaches `VoiceprintWriterFunction`, `template.yaml:3828-3832,3841-3844`). Phase 2/3 connectors use a new writer op `attendance_upsert`, synchronous, and log the writer's returned counts. S3 stays out of it. (Rows are not biometric, so the "never at rest outside the column" rule that forces the synchronous *profiles* hop does not apply; the direct invoke is chosen for wiring cost, not for privacy.)

7. **`manual` needs an org-api route family.** The spec says "nothing" is needed. org-api is the only in-VPC function the frontend can reach and it already owns site ACL (`_resolve_site_param` / `_allowed_site_ids`, `lambda_org_api.py:5955-6010`). Phase 1 ships `GET/POST/DELETE /api/org/sites/{id}/attendance`.

8. **NZ date pitfalls, three of them, all avoided by never deriving the date server-side from `now()`:**
   - Manual: the client sends `date=YYYY-MM-DD` (an NZ calendar day it displayed). Default when absent is `nz_time.nz_today()` — never `datetime.now().date()` (BUG-37).
   - Graph (Phase 2): `start.dateTime` arrives with `start.timeZone` (often `UTC`) → parse, `nz_time.to_nz(dt).date()`. An all-day event spans `[start, end)` where `end` is exclusive; a multi-day event yields one row per NZ day in that range.
   - Sign-in systems (Phase 3): ISO timestamps with offset → `to_nz(...).date()`. The spec's test "a 07:00 NZDT sign-in lands on that NZ day" is 18:00 UTC the previous calendar day; the test must construct that timestamp in UTC so it goes red under a UTC date.
   - Idempotency key includes `attend_date` (spec UNIQUE) so a source that reports the same sign-in twice on either side of midnight yields two rows, which is correct: they were on site both days.

9. **Runtime Secrets Manager is new to this stack.** Every credential today is deploy-time injected (`{{resolve:secretsmanager:…}}`, BUG-36); there is **no** `secretsmanager:GetSecretValue` IAM statement anywhere in `template.yaml`. Phase 2's per-company Graph secret is the first runtime read. It is legitimate because the connector is non-VPC, but the policy must be resource-scoped to a name prefix (`fieldsight/${Stage}/attendance/*`), and `test_template_*` needs a pin that the in-VPC functions never gain that action.

10. **Scheduled connector shape.** Non-VPC scheduled functions exist (`OrchestratorFunction`, `ExtractionBacklogFunction`, …). Copy `FloorRecomputeFunction`'s `Events.Schedule` with `Input` and `State: !If [ShouldEnableSchedules, …]` (`template.yaml:3950-3957`) so `test_template_schedule_state.py` passes, and keep `Timeout` under the interval (`test_template_timeout_invariants.py::test_a_scheduled_function_is_shorter_than_its_interval`).

---

## Phase 1 — table, repository, consumer, manual source (implementable now)

### Task 1: Migration `0072_site_attendance.sql`

**Files:** `src/migrations/0072_site_attendance.sql`; `tests/unit/test_migration_0072_site_attendance.py`; `tests/integration/test_site_attendance_sql.py`.

- [ ] **Step 1 (red).** Unit text test, same shape as `test_migration_0056_day_recording_segments.py`: no other `0072_`; `CREATE TABLE IF NOT EXISTS site_attendance`; columns `company_id uuid NOT NULL REFERENCES companies(id) ON DELETE CASCADE`, `site_id uuid NOT NULL REFERENCES sites(id) ON DELETE CASCADE`, `attend_date date NOT NULL`, `display_name text NOT NULL`, `user_id uuid REFERENCES users(id)`, `voiceprint_id uuid REFERENCES speaker_voiceprints(id) ON DELETE SET NULL`, `employer_name text`, `source text NOT NULL CHECK (source IN ('graph_calendar','signonsite','hammertech','1breadcrumb','manual'))`, `source_ref text NOT NULL`, `first_seen_at timestamptz NOT NULL DEFAULT now()`, `last_seen_at timestamptz NOT NULL DEFAULT now()`, `created_by uuid REFERENCES users(id)`, `UNIQUE (company_id, source, source_ref, attend_date)`, and an index `(company_id, site_id, attend_date)`.
- [ ] **Step 2.** Write the migration with a header comment explaining: NZ date (never a UTC date), `user_id`/`voiceprint_id` are resolution hints (correction 4), why `ON DELETE SET NULL` on the profile (a withdrawal must not delete the fact that a person was on site).
- [ ] **Step 3 (integration, `db` fixture from `tests/conftest.py`, rolled back).** UNIQUE dedupes a second upsert; CHECK refuses `source='outlook'`; `voiceprint_id` referencing a deleted profile becomes NULL.
- [ ] **Step 4.** Run `pytest tests/unit/test_migration_0072_site_attendance.py`. Revert-to-red: delete the file, confirm red, restore.

### Task 2: Repository `src/repositories/site_attendance.py`

**Files:** new module; `tests/unit/test_site_attendance_repo.py` (FakeConn/FakeCursor copied from `test_voiceprints_repo.py`, positional results — read its warning).

Functions:
- `upsert(conn, company_id, site_id, attend_date, rows, source, created_by=None) -> {"inserted", "updated"}` — `INSERT … ON CONFLICT (company_id, source, source_ref, attend_date) DO UPDATE SET last_seen_at = now(), display_name = EXCLUDED.display_name, employer_name = COALESCE(EXCLUDED.employer_name, site_attendance.employer_name), user_id = COALESCE(site_attendance.user_id, EXCLUDED.user_id), voiceprint_id = COALESCE(site_attendance.voiceprint_id, EXCLUDED.voiceprint_id)`. A `manual` row's `source_ref` is `lower(trim(display_name))` so re-typing a name is idempotent. Resolution at write: `users.resolve_display_name` (ambiguous → NULL, never a guess); `voiceprint_id` via a company-scoped `speaker_voiceprints` lookup by `user_id`, else by unique `lower(display_name)` among `status <> 'withdrawn'`.
- `for_day(conn, company_id, site_id, attend_date) -> list[dict]` — the roster.
- `remove(conn, company_id, site_id, attend_date, row_id) -> int` — manual rows only (`source='manual'`); connector rows are the source's to remove.
- `on_roster_profile_ids(conn, company_id, site_id, attend_date) -> set[str]` — the three-arm join from correction 4, `status <> 'withdrawn'`, returned as strings. **Raises on a missing company_id** (`_require_company` pattern); returns `set()` when the roster is empty — and the caller treats an empty set as "no roster", not "nobody" (the empty-list-means-no-filter trap, deliberately inverted here: empty narrows nothing).

- [ ] **Step 1 (red).** Tests: SQL text carries the UNIQUE conflict target verbatim; `manual` source_ref is the normalised name; ambiguous name stores `user_id NULL`; `on_roster_profile_ids` SQL has all three arms and `status <> 'withdrawn'`; company id required.
- [ ] **Step 2.** Implement. **Step 3.** Revert-to-red on the three-arm join (remove the name arm; the test must fail).
- [ ] **Step 4 (integration).** In `tests/integration/test_site_attendance_sql.py`: insert a profile with `user_id NULL` and `display_name 'Sam Yu'`, a manual row `'sam yu'`; assert `on_roster_profile_ids` returns it (the name arm works against a real `lower()`), and the CASE/`::uuid` parameters prepare (CLAUDE.md "Assert on SQL text where a double cannot type-check").

### Task 3: Writer — `_profiles` returns `on_roster`

**Files:** `src/lambda_voiceprint_writer.py:376-401`; `tests/unit/test_lambda_voiceprint_writer.py` (beside `test_fetching_profiles_hands_the_companys_floor_to_the_embedder`).

- [ ] **Step 1 (red).** Test: with `event={"op":"profiles","company_id":CO,"site_id":S,"date":"2026-09-30"}` and `site_attendance.on_roster_profile_ids` patched to return `{VP_A}`, the reply has `profiles[i]["on_roster"] is True` for A and `False` for B, and `reply["roster_size"] == 1`. Test: with no `site_id` or no `date`, `on_roster` is **absent** from every profile (not `False`) and `on_roster_profile_ids` is never called. Test: an exception from the roster read is logged and the reply is the no-roster shape (narrows, never blocks — a roster outage must not stop matching).
- [ ] **Step 2.** Implement inside the existing `get_connection()` block; the roster read is wrapped in `try/except Exception: logger.exception(...)`.
- [ ] **Step 3.** Revert-to-red.

### Task 4: Embedder — forward `date`, apply the roster rule

**Files:** `src/lambda_speaker_embed.py:1220-1221` (add `"date": date` to the profiles invoke), `:612-659` (`_match`); `tests/unit/test_lambda_speaker_embed.py`; `tests/unit/test_embedder_writer_contract.py`.

- [ ] **Step 1 (red), contract seam first.** `test_embedder_writer_contract.py::test_no_field_crosses_this_seam_unread_in_either_direction` — extend so `date` on the `profiles` op and `on_roster` on the reply are both read. Then in `test_lambda_speaker_embed.py`, with `embed_audio`/`_window_audio` stubbed to a fixed vector:
  - **Tie-break:** two profiles at similarity 0.60/0.58 (margin 0.02 < 0.15), B on-roster, A off → result names B; without `on_roster` keys → `tentative` A (today's result, all fields equal).
  - **Off-roster cap:** A off-roster wins by 0.40 with the floor satisfied → `tentative`, reason mentions the roster.
  - **Single on-roster profile:** subset of 1 → the full-pool decision is used (documented fallback).
  - **Duration and absent floor still apply** under the roster (a 2 s turn is `unknown`; a best of 0.20 with no company floor is `unknown`).
- [ ] **Step 2.** Implement per correction 3. Put the rule in a pure helper `voiceprint_utils.decide_with_roster(scores, on_roster: set, duration_s, floor)` so it is testable without audio and the embedder's `_match` only calls it.
- [ ] **Step 3.** Revert-to-red: remove the `date` key from the invoke; the contract test must fail. Remove the cap; the off-roster test must fail.

### Task 5: org-api — the `manual` source

**Files:** `src/lambda_org_api.py` (dispatch near `:806` `/sites/{id}/voice`; handlers beside `list_site_voice`); `tests/unit/test_org_api_site_attendance.py` (follow `test_org_api_speaker_corrections.py` for the caller/conn doubles).

Contract (see §Frontend): `GET /api/org/sites/{id}/attendance?date=YYYY-MM-DD`, `POST` body `{"date","names":[{"displayName","employerName"?}]}`, `DELETE /api/org/sites/{id}/attendance/{rowId}?date=…`.

- [ ] **Step 1 (red).** Site ACL via `_allowed_site_ids` (403 for a site outside scope); roles `_CORRECTION_ROLES` (`:1911`) for POST/DELETE, any member for GET; `date` validated with `REPORT_DATE_RE`, defaults to `nz_time.nz_today()` (test patches `nz_time.nz_now` to 2026-09-30 11:30 UTC = 2026-10-01 00:30 NZDT and asserts the default is `2026-10-01`); blank names 400; 50-name cap; response lists rows with `source`, `resolved: {"userId","voiceprintId"}` so the UI can show who is unknown to the directory; DELETE refuses a non-manual row with 409.
- [ ] **Step 2.** Implement; every write inside `conn.transaction()`.
- [ ] **Step 3.** Revert-to-red on the ACL check (remove it; the 403 test must fail).
- [ ] **Step 4.** `list_name_suggestions`: add `rosterNames` per suggestion (site from the session via `_site_from_meeting_session`-equivalent lookup in org-api; empty list when unknown). Test: names appear; absent site → `[]`.

### Task 6: Real-DB verification (before merge)

- [ ] Run `pytest tests/unit -q` and confirm the count went **up** by the new tests (CLAUDE.md: match the workflow's pip install line first; onnxruntime/pglast differ locally).
- [ ] Apply `0072` on `fieldsight_test` inside a rolled-back RDS Data API transaction (CLAUDE.md "Testing"); run the Task 2 Step 4 SQL by hand; confirm `ON CONFLICT` target infers the UNIQUE index; rollback.
- [ ] On TEST after deploy: `POST` a roster for a site/day that has a finalized session, re-emit that session's match request (`aws s3 cp … --metadata-directive REPLACE` on its `voiceprint_requests/…/match-*.json`), and read the embedder log line `match(shadow): … would name …` before/after. **The reply must list `roster_size`**; a log that does not mention the roster means the field did not cross the hop (the 2026-08-17 seam lesson).
- [ ] Confirm `ItemWriterFunction`'s IAM is untouched and `VoiceprintWriterFunction` gained no policy (`git diff src/template.yaml` is empty for Phase 1).

---

## Phase 2 — Microsoft Graph connector — **BLOCKED on the owner's Entra app registration**

**What the owner must provide before any code is written:**
1. An Entra ID **multi-tenant** app registration (`signInAudience: AzureADMultipleOrgs`) with **application** permission `Calendars.Read` (not delegated — the connector runs on a schedule with nobody signed in). Give: tenant id, client id, and a client secret or certificate.
2. A decision on **whose calendar** defines "on site": (a) a per-site shared mailbox/room the customer invites people to, or (b) each member's calendar filtered by a site keyword/location. (a) needs one mailbox per site and `Calendars.Read` on it; (b) needs `Calendars.Read` for every user and a matching rule the customer maintains. The plan assumes (a) until told otherwise.
3. Each customer's Global Admin performs **admin consent** once (the URL is `https://login.microsoftonline.com/common/adminconsent?client_id=…`); the connector stores the granted tenant id against `companies.id`.
4. A Secrets Manager naming rule: `fieldsight/${Stage}/attendance/graph/${company_id}` holding `{tenant_id, client_id, client_secret, mailbox}`.

**Tasks when unblocked (test-first, each with revert-to-red):**
- [ ] `src/connectors/graph_calendar.py`: client-credentials token (`https://login.microsoftonline.com/{tenant}/oauth2/v2.0/token`, scope `https://graph.microsoft.com/.default`), `calendarView?startDateTime=&endDateTime=` for the NZ day expressed in UTC (`nz_time.from_nz`), paging via `@odata.nextLink`, attendees → rows (`display_name = attendee.emailAddress.name`, `source_ref = f"{event.id}:{attendee.emailAddress.address}"`), NZ-day expansion for all-day/multi-day events (correction 8). Pure function over a fake HTTP client; unit tests use recorded JSON fixtures.
- [ ] `src/lambda_attendance_connector.py` (non-VPC, scheduled): loop companies with a Graph secret, call the connector, **direct-invoke** `VoiceprintWriterFunction` with `{"op":"attendance_upsert","company_id","site_id","attend_date","source":"graph_calendar","rows":[…]}`, log the writer's counts per company. Site mapping: mailbox → `sites.id` lives in the secret payload (owner decision 2).
- [ ] Writer op `attendance_upsert` (calls `site_attendance.upsert`); add to `lambda_handler`'s op list and to `test_embedder_writer_contract`'s known-op set.
- [ ] `template.yaml`: `AttendanceConnectorFunction` — no `VpcConfig`, `Timeout: 300`, `Events.Schedule: rate(1 hour)` during work hours (`cron(0 17-23,0-7 * * ? *)` = 05:00–19:59 NZST; mirrors the orchestrator's `:1501`), `Input: '{"op":"sync"}'`, `State: !If [ShouldEnableSchedules, ENABLED, DISABLED]`; policies: `lambda:InvokeFunction` on the writer ARN, `secretsmanager:GetSecretValue` on `arn:aws:secretsmanager:${Region}:${AccountId}:secret:fieldsight/${Stage}/attendance/*`. New test `test_template_attendance_connector_iam.py`: the connector has no `VpcConfig` and no `PG*` env; no in-VPC function has `secretsmanager:GetSecretValue`.
- [ ] Verification on TEST with the owner's own tenant: one invited attendee must appear in `GET …/attendance` within one schedule tick.

## Phase 3 — SignOnSite / HammerTech / 1Breadcrumb — **BLOCKED on API access confirmation**

**What must be confirmed per vendor before design:** that an API exists for customers (not only partners), the auth model (API key vs OAuth), whether a "who is currently signed in at site X on date D" query exists or only a webhook stream, rate limits, and the site identifier the vendor uses (so `sites` gains an `external_refs jsonb` mapping — one small migration when the first vendor is confirmed).

The shape is fixed by Phase 2: one `src/connectors/<vendor>.py` pure module per vendor, the same scheduled connector Lambda dispatching by which secrets exist, the same `attendance_upsert` writer op with `source` set per vendor and `source_ref` = the vendor's sign-in id. `employer_name` from a sign-in system is stored with the row and, on enrolment, may seed `speaker_voiceprints.employer_source='sign_on_site'` (already a legal value, `repositories/voiceprints.py:51`).

---

## Frontend — minimal manual entry (fieldsight-ui repo; API contract only)

Lives in `fieldsight-ui`, integration branch `dev`. A site-day page: a date picker (NZ day string, never a `Date` → `toISOString` round trip — BUG-19), a name list with employer, add/remove.

```
GET    /api/org/sites/{siteId}/attendance?date=2026-09-30
  200 {"site": "<uuid>", "date": "2026-09-30",
       "rows": [{"id": "<uuid>", "displayName": "Sam Yu", "employerName": "Cassidy",
                 "source": "manual", "resolved": {"userId": null, "voiceprintId": "<uuid>|null"}}]}

POST   /api/org/sites/{siteId}/attendance
  body {"date": "2026-09-30", "names": [{"displayName": "Sam Yu", "employerName": "Cassidy"}]}
  200 {"inserted": 1, "updated": 0, "rows": [...]}     (idempotent per lower(trim(name)))
  400 blank name / bad date / > 50 names   403 site not in scope or role below site_manager

DELETE /api/org/sites/{siteId}/attendance/{rowId}?date=2026-09-30
  200 {"removed": 1}   409 row is not source=manual
```

Also: `GET /api/org/name-suggestions` rows now carry `rosterNames: ["Sam Yu", …]`; the dialog may prefill from it. **Remember `scripts/api/*.js` whitelists request bodies** — add `date`, `names`, `employerName` there or they are silently dropped.
