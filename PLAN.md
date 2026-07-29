# FieldSight Backend — PLAN

**The living action ledger for `fieldsight-pipeline`** — what's done, in flight, next, and
blocking. Updated **in place** every session (no new dated snapshots). See `CLAUDE.md` for *how the
system works + traps (BUG-01..38)*, `~/.claude/…/memory/` for cross-session facts, and
`docs/superpowers/specs|plans` for per-feature design. The dated
`docs/PROJECT-SUMMARY-2026-07-29.md` is a one-time onboarding narrative — **this file supersedes it
for "what's next."**

> Account 509194952652 · ap-southeast-2 · SoR = Aurora PG+pgvector · prod deploy = approval-gated.
> **Naming (don't conflate):** Delivery **A/B/C** = email / one-click mailto / templated review report ·
> voice **Tier-1/2** = rolling-during / correct-then-structure-after · **T1–T4** = Delivery-C build tasks.

---

## 1 · Shipped (prod)

- **Capture → report pipeline**: app upload → VAD → transcribe → Claude extraction → item store → python-docx reports (daily/weekly/monthly).
- **Aurora item store = System of Record** (topics / findings / action_items + pgvector; editable, queryable, traceable).
- **Dashboard read/serve** via org-api: Today / Timeline / Tasks / Safety-Quality / Programme / Reports / Insights / Library; **session picker (#11)**; **meeting mailto (Delivery B / #10)**; compliance mark-resolved.
- **AI**: content correction (edit → audit → reindex → RAG sync; **Phase D** ✓Save/✕Cancel + History word-diff), RAG Q&A, hybrid search, **life/work privacy separation** (redactions / non_work; optimistic Q2 both views).
- **Identity / access**: org-api + Cognito + role hierarchy (admin/gm/pm/site_manager/worker) + site ACL + graded visibility; recording→site attribution (write G5b + read `/contributors` union).
- **Infra**: authority flip (findings persist cross-day; manual ticks don't roll back) · schedules cutover (15-min in-hours sweep) · **test/prod DB isolation** (`fieldsight_test` vs `fieldsight`, one cluster — PR#114).

## 2 · In flight (branches)

- **`feat/voice-chunk-session`** (off develop): chunk/session identity + `meeting_session` migration + session-lifecycle endpoints + `email_sender.py` + provider abstraction (`llm_utils.py`) + Delivery-C **T1** (session title) + **T2** (preview endpoint `POST /sessions/{id}/report/preview`). **Shipped but largely INERT** — the ≤2-min email + the whole chunk-session paradigm don't activate until the mobile app mints & sends `session_id`.
- **`feat/session-continuity`** (currently checked out) — session-continuity work.

## 3 · Priorities / next focus (high → low)

1. **DNS + SES domain** — one decision unlocks BOTH the real-time email and report email; hard blocker (a bare CloudFront URL also reads as phishing). *Needs the user: who owns `fieldsightai.com` DNS?*
2. **Delivery C report review modal** (visible value, **works on today's data**): backend **T3** (`POST /sessions/{id}/report` generate) + **T4** (template fields) → frontend F1–F7 (modal preview + fillable fields + Download/Email). Spec: `docs/superpowers/specs/2026-07-28-session-report-review-export-design.md §11`.
3. **≤2-min confirmation email** (flagship, voice Thread 1): grace timer + finalize lambda (Tier-2-lite) + SES (blocked on #1) + rolling Tier-1 — **depends on #4**.
4. **Mobile chunk-session contract** (device `session_id` / ~1-min chunking / manifest / offline queue) — prerequisite for #3. Backend scaffolding done + inert. Spec: `docs/superpowers/specs/2026-07-28-mobile-client-session-contract-design.md`.
5. **ASR Soniox provider** + benchmark (user-owned).
6. Other voice threads: multi-device (3) · checklist import → ask-agent loop (4) · multi-vertical prompts/vocab (5) — designed, not built.
7. Legacy single-site API retirement (after UI fully cuts over to org-api).

**Recommended sequence:** ① DNS/SES → ② Delivery C modal, in parallel drive ④ mobile (nothing in flagship #3 moves until the device changes).

## 4 · Pending / open questions (from handoff 2026-07-22/23 §7)

- **#4 — prod media not bound (real prod bug · PIPELINE/DATA).** A voice recording's Audio tab has no file (transcript exists → audio *was* processed, but `audio_segments/` or `users/{folder}/audio/` may be empty) **and** photos aren't bound to the topic (`topic.related_photos` empty). **Needs the user's prod folder/name + date + topic** to probe prod S3 `fieldsight-data-509194952652` (missing-file vs missing-binding).
- **#1 — Today Mine/Team split confusing.** `today-adapter.js:470` strict-matches `task.assignee === currentUserName`; recorded tasks land in Team when `responsible` ≠ caller's display name. Fix options: unassigned+self → mine · allow Team check-off · relax the match. (Frontend.)
- **#3 — content edit doesn't propagate to sibling fields.** A confirmed glossary alias only normalizes at RAG-synthesis, not the stored `action_items`/`findings`. Feature: on confirm-correction, rewrite all fields in the topic (or company) + reindex.
- **Q1 — Today/Tasks tier-aware actions. DESIGN AGREED, spec not written.** Mirror the timeline: topic `redacted` → omit its action items; `work_class==='non_work'` → show marked "疑似个人" but exclude from open/overdue counts; missing fields → treat as work (backward compat). **Pure frontend** — org-api `/timeline` already returns `work_class`+`redacted` (needs `FS_TIMELINE_SOURCE=aurora`; prod has it); `today-adapter.js` + `tasks-aggregator.js` just drop those fields. No new endpoint. → write spec `docs/superpowers/specs/2026-07-23-today-tasks-tier-aware-actions-design.md` → plan → implement. **Do NOT blanket-exclude in `list_topics_for_date`** (that read also serves site/self tiers that should still see redacted items, marked).
- **Open product decision:** role-change is admin/platform_admin only server-side (`patch_member_role`). If site_managers *should* change positions → a permission change to decide.
- **Worktree cleanup:** `git worktree remove` the merged worktrees (`life-separation`, `life-separation-ui`, `content-correction-phase-d`, `content-correction-phase-d-ui`).

## 5 · Bottleneck

The backend can advance on unit tests indefinitely, but *making it produce real customer value* is blocked in places outside the backend loop:
1. **DNS / domain undecided** — SES needs a verified domain identity; blocks both emails.
2. **Mobile dependency (slow loop)** — the chunk-session paradigm needs device-side changes; backend scaffolding is fully in place but INERT until the device sends `session_id`. **The flagship's real starting point is the app.**
3. **No extraction-sourced sessions in test** — test data is legacy report-sourced; `item_writer`'s identity bridge (pinned company + Aurora membership) rejects synthetic fixtures → meeting/report features are **unit-test-only until a real recording lands in test**.
4. **Permission classifier blocks automation** — CronCreate, prod deploy approval, and test-bucket writes need the user to step in via `!`.

## 6 · Repo hygiene

Foreign / duplicate folders pending removal (details + git status in `CLAUDE.md` → *Docs & repo hygiene*):
`ui/` + `frontend/` (git-tracked stale copies), `claw-code/` (untracked separate project), `../fieldsight-pipeline-main/` (non-git loose copy). Stale docs `ARCHITECTURE.md` + `ROADMAP.md` now carry STALE banners.
