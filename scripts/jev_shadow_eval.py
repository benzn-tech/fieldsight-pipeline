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

Because the log is append-only and never deduped on disk, `--score` is the
runner's own file -> `scripts/jev_eval/score.py` boundary: `load_results()`
collapses each `(id, arm, run)` down to exactly one row (the last ok row if
one exists, else the last error row) before handing anything to
`score.score_set`, so a retried id's stale error line is never counted
alongside its later success.

## --score

`--score` reads the already-written `results/{set}.*.run*.jsonl` files (via
`load_results`), scores them with `scripts/jev_eval/score.py`, and writes
`results/scores.json`. No network or `aws` call, and no `DECISIONS_API_KEY`
-- it only reads files already on disk. The baseline arm's threshold is
fixed at the DEPLOYED gate's own threshold (`BASELINE_THRESHOLDS`, imported
from `lambda_programme_matcher.CONF_MIN` / `thread_match.MIN_SCORE`, and the
literal 0.5 for `work_class`'s P(non_work) midpoint, since it has no
analogous gate constant); every Jev arm is left to `score_set`'s own "fit"
(split-half) default.

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

import sys as _sys
from pathlib import Path as _Path

# Bootstrap: run directly (`python scripts/jev_shadow_eval.py ...`), the
# repo root and `src/` are not on `sys.path` (only pytest's
# `pythonpath = ["src", "."]` puts them there) -- without this, the first
# repo import below (`lambda_programme_matcher`) fails with
# `ModuleNotFoundError`.
_REPO_ROOT = _Path(__file__).resolve().parents[1]
for _p in (_REPO_ROOT, _REPO_ROOT / "src"):
    if str(_p) not in _sys.path:
        _sys.path.insert(0, str(_p))

import argparse
import json
import os
import re
import statistics
import subprocess
import sys
import threading
import time
import urllib.parse
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from pathlib import Path

import lambda_programme_matcher
import thread_match
import systemone_client as sc

from scripts.jev_eval import baseline as baseline_mod
from scripts.jev_eval import score as score_mod
from scripts.jev_eval.questions import (
    QUESTION_SETS,
    JevQuestionsError,
    question_hash,
)
from scripts.jev_eval.state import (
    JevStateError,
    build_raw_allowed,
    build_state,
    extract_words,
)

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

# --score mode: the baseline arm's threshold is fixed at the DEPLOYED gate's
# own threshold (imported, never re-derived -- same "call the real code"
# ruling as scripts/jev_eval/baseline.py). work_class has no analogous gate
# constant to import: 0.5 is the natural midpoint of its P(non_work) score,
# per controller ruling (fix round 1). Every Jev arm is left out of this map
# on purpose -- `score.score_set` defaults an arm missing from
# `threshold_policy` to "fit" (split-half fitted), which is what a Jev arm
# with no deployed threshold of its own should use.
BASELINE_THRESHOLDS = {
    "programme_match": lambda_programme_matcher.CONF_MIN,
    "threads": thread_match.MIN_SCORE,
    "work_class": 0.5,
}


class RunnerRefusal(RuntimeError):
    """Raised for every precondition this runner checks before doing work."""


class AmbiguousQuestionHashError(RuntimeError):
    """Fix wave 3, minor 4: one arm's already-written result rows carry more
    than one distinct `question_hash` -- e.g. a question definition changed
    (`questions.py` edited, or a deploy that changed
    `lambda_programme_matcher.build_prompt`) between two runs that share the
    same `results/{set}.{arm}.run*.jsonl` files. Scoring fits/compares a
    single composite or threshold per arm; silently pooling rows produced
    under two different question sets would score against a comparison that
    was never actually held constant."""


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

