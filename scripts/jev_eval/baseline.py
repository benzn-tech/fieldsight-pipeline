"""The baseline arm for the Jev shadow evaluation (Track A, Task 5).

The baseline is TODAY'S GATE, imported rather than re-derived. Global rule
(controller): call the real code paths --
`lambda_programme_matcher.build_prompt` / `parse_verdict` and
`thread_match.MIN_SCORE` -- rather than re-implementing their logic here, and
never publish anything to a real questions endpoint. This module produces
Task 7 result rows for the "baseline" arm only; the systemone/decisions arms
live elsewhere.

Row contract (controller ruling #1), matching `scripts/jev_eval/score.py`'s
documented shape plus two fields that shape does not need but the baseline
does:

    {"id", "label", "arm": "baseline", "run", "score", "answers": None,
     "question_hash", "latency_ms", "tokens", "error", "accepted"}

`accepted` is a bool (the stored/real gate accepted this row) or None when
the row itself failed (no verdict was ever reached). `deterministic: True`
is added for the threads and work_class sets, which are single-valued by
construction -- run 2 reads the exact same stored value as run 1, never a
fresh model call.

On any LLM/parse failure, `error` is set and `score` is None -- NEVER 0.0.
0.0 is a real, valid answer (confident "no match"/"non_work" verdicts are
common and correct); collapsing a failure into it would make the baseline
look worse-calibrated than it is, not merely absent.

## programme_match

The stored `programme_progress_suggestions` row only ever recorded ONE
candidate task (the one the writer ultimately suggested, or none) -- Task 1's
export baseline dict is `{confidence, suggested_status, suggested_progress,
task_id}`, not the ranked candidate list the live matcher saw. So the
baseline call here is Claude re-scoring "does this ONE stored task_id match
this observation" through the exact same `build_prompt` / `parse_verdict`
functions the deployed matcher uses -- it does NOT re-run the embedding
shortlist stage (that stage lives upstream of the label, in Aurora rows this
export never sees). Score is the model's OWN reported confidence whenever
its returned task_id equals the row's stored task_id -- including a
confidence below CONF_MIN, per controller ruling #2 (scoring only ACCEPTED
verdicts collapses everything below CONF_MIN to a single point and leaves
the score distribution in {0} u [0.70, 1], which makes coverage@p95
degenerate). A `task_id` that does not match (including null, "no match") is
a real 0.0, not a failure. `accepted` records the double-gated real
`parse_verdict` outcome separately, so calibration analysis can see both the
raw confidence AND whether today's gate would actually have accepted it.

## threads / work_class

Both baselines are the stored gate output, already computed at label time --
no LLM call, no re-derivation, single-valued by construction. `threads`'
`accepted` is the stored score compared against the real
`thread_match.MIN_SCORE` (imported, not copied). `work_class`'s score is
P(non_work): the stored confidence when the stored verdict is "non_work",
else its complement -- so the score is always "how confident is the gate
that this observation is non-work", matching the human label's own
convention (`export_labels.py`'s `_LABEL_WORK_CLASS`: "yes" means non-work).

## Provider config

Per controller ruling #3, the LLM provider/model/temperature this arm's
programme_match calls run under must be READ FROM THE DEPLOYED TEST
`fieldsight-test-programme-matcher` function -- that IS the gate under
measurement, not `extract-session` (a different Lambda, a different prompt,
a different model) and not the workflow's own local default. `PROGRAMME_MATCHER_FUNCTION`
below was read out of `src/template.yaml:3976-3978`:
`FunctionName: !Sub ["${P}-programme-matcher", {P: !FindInMap [StageConfig, !Ref Stage, Prefix]}]`
with `StageConfig.test.Prefix = fieldsight-test` (`src/template.yaml:1339-1340`)
-> `fieldsight-test-programme-matcher`.
"""
from __future__ import annotations

import hashlib
import inspect
import json
import os
import subprocess
import time

import lambda_programme_matcher
import llm_utils
import thread_match

PROGRAMME_MATCHER_FUNCTION = "fieldsight-test-programme-matcher"

DEFAULT_PROFILE = "fieldsight-deployer"
DEFAULT_REGION = "ap-southeast-2"

# Only the variables `llm_utils` itself reads (module docstring + top-level
# `os.environ.get` calls, `src/llm_utils.py:16-17,34-68`) are ever copied --
# never the whole `Environment.Variables` map, which on a real Lambda also
# carries S3_BUCKET/SUGGESTION_WRITER_FUNCTION/etc that this offline harness
# has no business setting.
ALLOWED_ENV_KEYS = (
    "LLM_PROVIDER",
    "ANTHROPIC_API_KEY",
    "CLAUDE_MODEL",
    "QWEN_API_KEY",
    "DASHSCOPE_API_KEY",
    "QWEN_BASE_URL",
    "QWEN_MODEL",
    "QWEN_MODEL_NONTHINKING",
    "LLM_TEMPERATURE",
)

