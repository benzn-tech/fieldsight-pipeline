# Nightly J1 gate (NIGHTLY_J1) — design

Status: rev 2 (2026-11-13; Fable review: 4 Important, 6 Minor, all adopted except M9). Owner asked for it on 2026-11-13 ("先安排J1开关"). Amends
`2026-11-10-nightly-rematch-agent-design.md` §4.3 (flag) for J1 only.

## 1. Problem

J1 (`nightly_rematch._j1_catch_up`) re-emits a session's reconcile / cross-day request when a
recording made *earlier* is uploaded *later* (offline device, stale queue). Today J1 acts only
when `ctx.flag == "on"`, and `ctx.flag` is `NIGHTLY_REMATCH`, the one flag for the whole nightly
handler. `NIGHTLY_REMATCH=on` also turns on J3 recomputes and the J2 path whose bars (N0, the
nightly turn-pick pre-registration) are not met. So J1 — no model call of its own, every output
judged by the owning line's own bars — cannot be turned on without turning on J2/J3.

The late-upload case is now real: the mobile upload-queue fixes (fieldsight-mobile#2, pipeline
#1301) make delayed uploads arrive instead of being lost, and #1300 made reconcile order
candidates by recording time, so a re-emitted request for N now sees the late L's items.

## 2. Decision

A second flag `NIGHTLY_J1` ∈ `inherit` (default) | `off` | `shadow` | `on`, read by the nightly
handler for J1 only. J1's effective mode:

| NIGHTLY_REMATCH | NIGHTLY_J1 | J1 mode |
|---|---|---|
| off | any | J1 never runs (no tick, the hub never calls the run-rule — unchanged) |
| shadow / on | inherit | = NIGHTLY_REMATCH (today's behaviour, byte for byte) |
| shadow / on | off | off: no detection reads, `j1_stale` stays 0 |
| shadow / on | shadow | shadow: detect and count only (no record, no S3) |
| shadow / on | on | on: re-emit, subject to everything J1 already obeys |

So "J1 on alone" is `NIGHTLY_REMATCH=shadow` + `NIGHTLY_J1=on`: J3 counts only, J2 writes its
shadow request artifacts nobody reads (the worker is not wired), J1 re-emits.

Unchanged by this design and still binding in `on`:
- the owning line's own flag (`_line_mode`: `RECONCILE_STATUS=true`, `CROSS_DAY_CONTINUITY=on`
  exactly) — a line that is off or in shadow is never re-emitted (`j1_line_off`);
- J1_MAX (10 per site per night), J1_LATE_HOURS (72), RUN_BUDGET_S, the per-pair savepoint,
  `nightly_reemits` uniqueness, the group gate, the order "J1 before the J2 puts";
- the circuit breaker stays a J2 brake (it counts only J2 artifacts stamped `on`); J1 is not
  behind it, as today.

`j1_mode` fails closed: when `run_flag` is not exactly `shadow` or `on` it returns off whatever
`NIGHTLY_J1` says (row 1 does not rely on the hub alone; tests and future callers call
`run_counts` directly).

An unknown `NIGHTLY_J1` value (typo, empty string) reads as `inherit` — the safe direction, since
inherit never turns J1 on unless NIGHTLY_REMATCH already does. Values are `strip().lower()`ed,
like `_line_mode`.

### Why `inherit` and not a plain off|shadow|on default `off`

A default of `off` would silently change J1 behaviour wherever NIGHTLY_REMATCH is already
`shadow` (TEST today) or later `on` (prod, per the parent plan): J1 shadow counts on TEST would
stop. `inherit` keeps every existing deployment identical until someone sets the new variable.

## 3. Where the mode is read

- `nightly_rematch.j1_mode(run_flag: str) -> str | None` returns `"on"`, `"shadow"` or `None`
  (off), applying the table above to `run_flag` (= `ctx.flag`) and `os.environ["NIGHTLY_J1"]`,
  read at call time (tests use monkeypatch; the Lambda env is fixed per deploy).
- `run_counts` computes it once. When it is `None` it skips `_stale_groups` (no detection reads);
  otherwise it calls `_stale_groups` exactly where it does today (after the J2 plan, before the J2
  puts) and passes the mode to `_j1_catch_up`, which re-emits only when the mode is `"on"`
  (replacing its `ctx.flag != "on"` test).
- The per-run counts log line gains `j1_mode=<on|shadow|off>`; the counts dict keys do not change.
- Text that becomes false is corrected: the module docstring's shadow paragraph
  (src/nightly_rematch.py, "In shadow the only DB writes are the hub's own event and run rows")
  and the `NightlyRematch` parameter comment/description in src/template.yaml must say that with
  `NIGHTLY_J1=on` J1 writes `nightly_reemits` rows and puts reconcile / cross-day requests even
  while NIGHTLY_REMATCH is shadow. The `NightlyJ1` parameter comment states the same.
- The hub, `RunContext`, `decision_rules` and `lambda_decision_scan` do not change.

## 4. Wiring (three places, as the parent §4.3)

- Template parameter `NightlyJ1`: `Type String`, `Default 'inherit'`,
  `AllowedValues ['inherit', 'off', 'shadow', 'on']`, with a comment pointing here.
- Decision-scan function env: `NIGHTLY_J1: !Ref NightlyJ1`. No other function gets it.
- Workflows: `"NightlyJ1=${{ vars.TEST_NIGHTLY_J1 || 'inherit' }}"` in deploy.yml and
  `"NightlyJ1=${{ vars.PROD_NIGHTLY_J1 || 'inherit' }}"` in deploy-prod.yml (SAM keeps a stack's
  previous parameter value, so the fallback must be explicit in the workflow).
- No new IAM: the scan role already has GetObject on `extractions/*` and PutObject on
  `reconcile_requests/*` and `continuity_xday_requests/*` (pinned by
  `test_scan_j1_reads_finals_and_knows_the_owning_lines_flags`). No migration, no new resource,
  no condition change (`ShouldRunNightlyRematch` still needs NIGHTLY_REMATCH ≠ off).

## 5. Rollout

- TEST: after the deploy, set repo variable `TEST_NIGHTLY_J1=on` and redeploy. TEST today has
  NIGHTLY_REMATCH=shadow, RECONCILE_STATUS=true, CROSS_DAY_CONTINUITY=shadow, so only the
  reconcile line is re-emitted; cross-day pairs count as `j1_line_off`.
  The parent plan's J1 bar (two weeks of shadow counts, §7) measures *volume* — whether J1 is
  worth having — not correctness; the owner asked for J1 now, and every re-emitted request is
  judged by reconcile's own R0 path (window, #1300 recording-time filter). TEST keeps counting
  volume while on.
- Same-key overwrite: a reconcile re-emit is put at N's existing request key, which re-triggers
  the matcher (ObjectCreated, no conditional put). Its `reconcile_results/` marker (#1317) is a
  prefix swap of that key, so it *overwrites* the earlier marker: the marker records the last
  processing of the key, not every one. A future J1 reader cannot tell "never answered" from
  "answered, then overwritten" from the marker alone (it would need a nonce'd marker key or the
  `lake_manifest/` lines) — recorded for §7, not solved here. If N's original request is still
  unprocessed when J1 overwrites it, the matcher may run twice on the larger body (two model
  calls); bounded by J1_MAX, suggestions absorbed by UNIQUE (stable_id, suggested_status).
- Duplicate pick cards (accepted on TEST, not prod): status suggestions are idempotent, but
  pick cards dedupe on (segment_key, offset_sec, suggested_status), an anchor taken from the
  model's quote clock. A re-run over N's full transcript that quotes a different sentence for the
  same statement can add a second pending card, including for one already resolved. On TEST this
  is accepted and measured: the verification records pending pick cards for the site before and
  after. Pick-card dedupe across re-emits plus dead-card expiry are prerequisites for J1 on in
  prod (with the reconcile prod prerequisites already tracked).
- TEST schedules: `EnableSchedules=false` is hard-coded for TEST (deploy.yml), so the tick never
  runs there, and a manual `{"scan": "nightly_tick"}` emits only for sites whose NZ local hour is
  01-04 (and once per night). Verification therefore inserts a `nightly.due` site_event for the
  test site directly (payload `{"night": ...}`, source nightly_scan) and invokes
  `{"scan": "process"}`; it checks the counts line (`j1_mode=on`, `j1_reemitted`, `j1_failed`,
  `j1_time_paused`), the Lambda duration (first deployed exercise of the on path: the lazy
  `lambda_item_writer` import and J2 shadow planning share the 45 s RUN_BUDGET_S), the
  `nightly_reemits` row, the re-put request, its new marker, and the pick-card counts.
- Prod: nothing changes (`PROD_NIGHTLY_REMATCH` is off, so J1 cannot run whatever NIGHTLY_J1 is).
  Turning J1 on in prod is not part of this work. Besides the owner's flags
  (PROD_NIGHTLY_REMATCH=shadow + PROD_NIGHTLY_J1=on) it needs a code change first: deploy-prod.yml
  passes no `ReconcileStatus=` at all, so prod reconcile is off by template default and J1 there
  would be all `j1_line_off` — plus the pick-card prerequisites above.

## 6. Tests

Unit:
- `j1_mode` table: every row of §2, plus unknown/empty/whitespace/upper-case values, plus an
  unexpected `run_flag` ("off", "", None) → off whatever NIGHTLY_J1 is.
- Template/workflow wiring, mirroring `test_nightly_flag_wired_three_places`: parameter shape,
  DecisionScan env, both workflow lines, each workflow names only its own environment's variable,
  and no function other than DecisionScan references `NightlyJ1`.
- The counts log line carries `j1_mode=`.

Integration (`tests/integration/test_nightly_catchup.py`, existing Seed):
- NIGHTLY_REMATCH shadow + NIGHTLY_J1 on → the reconcile pair is re-emitted (put + nightly_reemits
  row); table counts before/after show nightly_reemits grew and decision_records, event_links,
  nightly_requested_items did not; J2 artifacts are still put stamped `"flag": "shadow"`;
  `_breaker_open` is not called; J3 is count-only.
- NIGHTLY_REMATCH on + NIGHTLY_J1 off → no J1 put, no record, `j1_stale == 0`, and the detection
  reads are skipped (monkeypatch `nightly.site_finals` / `nightly.recorded_reemit_pairs` to
  raise — the run still succeeds).
- NIGHTLY_REMATCH on + NIGHTLY_J1 shadow → counts, no put, no record.
- inherit (unset) → existing tests unchanged (they set no NIGHTLY_J1).
- NIGHTLY_J1 on + reconcile line off → `j1_line_off`, nothing put (owning-line gate still binds).

## 7. Out of scope

- The J1 *reader* of `reconcile_results/` markers (skip a re-emit whose N was never answered, or
  prove a re-emit was processed) — later nightly work; interface already handed over (#1317).
- Expiry of dead pending pick cards that J1-driven re-emits may create (tracked separately, before
  reconcile goes to prod).
- Any change to J2, J3, the breaker or the hub.
