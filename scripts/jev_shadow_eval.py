"""Runner for the Jev shadow evaluation (Track A, Task 6).

Sends the human-labelled decisions Task 1 exported (`scripts/fixtures/jev_eval/
{programme_match,threads,work_class}.jsonl`) through the third-party Jev
("System One") Decisions endpoint and through today's own gate ("baseline"),
and writes one result row per `(id, arm, run)` for Task 7's `score.py` to
read.

This script NEVER touches Aurora or S3, and NEVER reads a database -- it only
reads the fixture files Task 1 already wrote. `--database` exists solely so
the value that produced those fixtures is recorded in `results/summary.json`;
it is never used to open a connection.

## Owner condition

Data may leave the country only as structured, name-masked event JSON --
never transcript text (`scripts/jev_eval/state.py` is the enforcement point;
this runner never bypasses it, and never sends `row["features"]` directly to
`systemone_client.ask`). `--dry-run` is the owner's own spot-check of exactly
what would leave: it builds every state (including every control state) for
the selected rows and writes them, verbatim, to
`scripts/fixtures/jev_eval/results/preview_states.jsonl` (gitignored, like
the rest of `results/`) without making any network or `aws` call and without
needing `DECISIONS_API_KEY`.

## Arms

    baseline              -- today's gate, via scripts/jev_eval/baseline.py
    broad                 -- Jev, single question, on the real masked state
    decomposed            -- Jev, multi-question composite, same state
    control_broad         -- Jev, broad question, on a donor-substituted state
    control_decomposed    -- Jev, decomposed questions, same donor-substituted state

`--arms` defaults to all five. One `ask()` call is made per `(row, arm, run)`
-- never shared across arms, even though `control_broad`/`control_decomposed`
share the same underlying control state (ruling: the control state is
derived once from the row's real state, then each Jev arm asks its own
question set against it).

## Result row contract (read by scripts/jev_eval/score.py)

    {"id", "label", "arm", "run", "score", "answers", "question_hash",
     "latency_ms", "tokens", "error"}

plus stamps every row carries so a results file self-documents what produced
it: `set`, `route` (the Decisions endpoint's hostname; None for baseline,
which never makes an HTTP call of its own), `model`, `provider`,
`temperature` (always None for a Jev row -- Jev has no exposed temperature
knob; real for baseline, read from `scripts/jev_eval/baseline.baseline_config()`),
`started_at`.

A failed call writes a row with `error` set (the exception's class name plus
a short message -- NEVER the state or questions, which may carry
customer/site text even after masking) and `score: None` -- never `0.0`,
which is a real, valid answer for several of these questions.

## Idempotency

Output files are `results/{set}.{arm}.run{n}.jsonl`, append-only, flushed
after every row. Re-running with the same arguments skips any `(id, arm,
run)` already present in its file WITHOUT an `error` (already-succeeded);
rows present WITH an `error` are retried and a new line is appended (the old
error line is left in place -- this file is a log, not a keyed table). This
means a run interrupted mid-flight, or one that hit a 429 storm, can simply
be re-invoked with the same arguments.

## Refusals (before any work, and before any network/aws call)

- `scripts/fixtures/jev_eval/counts.json` is missing, or has no entry for a
  selected `--set` (export it first: `scripts/jev_eval/export_labels.py`).
- A selected set's `.jsonl` fixture file is missing.
- A Jev arm (`broad`/`decomposed`/`control_broad`/`control_decomposed`) is
  selected, `--dry-run` is NOT given, and `DECISIONS_API_KEY` is not set.

## Concurrency and retries

`(row, arm, run)` tasks run in a `ThreadPoolExecutor` at concurrency 4.
Output file handles are per `(set, arm, run)` and each is guarded by its own
lock, so concurrent writers never interleave lines within one file.
`systemone_client.ask` already retries one 429/5xx internally; if that comes
back out as a `SystemOneError` whose message still names a 429, this runner
backs off (an injectable sleeper, real `time.sleep` by default) and retries
up to `EXTRA_RETRIES_ON_429` more times before giving up and writing an
error row. A non-429 failure is written as an error row immediately -- no
runner-level retry.
"""
from __future__ import annotations

import argparse
import json
import os
import statistics
import sys
import threading
import time
import urllib.parse
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from pathlib import Path

import systemone_client as sc

