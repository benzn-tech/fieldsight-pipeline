# Ask routing eval

Decides whether `TEST_ASK_AGENTIC` may be switched on. Runs the spec §9 question set
through the real TEST worker on both channels and scores each turn from its result
plus its `ASK_ROUTE` log line.

- Spec: `docs/superpowers/specs/2026-10-15-ask-orchestration-v2-design.md` §9
- Plan: `docs/superpowers/plans/2026-10-16-ask-orchestration-v2-phase4-eval.md`

| File | Role |
|---|---|
| `set.v1.jsonl` | the question set (22 items) |
| `run.py` | drives the turns; the only file that touches AWS |
| `score.py` | pure scoring and the gate, stdlib only |
| `results/` | one JSON per run; git-ignored, because it holds answer text |

## Running it

```bash
export AWS_PROFILE=fieldsight-deployer MSYS_NO_PATHCONV=1
# 1. The estimate. Offline: no AWS call is made.
python tools/ask-routing-eval/run.py --stack fieldsight-test --sub <cognito sub> \
    --channel both --repeat 3 --dry-run
# 2. State that number to the owner. Then run with it.
python tools/ask-routing-eval/run.py --stack fieldsight-test --sub <cognito sub> \
    --channel both --repeat 3 --confirm-calls <N>
```

`--only pd1,g1` runs a subset. `--site-id X` sets the body `site_id` of the scoped
item; without it the harness reads the first site `/api/org/sites` returns for the
same sub.

Refusals, all with exit code 2:

- the stack name contains `prod` (before any AWS client is made);
- `--confirm-calls` is missing or below the estimate;
- the deployed worker's `ASK_AGENTIC` does not list the channel under test.

The harness never sets `ASK_AGENTIC` or any repo variable. It only reads the worker's
configuration.

## Cost estimate

Per turn: 1 pre-check + 1 judge. Web groups add 2.5 and multi-round groups add 1.
The total is rounded up per channel.

- Web groups: `general`, `mixed`, `explicit_web`, `web_cap`.
- Multi-round groups: `mixed`, `web_cap`.

The pre-check is counted on every turn, even where none runs (voice, `never`), so
the number is an upper bound.

## The set

These are real `Ben_UCPK2` days on `fieldsight_test`, site UC PK. They were read with
read-only RDS Data API SELECTs on 2026-10-16, and each item's `expect.dates` holds
the dates it relies on.

- **Recorded days:** 2026-08-27, 09-02, 09-03, 09-07 and 09-23.
- **Latest record:** 2026-09-23. No records after it.

Some limits of the set:

- **Explicit dates are ISO.** `query_slots.time_range` only parses ISO dates
  (`2026-09-03`). "3 September" is not anchored, so a question phrased that way gets
  no `day_overview` and the `auto` policy.
- **Relative dates.** `pe1` ("yesterday") cannot pin `now`. `/api/ask/start`
  whitelists its body and drops `now`, and the voice worker never reads it. The
  results record the run date. `expect.latest_before` (2026-09-23) holds only while
  no records land after that day.
- **The scoped item is screen-only.** The voice worker builds its body from the
  transcript, so it has no `site_id`.

## Scoring

`score.normalize` maps each channel's terminal state onto one shape.

- **Voice** states carry no citation cards and no `web_offer`. On voice,
  "citations ≥ 1" reads `route.refs_r`. The offer is recognised by the fixed
  sentence the worker appends (`voice_web.OFFER_ALSO` / `OFFER_NO_RECORDS`).
- **Unjoined turns:** a turn with no joined `ASK_ROUTE` line is `unscored`, and any
  unscored turn fails the gate.
- **Web cap:** round 1's web run is not listed in `route.calls`. A refused second
  `web_search` is still listed there. So `web_cap` passes on `route.web_ran`, and
  it notes how many times the judge asked again.

Gate lines (`score.gate`):

| Line | Rule |
|---|---|
| `project_day`, `project_empty_day`, `person_or_latest` | every repeat passes, per item and channel |
| `general`, `mixed` | ≥ 2/3 of repeats pass |
| `explicit_web`, `scoped_general`, `web_cap` | reported per item, not gated (spec §9 gives no gate) |
| bad refs reaching the client | 0 |
| guard fires | ≤ 1 in total, and 0 on project items |
| p50 latency | project/screen ≤ 7 s; web groups ≤ 25 s |

- **Bad refs** are counted in the *client* answer: any `[R…]`/`[W…]` token, any `[n]`
  with no card behind it, or any `[n]` on voice. `route.bad_refs` counts the invented
  refs that `to_client` stripped. It is printed as an INFO line and never gated.
- **Guard fires** count the judge's raw verdict, `route.guard` (or
  `fallback == "guard"`), not the client answer.
- **Latency** is `route.ms.total`, the service-side time of the orchestrator. The
  harness's own wall clock is recorded beside it in the results.

Project facts are not machine-checked. Check them by hand against the UI timeline for
those days, and state the method in the report.
