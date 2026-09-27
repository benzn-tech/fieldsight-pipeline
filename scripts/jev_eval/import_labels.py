"""Import owner-labelled batch labels into the Jev shadow-eval fixtures
(Track A, Task 10).

Reads a `{set}.labels.json` file downloaded from `label_page.py` (`{id:
"yes"|"no"|"unsure"}`) plus the matching
`scripts/fixtures/jev_eval/batch/{set}.batch.jsonl` batch file (for the
`features`/`baseline`/`site_id`/`company_id` that go with each id), and:

- **yes/no** rows are merged into `scripts/fixtures/jev_eval/{set}.jsonl`
  (the same file `export_labels.py` writes) in the Task 1 shape, with
  `label_source: "owner"` and `decided_at` set to the import time.
- **unsure** rows are dropped and counted, never written to `{set}.jsonl`.
- **Idempotent by id**: re-importing the same (or an updated) labels file
  replaces any existing row for that id in `{set}.jsonl` rather than
  duplicating it -- the merge is keyed on `id`, so importing twice leaves
  the file exactly as one import would.
- `counts.json` is refreshed for `{set}` with a `label_source` breakdown
  (`{"owner": n, ...}` over whatever `label_source` values are present in
  the merged file) -- every other field `export_labels.py` writes
  (`database`, `exported_at`, `exclusions`, `descriptive_only`) is
  preserved untouched; only `n`/`positives`/`negatives` are recomputed from
  the merged file, since the import just changed it.
"""
from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

from scripts.jev_eval import export_labels as ex
from scripts.jev_eval.sample_batch import BATCH_DIR

FIXTURES_DIR = ex.FIXTURES_DIR


def load_jsonl(path: Path) -> list:
    if not path.exists():
        return []
    rows = []
    for line in path.read_text(encoding="utf-8").splitlines():
        if line.strip():
            rows.append(json.loads(line))
    return rows


def write_jsonl(path: Path, rows: list) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", encoding="utf-8") as fh:
        for row in rows:
            fh.write(json.dumps(row, sort_keys=True))
            fh.write("\n")


def load_labels(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def build_import_rows(set_name: str, batch_rows: list, labels: dict, now_iso: str) -> tuple:
    """`(new_rows, n_unsure, n_skipped_unknown_id)`. `new_rows` are the Task
    1-shaped rows for every yes/no label whose id is present in
    `batch_rows`; a label id with no matching batch row (fix wave 3, minor
    2: it belongs to a different, or a since-resampled, batch) is skipped
    and COUNTED, rather than silently dropped, so a stale labels file
    produces a visible number instead of a quietly-smaller import."""
    by_id = {row["id"]: row for row in batch_rows}
    new_rows = []
    n_unsure = 0
    n_skipped_unknown_id = 0
    for item_id, verdict in labels.items():
        if verdict == "unsure":
            n_unsure += 1
            continue
        if verdict not in ("yes", "no"):
            continue
        batch_row = by_id.get(item_id)
        if batch_row is None:
            n_skipped_unknown_id += 1
            continue
        new_rows.append({
            "set": set_name,
            "id": item_id,
            "label": verdict,
            "features": batch_row.get("features"),
            "site_id": batch_row.get("site_id"),
            "company_id": batch_row.get("company_id"),
            "decided_at": now_iso,
            "baseline": batch_row.get("baseline"),
            "label_source": "owner",
        })
    return new_rows, n_unsure, n_skipped_unknown_id


def merge_rows(existing_rows: list, new_rows: list) -> list:
    """Idempotent-by-id merge: a new row replaces any existing row with the
    same id; everything else is kept. Sorted by id for a stable diff."""
    by_id = {row["id"]: row for row in existing_rows}
    for row in new_rows:
        by_id[row["id"]] = row
    return [by_id[key] for key in sorted(by_id, key=str)]


def _label_source_breakdown(rows: list) -> dict:
    breakdown: dict = {}
    for row in rows:
        source = row.get("label_source") or "unknown"
        breakdown[source] = breakdown.get(source, 0) + 1
    return breakdown


def refresh_counts(counts_path: Path, set_name: str, merged_rows: list) -> dict:
    counts = json.loads(counts_path.read_text(encoding="utf-8")) if counts_path.exists() else {}
    existing_entry = counts.get(set_name, {})

    n = len(merged_rows)
    positives = sum(1 for r in merged_rows if r.get("label") == "yes")
    negatives = sum(1 for r in merged_rows if r.get("label") == "no")

    entry = dict(existing_entry)
    entry["n"] = n
    entry["positives"] = positives
    entry["negatives"] = negatives
    entry["descriptive_only"] = n < ex.MIN_N_FOR_CONCLUSIONS
    entry["label_source_breakdown"] = _label_source_breakdown(merged_rows)
    counts[set_name] = entry
    counts.setdefault("route_note", ex.ROUTE_NOTE)

    counts_path.parent.mkdir(parents=True, exist_ok=True)
    with open(counts_path, "w", encoding="utf-8") as fh:
        json.dump(counts, fh, indent=2, sort_keys=True)
        fh.write("\n")
    return counts


def import_set(set_name: str, labels_path: Path, *, batch_dir: Path = BATCH_DIR,
                fixtures_dir: Path = FIXTURES_DIR, now_iso: str | None = None) -> dict:
    now_iso = now_iso or datetime.now(timezone.utc).isoformat()

    batch_path = batch_dir / f"{set_name}.batch.jsonl"
    if not batch_path.exists():
        raise FileNotFoundError(
            f"missing batch file: {batch_path}; run scripts/jev_eval/sample_batch.py first")
    batch_rows = load_jsonl(batch_path)

    labels = load_labels(labels_path)
    new_rows, n_unsure, n_skipped_unknown_id = build_import_rows(
        set_name, batch_rows, labels, now_iso)

    fixture_path = fixtures_dir / f"{set_name}.jsonl"
    existing_rows = load_jsonl(fixture_path)
    merged_rows = merge_rows(existing_rows, new_rows)
    write_jsonl(fixture_path, merged_rows)

    counts = refresh_counts(fixtures_dir / "counts.json", set_name, merged_rows)

    return {
        "set": set_name,
        "imported_yes_no": len(new_rows),
        "unsure_dropped": n_unsure,
        "skipped_unknown_id": n_skipped_unknown_id,
        "total_after_merge": len(merged_rows),
        "fixture_path": str(fixture_path),
        "counts": counts[set_name],
    }


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--set", dest="set_name", required=True,
                         choices=("threads", "work_class"))
    parser.add_argument("--labels", required=True, type=Path,
                         help="path to the downloaded {set}.labels.json")
    args = parser.parse_args(argv)

    result = import_set(args.set_name, args.labels)
    print(json.dumps(result, indent=2, sort_keys=True, default=str))
    return 0


if __name__ == "__main__":
    sys.exit(main())