def collect_common_words(sets: list = None, args=None) -> set:
    """Fix wave 6: the `common_words` corpus gate for the generic name pass
    (see `scripts/jev_eval/state.py`), computed ONCE per run -- so broad,
    decomposed and control states all see the identical gate. Built from
    words in each row's own ALLOWLISTED text (`build_raw_allowed` applies the
    exact same field allowlist `build_state` does), never from anything
    outside it. Not persisted anywhere -- recomputed fresh on every
    invocation, including `--dry-run`.

    Fix round 2 (controller ruling, minor): this ALWAYS reads the FULL rows
    of every set present under `FIXTURES_DIR` -- `--set` and `--limit` are
    IGNORED here on purpose, so a `--dry-run --limit 5` preview masks
    exactly the same way the full run would (a smaller corpus computed only
    from the first 5 rows would recover fewer headings than the real run
    ever will, making the preview a pessimistic, misleading rehearsal of
    what actually gets sent). The `sets`/`args` parameters are accepted for
    call-site compatibility but no longer consulted; a set with no fixture
    file on disk simply contributes nothing (unlike `load_rows`, this never
    raises `RunnerRefusal` -- a corpus-building pass should not block on a
    set the caller never asked to run)."""
    words: set = set()
    for set_name in SETS:
        path = FIXTURES_DIR / f"{set_name}.jsonl"
        if not path.exists():
            continue
        for line in path.read_text(encoding="utf-8").splitlines():
            if not line.strip():
                continue
            row = json.loads(line)
            allowed = build_raw_allowed(set_name, row.get("features") or {})
            words |= extract_words(allowed)
    return words


def _build_states(set_name: str, rows: list, aliases: list, common_words: set | None = None) -> dict:
    """`id -> {"state", "site_id", "company_id"}`. Every row's masked state
    is built exactly once here, shared by the broad/decomposed arms and used
    as the input to `control()` for the control arms (ruling #5). Lets
    `JevStateError` propagate -- a transcript-like key surviving into a
    fixture is a privacy bug in the export, not a per-row runner failure to
    swallow and continue past."""
    out = {}
    for row in rows:
        state = build_state(set_name, row.get("features") or {}, aliases, common_words)
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


def load_results(results_dir: Path, set_name: str) -> dict:
    """Read every `results/{set_name}.{arm}.run{n}.jsonl` file under
    `results_dir` and collapse the append-only log to exactly one row per
    `(id, arm, run)`: the LAST ok row (`error is None`) for that id if one
    exists in the file, else the LAST error row.

    This is the runner's own file -> `score.score_set` boundary (controller
    ruling, fix round 1): the log stays append-only (a retried row leaves
    its old error line in place, by design -- it's a log, not a keyed
    table), but nothing downstream of this function ever sees a stale error
    line for an id that later succeeded, or double-counts a row that
    appears twice.

    Returns `{arm: {run: [rows]}}`, exactly the shape
    `scripts.jev_eval.score.score_set` expects. Rows within each `[rows]`
    list are sorted by id for determinism.
    """
    result: dict = {}
    if not results_dir.exists():
        return result

    prefix = f"{set_name}."
    for path in sorted(results_dir.glob(f"{set_name}.*.run*.jsonl")):
        rest = path.name[len(prefix):]
        if not rest.endswith(".jsonl"):
            continue
        rest = rest[: -len(".jsonl")]
        arm, sep, run_str = rest.rpartition(".run")
        if not sep or not run_str.isdigit():
            continue
        run = int(run_str)

        lines_by_id: dict = {}
        for line in path.read_text(encoding="utf-8").splitlines():
            if not line.strip():
                continue
            row = json.loads(line)
            lines_by_id.setdefault(row.get("id"), []).append(row)

        collapsed = []
        for row_id in sorted(lines_by_id, key=str):
            lines = lines_by_id[row_id]
            ok_lines = [r for r in lines if r.get("error") is None]
            collapsed.append(ok_lines[-1] if ok_lines else lines[-1])

        result.setdefault(arm, {})[run] = collapsed

    # Fix wave 3, minor 4: refuse if one arm's rows (across every run) carry
    # more than one distinct question_hash -- see AmbiguousQuestionHashError.
    for arm, by_run in result.items():
        all_hashes = {
            row.get("question_hash")
            for rows in by_run.values() for row in rows
            if row.get("question_hash") is not None
        }
        if len(all_hashes) > 1:
            raise AmbiguousQuestionHashError(
                f"arm {arm!r} of set {set_name!r} has rows scored under "
                f"{len(all_hashes)} different question_hash values "
                f"({sorted(all_hashes, key=str)}); refusing to score a mixed "
                "batch -- re-run this arm from scratch after a question "
                "definition change")

    return result


