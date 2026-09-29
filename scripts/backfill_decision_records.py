"""One-off backfill: pre-decision_records human verdicts become decision_records rows.

Track B Task 7. `decision_records` (migration 0073) only started filling up once
Tasks 6a/6b's writers/endpoints shipped -- every `programme_progress_suggestions` /
`topic_thread_suggestions` / `classification_feedback` row a human decided BEFORE
that point has no row here. This script reconstructs one, so Track A's eval sees a
single consistent shape regardless of when a verdict was decided.

DRY-RUN BY DEFAULT: everything runs inside one RDS Data API transaction
(begin-transaction -> a SELECT per source for counts, an INSERT per source when
`--apply` -> commit-transaction only with `--apply`, rollback-transaction
otherwise). A dry run never calls commit-transaction at all -- see
`test_dry_run_never_commits` / `test_apply_commits`.

Keying (must match what tasks 6a/6b's live writers/endpoints already produce, so a
row backfilled here and a row written live are the SAME shape to `list_for_eval`):
  - programme_progress_suggestions -> kind='programme_match', subject_type='topic',
    subject_stable_id=topic_id (lambda_org_api.confirm_suggestion/reject_suggestion),
    object_ref=task_id (task-6b-report.md's note #3: topics are not re-keyed, no
    resolution step). Rows with topic_id IS NULL (ON DELETE SET NULL) are skipped
    and counted -- there is no subject to key the record on.
  - topic_thread_suggestions -> kind='thread', subject_type='topic',
    subject_stable_id=topic_id, object_ref=parent_topic_id when set, else the
    object_ref decision_records.object_ref_for_accepted would resolve for this
    subject (task-6b-report.md's `_thread_decision_object_ref`: migration 0032's
    thread_id XOR parent_topic_id CHECK means the specific earlier candidate topic
    the writer scored against is only recoverable, for the thread_id branch, from a
    decision_records row the LIVE writer already wrote for this subject after Task
    6a shipped -- backfilled here as the identical correlated lookup, self-
    consistently against whatever this same transaction has already inserted).
    threshold = thread_match.MIN_SCORE, imported (not duplicated as a literal) so
    this never drifts from the live accept bar.
  - classification_feedback -> kind='work_class', subject_type='topic',
    subject_stable_id=topic_id, object_ref=NULL (lambda_org_api's
    create_classification_feedback_endpoint: "a work_class verdict has no 'other
    side'"). human_verdict -> human_outcome: confirm_non_work -> confirmed;
    reject_is_work / missed_personal -> rejected (both are the human overturning
    the classifier, in opposite directions -- same mapping 6b's endpoint uses).

`provider='legacy'` on every backfilled row, not the live writers' own
'anthropic'/'qwen'/'lexical': none of these three tables recorded which provider
made the original call, so 'legacy' is an honest "reconstructed, provider
unknown" marker rather than a guess (this user's memory: a wrong name is worse
than no name). `auto_outcome='accepted'` throughout -- a suggestion/thread
candidate/feedback row only exists at all because the live gate (confidence
>= threshold, score >= MIN_SCORE, or "always" for classification) already
accepted it; this script has no visibility into anything the gate rejected,
because a rejected verdict was never written anywhere before decision_records
existed.

`created_at` on the backfilled row is the SOURCE row's OWN `created_at` (when
the verdict was made), never `now()` -- this is a backfill of history, not a
fresh event. `human_at` is the source row's own decision timestamp
(`decided_at` / `resolved_at`), or, for classification_feedback, `created_at`
itself: that table's row is born already decided (human_verdict is NOT NULL on
insert; there is no separate pending phase), so its creation IS the human
decision.

Idempotent on (kind, subject_stable_id, object_ref, created_at) -- each INSERT is
`INSERT ... SELECT ... WHERE NOT EXISTS (...)`, with `object_ref` compared
`IS NOT DISTINCT FROM` (same convention as decision_records.set_human_outcome)
so a legitimately-NULL object_ref matches a NULL object_ref rather than never
matching anything. Running this script twice inserts nothing the second time --
proven by `tests/integration/test_backfill_decision_records_sql.py`'s
`test_second_run_inserts_nothing`.

`output` (jsonb, NOT NULL on the table) carries only ids/enums/numbers, built with
Postgres's own `jsonb_build_object()` from the source row's typed columns --
never a Python-side string concatenation, and never a free-text column (plan
Global Constraint: decision_records never carries transcript text; none of these
three tables' human-decision columns ARE free text anyway -- `topic_category` is
deliberately left out of the work_class output for the same reason
`classification_feedback`'s own migration comment gives: "NEVER the transcript or
any personal text").

`human_actor`: the source table's own actor column, but only when it names a REAL
row in `users` -- resolved with a LEFT JOIN, never a bare cast. Two of the three
actor columns (programme_progress_suggestions.decided_by,
classification_feedback.actor_user_id) are already `uuid REFERENCES users(id)`, so
the join is a formality; `topic_thread_suggestions.resolved_by` is plain `text`
(set from `str(caller["id"])` by lambda_org_api's confirm/reject_thread_suggestion,
migration 0032 predates this whole design) and is never cast to uuid at all --
comparing `users.id::text = resolved_by` means a malformed or empty string simply
joins to nothing (NULL) rather than raising a cast error mid-backfill.

SQL: every statement below is a plain string with NO named parameters (`:name`)
and no `%s` placeholders -- there is nothing per-row to bind, since each
statement is one INSERT...SELECT...WHERE NOT EXISTS over a whole table. The
exact same string therefore runs unchanged through the RDS Data API
(`execute-statement --sql "..."`) or through psycopg (`conn.execute(sql)`) --
`tests/integration/test_backfill_decision_records_sql.py` runs these same
`sql_stats_*()` / `sql_insert_*()` return values via psycopg against real
Postgres; nothing here needs a translation layer.

Usage (dry run, prints counts, writes nothing):
    AWS_PROFILE=fieldsight-deployer python scripts/backfill_decision_records.py

Usage (writes, inside one transaction, committed once at the end):
    AWS_PROFILE=fieldsight-deployer python scripts/backfill_decision_records.py --apply

`export MSYS_NO_PATHCONV=1` first in Git Bash if you pass a `--cluster`/`--secret`
ARN on the command line (BUG-28/42) -- the defaults below need no such export.
`--database fieldsight` (prod) is refused unless `--allow-prod` is also given,
checked before any `aws` call is made, same posture as
`scripts/jev_eval/export_labels.py`.

Task 7 does NOT run this against any AWS database -- that is Task 8's job.
"""
from __future__ import annotations

