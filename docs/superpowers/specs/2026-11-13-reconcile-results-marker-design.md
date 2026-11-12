# Reconcile results marker

Date: 2026-11-13. Line: status suggestions / cross-session reconcile.

Agreed interface from `docs/superpowers/specs/2026-11-10-nightly-rematch-agent-design.md`:
- §J1 "Dropped requests";
- the §12 status-line row, item (3).

## 1. Problem

The matcher Lambda processes a `reconcile_requests/<site>/<date>/<digest>.json` request and returns. The S3 event fails and retries only when the model call or the writer invoke raises. Once retries run out, a dropped request looks exactly like one that produced zero suggestions. The nightly job's J1 needs a per-request "processed" record so it can re-emit a request that is older than 6 h and has no record, once.

## 2. Contract

**When it is written:**
- once per processed `reconcile_requests/` record;
- only after `lambda_programme_matcher.lambda_handler` has finished that event's writer step;
- "finished" means the writer invoke returned without `FunctionError`, or nothing needed writing;
- never in `dry_run`.

**Key:** `reconcile_request.results_key(request_key)`: the request key with its prefix `reconcile_request.PREFIX` replaced by a new `reconcile_request.RESULTS_PREFIX = "reconcile_results/"`. The rest stays byte-identical. Nightly J1 already imports `reconcile_request`, so it uses the same helper and never rebuilds the key by string surgery.

**Body (JSON):** counts and ids only. It never contains quote, item text or transcript.

```json
{"request_key": "reconcile_requests/<site>/<date>/<digest>.json",
 "site_id": "<uuid>", "date": "YYYY-MM-DD", "extraction_key": "...",
 "trigger": null, "counts": {"offered": 2, "kept": 2, "picks": 0, "dropped_by": {}},
 "input_hash": "<sha256 of the request bytes>", "request_etag": "<GetObject ETag>",
 "status_suggestions": 2, "pick_cards": 0, "processed_at": "<UTC ISO>"}
```

- `counts` is `reconcile_worker.process_request(...)["counts"]` as is.
- `trigger` is the request's `trigger` (for example "nightly"), or null. Requests built by the item writer carry no trigger.
- `input_hash` is the sha256 the handler already computes before `json.loads`.
- `request_etag` is the `ETag` from the same GetObject response.
- `status_suggestions` and `pick_cards` count what was SENT to the writer. The writer's own skips (duplicate, gone) are not read back.

**Semantics:**
- **A request counts as processed** when a marker exists AND its `input_hash` equals the sha256 of the current request bytes. The reader may instead compare `request_etag` with the current object's ETag from ListObjectsV2, which needs no extra GetObject. A marker from an older version of a re-emitted request, which uses the same key, does NOT count. This mirrors the cross-day line's (input_key, input_hash) dedupe.
- "No marker", or a marker whose hash does not match, means not processed.
- These outcomes are all "processed": the model ran, there were no candidates, or `no_transcript`. In each case the writer accepted the batch, or there was nothing to send.
- A re-run overwrites the marker. The last successful processing wins.

## 3. Design

- **Matcher.** Collect `(key, req, out)` for each reconcile record in the loop. After the existing writer step succeeds, put one marker per collected record.
  - A marker put that fails is logged with the key only. It does NOT raise.
  - Raising would retry the whole S3 event and repeat a paid model call. A missing marker costs at most one nightly re-emit, and the writer's unique keys absorb it.
- **Write path.** Call `lake_put(S3_BUCKET, key, body, client=s3(), owner_hint={"site_id": site_id}, ContentType="application/json")`, exactly as `reconcile_request.emit` does.
  - Pass `client=s3()`. Without it, lake_put builds its own global client and the matcher tests cannot stub it.
  - The owner hint puts the object into the lake ownership index, so company export and deletion cover it.
  - Wrap each marker put in its own try/except. One failure never skips the others.
- **`lake_keys`.** Add the family `("reconcile_result", rf"^reconcile_results/(?P<site_id>{_UUID})/{_DATE}/[^/]+$")` directly after `reconcile`, with the same scope and owner handling.
  - This is load-bearing. `lake_owner._one` returns "unresolved" before it applies the hint when `parse()` is None. Export and deletion read only `lake_objects`.
  - `test_lake_keys` has no completeness check, so the plan must add an explicit row. That row must use a real uuid site id.
- **IAM** (`src/template.yaml`, the matcher function's policy):
  - `s3:PutObject` on `${IngestBucketName}/reconcile_results/*`;
  - `s3:PutObject` on `${IngestBucketName}/lake_manifest/*`. The manifest goes to the same bucket as the data put. Copying the house statement, which lists both buckets, is fine.
- **Readers.** None in this change. Nightly J1's dropped-request re-emit is the owning line's work. The handover message gives it three things:
  - `results_key()`;
  - the processed rule (hash or ETag match);
  - the grants its host needs: `s3:GetObject` on `reconcile_results/*`, plus a ListBucket with an `s3:prefix` condition on `reconcile_results/*`. Without the ListBucket grant, a missing marker returns 403, not 404.

## 4. Non-goals

- Recording failures. A raise stays a raise, so the S3 retry semantics are unchanged.
- Any change to `match_requests/` processing. Its payload and behaviour stay byte-identical.

## 5. Tests

Unit tests in `tests/unit/test_lambda_programme_matcher.py`:
- the marker key, written after the invoke, with `client` being the stubbed s3;
- the body fields, including input_hash and request_etag, with no text;
- no marker when the invoke raises on FunctionError;
- no marker in dry_run;
- a marker when nothing needed writing (zero suggestions);
- a failing marker put is logged and does not raise;
- a mixed match plus reconcile event writes markers only for the reconcile keys.

Other tests:
- `tests/unit/test_lake_keys.py`: parse a results key with a uuid site, giving its family, site_id and scope.
- `tests/unit/test_reconcile_request.py`: `results_key` round trip.
- `tests/unit/test_template_reconcile_iam.py`: the matcher policy grants PutObject on `reconcile_results/*` and on lake_manifest.

## 6. Rollout

develop, then TEST. It ships to prod with the next release. The template IAM change is additive. No flag, no migration.
