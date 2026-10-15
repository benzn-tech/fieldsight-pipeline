# Ask orchestration v2 — phase 4 (routing eval harness) implementation plan

> **For agentic workers:** REQUIRED SUB-SKILL: superpowers:subagent-driven-development. Steps use checkbox (`- [ ]`) syntax.

**Goal:** a repeatable, cost-guarded eval that decides whether `TEST_ASK_AGENTIC` may be switched on. It runs the spec §9 question set through the real TEST worker, on both channels, and scores each turn from the result plus its `ASK_ROUTE` log line.

**Architecture:** `tools/ask-routing-eval/` holds the files below. Only `run.py` touches AWS.

| File | Role |
|---|---|
| `set.v1.jsonl` | the question set |
| `run.py` | drives the turns |
| `score.py` | pure scoring |
| `README.md` | how to run it |

`run.py` drives the turns:
- **Screen:** through ApiFunction `/api/ask/start` and `/api/ask/result`, with a synthetic authorizer event.
- **Voice:** it writes a `pending` state with `ask_voice_turn.put_state`, then Event-invokes the worker with the same event shape `start` sends.
- **Logs:** it reads `ASK_ROUTE` from the worker's log group, keyed by turn id.

**Spec:** `docs/superpowers/specs/2026-10-15-ask-orchestration-v2-design.md` §9 (on branch `spec/ask-orchestration-v2`). Gates and groups are taken verbatim from it.

## Global Constraints

- **Logging:** never log or print question, answer or `caller_sub` text from the *service*. The harness may print its own questions, since they are fixtures; answers go only to the local results file under `tools/ask-routing-eval/results/`, which is git-ignored.
- **Cost guard:** `run.py` refuses to start unless `--confirm-calls N` is given and N ≥ its own estimate of model calls. The estimate is printed by `--dry-run`.
- **Environment:** TEST only. The function names are taken from `--stack fieldsight-test`, and anything containing `prod` is refused.
- **Git:** stage named files only. Never `git stash`.
- **Tests:** run with `python -m pytest -q -p no:cacheprovider <files>`.

## Review Focus

1. A run against prod must be impossible: a stack name containing "prod" exits with code 2 before any AWS call.
2. The harness must never set `TEST_ASK_AGENTIC` or any repo variable. It reads the deployed flag value and refuses to run if the flag does not include the channel under test.
3. A turn whose `ASK_ROUTE` line cannot be found is scored `unscored`, not `pass`.
4. The gate "guard fires" counts the judge's raw `basis` (`ASK_ROUTE.basis` / `basis_derived` / `guard`), not the client answer.
5. Relative-date items ("yesterday") record the run date in the results, so a re-run on another day is not compared blindly.

---

### Task 1: turn id in `ASK_ROUTE`

**Files:** Modify `src/ask_orchestrator.py` and `src/ask_voice_turn.py`. Test `tests/unit/test_ask_orchestrator_wiring.py`.

- [ ] Test first: when the worker routes a turn through the orchestrator, the logged `ASK_ROUTE` JSON carries `"turn": <turn_id>`.
  - The turn id is a random hex string, not user text, so logging it is allowed.
  - With the flag off, the old `_rag_answer` `ASK_ROUTE` line is unchanged: no new key.
- [ ] Implement:
  - `answer(..., turn_id=None)` puts `route["turn"] = turn_id`.
  - Both worker branches pass it.
- [ ] Commit `ask_orchestrator: turn id in ASK_ROUTE`.

### Task 2: question set and scorer

**Files:**
- Create `tools/ask-routing-eval/set.v1.jsonl`, `tools/ask-routing-eval/score.py` and `tools/ask-routing-eval/README.md`.
- Add `tools/ask-routing-eval/results/` to `.gitignore`.
- Test: `tests/unit/test_ask_routing_eval_score.py`.

**Set:** 22 items, in the groups of spec §9, with these fields:
- `id`, `group`, `question`, `channels`, `expect`.
- Groups:
  - `project_day` ×4
  - `project_empty_day` ×3
  - `person_or_latest` ×3
  - `general` ×5
  - `mixed` ×3
  - `explicit_web` ×2
  - `scoped_general` ×1: carries a body `site_id`. The harness reads one visible site id at run time, from `/api/org/sites` through org-api with the same sub, or takes `--site-id`.
  - `web_cap` ×1: a question that invites two lookups.
- Write questions about the TEST account's real days, using `Ben_UCPK2`'s records:
  - list the dates with records using RDS Data API on `fieldsight_test`, read-only;
  - put the dates the questions rely on in `expect.dates`.