from scripts.jev_eval import baseline as baseline_mod
from scripts.jev_eval.questions import (
    QUESTION_SETS,
    JevQuestionsError,
    question_hash,
)
from scripts.jev_eval.state import JevStateError, build_state

FIXTURES_DIR = Path(__file__).resolve().parent / "fixtures" / "jev_eval"
RESULTS_DIR = FIXTURES_DIR / "results"

SETS = ("programme_match", "threads", "work_class")
JEV_ARMS = ("broad", "decomposed", "control_broad", "control_decomposed")
ALL_ARMS = ("baseline",) + JEV_ARMS

DEFAULT_RUNS = 2
CONCURRENCY = 4
EXTRA_RETRIES_ON_429 = 3
BACKOFF_SECONDS = 2.0

# "list price from public docs, 2026-09" -- a constant, not a live lookup.
# Applied to the row's single `tokens` figure (prompt + completion combined,
# since the wire contract carries only one number); for this workload
# (short noul/choice answers) that is a conservative over-estimate of the
# input-only cost the brief asks for, never an under-estimate.
COST_PER_MILLION_INPUT_TOKENS = 0.042

DEFAULT_BASELINE_FUNCTION = baseline_mod.PROGRAMME_MATCHER_FUNCTION
DEFAULT_DATABASE = "fieldsight_test"


class RunnerRefusal(RuntimeError):
    """Raised for every precondition this runner checks before doing work."""


# ---------------------------------------------------------------------------
# Fixture loading -- pure I/O, no database, ever.
# ---------------------------------------------------------------------------

def load_counts() -> dict:
    path = FIXTURES_DIR / "counts.json"
    if not path.exists():
        raise RunnerRefusal(
            f"counts.json not found at {path}; run "
            "scripts/jev_eval/export_labels.py first")
    return json.loads(path.read_text(encoding="utf-8"))


def load_rows(set_name: str) -> list:
    path = FIXTURES_DIR / f"{set_name}.jsonl"
    if not path.exists():
        raise RunnerRefusal(
            f"missing fixture file: {path}; run "
            "scripts/jev_eval/export_labels.py first")
    rows = []
    for line in path.read_text(encoding="utf-8").splitlines():
        if line.strip():
            rows.append(json.loads(line))
    return rows


def load_aliases() -> list:
    path = FIXTURES_DIR / "name_aliases.json"
    if not path.exists():
        return []
    return json.loads(path.read_text(encoding="utf-8"))


def check_preconditions(sets: list, arms: list, dry_run: bool) -> dict:
    """Refuse before any work (and before any network/aws call). Returns the
    parsed `counts.json` on success."""
    counts = load_counts()
    for set_name in sets:
        if set_name not in counts:
            raise RunnerRefusal(
                f"counts.json has no entry for set {set_name!r}; export it first")
        path = FIXTURES_DIR / f"{set_name}.jsonl"
        if not path.exists():
            raise RunnerRefusal(f"missing fixture file: {path}")

    jev_selected = any(arm in JEV_ARMS for arm in arms)
    if jev_selected and not dry_run and not os.environ.get("DECISIONS_API_KEY"):
        raise RunnerRefusal(
            "DECISIONS_API_KEY is not set; required for the Jev arms "
            f"({', '.join(JEV_ARMS)}). Set it, drop those arms from --arms, "
            "or pass --dry-run.")
    return counts


# ---------------------------------------------------------------------------
# State + donor building -- pure except for the state-builder's own guards.
# ---------------------------------------------------------------------------

def _build_states(set_name: str, rows: list, aliases: list) -> dict:
    """`id -> {"state", "site_id", "company_id"}`. Every row's masked state
    is built exactly once here, shared by the broad/decomposed arms and used
    as the input to `control()` for the control arms (ruling #5). Lets
    `JevStateError` propagate -- a transcript-like key surviving into a
    fixture is a privacy bug in the export, not a per-row runner failure to
    swallow and continue past."""
    out = {}
    for row in rows:
        state = build_state(set_name, row.get("features") or {}, aliases)
        out[row["id"]] = {
            "state": state,
            "site_id": row.get("site_id"),
            "company_id": row.get("company_id"),
        }
    return out