# Identifies the exact prompt template that produced a programme_match
# baseline row (controller ruling #6) -- the hash changes the moment
# `build_prompt`'s source changes, so a results file can be checked against
# "was this run before or after the prompt changed" without re-reading code.
_PROGRAMME_MATCH_QUESTION_HASH = hashlib.sha256(
    inspect.getsource(lambda_programme_matcher.build_prompt).encode("utf-8")
).hexdigest()

_THREADS_QUESTION_HASH = "stored:thread_match"
_WORK_CLASS_QUESTION_HASH = "stored:classifier"


class JevBaselineError(ValueError):
    """Raised for a baseline request this module cannot map (unknown set)."""


# ---------------------------------------------------------------------------
# Provider config -- I/O (subprocess -> aws CLI). Never called by tests,
# which pass a fake `run` instead (no aws, no network).
# ---------------------------------------------------------------------------

def load_deployed_llm_env(function_name, *, profile=DEFAULT_PROFILE,
                           region=DEFAULT_REGION, run=subprocess.run):
    """Copy the deployed function's LLM-related env vars into THIS process's
    `os.environ`, so `llm_utils`'s module-level env reads (evaluated at
    import time) pick up the same provider/model/temperature TEST's
    programme-matcher actually runs under -- not this workflow's own default.

    Style follows `scripts/eval_task_admission.py:16-21`: read the deployed
    function's configuration and copy an explicit allowlist of keys, never
    the whole environment. Controller ruling #3 moves the transport from
    boto3 to the `aws` CLI (matching `export_labels.py`'s `_aws` helper) so a
    test can inject a fake `subprocess.run` and this function is never
    called for real outside a human running the eval by hand.

    Returns the sorted list of key NAMES actually copied (present and
    truthy in the deployed config) -- never the values, so a report or log
    line built from this return value cannot leak a secret.
    """
    result = run(
        [
            "aws", "lambda", "get-function-configuration",
            "--function-name", function_name,
            "--profile", profile, "--region", region,
            "--output", "json",
        ],
        capture_output=True, text=True,
    )
    if result.returncode != 0:
        raise RuntimeError(
            f"aws lambda get-function-configuration failed for "
            f"{function_name!r}: {result.stderr.strip()[:4000]}"
        )
    config = json.loads(result.stdout)
    env = (config.get("Environment") or {}).get("Variables") or {}

    copied = []
    for key in ALLOWED_ENV_KEYS:
        value = env.get(key)
        if value:
            os.environ[key] = value
            copied.append(key)
    return sorted(copied)


def baseline_config() -> dict:
    """Snapshot of the provider/model/temperature this process is about to
    run the programme_match baseline under, for the runner to stamp into
    results alongside every row -- so a results file self-documents which
    gate configuration produced it without a separate lookup."""
    provider = os.environ.get("LLM_PROVIDER", "anthropic")
    if provider == "qwen":
        model = os.environ.get("QWEN_MODEL")
    else:
        model = os.environ.get("CLAUDE_MODEL")
    return {
        "provider": provider,
        "model": model,
        "temperature": os.environ.get("LLM_TEMPERATURE"),
    }


# ---------------------------------------------------------------------------
# Row builder -- pure except for the `call` seam (defaults to the real
# `llm_utils.call_llm`, injectable so tests never touch the network).
# ---------------------------------------------------------------------------

def _row(row, run, *, score, question_hash, accepted, error=None,
         latency_ms=None, tokens=None, deterministic=None) -> dict:
    out = {
        "id": row.get("id"),
        "label": row.get("label"),
        "arm": "baseline",
        "run": run,
        "score": score,
        "answers": None,
        "question_hash": question_hash,
        "latency_ms": latency_ms,
        "tokens": tokens,
        "error": error,
        "accepted": accepted,
    }
    if deterministic is not None:
        out["deterministic"] = deterministic
    return out


def baseline_row(set_name: str, row: dict, run: int, *, call=None) -> dict:
    """One Task 7 result row for the baseline arm, dispatched by `set_name`.

    `call` is the LLM entry point used for programme_match ONLY (threads and
    work_class never call an LLM -- their baseline is already-stored gate
    output). Defaults to the real `llm_utils.call_llm`; tests pass a fake
    with the same `(prompt, max_tokens, force_json, caller) -> (raw, error)`
    signature.
    """
    if set_name == "programme_match":
        return _baseline_programme_match(row, run, call or llm_utils.call_llm)
    if set_name == "threads":
        return _baseline_threads(row, run)
    if set_name == "work_class":
        return _baseline_work_class(row, run)
    raise JevBaselineError(f"unknown set {set_name!r}")


