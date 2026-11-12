# Reconcile Results Marker Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** After a reconcile request is processed and its writer step succeeds, the matcher writes `reconcile_results/<same suffix>`. The marker holds counts, `input_hash` and `request_etag`, so nightly J1 can tell a dropped request from an empty one.

**Architecture:** Three parts:
- `reconcile_request` gains `RESULTS_PREFIX` and `results_key()`.
- The matcher collects processed reconcile records and puts one marker each through `lake_put`, after the writer invoke.
- `lake_keys` gets a new family, and the template gets two PutObject grants.

**Tech Stack:** Python 3.11, boto3, pytest, SAM template.

**Spec:** `docs/superpowers/specs/2026-11-13-reconcile-results-marker-design.md` (rev 2, after the Fable review).

## Global Constraints

- The marker body carries counts and ids only. Never quote, item text or transcript.
- A failing marker put is logged with the key only and never raises. Each put has its own try/except.
- No marker in `dry_run`, and no marker when the writer invoke raises.
- `match_requests/` behaviour and the writer payload stay byte-identical.
- No flag, no migration. The IAM change is additive.
- Python: `C:\Users\camil\fspy\Scripts\python.exe`, unit tests with `PYTHONPATH=src`.

## Review Focus

1. **A re-emitted request uses the same key.** The marker must carry the new `input_hash`, and a re-run overwrites it. Pinned by `test_marker_carries_input_hash_and_etag`.
2. **A mixed match + reconcile event.** Markers are written only for the reconcile keys. Pinned by `test_mixed_event_marks_only_reconcile_keys`.
3. **The first of two marker puts throws.** The second is still written. Pinned by `test_marker_put_failure_is_logged_not_raised`.

---

### Task 1: Write the marker

**Files:**
- Modify:
  - `src/reconcile_request.py`: add `RESULTS_PREFIX` and `results_key`;
  - `src/lambda_programme_matcher.py`: `lambda_handler`, ~940-1010;
  - `src/lake_keys.py`: the family list, right after `reconcile` at line 48;
  - `src/template.yaml`: the ProgrammeMatcher (MatcherFunction) policy, ~5860-5900.
- Test:
  - `tests/unit/test_reconcile_request.py`;
  - `tests/unit/test_lambda_programme_matcher.py`;
  - `tests/unit/test_lake_keys.py`;
  - `tests/unit/test_template_reconcile_iam.py`.

**Interfaces:**
- Produces:
  - `reconcile_request.RESULTS_PREFIX = "reconcile_results/"`;
  - `reconcile_request.results_key(request_key: str) -> str`. It raises ValueError when the key does not start with `PREFIX`;
  - the marker body, per spec §2: `request_key, site_id, date, extraction_key, trigger, counts, input_hash, request_etag, status_suggestions, pick_cards, processed_at`.

- [ ] **Step 1: Write the failing tests.**
  - `test_results_key_round_trip`: `results_key("reconcile_requests/S/2026-11-12/ab.json") == "reconcile_results/S/2026-11-12/ab.json"`. A `match_requests/...` key raises ValueError.
  - `test_reconcile_results_key_parses`, in test_lake_keys: a uuid site gives family `reconcile_result`, `site_id` equal to that uuid, and the same scope as a `reconcile_requests/` key.
  - The matcher tests:
    - `test_marker_written_after_invoke`: one put to `reconcile_results/…` after the writer invoke, through the stubbed s3 client.
    - `test_marker_carries_input_hash_and_etag`: `input_hash` is the sha256 of the request bytes, `request_etag` is the GetObject ETag, the counts are those from `process_request`, and no `quote` or `text` key appears anywhere in the body.
    - `test_no_marker_when_writer_fails`: FunctionError raises, and nothing is put.
    - `test_no_marker_in_dry_run`.
    - `test_marker_when_nothing_to_write`: zero suggestions, no invoke, and the marker is still written.
    - `test_mixed_event_marks_only_reconcile_keys`.
    - `test_marker_put_failure_is_logged_not_raised`: two reconcile records; the first put raises; the second is still put; the handler returns normally.
  - `test_matcher_can_write_reconcile_results`, in the IAM test: the policy grants PutObject on `${IngestBucketName}/reconcile_results/*` and on `${IngestBucketName}/lake_manifest/*`. Follow the file's existing assertion style.
- [ ] **Step 2:** Run the four test files. Expected: the new tests fail.
- [ ] **Step 3: Implement** per spec §3.
  - Use `lake_put(S3_BUCKET, key, json.dumps(body), client=s3(), owner_hint={"site_id": site_id}, ContentType="application/json")`.
  - Take `processed_at` from `datetime.now(timezone.utc).isoformat()`.
- [ ] **Step 4:** Run the four files, then `-k "matcher or reconcile or lake_keys or template"` over the unit tests. Expected: all pass. cfn-lint is pinned in CI; run it if it is installed locally.
- [ ] **Step 5: Commit** `feat(reconcile): reconcile_results marker per processed request`.