def _donor_states(states_by_id: dict, row_id) -> list:
    """Donor states for `row_id`'s control arm (ruling #4): built states of
    rows from a DIFFERENT `site_id`, preferring a different `company_id`
    when any exist. A `None` site_id on either side is never treated as
    "different" -- an unknown site is not evidence of a different one."""
    me = states_by_id[row_id]
    others = [
        v for rid, v in states_by_id.items()
        if rid != row_id
        and me["site_id"] is not None
        and v["site_id"] is not None
        and v["site_id"] != me["site_id"]
    ]
    if not others:
        return []
    diff_company = [v for v in others if v["company_id"] != me["company_id"]]
    pool = diff_company if diff_company else others
    return [v["state"] for v in pool]


def _build_control_states(set_name: str, rows: list, states_by_id: dict) -> tuple:
    """One control state per row (shared by control_broad/control_decomposed,
    ruling #5). Returns (control_states, control_errors) -- a row whose
    `control()` call raises `JevQuestionsError` (no usable donor) gets an
    entry in `control_errors` instead, and both control arms for that row
    become error rows rather than crashing the batch (ruling #4)."""
    control_fn = QUESTION_SETS[set_name]["control"]
    control_states: dict = {}
    control_errors: dict = {}
    for row in rows:
        row_id = row["id"]
        try:
            control_states[row_id] = control_fn(
                states_by_id[row_id]["state"],
                _donor_states(states_by_id, row_id),
                row_id,
            )
        except JevQuestionsError as exc:
            control_errors[row_id] = str(exc)
    return control_states, control_errors


# ---------------------------------------------------------------------------
# Small helpers
# ---------------------------------------------------------------------------

def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _format_error(exc: Exception) -> str:
    """Exception class + a short message -- never the state or questions."""
    return f"{type(exc).__name__}: {str(exc)[:200]}"


def _total_tokens(usage: dict):
    """Read prompt/completion tokens defensively (both the OpenRouter and
    TypeSafe-direct field-name conventions, same as `systemone_client`
    itself), summed into the single `tokens` figure the row contract
    carries. `None` if neither convention yielded a number."""
    prompt = usage.get("prompt_tokens", usage.get("input_tokens"))
    completion = usage.get("completion_tokens", usage.get("output_tokens"))
    values = [v for v in (prompt, completion) if isinstance(v, (int, float))]
    if not values:
        return None
    return int(sum(values))


def _ask_with_retry(state, questions: dict, *, caller: str, sleeper=time.sleep,
                     model=None, timeout=None):
    """`systemone_client.ask` already retries one 429/5xx internally. If a
    SECOND 429 still surfaces as a `SystemOneError`, back off and retry up
    to `EXTRA_RETRIES_ON_429` more times before letting it propagate to the
    caller as a row error."""
    attempt = 0
    while True:
        try:
            return sc.ask(state, questions, model=model, timeout=timeout, caller=caller)
        except sc.SystemOneError as exc:
            if "429" in str(exc) and attempt < EXTRA_RETRIES_ON_429:
                attempt += 1
                sleeper(BACKOFF_SECONDS * attempt)
                continue
            raise


# ---------------------------------------------------------------------------
# Result writer -- append-only, per-(set,arm,run) file, one lock per file.
# ---------------------------------------------------------------------------

class ResultWriter:
    def __init__(self, results_dir: Path):
        self._dir = results_dir
        self._dir.mkdir(parents=True, exist_ok=True)
        self._files: dict = {}
        self._locks: dict = {}
        self._setup_lock = threading.Lock()

    def _handle(self, set_name: str, arm: str, run: int):
        key = (set_name, arm, run)
        with self._setup_lock:
            if key not in self._files:
                path = self._dir / f"{set_name}.{arm}.run{run}.jsonl"
                self._files[key] = open(path, "a", encoding="utf-8")
                self._locks[key] = threading.Lock()
            return self._files[key], self._locks[key]

    def write(self, set_name: str, arm: str, run: int, row: dict) -> None:
        fh, lock = self._handle(set_name, arm, run)
        with lock:
            fh.write(json.dumps(row, sort_keys=True))
            fh.write("\n")
            fh.flush()

    def close(self) -> None:
        for fh in self._files.values():
            fh.close()


def _load_existing_ok_ids(path: Path) -> set:
    if not path.exists():
        return set()
    ok_ids = set()
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        row = json.loads(line)
        if row.get("error") is None:
            ok_ids.add(row.get("id"))
    return ok_ids


# ---------------------------------------------------------------------------
# Per-row-arm-run processing
# ---------------------------------------------------------------------------