import sys as _sys
from pathlib import Path as _Path

# Bootstrap: see scripts/jev_eval/export_labels.py's identical block for why --
# only pytest puts src/ and the repo root on sys.path.
_REPO_ROOT = _Path(__file__).resolve().parents[1]
for _p in (_REPO_ROOT, _REPO_ROOT / "src"):
    if str(_p) not in _sys.path:
        _sys.path.insert(0, str(_p))

import argparse
import json
import os
import subprocess
import sys

from scripts.jev_eval.export_labels import decode_field, record_to_dict
from scripts.verify_programme_schema import CLUSTER as _TEST_CLUSTER, SECRET as _TEST_SECRET
from thread_match import MIN_SCORE as _THREAD_MIN_SCORE

DEFAULT_CLUSTER = os.environ.get("DB_CLUSTER_ARN", _TEST_CLUSTER)
DEFAULT_SECRET = os.environ.get("DB_SECRET_ARN", _TEST_SECRET)
DEFAULT_DATABASE = os.environ.get("DB_NAME", "fieldsight_test")
PROD_DATABASE = "fieldsight"
DEFAULT_PROFILE = os.environ.get("AWS_PROFILE", "fieldsight-deployer")
DEFAULT_REGION = os.environ.get("AWS_REGION", "ap-southeast-2")

# The programme_match threshold is a literal per the Task 7 brief (0.70 -- the
# same default lambda_programme_matcher.CONF_MIN carries). The thread threshold
# is imported (thread_match.MIN_SCORE) rather than duplicated, so a future change
# to the live accept bar cannot silently drift from what this backfill stamps.
_PROGRAMME_MATCH_THRESHOLD = 0.70

# ---------------------------------------------------------------------------
# SQL -- pure. No AWS client, no named/%s parameters (see module docstring's
# "SQL" paragraph for why none are needed). One CTE per source, reused by both
# that source's stats (dry-run reporting) query and its INSERT.
# ---------------------------------------------------------------------------

_INSERT_COLUMNS = (
    "company_id", "site_id", "kind", "subject_type", "subject_stable_id",
    "object_ref", "provider", "model", "model_version", "question_set",
    "input_key", "input_hash", "output", "score", "threshold", "auto_outcome",
    "human_outcome", "human_actor", "human_at", "created_at",
)
_INSERT_PREFIX = f"INSERT INTO decision_records ({', '.join(_INSERT_COLUMNS)})\n"

