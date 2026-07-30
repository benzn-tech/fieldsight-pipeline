# Chunk-Driven Session Lifecycle — Implementation Plan (2026-07-31)

**Spec:** `docs/superpowers/specs/2026-07-27-asr-switch-stop-continuity-test-design.md`
§8.4 (session identity & offline durability); device side
`2026-07-28-mobile-client-session-contract-design.md` §5–§6.

## The gap this closes

The Tier-0 finalize confirmation email (and the whole `meeting_session` state
machine) is currently driven **only** by the device's best-effort live
`POST /sessions/{id}/open` and `/close` (org-api `session_open`/`session_close`,
the sole callers of `ensure_open`/`mark_pending_close`). `touch_segment` is
defined but **never called** — dead code. So:

- If the device never calls `/open` (offline, dropped fire-and-forget, or the
  mobile client hasn't implemented the calls), the session **never enters the
  state machine** → `list_due_finalize` never sees it → **no email ever fires**.
- If it calls `/open` but the `/close` is dropped/crashes, the session sits
  `open` forever → **no email**.

This contradicts the spec's load-bearing rule (device stamps everything on the
data; live API is optimization only) and §8.4's explicit design:

> start = **first chunk** / open record; end = close record; **if close is
> missing, the server infers close when the session_id sees no new chunk for
> `SESSION_GAP_MINUTES`.**

The device already stamps `sid{32hex}` + `c{NNNN}` on every chunk (P0-a, shipped
+ device-verified), so the chunk stream is a real, present signal — this work is
NOT speculative; it wires the durable path §8.4 already designed.

## Design decisions (locked by §8.4)

- **Two close paths, both spec-sanctioned:**
  - (a) explicit `/close` → existing 30 s `STOP_GRACE_SECONDS` → **fast ≤2-min
    email** (online path — unchanged).
  - (b) **inferred idle-close**: an `open` session whose last chunk is older than
    `SESSION_GAP_MINUTES` (15) → `pending_close` (intent `idle`) → finalize.
    Timeliness degrades to "after sync" for offline recordings — §8.4's stated,
    accepted limitation. Integrity (grouping/ordering/mis-touch) is preserved.
- **Open from the chunk stream:** first transcript of a session `ensure_open`s it
  (idempotent COALESCE, so a later `/open` — or an earlier one — never regresses).
  `site_id` is **NULL** when opened from a chunk (no site pick in the chunk key);
  a best-effort `/open` with `siteId` fills it via COALESCE, otherwise item_writer
  attributes by membership (existing fallback — see
  `fieldsight-recording-site-attribution-gap`). No behaviour regression.
- **Activity tracking:** each transcript `touch_segment`s (advances
  `last_segment_at`; a late resume chunk flips `pending_close`→`open` and bumps
  `version`, so a stale scheduled finalize no-ops — the §8.4 idempotency guard,
  finally live).
- **Trigger:** reuse the `transcripts/` EventBridge signal the rolling summary
  already uses. The activity step is **in-VPC** (touches Aurora), a SEPARATE
  lambda from the non-VPC rolling summary (BUG-36 split).
- **Inert-safe rollout:** gate the new lambda + the sweep's inferred-close behind
  the existing `EnableFinalize` condition, so it ships dark on prod exactly like
  the rest of the finalize scaffolding until flipped.

## Tasks

### Task 1 — `repositories/meeting_session.py`: idle-open query
Add `list_idle_open(conn, idle_seconds) -> [{session_id, version}]`: sessions
still `status='open'` whose `COALESCE(last_segment_at, opened_at)` is older than
`idle_seconds`. (Uses `last_segment_at` so a long *active* meeting is never
mistaken for idle — this is why touch_segment must be wired, Task 2.) Unit-test
against FakeConn (mirror `test_meeting_session_repo.py`).

### Task 2 — `src/session_activity.py`: pure core + in-VPC handler
On a `transcripts/{folder}/{date}/{...sid...}.json` arrival:
1. `extract_session_id_from_filename` (transcript_utils) → 32-hex `sid`, else
   skip (legacy/whole-file transcript — no device session).
2. `chunk_start = extract_base_time_from_filename` (T1); `date`, `folder` from key.
3. Resolve `company = lambda_ingest.resolve_company(conn, folder)`;
   `user_id = lambda_ingest.resolve_user(conn, company_id, folder)` (may be None).
4. `meeting_session.ensure_open(sid, company_id, user_id, site_id=None, kind,
   opened_at=chunk_start)` then `touch_segment(sid, chunk_start)`.
Pure core takes injected `conn` + resolvers (unit-test with FakeConn, no AWS).
`lambda_handler` reads the EventBridge/`Records` key shapes (reuse
`_keys_from_event` pattern from `lambda_rolling_summary`).

### Task 3 — inferred-close in the finalize sweep
In `lambda_finalize_claim.sweep`, BEFORE the due-finalize loop: for each
`meeting_session.list_idle_open(conn, SESSION_GAP_MINUTES*60)`, call
`mark_pending_close(conn, sid, closed_at=last activity, intent='idle')`. The
existing `list_due_finalize` (idle stop older than grace) then picks them up the
same tick or the next. Gate behind `EnableFinalize` via env, default off.
Unit-test the sweep orders idle-close before finalize and is idempotent.

### Task 4 — SAM wiring (`src/template.yaml`)
`SessionActivityFunction`: in-VPC (reuse the VpcConfig + DbSecret + subnets of an
existing in-VPC lambda, e.g. FinalizeSweep/OrgApi), EventBridge rule on
`transcripts/` prefix (mirror `RollingSummaryFunction`'s rule), `State`
`!If [ShouldEnableFinalize, ENABLED, DISABLED]`. IAM: Aurora connect + S3
GetObject `transcripts/*`. No new params. `simulate-principal-policy` the deploy
role for any new resource (memory `fieldsight-org-api-new-route-iam-trap`).

### Task 5 — tests + inert deploy verification
Full unit suite green. Deploy to test with `TEST_ENABLE_FINALIZE=true` already
set; record a chunk session WITHOUT calling `/open|/close` and confirm the
session opens from the stream, tracks activity, and finalizes by inference.

## Rollout / risk
- Ships **inert on prod** (EnableFinalize off) — zero prod impact until flipped.
- New always-on in-VPC lambda = modest cost (one invoke per transcript, same
  cadence as the rolling summary). Flag for the user before enabling on prod.
- Manifest ingestion (open/pause/resume/close events + intent from the uploaded
  `_manifest.json`) is a **later layer** — it sharpens boundaries and carries
  explicit `end` intent (grace 0), but is only produced once the mobile client
  ships the manifest (contract §5, not yet implemented). This plan works from the
  chunk stream alone in the meantime.

## Self-Review
Covers §8.4 "start = first chunk" (Task 2 ensure_open), "infer close by
inactivity" (Tasks 1+3), the idempotency version-bump guard (Task 2
touch_segment), and the online fast path unchanged (Task 3 only adds the idle
path). Site attribution degrades gracefully to membership when opened from a
chunk (documented). Inert-gated (Task 4) so prod is unaffected until deliberate.