def _process_baseline(set_name: str, row: dict, run: int, started_at: str) -> dict:
    t0 = time.time()
    try:
        out = dict(baseline_mod.baseline_row(set_name, row, run))
    except Exception as exc:  # noqa: BLE001 - never crash the batch on one row
        elapsed_ms = int((time.time() - t0) * 1000)
        out = {
            "id": row.get("id"), "label": row.get("label"), "arm": "baseline",
            "run": run, "score": None, "answers": None, "question_hash": None,
            "latency_ms": elapsed_ms, "tokens": None, "error": _format_error(exc),
        }

    if set_name == "programme_match":
        cfg = baseline_mod.baseline_config()
    else:
        # threads/work_class: no LLM call, stored gate output only.
        cfg = {"provider": "stored-gate", "model": "stored", "temperature": None}

    out["set"] = set_name
    out["route"] = None  # baseline never makes an HTTP call of its own
    out["model"] = cfg["model"]
    out["provider"] = cfg["provider"]
    out["temperature"] = cfg["temperature"]
    out["started_at"] = started_at
    return out


def _process_jev(set_name: str, row: dict, arm: str, run: int, state_bundle: dict,
                  sleeper, jev_route, jev_provider, jev_model, started_at: str) -> dict:
    t0 = time.time()
    qset_name = "broad" if arm in ("broad", "control_broad") else "decomposed"
    try:
        if arm in ("broad", "decomposed"):
            state = state_bundle["states"][row["id"]]["state"]
        else:
            if row["id"] in state_bundle["control_errors"]:
                raise JevQuestionsError(state_bundle["control_errors"][row["id"]])
            state = state_bundle["control_states"][row["id"]]

        questions = QUESTION_SETS[set_name][qset_name]
        caller = f"jev_eval_{set_name}_{arm}"
        result = _ask_with_retry(state, questions, caller=caller, sleeper=sleeper)
        answers = result["answers"]
        score_fn = QUESTION_SETS[set_name][
            "broad_score" if qset_name == "broad" else "composite"]
        score = score_fn(answers)
        out = {
            "id": row["id"], "label": row["label"], "arm": arm, "run": run,
            "score": score, "answers": answers,
            "question_hash": question_hash(set_name, qset_name),
            "latency_ms": result["latency_ms"],
            "tokens": _total_tokens(result.get("usage") or {}),
            "error": None,
        }
    except Exception as exc:  # noqa: BLE001 - never crash the batch on one row
        elapsed_ms = int((time.time() - t0) * 1000)
        try:
            qh = question_hash(set_name, qset_name)
        except Exception:  # noqa: BLE001
            qh = None
        out = {
            "id": row.get("id"), "label": row.get("label"), "arm": arm, "run": run,
            "score": None, "answers": None, "question_hash": qh,
            "latency_ms": elapsed_ms, "tokens": None, "error": _format_error(exc),
        }

    out["set"] = set_name
    out["route"] = jev_route
    out["provider"] = jev_provider
    out["model"] = jev_model
    out["temperature"] = None  # Jev has no exposed temperature knob
    out["started_at"] = started_at
    return out


def _process_task(set_name: str, row: dict, arm: str, run: int, state_bundle,
                   sleeper, jev_route, jev_provider, jev_model) -> dict:
    started_at = _now_iso()
    if arm == "baseline":
        return _process_baseline(set_name, row, run, started_at)
    return _process_jev(set_name, row, arm, run, state_bundle, sleeper,
                         jev_route, jev_provider, jev_model, started_at)


# ---------------------------------------------------------------------------
# Dry run -- builds every state that WOULD be sent, sends nothing.
# ---------------------------------------------------------------------------

