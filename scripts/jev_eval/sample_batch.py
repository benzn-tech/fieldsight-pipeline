"""Sample an owner-labelled batch for the Jev shadow evaluation (Track A, Task 10).

READ-ONLY, same safety shape as `scripts/jev_eval/export_labels.py`: every
query is a SELECT/WITH, run inside one begin-transaction / execute-statement /
rollback-transaction cycle (ALWAYS rolled back, in a `finally`), and
`--database fieldsight` (prod) is refused unless `--allow-prod` is also given
-- checked before any `aws` call. This module deliberately REUSES that
runner rather than re-implementing it: `_begin_transaction`, `_execute`,
`_rollback`, `record_to_dict`, `decode_field`, `CLUSTER`/`SECRET` (via
`scripts.verify_programme_schema`), `DEFAULT_DATABASE`/`PROD_DATABASE`/
`DEFAULT_PROFILE`/`DEFAULT_REGION` all come from `export_labels` (imported,
not copied).

Why this batch exists: `export_labels.py` can only export DECIDED rows (a
human already confirmed/rejected something), and on 2026-09-27 every set was
near-empty or one-class-only (see task-10-brief.md). This script builds
~`--size` UNDECIDED rows per set for the owner to label by hand with
`label_page.py`, in exactly the Task 1 row shape (`features`/`baseline`) so
`import_labels.py` can merge them straight into `{set}.jsonl` once labelled.

Two sets, `threads` and `work_class` (`programme_match` is out of scope for
this batch -- see task-10-brief.md line 9: candidate generation there needs
the matcher's embedding gate).

## threads

Per site, pull topics from the last `--window-days` days (default 120),
shaped exactly like `repositories.threads.candidate_corpus` (title / summary
/ open_items / report_date / id), with `deleted_predicates.visible_topics_predicate`
applied to the `topics` alias. Pairs are generated in PYTHON with the REAL
`thread_match.score_pair` / `thread_match.find_candidates` (imported, not
reimplemented) -- never a re-derived scorer:

- `find_candidates(later, corpus, min_score=LOWERED_FLOOR, max_gap_days=thread_match.MAX_GAP_DAYS)`
  is called once per topic with the floor LOWERED to `LOWERED_FLOOR` (0.10,
  well below `thread_match.MIN_SCORE`). This reuses find_candidates' own
  eligibility rules unmodified -- both topics need `open_items > 0`, same
  site, strictly earlier, gap capped -- and yields both the "high" stratum
  (score >= `thread_match.MIN_SCORE`, what the matcher itself would surface)
  and the "mid" stratum (`LOWERED_FLOOR` <= score < `MIN_SCORE`, its near
  misses) in one pass.
- A separate harvest produces the "low" stratum: pairs scoring BELOW
  `LOWERED_FLOOR` on the same `score_pair` but sharing at least one title
  token. This harvest applies `find_candidates`' eligibility rules WHERE THEY
  APPLY (dispatch instruction, fix round 1): `open_items > 0` on the LATER
  topic, and the gap capped at `thread_match.MAX_GAP_DAYS` -- the same two
  gates `find_candidates` itself enforces. It does NOT require `open_items`
  on the EARLIER side, which is the one respect in which it differs from
  `find_candidates` (that asymmetry is what lets a topic with no open work of
  its own still serve as a "why did you say no to this" hard negative).
  Cost is bounded two ways (fix round 1, Important #2): `cap_topics_per_site`
  keeps at most `--max-topics-per-site` (default 200) of the MOST RECENT
  topics per site before any pair is generated or scored, and for each later
  topic, at most `--max-pairs-per-topic` (default 15) eligible earlier
  topics are chosen (via the seeded RNG, so this stays deterministic) before
  the REAL `thread_match.score_pair` is called on them -- `score_pair` itself
  rebuilds its IDF map over the whole per-site corpus on every call, so
  scoring every eligible pair unconditionally is cubic in topics-per-site;
  capping the number of `score_pair` CALLS per later topic keeps the total
  work quadratic in the (now also capped) topic count instead. Per-site
  topic counts and the number of `score_pair` calls made are printed by the
  CLI (`sample_threads`).

Excludes any (topic_id, parent_topic_id) pair already present in
`topic_thread_suggestions`, in ANY status (a rejected pair re-appearing in a
fresh batch teaches the owner nothing new). `thread_id`-based suggestion rows
are not matched against here -- like `export_labels.py` states, prod holds
zero rows in `topic_threads` (2026-09-01), so this branch is inert on real
data; the exclusion set only needs the direct parent_topic_id shape.

## work_class

Topics with `work_class` NOT NULL, `visible_topics_predicate` applied,
excluding any topic already in `classification_feedback` (any row). Stratified
~50/50 by `work_class` ('work'/'non_work'), oversampling `work_confidence < 0.8`
within each half (low-confidence rows are the ones worth a second human look).

## Output

`scripts/fixtures/jev_eval/batch/{set}.batch.jsonl` -- already covered by the
existing `scripts/fixtures/jev_eval/.gitignore` (`*` with only `!counts.json`
kept), confirmed with `git check-ignore -v --no-index`. One row per sampled
item, in the Task 1 shape (`set`, `id`, `features`, `site_id`, `company_id`,
`baseline`) plus `label: null`, `label_source: "owner"`, and `display` -- the
plain fields `label_page.py` shows the owner (never `features`/`baseline`
directly, so the page and the eventual `build_state` input can diverge in
shape without one leaking into the other). Rows also carry an internal
`stratum` field for this script's own summary printing and for the unit
tests; `label_page.py` never renders it.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import random
import sys
from datetime import date
from pathlib import Path

import thread_match
from deleted_predicates import visible_topics_predicate

from scripts.jev_eval import export_labels as ex

BATCH_DIR = ex.FIXTURES_DIR / "batch"

DEFAULT_SIZE = 100
DEFAULT_WINDOW_DAYS = 120
DEFAULT_SEED = 0

# Bounds on the threads pair-generation work (fix round 1, Important #2):
# `score_pair` rebuilds its IDF map over the whole per-site corpus on every
# call, so scoring every eligible pair in the low-stratum harvest
# unconditionally is O(topics-per-site^3). These two caps keep it bounded
# regardless of how many topics a site accumulates in the window.
DEFAULT_MAX_TOPICS_PER_SITE = 200
DEFAULT_MAX_PAIRS_PER_TOPIC = 15

# Below thread_match.MIN_SCORE, above this: the "mid" (near-miss) stratum.
# Well below thread_match's own floor so find_candidates' eligibility rules
# (open items, gap cap) still apply to everything this floor admits.
LOWERED_FLOOR = 0.10

STRATA = ("high", "mid", "low")

SETS = ("threads", "work_class")


# ---------------------------------------------------------------------------
# SQL, one function per query -- pure, no I/O. Every string starts with
# SELECT or WITH (asserted by tests/unit/test_jev_eval_label_batch.py).
# ---------------------------------------------------------------------------

THREADS_TOPIC_COLUMNS = (
    "id", "report_date", "site_id", "company_id", "title", "summary", "open_items",
)


def sql_threads_topics(window_days: int) -> str:
    """Same shape as `repositories.threads.candidate_corpus` (title / summary /
    open_items / report_date / id), but across ALL sites within the window --
    grouping into per-site corpora happens in Python (`group_by_site`) so this
    stays one query rather than one per site. `window_days` is an int this
    script's own CLI/default supplies, never user text -- interpolated the
    same way `export_labels.sql_name_aliases` interpolates its trusted
    company_ids.

    Visibility uses `deleted_predicates.visible_topics_predicate("t")`
    (imported, not copied) -- the same predicate `candidate_corpus` applies,
    ANDed into `WHERE` exactly where that function puts it."""
    visible_t = visible_topics_predicate("t")
    window_days = int(window_days)
    return (
        "SELECT t.id, t.report_date, t.site_id, si.company_id, t.title, t.summary, "
        "       count(a.id) FILTER (WHERE a.status='open') AS open_items "
        "FROM topics t "
        "JOIN sites si ON si.id = t.site_id "
        "LEFT JOIN action_items a ON a.topic_id = t.id "
        f"WHERE t.report_date >= (CURRENT_DATE - {window_days}::int) "
        f"AND {visible_t} "
        "GROUP BY t.id, si.company_id"
    )


THREADS_EXISTING_COLUMNS = ("topic_id", "parent_topic_id")


def sql_threads_existing_pairs() -> str:
    """Every (topic_id, parent_topic_id) pair already proposed, in ANY status
    -- a rejected pair reappearing in a fresh batch teaches the owner
    nothing new. `thread_id`-based rows are not selected here: prod holds
    zero rows in `topic_threads` (see `export_labels`'s module docstring),
    so that branch is inert on real data and this script only needs the
    direct parent_topic_id shape."""
    return (
        "SELECT topic_id, parent_topic_id FROM topic_thread_suggestions "
        "WHERE parent_topic_id IS NOT NULL"
    )


WORK_CLASS_TOPIC_COLUMNS = (
    "id", "title", "summary", "category", "work_class", "work_confidence",
    "site_id", "company_id",
)


def sql_work_class_topics() -> str:
    """Topics with a classifier verdict, visibility predicate applied on `t`."""
    visible_t = visible_topics_predicate("t")
    return (
        "SELECT t.id, t.title, t.summary, t.category, t.work_class, t.work_confidence, "
        "       t.site_id, si.company_id "
        "FROM topics t "
        "JOIN sites si ON si.id = t.site_id "
        f"WHERE t.work_class IS NOT NULL AND {visible_t}"
    )


def sql_work_class_existing() -> str:
    """Every topic_id already fed back on -- no FK on this column
    (`classification_feedback.topic_id`, see export_labels's docstring), so
    this is a plain SELECT, not a join."""
    return "SELECT DISTINCT topic_id FROM classification_feedback"


# ---------------------------------------------------------------------------
# threads: pure pair generation + stratification.
# ---------------------------------------------------------------------------

def group_by_site(topic_rows: list) -> dict:
    out: dict = {}
    for row in topic_rows:
        out.setdefault(row["site_id"], []).append(row)
    return out


def _as_date(value):
    if isinstance(value, date):
        return value
    try:
        return date.fromisoformat(str(value)[:10])
    except (TypeError, ValueError):
        return None


def _shares_title_token(a: dict, b: dict) -> bool:
    return bool(thread_match._tokens(a.get("title")) & thread_match._tokens(b.get("title")))


def cap_topics_per_site(topics_by_site: dict, max_topics_per_site: int) -> dict:
    """Keep at most `max_topics_per_site` topics per site -- the MOST RECENT
    ones by `report_date` (ties broken by id, descending, for a stable,
    order-independent cap). Bounds every downstream O(n^2)/O(n^3) loop in
    `generate_thread_pairs` regardless of how many topics a site accumulates
    inside the window (fix round 1, Important #2)."""
    out = {}
    for site_id, site_topics in topics_by_site.items():
        ordered = sorted(
            site_topics,
            key=lambda t: (_as_date(t.get("report_date")) or date.min, str(t["id"])),
            reverse=True,
        )
        out[site_id] = ordered[:max_topics_per_site]
    return out


def generate_thread_pairs(topics_by_site: dict, *,
                           max_pairs_per_topic: int = DEFAULT_MAX_PAIRS_PER_TOPIC,
                           seed: int = DEFAULT_SEED) -> tuple:
    """`(pairs, diagnostics)`. `pairs` is one dict per (later, earlier)
    candidate: `{"later", "earlier", "score", "gap_days"}`.

    High/mid strata come from the REAL `thread_match.find_candidates` with a
    lowered floor (its own eligibility rules apply unmodified). The low
    stratum is harvested separately with the REAL `thread_match.score_pair`,
    applying `find_candidates`' eligibility WHERE IT APPLIES (dispatch
    instruction, fix round 1): `open_items > 0` on the LATER topic, and the
    gap capped at `thread_match.MAX_GAP_DAYS` -- but NOT `open_items` on the
    earlier side, so a topic with no open work of its own can still surface
    as a hard negative.

    Bounded (fix round 1, Important #2): for each later topic, at most
    `max_pairs_per_topic` of its eligible earlier topics are scored, chosen
    with `random.Random(seed)` from the eligible set sorted by id first (so
    the choice is deterministic and independent of database row order) --
    `score_pair` rebuilds its IDF map over the whole per-site corpus on every
    call, so scoring every eligible pair unconditionally would be cubic in
    topics-per-site.

    `diagnostics` is `{site_id: {"n_topics", "low_pairs_scored"}}` -- the
    per-site topic count actually used and the number of REAL `score_pair`
    calls made, for the CLI to print and for tests to assert the caps hold."""
    pairs = []
    diagnostics = {}
    rng = random.Random(seed)

    for site_id, site_topics in topics_by_site.items():
        site_pairs = []
        for later in site_topics:
            for cand in thread_match.find_candidates(
                later, site_topics, min_score=LOWERED_FLOOR,
                max_gap_days=thread_match.MAX_GAP_DAYS,
            ):
                site_pairs.append({
                    "later": later, "earlier": cand,
                    "score": cand["match_score"], "gap_days": cand["gap_days"],
                })

        seen_high_mid = {
            (p["later"]["id"], p["earlier"]["id"])
            for p in site_pairs
        }

        low_pairs_scored = 0
        later_sorted = sorted(site_topics, key=lambda t: str(t["id"]))
        for later in later_sorted:
            later_date = _as_date(later.get("report_date"))
            if later_date is None:
                continue
            if not (later.get("open_items") or 0):
                continue  # eligibility: open_items on the LATER topic

            eligible = []
            for earlier in site_topics:
                if earlier is later or earlier.get("id") == later.get("id"):
                    continue
                if (later["id"], earlier["id"]) in seen_high_mid:
                    continue
                earlier_date = _as_date(earlier.get("report_date"))
                if earlier_date is None or earlier_date >= later_date:
                    continue
                gap = (later_date - earlier_date).days
                if gap > thread_match.MAX_GAP_DAYS:
                    continue  # eligibility: same gap cap find_candidates uses
                eligible.append((earlier, gap))

            eligible.sort(key=lambda pair: str(pair[0]["id"]))  # stable before sampling
            if len(eligible) > max_pairs_per_topic:
                chosen = rng.sample(eligible, max_pairs_per_topic)
            else:
                chosen = eligible

            for earlier, gap in chosen:
                score = thread_match.score_pair(later, earlier, site_topics)
                low_pairs_scored += 1
                if score >= LOWERED_FLOOR:
                    continue
                if not _shares_title_token(later, earlier):
                    continue
                site_pairs.append({
                    "later": later, "earlier": earlier, "score": score,
                    "gap_days": gap,
                })

        diagnostics[site_id] = {
            "n_topics": len(site_topics),
            "low_pairs_scored": low_pairs_scored,
        }
        pairs.extend(site_pairs)
    return pairs, diagnostics


def thread_pair_id(later_id, earlier_id) -> str:
    """Stable hash of the two topic ids -- deterministic across runs and
    independent of database row order."""
    digest = hashlib.sha256(f"{later_id}:{earlier_id}".encode("utf-8")).hexdigest()
    return f"threads:{digest[:24]}"


def apply_thread_exclusions(pairs: list, existing_pairs: set) -> list:
    """Drop any pair already present in `topic_thread_suggestions`, in ANY
    status. `existing_pairs` is a set of (topic_id, parent_topic_id) tuples."""
    return [
        p for p in pairs
        if (p["later"]["id"], p["earlier"]["id"]) not in existing_pairs
    ]


def _stratum_for_score(score: float) -> str:
    if score >= thread_match.MIN_SCORE:
        return "high"
    if score >= LOWERED_FLOOR:
        return "mid"
    return "low"


def stratify_thread_pairs(pairs: list, size: int, seed: int) -> tuple:
    """~40% high / ~40% mid / ~20% low, deterministic given `seed`.

    Sorted by a stable key (the pair id) BEFORE shuffling, so the outcome
    depends only on the pool's contents and the seed -- never on the order
    the database happened to return rows in."""
    by_stratum: dict = {s: [] for s in STRATA}
    for p in pairs:
        by_stratum[_stratum_for_score(p["score"])].append(p)

    rng = random.Random(seed)
    for stratum in STRATA:
        by_stratum[stratum].sort(
            key=lambda p: thread_pair_id(p["later"]["id"], p["earlier"]["id"]))
        rng.shuffle(by_stratum[stratum])

    quotas = {
        "high": round(size * 0.4),
        "mid": round(size * 0.4),
    }
    quotas["low"] = max(size - quotas["high"] - quotas["mid"], 0)

    chosen = []
    counts = {}
    for stratum in STRATA:
        take = by_stratum[stratum][: quotas[stratum]]
        chosen.extend((p, stratum) for p in take)
        counts[stratum] = {"quota": quotas[stratum], "available": len(by_stratum[stratum]),
                            "sampled": len(take)}
    return chosen, counts


def build_thread_batch_row(pair: dict, stratum: str) -> dict:
    later, earlier = pair["later"], pair["earlier"]
    features = {
        "earlier": {
            "title": earlier.get("title"),
            "summary": earlier.get("summary"),
            "date": str(earlier.get("report_date")) if earlier.get("report_date") else None,
        },
        "later": {
            "title": later.get("title"),
            "summary": later.get("summary"),
            "date": str(later.get("report_date")) if later.get("report_date") else None,
        },
        "gap_days": pair["gap_days"],
    }
    display = {
        "earlier_title": earlier.get("title"),
        "earlier_summary": earlier.get("summary"),
        "earlier_date": str(earlier.get("report_date")) if earlier.get("report_date") else None,
        "later_title": later.get("title"),
        "later_summary": later.get("summary"),
        "later_date": str(later.get("report_date")) if later.get("report_date") else None,
        "gap_days": pair["gap_days"],
    }
    return {
        "set": "threads",
        "id": thread_pair_id(later["id"], earlier["id"]),
        "label": None,
        "label_source": "owner",
        "features": features,
        "display": display,
        "site_id": later.get("site_id"),
        "company_id": later.get("company_id"),
        "baseline": {"score": pair["score"]},
        "stratum": stratum,
    }


# ---------------------------------------------------------------------------
# work_class: pure stratification.
# ---------------------------------------------------------------------------

def apply_work_class_exclusions(topics: list, already_fed_back: set) -> list:
    return [t for t in topics if t["id"] not in already_fed_back]


def _low_confidence(topic: dict) -> bool:
    confidence = topic.get("work_confidence")
    return confidence is not None and confidence < 0.8


def stratify_work_class(topics: list, size: int, seed: int) -> tuple:
    """~50/50 by work_class, oversampling work_confidence < 0.8 within each
    half. Deterministic given `seed` (sorted by id before shuffling, same
    rule as `stratify_thread_pairs`)."""
    rng = random.Random(seed)

    def _ordered(group: list) -> list:
        low = sorted((t for t in group if _low_confidence(t)), key=lambda t: t["id"])
        high = sorted((t for t in group if not _low_confidence(t)), key=lambda t: t["id"])
        rng.shuffle(low)
        rng.shuffle(high)
        return low + high

    work = _ordered([t for t in topics if t.get("work_class") == "work"])
    non_work = _ordered([t for t in topics if t.get("work_class") == "non_work"])

    n_work = size // 2
    n_non_work = size - n_work

    chosen_work = work[:n_work]
    chosen_non_work = non_work[:n_non_work]

    counts = {
        "work": {"quota": n_work, "available": len(work), "sampled": len(chosen_work)},
        "non_work": {"quota": n_non_work, "available": len(non_work),
                     "sampled": len(chosen_non_work)},
    }
    chosen = [(t, "work") for t in chosen_work] + [(t, "non_work") for t in chosen_non_work]
    return chosen, counts


def build_work_class_batch_row(topic: dict, stratum: str) -> dict:
    features = {
        "title": topic.get("title"),
        "summary": topic.get("summary"),
        "category": topic.get("category"),
    }
    display = {
        "title": topic.get("title"),
        "summary": topic.get("summary"),
        "category": topic.get("category"),
    }
    return {
        "set": "work_class",
        "id": str(topic["id"]),
        "label": None,
        "label_source": "owner",
        "features": features,
        "display": display,
        "site_id": topic.get("site_id"),
        "company_id": topic.get("company_id"),
        "baseline": {
            "classifier_verdict": topic.get("work_class"),
            "classifier_confidence": topic.get("work_confidence"),
        },
        "stratum": stratum,
    }


# ---------------------------------------------------------------------------
# I/O -- the RDS Data API runner, reusing export_labels' begin/execute/rollback.
# ---------------------------------------------------------------------------

def _write_batch(set_name: str, rows: list) -> Path:
    BATCH_DIR.mkdir(parents=True, exist_ok=True)
    path = BATCH_DIR / f"{set_name}.batch.jsonl"
    with open(path, "w", encoding="utf-8") as fh:
        for row in rows:
            fh.write(json.dumps(row, sort_keys=True))
            fh.write("\n")
    return path


def sample_threads(database: str, *, size: int = DEFAULT_SIZE,
                    window_days: int = DEFAULT_WINDOW_DAYS, seed: int = DEFAULT_SEED,
                    max_topics_per_site: int = DEFAULT_MAX_TOPICS_PER_SITE,
                    max_pairs_per_topic: int = DEFAULT_MAX_PAIRS_PER_TOPIC,
                    profile: str = ex.DEFAULT_PROFILE, region: str = ex.DEFAULT_REGION) -> dict:
    tx = ex._begin_transaction(database, profile, region)
    try:
        topics_result = ex._execute(database, tx, sql_threads_topics(window_days), profile, region)
        topic_rows = [
            ex.record_to_dict(THREADS_TOPIC_COLUMNS, r)
            for r in topics_result.get("records", [])
        ]
        existing_result = ex._execute(database, tx, sql_threads_existing_pairs(), profile, region)
        existing_pairs = {
            (ex.record_to_dict(THREADS_EXISTING_COLUMNS, r)["topic_id"],
             ex.record_to_dict(THREADS_EXISTING_COLUMNS, r)["parent_topic_id"])
            for r in existing_result.get("records", [])
        }
    finally:
        ex._rollback(tx, profile, region)

    topics_by_site = group_by_site(topic_rows)
    topics_by_site = cap_topics_per_site(topics_by_site, max_topics_per_site)
    pairs, diagnostics = generate_thread_pairs(
        topics_by_site, max_pairs_per_topic=max_pairs_per_topic, seed=seed)
    pairs = apply_thread_exclusions(pairs, existing_pairs)
    chosen, counts = stratify_thread_pairs(pairs, size, seed)
    rows = [build_thread_batch_row(pair, stratum) for pair, stratum in chosen]
    path = _write_batch("threads", rows)

    for site_id, site_diag in sorted(diagnostics.items(), key=str):
        print(f"site {site_id}: {site_diag['n_topics']} topics "
              f"(capped at {max_topics_per_site}), "
              f"{site_diag['low_pairs_scored']} low-stratum pairs scored "
              f"(capped at {max_pairs_per_topic} per later topic)")

    return {"set": "threads", "n": len(rows), "strata": counts, "path": str(path),
            "database": database, "window_days": window_days, "seed": seed,
            "max_topics_per_site": max_topics_per_site,
            "max_pairs_per_topic": max_pairs_per_topic,
            "per_site_diagnostics": diagnostics}


def sample_work_class(database: str, *, size: int = DEFAULT_SIZE, seed: int = DEFAULT_SEED,
                       profile: str = ex.DEFAULT_PROFILE, region: str = ex.DEFAULT_REGION) -> dict:
    tx = ex._begin_transaction(database, profile, region)
    try:
        topics_result = ex._execute(database, tx, sql_work_class_topics(), profile, region)
        topic_rows = [
            ex.record_to_dict(WORK_CLASS_TOPIC_COLUMNS, r)
            for r in topics_result.get("records", [])
        ]
        existing_result = ex._execute(database, tx, sql_work_class_existing(), profile, region)
        already_fed_back = {
            ex.decode_field(r[0]) for r in existing_result.get("records", [])
        }
    finally:
        ex._rollback(tx, profile, region)

    topics = apply_work_class_exclusions(topic_rows, already_fed_back)
    chosen, counts = stratify_work_class(topics, size, seed)
    rows = [build_work_class_batch_row(topic, stratum) for topic, stratum in chosen]
    path = _write_batch("work_class", rows)
    return {"set": "work_class", "n": len(rows), "strata": counts, "path": str(path),
            "database": database, "seed": seed}


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--set", dest="set_name", default="all",
                         choices=SETS + ("all",))
    parser.add_argument("--size", type=int, default=DEFAULT_SIZE)
    parser.add_argument("--window-days", type=int, default=DEFAULT_WINDOW_DAYS)
    parser.add_argument("--seed", type=int, default=DEFAULT_SEED)
    parser.add_argument("--max-topics-per-site", type=int, default=DEFAULT_MAX_TOPICS_PER_SITE,
                         help="threads only: cap topics per site to the most recent N "
                              "before generating pairs")
    parser.add_argument("--max-pairs-per-topic", type=int, default=DEFAULT_MAX_PAIRS_PER_TOPIC,
                         help="threads only: cap eligible earlier topics scored per later "
                              "topic in the low-stratum harvest")
    parser.add_argument("--database", default=ex.DEFAULT_DATABASE)
    parser.add_argument("--allow-prod", action="store_true",
                         help="required to target --database fieldsight")
    parser.add_argument("--profile", default=ex.DEFAULT_PROFILE)
    parser.add_argument("--region", default=ex.DEFAULT_REGION)
    args = parser.parse_args(argv)

    if args.database == ex.PROD_DATABASE and not args.allow_prod:
        print(
            f"refusing --database {ex.PROD_DATABASE!r} without --allow-prod "
            "(this is prod)",
            file=sys.stderr,
        )
        return 2

    sets = list(SETS) if args.set_name == "all" else [args.set_name]
    summary = {}
    for set_name in sets:
        if set_name == "threads":
            summary["threads"] = sample_threads(
                args.database, size=args.size, window_days=args.window_days,
                seed=args.seed, max_topics_per_site=args.max_topics_per_site,
                max_pairs_per_topic=args.max_pairs_per_topic,
                profile=args.profile, region=args.region)
        else:
            summary["work_class"] = sample_work_class(
                args.database, size=args.size, seed=args.seed,
                profile=args.profile, region=args.region)

    print(json.dumps(summary, indent=2, sort_keys=True, default=str))
    return 0


if __name__ == "__main__":
    sys.exit(main())
