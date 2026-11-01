# Action pack eval harness

The gates in spec `docs/superpowers/specs/2026-11-01-background-action-agent-design.md` §8
must pass before any `ACTION_PACKS` flag step (§9). Every number is counted in code from
files. No model grades anything (§8.5). The harness reports the writer's raw rates and the
post-verifier rates separately (§14 S16).

- **Data stays out of git.** Real gold sets, labels, recorded packs, blind subsets and
  reports go under an `action_pack_eval_runs/` directory, which is gitignored. Every write the
  harness makes is refused anywhere else (`paths.require_run_dir`, the `continuity_eval`
  convention). The only committed data is `examples/`, which is fully synthetic.
- **TEST only.** `submit` and `fetch` talk to TEST org-api. Run them only after merge, with
  `ACTION_PACKS=internal` on TEST. Nothing in the tests or CI makes a network call.

File formats and matching rules: [SCHEMA.md](SCHEMA.md).

## Offline commands (run from the repo root)

```bash
PY=C:/Users/camil/fspy/Scripts/python.exe
E=scripts/action_pack_eval/examples

# 1. Gold set: errors exit 1. Having fewer than 20 instructions, or fewer per scenario than
#    the spec's composition, only produces a warning.
$PY -m scripts.action_pack_eval.cli validate-gold --gold $E/gold.json

# 2. Score one run: writes report.json and report.md. Exits 0 only when every gate passes.
$PY -m scripts.action_pack_eval.cli score --gold $E/gold.json --run $E/run_a.json \
    --annotations $E/annotations_a.jsonl --prices $E/prices.json \
    --out action_pack_eval_runs/example
#    --second <labels.jsonl>   adds inter-annotator agreement (the ceiling)
#    --thresholds <t.json>     overrides metrics.DEFAULT_THRESHOLDS keys

# 3. Blind second-annotator subset: a seeded, deterministic sample of the elements the first
#    annotator labelled. It carries no first-annotator labels and no verifier labels.
$PY -m scripts.action_pack_eval.cli make-blind-subset --run $E/run_a.json \
    --annotations $E/annotations_a.jsonl --fraction 0.3 --seed 20261101 \
    --out action_pack_eval_runs/example/blind.jsonl

# 4. Agreement between the two label files (percent agreement and Cohen's kappa).
$PY -m scripts.action_pack_eval.cli agreement --first <ann1.jsonl> --second <ann2.jsonl> \
    --out action_pack_eval_runs/example/agreement.json

# 5. Noise floor and variants (report.json files). Refuses with fewer than two baseline runs.
$PY -m scripts.action_pack_eval.cli compare \
    --baseline action_pack_eval_runs/base1/report.json action_pack_eval_runs/base2/report.json \
    --variant action_pack_eval_runs/v1/report.json --out action_pack_eval_runs/cmp
```

Default thresholds (spec §8.3; the owner may tighten them):

| Gate | Default |
|---|---|
| Fabrication of shown elements | ≤ 5% |
| Wrong target (shown) | 0 |
| Recall | ≥ 60% |
| Latency p90 | ≤ 300 s, measured on at least 20 finished packs |

"Uncertain" honesty, cost and agreement are reported without a gate.

## After merge: the TEST run (owner-approved, TEST only)

1. Write the real gold set (≥ 20 instructions, with the owner's expected actions and the
   §8.1 composition) to `action_pack_eval_runs/gold.json`, then run `validate-gold`.
2. Set `ACTION_PACKS=internal` on TEST (§9 step 2).
3. Put a TEST platform_admin's Cognito id token in `ACTION_PACK_EVAL_TOKEN`. If any gold
   `site` is a name rather than a uuid, write a `{name: uuid}` file for `--site-map`.
4. Submit. Packs run one at a time, because org-api allows one active pack per creator:
   ```bash
   $PY -m scripts.action_pack_eval.cli submit --gold action_pack_eval_runs/gold.json \
       --base-url https://<TEST api>/api/org --site-map action_pack_eval_runs/sites.json \
       --config-label baseline --out action_pack_eval_runs/base1/run.json
   ```
   Re-run `fetch --run ... --out ...` to refresh the packs later. Score before anyone applies
   or archives an action: `updated_at` is the end-to-end latency clock.
5. Attach `run_trace` and `models`. **GET does not return them** (`action_pack_view.shape_pack`),
   so export them read-only from TEST's `action_packs` table, one JSON object per line:
   ```bash
   psql "$TEST_DATABASE_URL" -At -c "SELECT json_build_object('id', id, 'run_trace',
     run_trace, 'models', models) FROM action_packs
     WHERE id = ANY('{<pack uuid>,<pack uuid>}'::uuid[])" > action_pack_eval_runs/base1/traces.jsonl
   ```
   ```bash
   $PY -m scripts.action_pack_eval.cli attach-traces --run action_pack_eval_runs/base1/run.json \
       --traces action_pack_eval_runs/base1/traces.jsonl --out action_pack_eval_runs/base1/run.json
   ```
   Without traces, latency falls back to timestamps only, and cost and the verifier-drop
   counts read as n/a or 0.
6. Label every stored element (SCHEMA.md). Make the blind subset and give it to a second
   annotator who has not seen the packs.
7. Score the run with `--second`, and with `--prices <price file>`. The price file holds
   current per-1k-token rates. No rate is hardcoded.
8. **Noise floor first** (§8.4). Submit the same gold set again with no change, as `base2`.
   Label it, score it, then `compare` base1 against base2. Only after that, run and compare
   variants. A delta inside the floor is reported as `within_noise`.

## Known approximations

- **Cost.** `run_trace.tokens` is one total over planner, writer and verifier. Where a vendor
  returned no usage, it includes the agent's chars/4 estimate. The harness applies one
  blended rate per pack, looked up as `writer_served`, then `writer`, then `default`.
- **Rejected conclusion lines are not stored.** Unsupported actions are stored, but conclusion
  lines the verifier rejected are not, so nobody can label them. The report counts them from
  `run_trace.verify` and also gives a raw upper bound that counts each one as a fabrication.
- **Two-run floor.** A noise floor from two runs is a minimum bar, not a confidence interval.
- **Latency minimum.** `latency_min_n` defaults to 20, the gold minimum. With exactly 20
  instructions, a single entry that never produced a pack (`site_unresolved`, `post_4xx`)
  leaves the latency gate `insufficient` unless `--thresholds` lowers it. Timed-out
  instructions are not dropped: each one enters the gate as a censored value equal to its
  client-side wait.
- **409 at submit.** `submit` does not retry a 409. Make sure no pack of the token's user is
  queued or running before a run starts; otherwise every POST gets `post_409`.