STATS_COLUMNS = ("eligible", "skipped_topic_null", "already_present")


def _already_present(alias: str, kind: str) -> str:
    """`NOT EXISTS`'d against by every INSERT, `EXISTS`'d for the stats query --
    the idempotency key (kind, subject_stable_id, object_ref, created_at), object_ref
    compared IS NOT DISTINCT FROM so a legitimately-NULL object_ref (work_class,
    always; thread, when no earlier candidate could be resolved) matches a NULL
    column rather than matching nothing."""
    return (
        f"EXISTS (SELECT 1 FROM decision_records dr "
        f"WHERE dr.kind='{kind}' AND dr.subject_stable_id = {alias}.topic_id "
        f"AND dr.object_ref IS NOT DISTINCT FROM {alias}.object_ref "
        f"AND dr.created_at = {alias}.created_at)"
    )


# ---- programme_match (programme_progress_suggestions) ---------------------

_PPS_CTE = """WITH pps_candidates AS (
    SELECT pps.id AS source_id,
           pps.topic_id,
           pps.site_id,
           si.company_id,
           pps.task_id::text AS object_ref,
           pps.confidence AS score,
           pps.state AS human_outcome,
           pps.decided_at AS human_at,
           pps.created_at,
           hu.id AS human_actor,
           jsonb_build_object(
               'task_id', pps.task_id,
               'confidence', pps.confidence,
               'suggested_status', pps.suggested_status,
               'suggested_progress', pps.suggested_progress
           ) AS output
    FROM programme_progress_suggestions pps
    JOIN sites si ON si.id = pps.site_id
    LEFT JOIN users hu ON hu.id = pps.decided_by
    WHERE pps.state IN ('confirmed', 'rejected')
)
"""


def sql_stats_programme_match() -> str:
    already = _already_present("pps_candidates", "programme_match")
    return (
        _PPS_CTE +
        "SELECT "
        "count(*) FILTER (WHERE topic_id IS NOT NULL) AS eligible, "
        "count(*) FILTER (WHERE topic_id IS NULL) AS skipped_topic_null, "
        f"count(*) FILTER (WHERE topic_id IS NOT NULL AND {already}) AS already_present "
        "FROM pps_candidates"
    )


def sql_insert_programme_match() -> str:
    already = _already_present("pps_candidates", "programme_match")
    select_list = (
        "company_id, site_id, 'programme_match', 'topic', topic_id, object_ref, "
        "'legacy', NULL, NULL, NULL, NULL, NULL, "
        f"output, score, {_PROGRAMME_MATCH_THRESHOLD}, 'accepted', "
        "human_outcome, human_actor, human_at, created_at"
    )
    return (
        _PPS_CTE + _INSERT_PREFIX +
        f"SELECT {select_list} FROM pps_candidates "
        f"WHERE topic_id IS NOT NULL AND NOT {already} "
        "RETURNING id"
    )


# ---- thread (topic_thread_suggestions) -------------------------------------

_TTS_CTE = """WITH tts_candidates AS (
    SELECT tts.id AS source_id,
           tts.topic_id,
           t.site_id,
           si.company_id,
           COALESCE(
               tts.parent_topic_id::text,
               (SELECT dr.object_ref FROM decision_records dr
                WHERE dr.kind = 'thread' AND dr.subject_type = 'topic'
                  AND dr.subject_stable_id = tts.topic_id
                  AND dr.auto_outcome = 'accepted'
                ORDER BY dr.created_at DESC LIMIT 1)
           ) AS object_ref,
           tts.score,
           tts.status AS human_outcome,
           tts.resolved_at AS human_at,
           tts.created_at,
           hu.id AS human_actor,
           jsonb_build_object(
               'match_score', tts.score,
               'gap_days', tts.gap_days,
               'thread_id', tts.thread_id
           ) AS output
    FROM topic_thread_suggestions tts
    JOIN topics t ON t.id = tts.topic_id
    JOIN sites si ON si.id = t.site_id
    LEFT JOIN users hu ON hu.id::text = tts.resolved_by
    WHERE tts.status IN ('confirmed', 'rejected')
)
"""


def sql_stats_thread() -> str:
    # topic_id is NOT NULL (migration 0032's FK) -- there is no analogue of
    # programme_match's "topic_id went NULL" skip case here, but the column is
    # kept in the SELECT so every source's stats row has the same shape.
    already = _already_present("tts_candidates", "thread")
    return (
        _TTS_CTE +
        "SELECT count(*) AS eligible, 0 AS skipped_topic_null, "
        f"count(*) FILTER (WHERE {already}) AS already_present "
        "FROM tts_candidates"
    )