def _run_dry_run(sets: list, arms: list, args) -> int:
    jev_arms_selected = [arm for arm in arms if arm in JEV_ARMS]
    RESULTS_DIR.mkdir(parents=True, exist_ok=True)
    preview_path = RESULTS_DIR / "preview_states.jsonl"

    n_entries = 0
    max_size = 0
    with open(preview_path, "w", encoding="utf-8") as fh:
        for set_name in sets:
            rows = load_rows(set_name)
            if args.limit:
                rows = rows[: args.limit]
            aliases = load_aliases()
            states_by_id = _build_states(set_name, rows, aliases)

            control_states, control_errors = {}, {}
            if any(arm in ("control_broad", "control_decomposed") for arm in jev_arms_selected):
                control_states, control_errors = _build_control_states(
                    set_name, rows, states_by_id)

            for row in rows:
                for arm in jev_arms_selected:
                    if arm in ("broad", "decomposed"):
                        state = states_by_id[row["id"]]["state"]
                        qset_name = arm
                    else:
                        qset_name = "broad" if arm == "control_broad" else "decomposed"
                        if row["id"] in control_errors:
                            # Nothing would actually be sent for this row/arm
                            # at run time either -- control() has no usable
                            # donor, so this becomes an error row, not a call.
                            continue
                        state = control_states[row["id"]]

                    entry = {
                        "set": set_name, "id": row["id"], "arm": arm,
                        "state": state,
                        "questions": QUESTION_SETS[set_name][qset_name],
                    }
                    line = json.dumps(entry, sort_keys=True)
                    fh.write(line + "\n")
                    n_entries += 1
                    max_size = max(max_size, len(line.encode("utf-8")))

    print(
        f"dry run: {n_entries} state(s) previewed across {len(sets)} set(s); "
        f"max serialised entry size = {max_size} bytes; wrote {preview_path}"
    )
    return 0


# ---------------------------------------------------------------------------
# Live run
# ---------------------------------------------------------------------------

def _resolve_jev_stamps():
    """Mirror `systemone_client.ask`'s own URL/model resolution so the stamps
    on a row describe exactly the route/model that call actually used."""
    decisions_url = os.environ.get("DECISIONS_URL", sc.DEFAULT_OPENROUTER_URL)
    provider = sc._provider_label(decisions_url)
    model = os.environ.get("DECISIONS_MODEL") or sc._default_model(decisions_url)
    route = urllib.parse.urlparse(decisions_url).hostname
    return route, provider, model


def _run_live(sets: list, arms: list, args) -> int:
    writer = ResultWriter(RESULTS_DIR)
    sleeper = time.sleep

    jev_route = jev_provider = jev_model = None
    if any(arm in JEV_ARMS for arm in arms):
        jev_route, jev_provider, jev_model = _resolve_jev_stamps()

    per_set_state = {}
    tasks = []  # (set_name, row, arm, run)

    for set_name in sets:
        rows = load_rows(set_name)
        if args.limit:
            rows = rows[: args.limit]
        aliases = load_aliases()
        states_by_id = _build_states(set_name, rows, aliases)

        control_states, control_errors = {}, {}
        if any(arm in ("control_broad", "control_decomposed") for arm in arms):
            control_states, control_errors = _build_control_states(
                set_name, rows, states_by_id)

        per_set_state[set_name] = {
            "states": states_by_id,
            "control_states": control_states,
            "control_errors": control_errors,
        }

        for arm in arms:
            for run in range(1, args.runs + 1):
                out_path = RESULTS_DIR / f"{set_name}.{arm}.run{run}.jsonl"
                ok_ids = _load_existing_ok_ids(out_path)
                for row in rows:
                    if row["id"] in ok_ids:
                        continue
                    tasks.append((set_name, row, arm, run))

    with ThreadPoolExecutor(max_workers=CONCURRENCY) as executor:
        futures = {
            executor.submit(
                _process_task, set_name, row, arm, run,
                per_set_state[set_name], sleeper, jev_route, jev_provider, jev_model,
            ): (set_name, arm, run)
            for (set_name, row, arm, run) in tasks
        }
        for future in as_completed(futures):
            set_name, arm, run = futures[future]
            out_row = future.result()
            writer.write(set_name, arm, run, out_row)

    writer.close()

    summary = _build_summary(sets, arms, args)
    summary_path = RESULTS_DIR / "summary.json"
    summary_path.write_text(json.dumps(summary, indent=2, sort_keys=True) + "\n",
                             encoding="utf-8")
    print(json.dumps(summary, indent=2, sort_keys=True))
    return 0