def rejoin_current_labels(rows_by_arm_run: dict, current_rows: list) -> tuple:
    """Fix wave 3, minor 4 / fix wave 4, A7: overwrite every scored row's
    `label` with the CURRENT value from `{set}.jsonl`, keyed by id -- a
    relabel (a re-import of owner labels, or a re-export) must not leave a
    stale `label` baked into an already-written `results/*.jsonl` row that
    `--score` later reads. A row whose id no longer exists in the current
    fixture at all (a hard delete, or the export dropping it) is EXCLUDED
    from the returned rows entirely -- fix wave 3 left it in place with its
    stale label, which meant `--score` still scored it against a label that
    no longer exists anywhere. Returns `(rows_by_arm_run, missing_ids)`:
    `missing_ids` is every excluded row's id, so a stale/relabelled run does
    not silently drop rows without anyone noticing."""
    current_by_id = {row["id"]: row for row in current_rows}
    missing_ids = set()
    result: dict = {}
    for arm, by_run in rows_by_arm_run.items():
        new_by_run = {}
        for run, rows in by_run.items():
            kept = []
            for row in rows:
                current = current_by_id.get(row.get("id"))
                if current is None:
                    missing_ids.add(row.get("id"))
                    continue
                row = dict(row)
                row["label"] = current.get("label")
                kept.append(row)
            new_by_run[run] = kept
        result[arm] = new_by_run
    return result, sorted(missing_ids, key=str)


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

# Fix wave 2, I1 requirement 5: --dry-run must show over-masking, not just
# leaks. Any run of PERSON_n placeholders, counted per state (not deduped --
# a state with the same person mentioned three times should show 3, since
# that's three tokens actually sent).
_PLACEHOLDER_COUNT_RE = re.compile(r"PERSON_\d+")
# A "title" field (or task.name for programme_match) that, once whitespace
# and light punctuation are stripped away, is made up ENTIRELY of PERSON_n
# placeholders -- the over-masking failure mode the brief's own probes hit
# ("Roof Framing" -> "PERSON_1"): the whole field is gone, not just a name
# inside it.
_PLACEHOLDER_ONLY_RE = re.compile(r"^(?:PERSON_\d+[\s.,!?]*)+$")


def _title_like(set_name: str, state: dict):
    """The one field per set that stands in for "the readable label a human
    reviewer would look at first" -- what the over-masking fraction checks."""
    if set_name == "work_class":
        return state.get("title")
    if set_name == "programme_match":
        task = state.get("task") or {}
        return task.get("name")
    if set_name == "threads":
        later = state.get("later") or {}
        return later.get("title")
    return None


def _masking_stats(set_name: str, states_by_id: dict) -> dict:
    """Per-set over-masking stats for --dry-run: mean PERSON_n placeholders
    per state, and the fraction of states whose title-like field is made up
    entirely of placeholders. Returns `None` for both fractions when there
    are no states (n=0) or no checkable titles, rather than dividing by
    zero."""
    states = [entry["state"] for entry in states_by_id.values()]
    n = len(states)
    if n == 0:
        return {"n": 0, "mean_placeholders_per_state": None,
                "title_all_placeholder_fraction": None}

    total_placeholders = sum(
        len(_PLACEHOLDER_COUNT_RE.findall(json.dumps(state))) for state in states
    )
    titles = [_title_like(set_name, state) for state in states]
    checkable = [t for t in titles if isinstance(t, str) and t]
    all_placeholder = sum(1 for t in checkable if _PLACEHOLDER_ONLY_RE.fullmatch(t))
    fraction = (all_placeholder / len(checkable)) if checkable else None

    return {
        "n": n,
        "mean_placeholders_per_state": total_placeholders / n,
        "title_all_placeholder_fraction": fraction,
    }


def _print_masking_stats(set_name: str, stats: dict) -> None:
    mean_ph = stats["mean_placeholders_per_state"]
    frac = stats["title_all_placeholder_fraction"]
    mean_str = f"{mean_ph:.2f}" if mean_ph is not None else "n/a"
    frac_str = f"{frac:.1%}" if frac is not None else "n/a"
    print(
        f"masking stats [{set_name}]: n={stats['n']} "
        f"mean_placeholders_per_state={mean_str} "
        f"title_all_placeholder_fraction={frac_str}"
    )


