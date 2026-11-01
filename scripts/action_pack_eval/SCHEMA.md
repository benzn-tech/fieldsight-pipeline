# Action pack eval: file formats and counting rules

## Gold set (`--gold`)

The gold set can be a JSON object `{"instructions": [...]}`, a JSON list, or JSONL. Each
instruction looks like this:

| Field | Type | Notes |
|---|---|---|
| `id` | str | Unique within the gold set. |
| `site` | str | TEST site uuid, or a name resolved through `submit --site-map`. |
| `instruction` | str | Non-empty, at most 2000 characters (org-api's limit). |
| `scenario` | str | `pour_review`, `tidy_open_items`, `standard_in_library`, `standard_not_in_library` or `other`. |
| `expected_actions` | list | See below. |
| `must_not_touch` | list | Stable uuids (str) or task numbers (int). A close or update on any of these is a wrong target. |
| `expected_uncertain` | list | `[{"id", "standard"}]`: standards claims the pack should state as uncertain. |

An expected action has an `id` (unique within its instruction) and a `type` from
`close_item`, `update_item`, `create_item`, `draft_rfi`, `draft_email` or `note`.

- **close / update:** set `target_stable_id`, which is preferred, or `target_task_no`. The view
  only shows `target_task_no` while the reader can see the item.
- **create / draft / note:** set `match_hint`, the text the annotator matches a proposal
  against.
- **One type per target.** Two expectations of the same type on the same item (by stable id
  or by task number) are an error: one action fills one expectation.
- **`fields`:** optional on any type. It is documentation for the annotator and is not scored.

Validation:

- Errors stop `score` and `submit`.
- Fewer than 20 instructions, or fewer per scenario than §8.1 asks for (1 pour, 3 tidy,
  5 in-library, 5 not-in-library), only produces warnings.

## Recorded run (`--run`)

```json
{"run_id": "base1", "config_label": "baseline", "gold_sha256": "...", "inputs_sha256": "...",
 "packs": [{"gold_id": "G1", "pack_id": "<uuid>", "pack": {...}, "error": null}]}
```

- **`pack`** is the body of `GET /api/org/action-packs/{id}`. That body includes `run_trace`
  (its counts-shaped keys) and `models` (ids). For packs recorded before GET returned them,
  `attach-traces` adds them. `pack` is `null` when no finished pack exists; that
  instruction's expectations then count as missed.
- **`error`** is one of `site_unresolved`, `post_<status>`, `get_<status>` or `timeout`.
  A timeout row carries `client_elapsed_s`.
- **`inputs_sha256`** hashes what `submit` sent: each instruction's id, site and text.
  - `score` refuses a run whose inputs hash differs from the gold file's.
  - `compare` refuses reports whose submitted inputs differ.
  - Edits to expected actions after submission only produce a warning.
- **Run-trace keys read:**
  - `tokens`
  - `ms.total`
  - `verify.conclusion_unsupported` and `verify.drop_*` (the verifier's drops)
  - `write_dropped.*` (the writer's schema drops)
- **Model keys read:** `writer_served`, `writer`.

## Elements

Every stored conclusion line and every stored action is one element:

- A conclusion line is keyed `conclusion:<index>`, its position in the stored `conclusion`
  list.
- An action is keyed `action:<seq>`.

An element is **shown** unless it is an action whose verifier label is `unsupported`. Those
actions are stored for this comparison but are never applicable.

Conclusion keys are positions in the RECORDED list. A later `fetch` can drop lines whose
sources were removed (S13), and that shifts the positions. Label the run file you score, and
do not re-fetch between labelling and scoring.

## Labels (`--annotations`, `--second`)

Labels are JSONL, one row per element. A file holds **one annotator**; a second
annotator's labels go in their own file (`--second`). A repeated (pack, element) is an
error.

| Field | Type | Notes |
|---|---|---|
| `pack_id` | str | |
| `element` | str | `conclusion:<i>` or `action:<seq>` |
| `annotator` | str | |
| `supported_by_refs` | `yes`, `no` or `unclear` | Do the cited refs support the element? |
| `is_standards_claim` | bool | Does it state what a standard or code requires? |
| `matches_expected` | str, optional | Actions only: the gold expected-action id this action fulfils. |
| `expected_uncertain_id` | str, optional | Conclusion lines: the gold `expected_uncertain` claim this line states. |

**Blind subset.** `make-blind-subset` samples elements the first annotator labelled, using a
seed, so the same seed always picks the same elements. Each item carries:

- the instruction;
- the element's text, or its action fields (type, target, proposed, rationale, the target's
  snapshot);
- its refs, with each source's kind, provenance, title, date and excerpt;
- blank label fields.

It never carries the first annotator's labels, `verifier`, `confidence`, `state` or
`applicable`.

## Counting rules

### Fabrication

The rate is "no" labels divided by labelled elements:

- **Raw:** every stored element, including actions the verifier labelled unsupported.
- **Shown:** shown elements only. This is the post-verifier rate, and the gate reads it.

"unclear" counts in the denominator only. `rate_unclear_as_no` is the strict version, where
"unclear" also counts as "no". The verdict is `insufficient` while any shown element is
unlabelled.

Elements the verifier removed before storage (`dropped_by_verifier`) are counted from the
run trace, never labelled. `raw_upper_bound_rate` counts each of them as a fabrication.

### Wrong target

A close or update action has a wrong target when either:

- its target is on the gold entry's `must_not_touch` list; or
- an annotator matched it (`matches_expected`) to a close/update expectation, and the targets
  differ. Targets are compared by stable id, or by task number when the gold entry gives only
  `target_task_no`.

The gate counts shown wrong targets (≤ 0). The verdict is `insufficient` when no close or
update action was shown.

### Recall

Matching is **one-to-one**: an action fills at most one expectation.

1. Every annotator match (`matches_expected`) is honoured first. It must still have the
   right type and, for close/update, the right target.
2. Then, in gold order, each open close/update expectation takes the first unused action of
   its type on its target.

A create, draft or note expectation is matched only through step 1. An action that is a
wrong target (on `must_not_touch`, or matched to an expectation on another item) never
counts toward recall.

The gate counts matches among shown actions. The raw figure also counts the stored
unsupported actions.

### "Uncertain" honesty

The denominator is shown conclusion lines that meet both conditions:

- an annotator marked the line `is_standards_claim`;
- it cites no project-library document, meaning no source has provenance `documents` (refs
  `D#`).

The rate is the share of those lines whose confidence is `uncertain`. Actions have no
confidence field, so standards claims on actions are counted but not scored. The metric is
report only.

### Latency

Latency covers ready and failed packs. Two measures are kept:

- **End-to-end:** `updated_at - created_at`.
- **Worker time:** `run_trace.ms.total`.

The gate uses end-to-end, and falls back per pack to worker time when the timestamps are
missing. A `timeout` row enters the gate as its `client_elapsed_s`, a censored lower bound,
so the slowest instructions stay in the tail. These are counted in `sources` but have no
latency:

- rows without a pack (`no_pack`: `site_unresolved`, `post_*` / `get_*` errors);
- packs not finished at fetch time (`unfinished`).

The p50 and p90 use the nearest rank. The verdict is `insufficient` with fewer than
`latency_min_n` values; that minimum defaults to ceil(0.8 × gold size).

### Cost

Cost per pack is `run_trace.tokens` × the per-1k rate from the price file:

```json
{"currency": "USD", "per_1k_tokens": {"<model id>": 0.01}, "default": 0.02}
```

The rate is looked up by `models.writer_served`, then `models.writer`, then `default`. A pack
with no matching rate is counted as unpriced.

### Agreement

Agreement is computed on the elements both annotators labelled. For `supported_by_refs` and
`is_standards_claim` it gives percent agreement and Cohen's kappa. Kappa is `null` when it is
undefined: no pairs, or chance agreement equal to 1.

### Compare

`compare` takes the report.json files from the scored runs:

- **Identity:** every report needs a `run_id`, a `pack_ids_sha256` (a hash of its recorded
  pack ids) and a `submitted_inputs_sha256`. A repeated run id, or two reports with the same
  pack ids, is refused: a run passed twice, or a copied report, would give a floor of 0.
- **Floor:** for each headline metric, the spread (max − min) across the baseline runs. There
  must be at least two baseline runs, and they must share the config label. Every compared
  report, baseline or variant, must share the gold set (sha256) and the submitted inputs.
- **Variant verdict:** the variant's delta is measured against the baseline mean. It is
  `better` or `worse` only when |delta| > floor; otherwise it is `within_noise`.