def _build_summary(sets: list, arms: list, args) -> dict:
    summary = {
        "generated_at": _now_iso(),
        "database": args.database,
        "database_note": (
            "recorded for reference only -- this runner reads the exported "
            "fixture files under scripts/fixtures/jev_eval/, never a database"
        ),
        "runs": args.runs,
        "cost_note": (
            f"${COST_PER_MILLION_INPUT_TOKENS} per million input tokens, "
            "list price from public docs, 2026-09; applied to each row's "
            "combined (input+output) token count as a conservative estimate"
        ),
        "sets": {},
    }

    for set_name in sets:
        arm_summaries = {}
        for arm in arms:
            all_rows = []
            for run in range(1, args.runs + 1):
                path = RESULTS_DIR / f"{set_name}.{arm}.run{run}.jsonl"
                if not path.exists():
                    continue
                for line in path.read_text(encoding="utf-8").splitlines():
                    if line.strip():
                        all_rows.append(json.loads(line))

            n_ok = sum(1 for r in all_rows if r.get("error") is None)
            n_failed = sum(1 for r in all_rows if r.get("error") is not None)
            latencies = [r["latency_ms"] for r in all_rows if r.get("latency_ms") is not None]
            tokens_list = [r["tokens"] for r in all_rows if r.get("tokens") is not None]
            total_tokens = sum(tokens_list)
            total_latency = sum(latencies)
            median_latency = statistics.median(latencies) if latencies else None
            stamp_row = next((r for r in all_rows if r.get("error") is None),
                              all_rows[0] if all_rows else {})

            arm_summaries[arm] = {
                "n_ok": n_ok,
                "n_failed": n_failed,
                "total_latency_ms": total_latency,
                "median_latency_ms": median_latency,
                "total_tokens": total_tokens,
                "estimated_cost_usd": (
                    (total_tokens / 1_000_000) * COST_PER_MILLION_INPUT_TOKENS
                ),
                "route": stamp_row.get("route"),
                "model": stamp_row.get("model"),
                "provider": stamp_row.get("provider"),
                "temperature": stamp_row.get("temperature"),
            }
        summary["sets"][set_name] = {"arms": arm_summaries}

    return summary


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def _parse_args(argv=None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument(
        "--set", dest="set_name", default="all",
        choices=("programme_match", "threads", "work_class", "all"))
    parser.add_argument(
        "--arms", default=",".join(ALL_ARMS),
        help=f"comma-separated subset of: {', '.join(ALL_ARMS)} (default: all five)")
    parser.add_argument("--runs", type=int, default=DEFAULT_RUNS)
    parser.add_argument("--limit", type=int, default=None,
                         help="cap the number of rows processed per set")
    parser.add_argument(
        "--database", default=DEFAULT_DATABASE,
        help=(
            "Recorded in results/summary.json only, for reference against "
            "which export produced the fixtures being scored. This runner "
            "NEVER queries a database -- it only reads the exported "
            "scripts/fixtures/jev_eval/*.jsonl files."
        ))
    parser.add_argument(
        "--baseline-function", default=DEFAULT_BASELINE_FUNCTION,
        help="deployed TEST programme-matcher Lambda whose LLM env the "
             "baseline arm's programme_match calls run under")
    parser.add_argument("--profile", default=baseline_mod.DEFAULT_PROFILE)
    parser.add_argument("--region", default=baseline_mod.DEFAULT_REGION)
    parser.add_argument(
        "--dry-run", action="store_true",
        help="build every state (and control state) that would be sent and "
             "write it to results/preview_states.jsonl; no network or aws call")
    return parser.parse_args(argv)


def main(argv=None) -> int:
    args = _parse_args(argv)

    arms = [a.strip() for a in args.arms.split(",") if a.strip()]
    unknown = [a for a in arms if a not in ALL_ARMS]
    if unknown:
        print(f"unknown arm(s) {unknown}; choose from {list(ALL_ARMS)}", file=sys.stderr)
        return 2

    sets = list(SETS) if args.set_name == "all" else [args.set_name]

    try:
        check_preconditions(sets, arms, args.dry_run)
    except RunnerRefusal as exc:
        print(f"refusing to start: {exc}", file=sys.stderr)
        return 2

    if "baseline" in arms and "programme_match" in sets and not args.dry_run:
        copied = baseline_mod.load_deployed_llm_env(
            args.baseline_function, profile=args.profile, region=args.region)
        print(f"loaded deployed LLM env from {args.baseline_function}: {copied}")

    try:
        if args.dry_run:
            return _run_dry_run(sets, arms, args)
        return _run_live(sets, arms, args)
    except JevStateError as exc:
        print(f"refusing to continue: {exc}", file=sys.stderr)
        return 3


if __name__ == "__main__":
    sys.exit(main())