def _run_dry_run(sets: list, arms: list, args) -> int:
    jev_arms_selected = [arm for arm in arms if arm in JEV_ARMS]
    RESULTS_DIR.mkdir(parents=True, exist_ok=True)
    preview_path = RESULTS_DIR / "preview_states.jsonl"

    n_entries = 0
    max_size = 0
    masking_stats_by_set: dict = {}
    common_words = collect_common_words()
    with open(preview_path, "w", encoding="utf-8") as fh:
        for set_name in sets:
            rows = load_rows(set_name)
            if args.limit:
                rows = rows[: args.limit]
            aliases = load_aliases()
            states_by_id = _build_states(set_name, rows, aliases, common_words)
            masking_stats_by_set[set_name] = _masking_stats(set_name, states_by_id)

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
    for set_name in sets:
        _print_masking_stats(set_name, masking_stats_by_set[set_name])
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
    common_words = collect_common_words()

    for set_name in sets:
        rows = load_rows(set_name)
        if args.limit:
            rows = rows[: args.limit]
        aliases = load_aliases()
        states_by_id = _build_states(set_name, rows, aliases, common_words)

        control_states, control_errors = {}, {}
        if any(arm in ("control_broad", "control_decomposed") for arm in arms):
            control_states, control_errors = _build_control_states(
                set_name, rows, states_by_id)

        per_set_state[set_name] = {
            "rows": rows,
            "states": states_by_id,
            "control_states": control_states,
            "control_errors": control_errors,
        }

    # Fix wave 3, minor 3: run 1 must finish completely -- every set, every
    # arm, every row -- before run 2's first task is even submitted, never
    # interleaved on the same executor. Interleaving them let run 2's calls
    # share a warm connection/cache window with a still-in-flight run 1 call,
    # which is exactly the kind of correlation a stability check across runs
    # is supposed to rule out.
    for run in range(1, args.runs + 1):
        # For run > 1: read run-1's already-written files fresh from disk
        # (the same collapse `load_results` uses -- last ok row per id, else
        # last error) so `identical_to_previous_run` is available even when
        # run 1 was completed in an EARLIER invocation of this script (a
        # resumed run), not only when this same call just produced it.
        previous_run_answers: dict = {}
        if run > 1:
            for set_name in sets:
                for arm in arms:
                    prev_path = RESULTS_DIR / f"{set_name}.{arm}.run{run - 1}.jsonl"
                    if not prev_path.exists():
                        continue
                    lines_by_id: dict = {}
                    for line in prev_path.read_text(encoding="utf-8").splitlines():
                        if not line.strip():
                            continue
                        r = json.loads(line)
                        lines_by_id.setdefault(r.get("id"), []).append(r)
                    for row_id, lines in lines_by_id.items():
                        ok_lines = [r for r in lines if r.get("error") is None]
                        if ok_lines:
                            previous_run_answers[(set_name, arm, row_id)] = ok_lines[-1].get("answers")

        tasks = []  # (set_name, row, arm)
        for set_name in sets:
            rows = per_set_state[set_name]["rows"]
            for arm in arms:
                out_path = RESULTS_DIR / f"{set_name}.{arm}.run{run}.jsonl"
                ok_ids = _load_existing_ok_ids(out_path)
                for row in rows:
                    if row["id"] in ok_ids:
                        continue
                    tasks.append((set_name, row, arm))

        with ThreadPoolExecutor(max_workers=CONCURRENCY) as executor:
            futures = {
                executor.submit(
                    _process_task, set_name, row, arm, run,
                    per_set_state[set_name], sleeper, jev_route, jev_provider, jev_model,
                ): (set_name, row, arm)
                for (set_name, row, arm) in tasks
            }
            for future in as_completed(futures):
                set_name, row, arm = futures[future]
                out_row = future.result()
                key = (set_name, arm, out_row.get("id"))
                if run > 1 and key in previous_run_answers:
                    out_row["identical_to_previous_run"] = (
                        out_row.get("answers") == previous_run_answers[key]
                        if out_row.get("error") is None else None
                    )
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
    parser.add_argument(
        "--score", action="store_true",
        help="score the already-written results/{set}.*.run*.jsonl files via "
             "scripts/jev_eval/score.py and write results/scores.json; no "
             "network or aws call, no API key needed, ignores --arms")
    return parser.parse_args(argv)


