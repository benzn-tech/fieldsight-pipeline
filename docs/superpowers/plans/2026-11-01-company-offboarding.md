# Company offboarding — implementation plan

Spec: `docs/superpowers/specs/2026-11-01-company-offboarding-design.md` (rev 2). Branch per task
group off `develop`; every task is TDD (failing test first) and ends with the full unit suite
green. Local Postgres integration tests run one pytest at a time. No prod writes; prod rollout is
the owner's (§10 of the spec).

Ordering constraint: `sam deploy` runs before migrations, so every code task must behave
correctly against the pre-0092 schema (missing column/table → company treated as `active`,
side-effect tables absent → endpoint returns 503 `not migrated`, never 500).

## Task 1 — Migration 0092
- `src/migrations/0092_company_status.sql`: `companies` columns (`status` default `'active'` +
  CHECK, `status_changed_at`, `status_reason`, `billing_stopped_at`, `exited_at`);
  `company_status_events`; `company_status_cognito` (unique `(event_id, user_sub, action)`). No GRANT.
- Tests: migration applies on the local Postgres, re-apply is a no-op, existing rows read `active`.

## Task 2 — `tenancy_status` module + flag
- `src/tenancy_status.py`: `company_status(conn, company_id)` (savepoint, missing column → `active`),
  `caller_blocked(conn, caller)` (`company_inactive` / `account_archived` / None; platform_admin
  exempt), `enforce_enabled()` reading `COMPANY_STATUS_ENFORCE`; in shadow mode log
  `COMPANY_GATE_WOULD_BLOCK` and return None.
- Template: parameter + env var on every function that gets the gate (default off); deploy
  workflows pass `PROD_`/`TEST_` repo vars like existing flags.
- Tests: states × archived × platform_admin × flag on/off; missing-column path; aborted-transaction
  safety (a failing read must not poison the request transaction).

## Task 3 — Gate at caller resolution + whitelist test
- org-api dispatch (replace the archived-only check; keep `GET /me`), rag-search (every resolve
  site; blocked shape `{"chunks": [], "error": "company_inactive"}`), ws-connect, voice-resolve,
  `lambda_check_gaps_db.py:91`, `checklist_reports.py:147`.
- `tests/unit/test_every_caller_resolution_is_gated.py`: AST-scan `src/` for `get_user_by_sub`;
  an explicit table classifies each call site as caller resolution or target lookup; unclassified
  fails; caller resolutions must be followed by `caller_blocked` in the same function.
- Tests: ask-agent receives the `error` and does not take the web-offer path.

## Task 4 — Live-company choke point
- One helper (`tenancy_status.live_site_filter_sql` or a repository wrapper) used by memberships
  helpers, `scope.visible_scope`, `sites.list_company_sites` / `list_all_sites`, ws broadcast
  recipient queries, `_capture_site`, `get_me`, and own-folder reads (by the recording's site
  company). platform_admin bypasses.
- Integration tests (local Postgres): two companies with external memberships both ways; A
  inactive → B's people do not see A's sites or their own recordings on A's sites; B's own sites
  fully visible; reactivation restores.

## Task 5 — Refuse writes into an inactive company
- `create_member`, `add_external_member`, `ensure_membership` revive, member `unarchive`, site
  create/patch → 409 `company_inactive`.
- Tests per endpoint.

## Task 6 — Fix `unarchive_user`
- Restore memberships whose `archived_at` equals the user's `archived_at`; leave earlier-removed
  memberships archived.
- Integration test with both kinds of membership.

## Task 7 — Status endpoints
- `POST /api/org/companies/{id}/status`, `GET /api/org/companies/{id}/status-events`, `status` in
  `GET /api/org/companies`. platform_admin only; refuses the caller's own company; reason required;
  idempotent; status + audit row in one transaction; enqueues the Cognito task after commit;
  `exited` sets `exited_at` (first time) and `billing_stopped_at`; `stop_billing` honoured for
  `suspended`. Deletes the company's `ws_connections` rows on exit/suspend.
- Tests: auth matrix, own-company refusal, idempotency, audit atomicity, pre-migration 503.

## Task 8 — Cognito side-effect task
- LakeOps-style async task (`task: company_status_cognito`): for an event, list home-company users,
  upsert `company_status_cognito` rows, call `AdminDisableUser` + `AdminUserGlobalSignOut` (or
  `AdminEnableUser` on reactivation for the union of users disabled by events since the last
  `active`), mark done/failed with attempts; re-invocation resumes pending/failed rows only.
- Template IAM: add the three Cognito actions on the pool for the function running the task.
- Tests with a stubbed Cognito client: idempotent retry, partial failure visible, union
  computation across suspend→exit→active.

## Task 9 — Email suppression
- `lambda_finalize_claim` (skip the email path for an inactive site company; finalize work still
  runs), `lambda_session_report._send_email`, `lambda_extraction_backlog` external notices (hold,
  do not send, do not expire while inactive). Site company first, recipient home company for
  site-less items.
- Report generator: skip per-company summaries and per-user reports whose site company is inactive
  (late uploads still transcribed/extracted).
- Tests per sender; a test that an inactive company's late upload still produces extraction rows.

## Task 10 — Team page login badge
- `_cognito_login_status_by_sub` returns `Enabled` alongside `UserStatus`; badge logic uses
  `Enabled`. Test with stubbed `ListUsers` pages.

## Task 11 — Structured records export
- `company_records_export.py`: one JSONL file per table + `README.txt` under
  `exports/{company_id}/{export_id}/records/`; whitelist by company ownership, per-row re-check,
  tombstones honoured; resumable like `company_export`. Wire into the existing export task.
- Integration test: seeded cross-company data → zero foreign rows; every listed table present.

## Task 12 — Operator handover script
- `scripts/presign_company_export.py --company --export-id --days N (≤7) --profile`: presigns the
  export's parts and records with the operator's profile, prints a link list; refuses prod without
  `--allow-prod`; never runs in a Lambda.
- Unit test with a stubbed S3 client.

## Task 13 — TEST rollout and end-to-end
- Release to TEST (flag off), check shadow logs; create a throwaway company with owner-alias users
  only; flag on for TEST; run the spec §11 end-to-end list; reactivate; record results.
- Owner hand-off: prod release PR (flag off), shadow-log check after a few days, owner flips the
  flag; owner adds the `exports/` lifecycle rule.

## Task 14 — UI hand-off note
- Write the fieldsight-ui contract (status badge/actions, `company_inactive` full-page notice,
  `GET /me` shape) for the frontend session; no UI code in this repo.