def sql_insert_thread() -> str:
    already = _already_present("tts_candidates", "thread")
    select_list = (
        "company_id, site_id, 'thread', 'topic', topic_id, object_ref, "
        "'legacy', NULL, NULL, NULL, NULL, NULL, "
        f"output, score, {_THREAD_MIN_SCORE}, 'accepted', "
        "human_outcome, human_actor, human_at, created_at"
    )
    return (
        _TTS_CTE + _INSERT_PREFIX +
        f"SELECT {select_list} FROM tts_candidates "
        f"WHERE NOT {already} "
        "RETURNING id"
    )


# ---- work_class (classification_feedback) ----------------------------------

_CF_CTE = """WITH cf_candidates AS (
    SELECT cf.id AS source_id,
           cf.topic_id,
           t.site_id,
           cf.company_id,
           NULL::text AS object_ref,
           cf.classifier_confidence AS score,
           CASE cf.human_verdict
               WHEN 'confirm_non_work' THEN 'confirmed'
               ELSE 'rejected'
           END AS human_outcome,
           cf.created_at AS human_at,
           cf.created_at,
           hu.id AS human_actor,
           jsonb_build_object(
               'classifier_verdict', cf.classifier_verdict,
               'human_verdict', cf.human_verdict
           ) AS output
    FROM classification_feedback cf
    LEFT JOIN topics t ON t.id = cf.topic_id
    LEFT JOIN users hu ON hu.id = cf.actor_user_id
    WHERE cf.human_verdict IN ('confirm_non_work', 'reject_is_work', 'missed_personal')
)
"""


def sql_stats_work_class() -> str:
    # classification_feedback.topic_id is NOT NULL (no FK, but never absent) --
    # same "no skip case, kept for shape parity" note as sql_stats_thread.
    already = _already_present("cf_candidates", "work_class")
    return (
        _CF_CTE +
        "SELECT count(*) AS eligible, 0 AS skipped_topic_null, "
        f"count(*) FILTER (WHERE {already}) AS already_present "
        "FROM cf_candidates"
    )


def sql_insert_work_class() -> str:
    already = _already_present("cf_candidates", "work_class")
    select_list = (
        "company_id, site_id, 'work_class', 'topic', topic_id, object_ref, "
        "'legacy', NULL, NULL, NULL, NULL, NULL, "
        "output, score, NULL, 'accepted', "
        "human_outcome, human_actor, human_at, created_at"
    )
    return (
        _CF_CTE + _INSERT_PREFIX +
        f"SELECT {select_list} FROM cf_candidates "
        f"WHERE NOT {already} "
        "RETURNING id"
    )


# One entry per source: (report key, stats sql fn, insert sql fn, has a
# skip-for-null-topic bucket that can be non-zero).
SOURCES = (
    ("programme_match", sql_stats_programme_match, sql_insert_programme_match, True),
    ("thread", sql_stats_thread, sql_insert_thread, False),
    ("work_class", sql_stats_work_class, sql_insert_work_class, False),
)


# ---------------------------------------------------------------------------
# I/O: the RDS Data API runner. Everything above this line is pure and
# unit-tested without a database or the aws CLI (see module docstring).
# ---------------------------------------------------------------------------

def _aws(args: list):
    """Same UTF-8-forcing subprocess wrapper as scripts/jev_eval/export_labels.py's
    `_aws` -- see its docstring for why (BUG-35: a non-ASCII byte, e.g. a
    programme task name, mis-decodes under an inherited GBK codepage on a
    Chinese-locale Windows box)."""
    env = dict(os.environ)
    env["PYTHONUTF8"] = "1"
    env["PYTHONIOENCODING"] = "utf-8"
    env["AWS_CLI_FILE_ENCODING"] = "UTF-8"
    result = subprocess.run(["aws"] + args, capture_output=True, env=env)
    if result.returncode != 0:
        stderr = (result.stderr or b"").decode("utf-8", errors="replace")
        raise RuntimeError(stderr.strip()[:4000])
    if not result.stdout or not result.stdout.strip():
        return {}
    try:
        stdout = result.stdout.decode("utf-8")
    except UnicodeDecodeError as exc:
        cmd = " ".join(args[:2]) if len(args) >= 2 else (args[0] if args else "")
        raise RuntimeError(f"aws {cmd} returned non-UTF-8 stdout ({exc})") from exc
    return json.loads(stdout)