def _print_score_table(all_scores: dict) -> None:
    for set_name, payload in all_scores.items():
        threshold = payload["baseline_threshold"]
        print(f"== {set_name} (baseline threshold={threshold}) ==")
        for arm, metrics in payload["scores"].items():
            if arm == "_control_checks":
                continue
            pooled = (metrics.get("coverage_at_p95") or {}).get("pooled") or {}
            print(
                f"  {arm:22s} n={metrics.get('n')!s:>4}  "
                f"n_failed={metrics.get('n_failed')!s:>4}  "
                f"n_yes={metrics.get('n_yes')!s:>3}  n_no={metrics.get('n_no')!s:>3}  "
                f"accuracy={metrics.get('accuracy')}  "
                f"precision={metrics.get('precision')}  "
                f"recall={metrics.get('recall')}  "
                f"held_out_coverage={pooled.get('coverage')}  "
                f"held_out_precision={pooled.get('precision')}"
            )

        verdict_obj = payload.get("verdict") or {}
        print(f"  -- verdict (decomposed vs baseline): {verdict_obj.get('verdict')} --")
        for reason in verdict_obj.get("reasons", []):
            print(f"     reason: {reason}")

        for arm_name, arm_inputs in (verdict_obj.get("inputs") or {}).items():
            if not arm_inputs:
                continue
            elig = arm_inputs.get("eligibility") or {}
            control = arm_inputs.get("control") or {}
            cov = arm_inputs.get("coverage_diff") or {}
            brier = arm_inputs.get("brier") or {}
            stability = arm_inputs.get("stability") or {}
            print(
                f"     [{arm_name}] arm_verdict={arm_inputs.get('verdict')}  "
                f"eligible={elig.get('eligible')}  "
                f"control={control.get('result') if control else None}  "
                f"coverage_diff_point={cov.get('point_estimate')}  "
                f"coverage_diff_ci90={cov.get('ci_90')}  "
                f"paired_brier_diff_point={brier.get('point_estimate')}  "
                f"paired_brier_diff_ci90={brier.get('ci_90')}  "
                f"stability_flips={stability.get('flips')}/{stability.get('allowed_flips')}  "
                f"identical_answers_fraction={stability.get('identical_answer_fraction')}"
            )


def _git_head_sha():
    """Best-effort git HEAD sha for a `scores.json` provenance stamp. Never
    raises: a checkout without git on PATH, or one that is not a git
    worktree at all, gets `None` rather than blocking scoring, which only
    reads files already on disk and has no other reason to fail here."""
    try:
        result = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            capture_output=True, text=True,
            cwd=str(Path(__file__).resolve().parent.parent),
        )
    except Exception:  # noqa: BLE001 - provenance is best-effort, never fatal
        return None
    if result.returncode != 0:
        return None
    return result.stdout.strip()


def _result_file_stats(results_dir: Path, set_name: str) -> tuple:
    """Per-`(arm, run)` raw line count and row-after-dedup count for
    `set_name`'s result files, plus the sorted list of file names read.
    Parses file names the same way `load_results` does (this IS the
    provenance for that function's own dedup step), so the two can never
    disagree about which files exist or how they're named. "Rows after
    dedup" here means distinct ids in the file -- the same count
    `load_results` would collapse each file down to, one row per id."""
    files_read: list = []
    per_arm_run: dict = {}
    if not results_dir.exists():
        return files_read, per_arm_run

    prefix = f"{set_name}."
    for path in sorted(results_dir.glob(f"{set_name}.*.run*.jsonl")):
        rest = path.name[len(prefix):]
        if not rest.endswith(".jsonl"):
            continue
        rest = rest[: -len(".jsonl")]
        arm, sep, run_str = rest.rpartition(".run")
        if not sep or not run_str.isdigit():
            continue
        run = int(run_str)

        lines = [ln for ln in path.read_text(encoding="utf-8").splitlines() if ln.strip()]
        ids = {json.loads(ln).get("id") for ln in lines}
        files_read.append(path.name)
        per_arm_run[f"{arm}.run{run}"] = {
            "raw_lines": len(lines),
            "rows_after_dedup": len(ids),
        }
    return files_read, per_arm_run


def _provenance_for_set(set_name: str) -> dict:
    """Everything a reader needs to tell a partial `scores.json` run from a
    full one, without re-deriving it from the raw result files: which files
    were read, per-(arm,run) raw-vs-deduped row counts, the git HEAD sha the
    run happened at (best-effort, see `_git_head_sha`), when scoring ran, and
    which of the five arms were expected but had no result file at all."""
    files_read, per_arm_run = _result_file_stats(RESULTS_DIR, set_name)
    arms_present = {key.split(".run")[0] for key in per_arm_run}
    missing_arms = sorted(arm for arm in ALL_ARMS if arm not in arms_present)
    return {
        "results_files_read": files_read,
        "per_arm_run": per_arm_run,
        "git_head_sha": _git_head_sha(),
        "scored_at": _now_iso(),
        "arms_expected_but_missing": missing_arms,
    }