**Scorer:** `score_turn(item, result, route) -> {"verdict": "pass"|"fail"|"unscored", "why": [...]}`. Rules by group, from spec §9:

| Group | Pass when |
|---|---|
| `project_day` | `citations` ≥ 1, `from_web` falsy, and `route.refs_w == 0` |
| `project_empty_day` | no W; the answer mentions the `latest_before` date (any of ISO, "23 September", "September 23", "the 23rd") or says nothing was recorded; `route.calls` includes `day_overview` |
| `person_or_latest` | `citations` ≥ 1, and the expected date appears in the answer or in a citation's `report_date` |
| `general` | `from_web` true when `route.policy` is auto/required; the voice channel passes on `web_offer` |
| `mixed` | R and W both cited (`route.refs_r` ≥ 1 and `refs_w` ≥ 1); on voice, the offer is appended |
| `explicit_web` | `route.web_ran` |
| `scoped_general` | `route.policy == "never"` and `refs_w == 0` |
| `web_cap` | at most one web call |

`gate(results) -> {"pass": bool, "lines": [...]}`, using the spec §9 gates:
- project and empty-day items: 3/3 per item;
- general and mixed: ≥ 2/3;
- zero `bad_refs` reaching the client;
- guard fires (`route.guard`) ≤ 1 in total, and 0 on project items;
- p50 latency: project ≤ 7 s, general ≤ 25 s.

Unscored turns fail the gate.

- [ ] Write tests with fixture results and routes for every rule and every gate line. Include an unscored turn and a guard fire on a project item.
- [ ] Implement `score.py`: pure, stdlib only.
- [ ] Commit `ask routing eval: set and scorer`.

### Task 3: runner

**Files:** Create `tools/ask-routing-eval/run.py`. Test: `tests/unit/test_ask_routing_eval_run.py`, using a fake boto3 client and no network.

- [ ] **CLI:**
  ```
  run.py --stack fieldsight-test --sub <cognito sub> --channel screen|voice|both
         --repeat 3 [--only id,id] [--site-id X] [--dry-run] [--confirm-calls N]
  ```
- [ ] **Cost estimate:** per turn, 1 pre-check + 1 judge, + 2.5 for web groups, + 1 for multi-round groups. Round up. Print it per channel and in total.
- [ ] **Refusals**, each with exit code 2 before any network call:
  - the stack contains "prod";
  - `--confirm-calls` is missing or below the estimate;
  - the deployed worker env `ASK_AGENTIC` (read with `lambda.get_function_configuration`) does not include the channel.
- [ ] **Screen turn:** invoke `<stack>-api` with the `/api/ask/start` event, then poll `/api/ask/result` until a terminal status or 70 s. Use the same event shape as the `screen_turn.py` smoke: `requestContext.authorizer.claims.sub`.
- [ ] **Voice turn:** both steps use the same functions the service uses.
  1. Put the pending state with `ask_voice_turn.put_state`, `ask_voice_turn.state_key` and `ask_voice_turn.sub_hash`. Import them from `src` by path.
  2. Event-invoke `<stack>-ask-voice-worker` with `{turn_id, caller_sub, transcript, tz, web_intent, offer_web: true}`. `web_intent` comes from `voice_web.detect_intent`.

  Then poll the S3 state until it is terminal.
- [ ] **Log join:** after each batch, run `logs.filter_log_events` on `/aws/lambda/<stack>-ask-voice-worker` with pattern `"ASK_ROUTE"` over the batch window. Parse the JSON and join on `turn`.
- [ ] **Results:** write `results/<UTC timestamp>-<channel>.json` (items, results, routes, verdicts, run date, flag values), then print the `gate()` lines.
- [ ] **Tests:**
  - every refusal;
  - the estimate arithmetic;
  - the screen and voice drivers against fakes (the right event shapes);
  - the log join, including a missing line ⇒ unscored.
- [ ] Commit `ask routing eval: runner`.

### After all tasks (controller)

1. PR to develop. Once CI is green, merge it.
2. When `ask_tools` is on TEST and the contract test has passed:
   1. Run `run.py --dry-run` and state the count to the owner.
   2. Set `TEST_ASK_AGENTIC=screen,voice`; this is TEST-only and the owner's earlier authorisation covers TEST flags. Redeploy.
   3. Run the eval.
   4. If the gate fails, unset the flag and redeploy. Do not leave a failing flag on.
   5. Report the results per item.