def _begin_transaction(cluster: str, secret: str, database: str, profile: str,
                       region: str) -> str:
    return _aws([
        "rds-data", "begin-transaction",
        "--resource-arn", cluster, "--secret-arn", secret,
        "--database", database, "--profile", profile, "--region", region,
        "--output", "json",
    ])["transactionId"]


def _execute(cluster: str, secret: str, database: str, tx: str, sql: str,
            profile: str, region: str) -> dict:
    return _aws([
        "rds-data", "execute-statement",
        "--resource-arn", cluster, "--secret-arn", secret,
        "--database", database, "--transaction-id", tx, "--sql", sql,
        "--profile", profile, "--region", region, "--output", "json",
    ])


def _commit_transaction(cluster: str, secret: str, tx: str, profile: str,
                        region: str) -> dict:
    return _aws([
        "rds-data", "commit-transaction",
        "--resource-arn", cluster, "--secret-arn", secret,
        "--transaction-id", tx, "--profile", profile, "--region", region,
        "--output", "json",
    ])


def _rollback(cluster: str, secret: str, tx: str, profile: str, region: str) -> dict:
    return _aws([
        "rds-data", "rollback-transaction",
        "--resource-arn", cluster, "--secret-arn", secret,
        "--transaction-id", tx, "--profile", profile, "--region", region,
        "--output", "json",
    ])


def _parse_stats(result: dict) -> dict:
    records = result.get("records", [])
    if not records:
        return {c: 0 for c in STATS_COLUMNS}
    rec = record_to_dict(STATS_COLUMNS, records[0])
    return {c: (rec.get(c) or 0) for c in STATS_COLUMNS}


def run_backfill(*, cluster: str, secret: str, database: str, apply: bool,
                 profile: str = DEFAULT_PROFILE, region: str = DEFAULT_REGION) -> dict:
    """Runs the stats query for every source, then (only if `apply`) that
    source's INSERT, all inside ONE transaction -- committed once at the very
    end, only when `apply`; rolled back in every other case, including an
    exception partway through (the `committed` flag below is set to True only
    right after a successful commit-transaction call, so the `finally` rolls
    back on any earlier failure too)."""
    tx = _begin_transaction(cluster, secret, database, profile, region)
    committed = False
    try:
        report: dict = {}
        for name, stats_fn, insert_fn, has_skip_bucket in SOURCES:
            stats = _parse_stats(
                _execute(cluster, secret, database, tx, stats_fn(), profile, region))
            eligible = stats["eligible"]
            skipped = stats["skipped_topic_null"] if has_skip_bucket else 0
            already_present = stats["already_present"]
            would_insert = eligible - already_present
            entry = {
                "eligible": eligible,
                "skipped_topic_null": skipped,
                "already_present": already_present,
            }
            if apply:
                inserted = len(
                    _execute(cluster, secret, database, tx, insert_fn(), profile, region)
                    .get("records", []))
                entry["inserted"] = inserted
            else:
                entry["would_insert"] = would_insert
            report[name] = entry
        if apply:
            _commit_transaction(cluster, secret, tx, profile, region)
            committed = True
        return report
    finally:
        if not committed:
            _rollback(cluster, secret, tx, profile, region)


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--cluster", default=DEFAULT_CLUSTER)
    parser.add_argument("--secret", default=DEFAULT_SECRET)
    parser.add_argument("--database", default=DEFAULT_DATABASE)
    parser.add_argument("--allow-prod", action="store_true",
                        help="required to target --database fieldsight")
    parser.add_argument("--profile", default=DEFAULT_PROFILE)
    parser.add_argument("--region", default=DEFAULT_REGION)
    parser.add_argument("--apply", action="store_true",
                        help="commit the inserts; default is a dry run "
                             "(rolled back, writes nothing)")
    args = parser.parse_args(argv)

    if args.database == PROD_DATABASE and not args.allow_prod:
        print(f"refusing --database {PROD_DATABASE!r} without --allow-prod "
              "(this is prod)", file=sys.stderr)
        return 2

    print(f"database: {args.database}  mode: {'APPLY' if args.apply else 'DRY RUN'}",
          file=sys.stderr)
    report = run_backfill(cluster=args.cluster, secret=args.secret,
                          database=args.database, apply=args.apply,
                          profile=args.profile, region=args.region)
    print(json.dumps(report, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    sys.exit(main())
