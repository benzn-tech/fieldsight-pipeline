# Derived roster — implementation plan (backend)

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking. Every task is test-first, and every task ends with the **revert-to-red check**: put the production change back the way it was and confirm the new test fails before moving on (CLAUDE.md, "Replay the actual defect").

**Goal:** `site_attendance.on_roster_profile_ids` returns the union of the explicit roster (#969) and two derived sources — people who recorded at this site on this NZ day, and people a human named at this site in the last N NZ days — behind a three-segment-wired switch that, when `off`, leaves #969 byte-for-byte.

**Spec:** `docs/superpowers/specs/2026-09-30-derived-roster-design.md`. Where the code contradicts the spec, the corrections below win.

**Worktree:** `C:\Users\camil\Dropbox\wt-roster`, branch `feat/derived-roster` off `origin/develop` at `bf93db7`. No migration, no new S3 prefix, no new trigger, no new IAM. Touches: `src/repositories/site_attendance.py`, `src/lambda_voiceprint_writer.py`, `src/template.yaml`, both deploy workflows, four test files.

---

## Spec corrections (read before Task 1)

1. **`recordings` has no date column; the day is the S3 key's date segment, never `started_at`.** Migration `0009_recordings.sql` gives `started_at timestamptz` (UTC) and `s3_key`. Every reader in `src/repositories/recordings.py` filters the day by the key segment and says why (`:112-118`, `:179-183`, `:335-340`, `:442-446`: "filtering by UTC would move an evening recording to the next day"). The key is `users/{folder}/{kind}/{date}/{file}` (`lambda_org_api.py:847`), so the day is `split_part(r.s3_key, '/', 4)`. That segment is the device's wall clock — the same clock the matcher's `date` is on (`session_scope.parse_extraction_key`, `extractions/{folder}/{date}/…`), so the spec's arm 2 needs no timezone conversion at all. The spec's "07:00 NZDT (previous UTC day) counts" test is therefore written as: a row whose `started_at` is `2026-09-29 18:00 UTC` but whose key date is `2026-09-30` counts for `2026-09-30`; a row whose key date is `2026-09-29` does not, whatever its `started_at` says.

2. **`speaker_turn_names` knows no site. Its `session_base` is the canonical `sid{32hex}`** (`turn_name_overlay.session_base:67-83`; `speaker_match_request.py:96` normalises before the writer sees it), and `meeting_session.session_id` is the same 32 hex without the prefix (`0026_meeting_session.sql`). The join is `t.session_base = 'sid' || ms.session_id`. `meeting_session.site_id` is nullable and **is NULL for every session opened offline** (CLAUDE.md BUG-43 root cause 3: `/open` is fire-and-forget; the VAD sidecar opens with `site_id=None`, `repositories/meeting_session.py:30-44`). `recordings.site_id` is the authority (BUG-41). Arm 3 therefore resolves a session's site as `ms.site_id`, falling back to a recording of that session (`recordings.s3_key LIKE '%\_sid{hex}\_c%'`, scoped by `r.company_id`, `r.user_id = ms.user_id`, `r.site_id = <site>` — the `idx_recordings_site` path). The spec's "session at this site" is a two-step lookup, not a column.

3. **Arm 3's "last N NZ days" is the SESSION's NZ day, anchored on the roster day, not on `now()`.** `speaker_turn_names.created_at` is when a human typed the name (possibly days after the meeting); `meeting_session.opened_at` (timestamptz, UTC; `COALESCE(opened_at, created_at)` for sidecar-opened rows) is when the session happened. Convert in SQL with `(… AT TIME ZONE 'Pacific/Auckland')::date` — the same zone `nz_time.TZ_NAME` names — and bound it by `BETWEEN %(day)s::date - %(lookback)s AND %(day)s::date`. Anchoring on `attend_date` rather than `nz_today()` keeps a re-run of an old session deterministic and makes the "3 days / 20 days" tests independent of the wall clock.

4. **`source = 'correction'` is the right human filter, with one widening.** The asserted row is `correction` (`lambda_voiceprint_writer.py:158`), propagation is `correction_propagation`, the matcher writes `voiceprint_match` (`:452`), inheritance `label_inheritance` (`:598`); a confirmed self-introduction or name proposal delegates to `speaker_corrections` (`lambda_org_api.py:2742`) and so also lands as `correction`. Rows written before 0040 have `source NULL` and are excluded, correctly. **But** a `correction` row's `voiceprint_id` may be NULL — 0040 dropped the NOT NULL precisely for a correction that created no profile — so arm 3 joins `p.id = t.voiceprint_id OR (t.voiceprint_id IS NULL AND lower(p.display_name) = lower(t.display_name))`, the same name arm #969 uses. `superseded_at IS NULL` is mandatory: `withdraw` (`voiceprints.py:779`), `record_turn_name` (`:964`) and the un-name path (`:1027`) all supersede rather than delete.

5. **Arm 2 cannot rely on `speaker_voiceprints.user_id` alone.** Most profiles have `user_id IS NULL` (`0045` header; `voiceprints.py:694-700`; #969's own reason for its name arm). Arm 2 therefore matches the recorder's profile by `p.user_id = r.user_id` OR (`p.user_id IS NULL AND lower(p.display_name) = lower(concat_ws(' ', u.first_name, u.last_name))`) — `concat_ws`, not `||`, because `'Ben' || ' ' || NULL` is NULL (`users.py:278`). Two profiles sharing a name both land on the roster; that widens, and widening is the safe direction (a duplicate becomes its own runner-up and refuses, `voiceprints.py` "asymmetry" note).

6. **This query runs more often than "per match request" suggests.** `_request_match` (`lambda_item_writer.py:1518`) is not gated on tier (`:1448` gates only the email), so a match request goes out on every extraction write, live ones included (~90 s while recording), and a long session is split into parts, each its own `profiles` invoke (`lambda_speaker_embed.py:399-410`, `:1227`). So arm 3 must be driven from `meeting_session` (`idx_meeting_session_site`, then `idx_turn_names_session (company_id, session_base)`), never from a company-wide scan of `speaker_turn_names` (which has no index on `source`). Arm 2 is driven by `idx_recordings_site`. Task 6 confirms with `EXPLAIN`.

7. **The switch is not a boolean, so the wiring sweep will not see it.** `test_every_boolean_toggle_is_reachable_from_a_repo_variable` only finds `AllowedValues: ['true','false']`. `RosterDerived` (`on`/`off`, spec) and `RosterLookbackDays` (a number) each need an explicit test in the shape of `test_the_speaker_identity_mode_is_wired_in_both_environments` (`tests/unit/test_template_workflow_parameter_wiring.py:980-1003`). The writer reads env at import (`lambda_voiceprint_writer.py:68`), so the repository takes `derived` and `lookback_days` as **arguments** and the writer passes its module constants; tests set `vw.ROSTER_DERIVED` rather than the environment.

8. **`kind IN ('audio','video')` on arm 2.** A photo row also carries `site_id` and a date segment; a person who only photographed is on site too, but photo rows are written at presign and may never upload (`recordings.py:450-457`). Keep photos out; the spec's "recorded at this site" means a capture session.

9. **A one-person derived roster changes behaviour at every site where only the wearer has a profile.** With the wearer on the roster and nobody else, `decide_with_roster` uses the full-pool result for the wearer (subset < 2, `voiceprint_utils.py:319-323`) but caps **every other** full-pool winner at `tentative`. The spec accepts this ("a question mark on a correct name"); it is listed under owner decisions because it is the one visible regression a customer will notice on day one.

---

## Task 0: Baseline

- [ ] `python -m pytest tests/unit -q -p no:cacheprovider 2>&1 | tail -3` from the worktree; record the passed count. Compare your `pip` state with the workflow's install line first (memory: local/CI differ on onnxruntime and pglast).
- [ ] `git status --short` shows only the spec file untracked. Do not commit or push in any task.

## Task 1: Repository — the switch, and `off` is byte-for-byte #969

**Files:** `src/repositories/site_attendance.py`; `tests/unit/test_site_attendance_repo.py`.

- [ ] **Step 1 (red).** Add to the unit test file a module constant `_ROSTER_SQL_969` holding the exact SQL string `on_roster_profile_ids` executes today (copy it from `site_attendance.py:158-166`, whitespace-normalised the way `FakeCursor` normalises: `" ".join(sql.split())`). Tests:
  - `test_off_runs_exactly_the_969_query`: `on_roster_profile_ids(conn, CO, SITE, "2026-09-30", derived=False)` executes one statement whose normalised text `== _ROSTER_SQL_969` and whose params are `(CO, SITE, "2026-09-30")`.
  - `test_derived_is_the_default`: calling without `derived=` executes SQL that contains `FROM recordings` and `FROM meeting_session` (the arms land in Task 2/3; this pins the default).
  - `test_lookback_default_is_fourteen`: the params of the derived call carry `14` under the `lookback` key.
  - `test_company_id_is_still_required_on_the_derived_path` (`ValueError`).
- [ ] **Step 2.** Change the signature to `on_roster_profile_ids(conn, company_id, site_id, attend_date, derived=True, lookback_days=14) -> set`. Keep the #969 statement verbatim as `_EXPLICIT_SQL`; when `derived` is false execute it unchanged with the positional params it has today. When true execute `_DERIVED_SQL` (Task 2/3 fill the arms; for now a `UNION` of the explicit arm alone written with **named** params `%(co)s %(site)s %(day)s %(lookback)s`). Extend the docstring: the union, why derived membership is computed at lookup and never written, why `off` must stay byte-identical.
- [ ] **Step 3.** Run the four tests green. **Revert-to-red:** change one character of the `off`-path SQL; `test_off_runs_exactly_the_969_query` must fail. Restore.

## Task 2: Arm 2 — people who recorded at this site on this day

**Files:** `src/repositories/site_attendance.py`; `tests/unit/test_site_attendance_repo.py`; `tests/integration/test_site_attendance_sql.py`.

- [ ] **Step 1 (red), unit (SQL text).** On the derived statement assert:
  - `split_part(r.s3_key, '/', 4) = %(day)s` is present and the string `started_at` is absent from the whole statement (correction 1);
  - `r.site_id = %(site)s`, `r.company_id = %(co)s`, `r.kind IN ('audio', 'video')`;
  - the recorder arm carries both `p.user_id = r.user_id` and `lower(concat_ws(' ', u.first_name, u.last_name))`;
  - `p.status <> 'withdrawn'` appears in the arm.
- [ ] **Step 2 (red), integration** (`db` fixture, rolled back; seed with plain SQL like the existing tests in that file):
  - `test_recorder_at_this_site_today_is_on_the_roster`: user U (first/last "Sam"/"Yu") with a profile `user_id = U`; a recording `site_id = S`, `kind='audio'`, key `users/Sam_Yu/audio/2026-09-30/x_sid<hex>_c0000.wav`, `started_at = '2026-09-29 18:00+00'` (07:00 NZDT next day); roster for (S, `2026-09-30`) contains the profile. Same recording, roster for (S2, same day) does not (spec test 1). A recording with key date `2026-09-29` and `started_at = '2026-09-29 23:30+00'` (12:30 NZDT on the 30th) is **not** on the 30th's roster: the key decides, not the clock.
  - `test_recorder_with_an_unlinked_profile_is_found_by_full_name`: profile `user_id NULL`, `display_name 'sam yu'`; the same recording puts it on the roster.
  - `test_a_photo_alone_does_not_put_the_recorder_on_the_roster`.
  - `test_a_withdrawn_recorder_profile_is_never_on_the_roster`.
- [ ] **Step 3.** Implement the arm inside `_DERIVED_SQL`:
  ```sql
  UNION
  SELECT p.id FROM recordings r
  JOIN users u ON u.id = r.user_id
  JOIN speaker_voiceprints p ON p.company_id = r.company_id
   AND (p.user_id = r.user_id
        OR (p.user_id IS NULL
            AND lower(p.display_name) = lower(concat_ws(' ', u.first_name, u.last_name))))
  WHERE r.company_id = %(co)s AND r.site_id = %(site)s
    AND r.kind IN ('audio', 'video')
    AND split_part(r.s3_key, '/', 4) = %(day)s
    AND p.status <> 'withdrawn'
  ```
- [ ] **Step 4.** Unit green; integration green if `TEST_DATABASE_URL` is set (else it skips — Task 6 covers it). **Revert-to-red:** replace `split_part(...) = %(day)s` with `r.started_at::date = %(day)s::date`; the unit `started_at`-absent test and the integration 12:30-NZDT case must both fail. Restore.

## Task 3: Arm 3 — people named at this site in the last N NZ days

**Files:** same three as Task 2.

- [ ] **Step 1 (red), unit (SQL text).** Assert the derived statement carries:
  - `t.session_base = 'sid' || ms.session_id`, `t.source = 'correction'`, `t.superseded_at IS NULL`;
  - `AT TIME ZONE 'Pacific/Auckland'` and `BETWEEN %(day)s::date - %(lookback)s AND %(day)s::date`; the strings `now()` and `CURRENT_DATE` are absent (correction 3);
  - the profile join has both `p.id = t.voiceprint_id` and `t.voiceprint_id IS NULL AND lower(p.display_name) = lower(t.display_name)` (correction 4);
  - the site test reads `ms.site_id = %(site)s OR EXISTS (SELECT 1 FROM recordings` (correction 2) and that subquery carries `r2.company_id = ms.company_id`, `r2.user_id = ms.user_id`, `r2.site_id = %(site)s`, `ESCAPE '\\'`.
- [ ] **Step 2 (red), integration.** Seed: site S, user U, profile P (`display_name 'Sam Yu'`, status `confirmed`), `meeting_session` row `session_id = <hex>`, `site_id = S`, `opened_at = '2026-09-26 20:00+00'` (27 Sep 09:00 NZDT, 3 days before the 30th), and a turn name `(company, voiceprint_id = P, session_base = 'sid<hex>', turn_ref 'f@1.0', state 'confirmed', source 'correction')`. Cases (spec test 2), each its own test:
  - 3 days ago → on the roster for (S, 2026-09-30); (S2, same day) → not.
  - `opened_at` 20 days before → not.
  - `source = 'correction_propagation'` → not. `source = 'voiceprint_match'` → not.
  - `superseded_at = now()` → not.
  - profile `status = 'withdrawn'` → not.
  - `voiceprint_id NULL`, `display_name 'sam yu'` → on (name arm).
  - **NZ boundary:** `opened_at = '2026-09-29 18:30+00'` (07:30 NZDT on the 30th), lookback 0 → on the 30th's roster; a UTC reading would put it on the 29th. Pass `lookback_days=0` explicitly so the test is about the boundary.
  - **Offline session:** `ms.site_id NULL`, plus a recording row `user_id = U`, `site_id = S`, key `users/Sam_Yu/audio/2026-09-27/dev_2026-09-27_09-00-00_sid<hex>_c0000.wav` → on the roster; with `site_id = S2` on that recording → not.
- [ ] **Step 3.** Implement the arm:
  ```sql
  UNION
  SELECT p.id FROM meeting_session ms
  JOIN speaker_turn_names t ON t.company_id = ms.company_id
   AND t.session_base = 'sid' || ms.session_id
   AND t.source = 'correction' AND t.superseded_at IS NULL
  JOIN speaker_voiceprints p ON p.company_id = t.company_id
   AND (p.id = t.voiceprint_id
        OR (t.voiceprint_id IS NULL AND t.display_name IS NOT NULL
            AND lower(p.display_name) = lower(t.display_name)))
  WHERE ms.company_id = %(co)s
    AND (COALESCE(ms.opened_at, ms.created_at) AT TIME ZONE 'Pacific/Auckland')::date
        BETWEEN %(day)s::date - %(lookback)s AND %(day)s::date
    AND (ms.site_id = %(site)s
         OR EXISTS (SELECT 1 FROM recordings r2
                     WHERE r2.company_id = ms.company_id AND r2.user_id = ms.user_id
                       AND r2.site_id = %(site)s
                       AND r2.s3_key LIKE '%%\_sid' || ms.session_id || '\_c%%' ESCAPE '\'))
    AND p.status <> 'withdrawn'
  ```
  Note the `%%` escaping inside a psycopg statement with named params, and that `%(lookback)s` is bound as an `int` (Postgres `date - integer`). Document in the docstring why the anchor is `attend_date` (correction 3) and why `meeting_session` leads the join (correction 6).
- [ ] **Step 4.** Green. **Revert-to-red, three times:** (a) drop `AT TIME ZONE 'Pacific/Auckland'` → the NZ-boundary integration test and the unit text test fail; (b) drop `t.superseded_at IS NULL` → the superseded case fails; (c) drop the `EXISTS` fallback → the offline-session case fails. Restore each.

## Task 4: Writer — constants, pass-through, one log line

**Files:** `src/lambda_voiceprint_writer.py:63-74, 404-414`; `tests/unit/test_lambda_voiceprint_writer.py` (beside the roster tests at `:383-440`).

- [ ] **Step 1 (red).** Tests, patching `vw.site_attendance.on_roster_profile_ids` with a recorder that captures kwargs:
  - `test_profiles_passes_the_derived_switch_and_lookback`: with `monkeypatch.setattr(vw, "ROSTER_DERIVED", True)` and `ROSTER_LOOKBACK_DAYS = 9`, the call receives `derived=True, lookback_days=9`.
  - `test_profiles_off_passes_derived_false`: `ROSTER_DERIVED = False` → `derived=False`.
  - `test_the_code_defaults`: `vw.ROSTER_DERIVED is True` and `vw.ROSTER_LOOKBACK_DAYS == 14` under a clean environment (`monkeypatch.delenv` both, `importlib.reload(vw)`, then reload again in a `finally` so the module is left as the other tests expect — see how existing tests in this file handle reloads, or assert on `os.environ.get("ROSTER_DERIVED", "on")` parsing via a tiny `_derived_from_env(value)` helper instead of reloading; the helper is the cleaner choice: `_derived_from_env("off") is False`, `("ON") is True`, `(None) is True`).
- [ ] **Step 2.** Add beside `PROPOSAL_WINDOW_HOURS`, with the same three-segment comment:
  ```python
  ROSTER_DERIVED = _derived_from_env(os.environ.get("ROSTER_DERIVED"))
  ROSTER_LOOKBACK_DAYS = int(os.environ.get("ROSTER_LOOKBACK_DAYS", "14"))
  ```
  In `_profiles`, pass `derived=ROSTER_DERIVED, lookback_days=ROSTER_LOOKBACK_DAYS` and add `logger.info("roster for site %s on %s: %d profile(s) (derived=%s)", ...)` after a successful read. Do **not** add a new key to the reply: `test_embedder_writer_contract.py` refuses unread fields across the seam and the embedder needs nothing new.
- [ ] **Step 3.** Green. **Revert-to-red:** remove the two kwargs from the call; the first two tests fail. Restore.

## Task 5: Switch wiring — template, both workflows, wiring tests

**Files:** `src/template.yaml` (Parameters after `ProposalWindowHours:` at `:1247`; `VoiceprintWriterFunction` env at `:3876`); `.github/workflows/deploy.yml:246`; `.github/workflows/deploy-prod.yml:296`; `tests/unit/test_template_workflow_parameter_wiring.py`.

- [ ] **Step 1 (red).** Add, modelled on `:980-1010`:
  - `test_the_derived_roster_switch_is_wired_in_both_environments`: `RosterDerived` and `RosterLookbackDays` in `_overrides(path)` for both workflows.
  - `test_the_derived_roster_defaults_to_on_everywhere`: template block `Default: 'on'` and `AllowedValues: ['on', 'off']`; both workflow lines carry `|| 'on'`; the lookback block `Default: '14'` and both lines `|| '14'`.
  - `test_the_writer_is_given_the_roster_switch`: `_env_text("VoiceprintWriterFunction")` contains `ROSTER_DERIVED: !Ref RosterDerived` and `ROSTER_LOOKBACK_DAYS: !Ref RosterLookbackDays`, and no other function's env text mentions either (the repository is only ever called from the writer).
  - Extend `test_the_code_defaults_match_the_template_defaults` with a second `pairs` list against `lambda_voiceprint_writer.py`: `("RosterLookbackDays", "ROSTER_LOOKBACK_DAYS")` via the existing `os.environ.get('…', '…')` regex — note the writer uses double quotes; either switch the new constant to single quotes or make the regex accept both.
- [ ] **Step 2.** Template parameters with descriptions (what each arm is, that `off` is the explicit-only #969 roster and the rollback); env on the writer only; workflow lines `"RosterDerived=${{ vars.TEST_ROSTER_DERIVED || 'on' }}"`, `"RosterLookbackDays=${{ vars.TEST_ROSTER_LOOKBACK_DAYS || '14' }}"` and the `PROD_` twins, placed directly after the `ProposalWindowHours` line in each file so the continuation block stays intact.
- [ ] **Step 3.** Run the whole wiring file (`test_every_override_names_a_real_template_parameter` and `test_no_override_can_evaluate_to_a_bare_key` must stay green). `cfn-lint` is not pinned locally; skip it, CI runs it. **Revert-to-red:** delete the prod workflow line for `RosterDerived`; the first new test fails. Restore.

## Task 6: Real-DB verification (orchestrator runs this; it needs AWS)

Nothing here is a unit test. CLAUDE.md "Testing": the doubles prove the handler and nothing about the SQL.

- [ ] **6a — integration file on a real Postgres.** If a `TEST_DATABASE_URL` is reachable, run `pytest tests/integration/test_site_attendance_sql.py -q`; every Task 2/3 case must pass. If not, run 6b.
- [ ] **6b — RDS Data API, one transaction, rolled back**, against `fieldsight_test` (cluster and secret ARNs in CLAUDE.md "Testing"; `MSYS_NO_PATHCONV=1`). Execute `_DERIVED_SQL` with the parameters bound for **Sam Yu's 2026-08-08 site** (spec test 5): first find it —
  ```sql
  SELECT r.site_id, s.name, COUNT(*) FROM recordings r JOIN sites s ON s.id = r.site_id
  JOIN users u ON u.id = r.user_id
  WHERE lower(concat_ws(' ', u.first_name, u.last_name)) = 'sam yu'
    AND split_part(r.s3_key, '/', 4) = '2026-08-08' GROUP BY 1, 2;
  ```
  then run the roster query for that `(company_id, site_id, '2026-08-08')` and confirm the returned ids include a `speaker_voiceprints` row whose `display_name` is Sam Yu and whose `status <> 'withdrawn'`. If it returns nothing, report WHICH arm failed (run the arms separately) — "Sam Yu has two profiles, one empty" was real on TEST on 2026-09-27; an empty profile is on the roster but never reaches `profiles_for_matching`, which is fine, but a profile with `user_id NULL` and a display name spelled differently from `users.first_name/last_name` is a finding, not a pass.
  Also run the same statement with `derived = off` (the #969 text) and confirm it returns the explicit-only set.
- [ ] **6c — `EXPLAIN (ANALYZE, BUFFERS)`** on `_DERIVED_SQL` for that site/day. Arm 2 must show an index scan on `idx_recordings_site`; arm 3 must start from `meeting_session` (`idx_meeting_session_site` or a seq scan over a small table) and reach `speaker_turn_names` through `idx_turn_names_session`. A seq scan over `speaker_turn_names` filtered on `source` is a fail — restructure the join order, not the index (no migration in this plan). Record the total time in the PR description; the query runs on every live extraction (correction 6).
- [ ] **6d — `git diff src/template.yaml`** shows only the two Parameters and the two env lines; no policy on any function changed.
- [ ] `aws rds-data rollback-transaction` at the end of 6b, and say so in the report.

## Task 7: Behaviour on TEST after deploy (owner-run, per spec "How the owner tests it")

Not part of the branch's tests; listed so the orchestrator hands the owner the exact evidence to look for.

- [ ] Rename a passage at site S on TEST (a `correction` row appears; `SELECT source, superseded_at FROM speaker_turn_names ORDER BY created_at DESC LIMIT 3`).
- [ ] Record again at S. In `/aws/lambda/fieldsight-test-voiceprint-writer` find `roster for site <S> on <date>: N profile(s) (derived=True)` with `N >= 2` (wearer + renamed person). A line with `N = 1` means only the wearer was found — check arm 3 via 6b's per-arm queries before concluding the renamed person's profile was withdrawn.
- [ ] In the embedder log, the renamed person's turns read `confirmed` only when on the roster; an unregistered visitor is never `confirmed`. Set `TEST_ROSTER_DERIVED=off`, redeploy, and confirm the writer's line reads `derived=False` — the rollback must be shown to work, not assumed (memory: "a switch that only exists in code is one nobody can turn").

## Task 8: Finish

- [ ] `python -m pytest tests/unit -q -p no:cacheprovider 2>&1 | tail -3`; the passed count must exceed Task 0's by the number of tests added (Tasks 1-5: at least 4 + 4 + 4 + 3 + 4 = 19 unit tests, plus the integration cases, which skip locally).
- [ ] Update the spec: replace "recordings rows with this site_id and this NZ date" with the key-segment rule; replace "in a session at this site" with the `meeting_session` → recordings ladder; record the `attend_date` anchor and the `voiceprint_id NULL` name arm; add correction 9 under "Behaviour it changes".
- [ ] Leave the branch uncommitted unless the orchestrator says otherwise. List, separately, what exists only in this worktree (memory: "ask what only exists locally").