def _run_score(sets: list, args) -> int:
    """`--score`: load the already-written result files (via `load_results`,
    which collapses the append-only log to one row per (id, arm, run) --
    ruling #1) and score them with `scripts.jev_eval.score.score_set`. No
    network or aws call is made, and no `DECISIONS_API_KEY` is needed --
    this only reads files already on disk under `results/`.

    Baseline's threshold is fixed at the deployed gate's own threshold
    (`BASELINE_THRESHOLDS`, imported from the real gate modules, never
    hardcoded/re-derived); every Jev arm is left to `score_set`'s own "fit"
    default (split-half fitted), per the controller's ruling."""
    all_scores: dict = {}
    for set_name in sets:
        rows_by_arm_run = load_results(RESULTS_DIR, set_name)
        if not rows_by_arm_run:
            continue

        # Fix wave 3, minor 4: re-join labels from the current {set}.jsonl
        # rather than trusting whatever label a row was written with -- a
        # relabel must not leave a stale label in the scored rows. Skipped
        # (not fatal) when the current fixture file is missing entirely, so
        # scoring an old results/ directory after fixtures moved elsewhere
        # still works, just without the re-join.
        try:
            current_rows = load_rows(set_name)
        except RunnerRefusal:
            current_rows = None
        if current_rows is not None:
            rows_by_arm_run, missing_ids = rejoin_current_labels(rows_by_arm_run, current_rows)
            if missing_ids:
                print(
                    f"warning: {len(missing_ids)} scored row id(s) for {set_name!r} "
                    f"no longer exist in {set_name}.jsonl -- EXCLUDED from scoring "
                    f"(fix wave 4, A7 -- never scored with a stale label): "
                    f"{missing_ids}",
                    file=sys.stderr,
                )

        threshold_policy = {}
        if "baseline" in rows_by_arm_run:
            threshold_policy["baseline"] = BASELINE_THRESHOLDS[set_name]
        scores = score_mod.score_set(rows_by_arm_run, threshold_policy)
        all_scores[set_name] = {
            "scores": scores,
            "baseline_threshold": BASELINE_THRESHOLDS[set_name],
            "provenance": _provenance_for_set(set_name),
            "verdict": score_mod.verdict(scores),
        }

    RESULTS_DIR.mkdir(parents=True, exist_ok=True)
    scores_path = RESULTS_DIR / "scores.json"
    scores_path.write_text(
        json.dumps(all_scores, indent=2, sort_keys=True) + "\n", encoding="utf-8")

    _print_score_table(all_scores)
    print(f"wrote {scores_path}")
    return 0


def main(argv=None) -> int:
    args = _parse_args(argv)

    sets = list(SETS) if args.set_name == "all" else [args.set_name]

    if args.score:
        try:
            return _run_score(sets, args)
        except AmbiguousQuestionHashError as exc:
            print(f"refusing to score: {exc}", file=sys.stderr)
            return 2

    arms = [a.strip() for a in args.arms.split(",") if a.strip()]
    unknown = [a for a in arms if a not in ALL_ARMS]
    if unknown:
        print(f"unknown arm(s) {unknown}; choose from {list(ALL_ARMS)}", file=sys.stderr)
        return 2

    try:
        check_preconditions(sets, arms, args.dry_run)
    except RunnerRefusal as exc:
        print(f"refusing to start: {exc}", file=sys.stderr)
        return 2

    # Fix wave 3, minor 5: skip the aws call and env-var copy entirely when
    # programme_match is not selected OR has zero rows to run -- there is
    # nothing for the baseline arm's config to apply to, and this call was
    # previously made (and could fail loudly, or just spend a network round
    # trip) whenever "baseline"+"programme_match" were both selected, even
    # with an empty/limited-to-zero programme_match.jsonl.
    if "baseline" in arms and "programme_match" in sets and not args.dry_run:
        programme_match_rows = load_rows("programme_match")
        if args.limit:
            programme_match_rows = programme_match_rows[: args.limit]
        if programme_match_rows:
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