def _baseline_programme_match(row: dict, run: int, call) -> dict:
    features = row.get("features") or {}
    baseline = row.get("baseline") or {}
    task_id = baseline.get("task_id")

    observation = features.get("observation") or {}
    task = features.get("task") or {}

    # Real `build_prompt` expects a `topic` dict (title/summary/date or
    # report_date, optionally action_items) and a `candidates` list of
    # programme-leaf dicts (task_id/name/status/progress_pct/...). The
    # export's `features` shape only carries the fields `build_prompt`
    # actually reads for this comparison; missing ones (action_items,
    # assignees, start/end) fall through its own `.get(...)` defaults, same
    # as a bare-bones real programme leaf.
    topic = {
        "title": observation.get("title"),
        "summary": observation.get("summary"),
        "report_date": observation.get("date"),
    }
    candidate = {
        "task_id": task_id,
        "name": task.get("name"),
        "status": task.get("status"),
        "progress_pct": task.get("progress_pct"),
    }

    prompt = lambda_programme_matcher.build_prompt(topic, [candidate])

    started = time.time()
    raw, error = call(prompt, max_tokens=512, force_json=True,
                       caller="jev_eval_baseline_programme")
    latency_ms = int((time.time() - started) * 1000)

    if raw is None:
        return _row(row, run, score=None, question_hash=_PROGRAMME_MATCH_QUESTION_HASH,
                     accepted=None, error=error or "LLM call failed",
                     latency_ms=latency_ms)

    parsed = llm_utils.extract_json(raw)
    if not parsed:
        return _row(row, run, score=None, question_hash=_PROGRAMME_MATCH_QUESTION_HASH,
                     accepted=None, error="could not parse JSON from LLM response",
                     latency_ms=latency_ms)

    # The real double-gate, run for real -- NOT re-derived. `accepted` is
    # whether TODAY'S GATE would have accepted this verdict; it is recorded
    # even when the raw confidence below scores it as a real 0.0 or a
    # sub-CONF_MIN value (controller ruling #2).
    verdict = lambda_programme_matcher.parse_verdict(
        raw, {task_id}, lambda_programme_matcher.CONF_MIN)
    accepted = verdict is not None

    picked_task_id = parsed.get("task_id")
    if picked_task_id is None or picked_task_id != task_id:
        # A valid "no match" (or a pick of some other task) is a real,
        # correct 0.0 -- not a failure.
        score = 0.0
    else:
        try:
            score = float(parsed.get("confidence"))
        except (TypeError, ValueError):
            return _row(row, run, score=None, question_hash=_PROGRAMME_MATCH_QUESTION_HASH,
                         accepted=accepted, error="confidence not numeric",
                         latency_ms=latency_ms)

    return _row(row, run, score=score, question_hash=_PROGRAMME_MATCH_QUESTION_HASH,
                 accepted=accepted, latency_ms=latency_ms)


def _baseline_threads(row: dict, run: int) -> dict:
    baseline = row.get("baseline") or {}
    score = baseline.get("score")
    if score is None:
        return _row(row, run, score=None, question_hash=_THREADS_QUESTION_HASH,
                     accepted=None, error="missing stored thread_match score",
                     deterministic=True)
    accepted = score >= thread_match.MIN_SCORE
    return _row(row, run, score=score, question_hash=_THREADS_QUESTION_HASH,
                 accepted=accepted, deterministic=True)


def _baseline_work_class(row: dict, run: int) -> dict:
    baseline = row.get("baseline") or {}
    verdict = baseline.get("classifier_verdict")
    confidence = baseline.get("classifier_confidence")
    if verdict is None or confidence is None:
        return _row(row, run, score=None, question_hash=_WORK_CLASS_QUESTION_HASH,
                     accepted=None,
                     error="missing stored classifier_verdict/classifier_confidence",
                     deterministic=True)
    try:
        confidence = float(confidence)
    except (TypeError, ValueError):
        return _row(row, run, score=None, question_hash=_WORK_CLASS_QUESTION_HASH,
                     accepted=None, error="classifier_confidence not numeric",
                     deterministic=True)

    score = confidence if verdict == "non_work" else 1.0 - confidence
    accepted = verdict == "non_work"
    return _row(row, run, score=score, question_hash=_WORK_CLASS_QUESTION_HASH,
                 accepted=accepted, deterministic=True)
