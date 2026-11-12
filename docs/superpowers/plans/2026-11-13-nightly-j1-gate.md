# Nightly J1 Gate Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** A J1-only flag `NIGHTLY_J1` (inherit|off|shadow|on) so nightly J1 catch-up can run `on` while NIGHTLY_REMATCH (J2/J3) stays shadow.

**Architecture:** `nightly_rematch.j1_mode(run_flag)` combines `ctx.flag` with env `NIGHTLY_J1`; `run_counts` uses it to skip or run J1 detection and to decide re-emit. Wired as template parameter `NightlyJ1` → DecisionScan env, with workflow variables per environment.

**Tech Stack:** Python 3.11, psycopg, AWS SAM template, GitHub Actions, pytest.

**Spec:** `docs/superpowers/specs/2026-11-13-nightly-j1-gate-design.md` (rev 2)

## Global Constraints

- Values: `inherit` (default) | `off` | `shadow` | `on`; env read with `(os.environ.get("NIGHTLY_J1") or "inherit").strip().lower()`; unknown → `inherit`.
- `run_flag` not exactly `"shadow"`/`"on"` → J1 off regardless of NIGHTLY_J1.
- Do not change the hub, `RunContext`, `decision_rules`, `lambda_decision_scan`, J2, J3, the breaker, the counts dict keys, IAM, or conditions.
- Workflow lines exactly: `"NightlyJ1=${{ vars.TEST_NIGHTLY_J1 || 'inherit' }}" \` (deploy.yml) and `"NightlyJ1=${{ vars.PROD_NIGHTLY_J1 || 'inherit' }}" \` (deploy-prod.yml), next to the `NightlyRematch=` line.
- Integration tests run one file at a time: `bash /c/Users/camil/fswork/run-tests.sh <file> -q`; unit: `C:\Users\camil\fspy\Scripts\python.exe -m pytest <file> -q`.
- Never `git add -A`; add named files. Commit messages: no AI attribution lines.

## Review Focus

1. NIGHTLY_J1 unset on an environment with NIGHTLY_REMATCH=shadow → behaviour identical to today (existing catchup tests unchanged and green).
2. NIGHTLY_J1=on with RECONCILE_STATUS false → nothing re-emitted (`j1_line_off`).
3. NIGHTLY_J1="ON " / "" / "bogus" → normalised / inherit.
4. shadow + J1 on must not write nightly_requested_items, decision_records, event_links, nor call `_breaker_open`.
5. NIGHTLY_J1=off must not run detection reads at all.

---

### Task 1: j1_mode and run_counts gating

**Files:**
- Modify: `src/nightly_rematch.py` (docstring shadow paragraph ~line 47; `run_counts` ~151-204; `_j1_catch_up` ~477)
- Test: `tests/unit/test_nightly_rematch.py`, `tests/integration/test_nightly_catchup.py`

**Interfaces:**
- Produces: `J1_ENV = "NIGHTLY_J1"`; `j1_mode(run_flag) -> str | None` returning `"on"`, `"shadow"` or `None`; `_j1_catch_up(conn, ctx, night, groups, counts, started, clock, mode)` re-emits only when `mode == "on"`.

- [ ] **Step 1: Unit tests for `j1_mode`** (monkeypatch env) — parametrized table:
  - run_flag in ("off", "", None, "bogus") × NIGHTLY_J1 in (unset, "on") → `None`
  - ("shadow", unset) → "shadow"; ("on", unset) → "on"; ("shadow", "inherit") → "shadow"
  - ("shadow","on") → "on"; ("on","off") → None; ("on","shadow") → "shadow"; ("shadow","off") → None
  - ("shadow"," ON ") → "on"; ("on","") → "on"; ("shadow","bogus") → "shadow"
- [ ] **Step 2: Unit test** that the counts log line contains `j1_mode=` — assert in `src/nightly_rematch.py` source text that the `logger.info("nightly site=` format includes `j1_mode=%s` (source pin, like neighbouring tests).
- [ ] **Step 3: Run, verify FAIL.**
- [ ] **Step 4: Implement.** `run_counts`: `mode = j1_mode(ctx.flag)`; `groups = _stale_groups(...) if mode else []`; pass `mode` to `_j1_catch_up`; log `j1_mode=%s` with `mode or "off"`. `_j1_catch_up`: still sets `counts["j1_stale"]`; returns unless `mode == "on"`. Update the docstring (item 4 and the shadow paragraph) per spec §3.
- [ ] **Step 5: Integration tests** in `test_nightly_catchup.py` using the existing `Seed`, `lines_on`, `_at`, and the two-session shape of `test_shadow_counts_without_put` (TUE N written 10 h ago recorded 10:00, TUE L written 1 h ago recorded 08:00):
  - `test_j1_on_while_rematch_shadow_reemits_reconcile_only_writes`: env NIGHTLY_J1=on, RECONCILE_STATUS=true, CROSS_DAY_CONTINUITY=shadow; `run_counts(db, s.event(), s.ctx("shadow"))`; assert `counts["j1_reemitted"] == 1`, one put under RECON, none under XDAY, `counts["j1_line_off"] == 1`; `nightly_reemits` count +1; decision_records, event_links, nightly_requested_items unchanged; monkeypatch `nr._breaker_open` to raise → not called; any J2 artifact under `nr.PREFIX` has `"flag": "shadow"`.
  - `test_j1_off_skips_detection_even_when_rematch_on`: NIGHTLY_J1=off, `lines_on`; monkeypatch `nightly.site_finals` and `nightly.recorded_reemit_pairs` (as imported in `nr`) to raise; `ctx("on")`; run succeeds, `j1_stale == 0`, no RECON/XDAY puts, no nightly_reemits row.
  - `test_j1_shadow_while_rematch_on_counts_only`: NIGHTLY_J1=shadow, `lines_on`, `ctx("on")`; `j1_stale == 2`, `j1_reemitted == 0`, no RECON/XDAY puts, no nightly_reemits row.
  - `test_j1_on_but_reconcile_line_off_emits_nothing`: NIGHTLY_J1=on, RECONCILE_STATUS=false, CROSS_DAY_CONTINUITY=off, `ctx("shadow")`; `j1_line_off == 2`, no puts, no rows.
- [ ] **Step 6: Run** unit file and `tests/integration/test_nightly_catchup.py` (whole file: existing tests must stay green), then `tests/integration/test_nightly_tick.py`. Expected all PASS.
- [ ] **Step 7: Commit** `feat(nightly): NIGHTLY_J1 gates J1 apart from NIGHTLY_REMATCH`.

### Task 2: Template and workflow wiring

**Files:**
- Modify: `src/template.yaml` (new parameter after `NightlyRematch` ~1729; its comment/description shadow text ~1720-1736; DecisionScan env next to `NIGHTLY_REMATCH: !Ref NightlyRematch` ~6157)
- Modify: `.github/workflows/deploy.yml` (~455), `.github/workflows/deploy-prod.yml` (~411)
- Test: `tests/unit/test_nightly_rematch.py`

**Interfaces:**
- Consumes: env name `NIGHTLY_J1` from Task 1.

- [ ] **Step 1: Test** `test_nightly_j1_flag_wired_three_places(doc)`: `Parameters.NightlyJ1` has `Default == "inherit"` and `AllowedValues == ["inherit","off","shadow","on"]`; `_env(doc, SCAN)["NIGHTLY_J1"] == {"Ref": "NightlyJ1"}`; each workflow contains its exact line (Global Constraints) and not the other env's `_NIGHTLY_J1`; no resource other than SCAN mentions `NightlyJ1` (walk `doc["Resources"]` JSON-dumped).
- [ ] **Step 2: Run, verify FAIL.**
- [ ] **Step 3: Implement** the parameter (with a comment citing the spec and that J1 on writes `nightly_reemits` and puts requests even while NightlyRematch is shadow; description ≤ one sentence on the table), the env entry, the two workflow lines; amend the NightlyRematch comment/description shadow wording to "(J1 aside, see NightlyJ1)".
- [ ] **Step 4: Run** `tests/unit/test_nightly_rematch.py` and any template lint test the repo has (`tests/unit/test_template*.py`). Expected PASS.
- [ ] **Step 5: Commit** `feat(nightly): wire NightlyJ1 parameter to the decision scan`.
