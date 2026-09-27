"""Export labelled decision sets for the Jev shadow evaluation (Track A, Task 1).

READ-ONLY. Every query below is a SELECT (or a WITH ... SELECT), run inside a
begin-transaction / execute-statement / rollback-transaction cycle (the same
RDS Data API pattern `scripts/verify_programme_schema.py` uses for its own
read-only probes) so the script cannot write by accident even though nothing
here needs to. The transaction is ALWAYS rolled back, in a `finally`, whether
the export succeeded or one of the execute-statement calls raised.

What gets written, all under `scripts/fixtures/jev_eval/` (gitignored --
only `counts.json` is tracked):

- `programme_match.jsonl`, `threads.jsonl`, `work_class.jsonl` -- one row per
  decided (confirmed/rejected) human label, in the shape
  `{"set", "id", "label", "features", "site_id", "company_id", "decided_at",
  "baseline"}`. `features` is the exact nested shape `scripts/jev_eval/state.py`
  consumes; `baseline` carries the stored gate output for the baseline arm and
  is never fed to `build_state` (kept out of `features` on purpose --
  `match_evidence` in particular can carry quoted transcript text and is
  never selected at all).
- `name_aliases.json` -- the `{wrong_term, right_term, kind}` rows (plus an
  optional `alias_group` key -- see `build_alias_rows`) `build_state`'s
  masker consumes, for the companies actually present in the export: active
  `name_aliases` rows; for each user with a last name, three `kind="person"`
  rows sharing one `alias_group` (first name, last name and full name, all
  mapping to the SAME placeholder -- fix wave 2, closing the "surname only"
  gap where the full name still leaked); for a user with no last name, one
  ungrouped row mapping their name to itself; and one `kind="company"` row
  each for every company name, every site name, and every programme task
  name of those companies' sites (protects them from the generic two-word
  masking pass -- fix wave 2 I1: previously only the recorder's own company
  was protected this way).
- `counts.json` -- the only fixture file this repo commits. Per set: `n`,
  `positives`, `negatives`, `descriptive_only` (n < 30), `database`,
  `exported_at`, and `exclusions` (rows dropped and why). Top-level
  `route_note` records that this is a read path.

Database: `fieldsight_test` by default. `--database fieldsight` (prod) is
refused unless `--allow-prod` is also given -- checked before any `aws` call
is made, so a typo cannot touch prod's Data API even read-only.

Threads' `earlier` side (controller ruling): `topic_thread_suggestions` has
`topic_id` (the later topic) and exactly one of `thread_id` / `parent_topic_id`
(migration 0032's CHECK). For `parent_topic_id`, `earlier` is that topic,
directly. For `thread_id`, the row that set it (`lambda_item_writer.py`'s
`_suggest_threads_inner`) picked `thread_id` because the CANDIDATE it scored
highest (`best`, a row from `threads.candidate_corpus`) already had a
`thread_id` of its own -- so the topic actually compared against is not
recoverable from the suggestion row itself, only the thread it belongs to is.
This export uses the EARLIEST topic on that thread (by `report_date`) as a
stand-in for "the earlier restatement a human would see when confirming this
link" -- the topic that anchored the thread in the first place. In practice
this branch is inert on today's data: prod holds zero rows in `topic_threads`
(2026-09-01 count), so no topic has ever had a `thread_id` to be picked as a
`best` candidate, and the code handles it defensively rather than because it
is exercised.

Both `topic_id` and `parent_topic_id` on `topic_thread_suggestions` are
`ON DELETE CASCADE` (migration 0032). Deleting either topic deletes the whole
suggestion row, not just one side's join -- so a hard-deleted "later" topic
makes its label vanish from the database entirely, uncountable from here;
`counts.json` records that as `"deleted_topic_rows": "invisible (FK cascades)"`
rather than a number it cannot actually produce. `classification_feedback.topic_id`
(work_class) carries no FK at all, so a topic that later disappears leaves a
feedback row whose join to `topics` comes back empty -- that case IS visible
and IS counted (`exclusions.topic_missing`).

Separately from that hard-delete/FK story: a CUSTOMER-FACING delete
(spec docs/superpowers/specs/2026-08-14-user-deletes-a-recording.md) is soft
-- the topic row survives, a `redactions` tombstone just hides it -- so none
of the above catches it. Every query here applies the repo's own visibility
predicates (`repositories.programme_suggestions.VISIBLE`,
`deleted_predicates.visible_topics_predicate`), imported rather than copied,
over every `topics`/`programme_progress_suggestions` alias: `sql_programme_match`
(the suggestion row itself, in `WHERE`), `sql_threads` (`t` in `WHERE`, `p`
and the `earliest_thread_topic` CTE's own topic scan, both ANDed into their
joins/WHERE the same way `repositories/threads.py` does), and `sql_work_class`
(`t`, ANDed into its `LEFT JOIN`). Where that predicate makes a LEFT-JOINed
row come back NULL (`p`, `earliest_thread_topic`, work_class's `t`), the
existing `orphaned_parent`/`orphaned_thread`/`topic_missing` exclusion
buckets count it for free. Where it excludes the primary row directly
(`sql_programme_match`, and threads' own `t`), the row is simply absent from
the output and NOT separately counted -- doing so would need a second query
per set, which the brief allows skipping in favour of stating it plainly, as
this paragraph does.
"""
from __future__ import annotations

import argparse
import json
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

from deleted_predicates import visible_topics_predicate
from repositories.programme_suggestions import VISIBLE as _PROGRAMME_MATCH_VISIBLE
from scripts.verify_programme_schema import CLUSTER, SECRET

DEFAULT_DATABASE = "fieldsight_test"
PROD_DATABASE = "fieldsight"
DEFAULT_PROFILE = "fieldsight-deployer"
DEFAULT_REGION = "ap-southeast-2"

FIXTURES_DIR = Path(__file__).resolve().parent.parent / "fixtures" / "jev_eval"

ROUTE_NOTE = "labels exported read-only; no rows written"

MIN_N_FOR_CONCLUSIONS = 30

SETS = ("programme_match", "threads", "work_class")


# ---------------------------------------------------------------------------
# SQL, one function per set -- pure, no I/O. Every string starts with SELECT
# or WITH (asserted by tests/unit/test_jev_eval_export_shapes.py).
# ---------------------------------------------------------------------------

PROGRAMME_MATCH_COLUMNS = (
    "id", "state", "decided_at", "report_date", "topic_title", "topic_summary",
    "task_name", "task_status_before", "task_progress_before",
    "suggested_status", "suggested_progress", "confidence", "task_id",
    "site_id", "company_id",
)


def sql_programme_match() -> str:
    """`programme_progress_suggestions` already holds the topic text the
    matcher saw (task/programme rulings, `programme_suggestions.py:_COLS`),
    so no join to `topics` is needed and topic-id churn is irrelevant here.

    Uses `repositories.programme_suggestions.VISIBLE` -- the SAME predicate
    `list_for_site`/`get` apply, imported rather than copied so a future
    change to what "deleted" means for this table reaches this export too.
    It hardcodes the unaliased table name (matching how the repo itself uses
    it), so this table is NOT aliased here. `VISIBLE` is a `NOT EXISTS`
    covering both the topic-tombstone arm and the source/recording-tombstone
    arm -- see its comment in `repositories/programme_suggestions.py` for why
    a suggestion's FROZEN COPY of a deleted topic's words needs both. Applied
    in `WHERE`, exactly where the repo's own callers put it: a redacted row
    is excluded and NOT separately counted here (would need a second query
    per set; the brief allows stating that instead)."""
    return (
        "SELECT programme_progress_suggestions.id, "
        "programme_progress_suggestions.state, "
        "programme_progress_suggestions.decided_at, "
        "programme_progress_suggestions.report_date, "
        "programme_progress_suggestions.topic_title, "
        "programme_progress_suggestions.topic_summary, "
        "programme_progress_suggestions.task_name, "
        "programme_progress_suggestions.task_status_before, "
        "programme_progress_suggestions.task_progress_before, "
        "programme_progress_suggestions.suggested_status, "
        "programme_progress_suggestions.suggested_progress, "
        "programme_progress_suggestions.confidence, "
        "programme_progress_suggestions.task_id, "
        "programme_progress_suggestions.site_id, "
        "si.company_id "
        "FROM programme_progress_suggestions "
        "JOIN sites si ON si.id = programme_progress_suggestions.site_id "
        "WHERE programme_progress_suggestions.state IN ('confirmed','rejected') "
        f"AND {_PROGRAMME_MATCH_VISIBLE}"
    )


THREADS_COLUMNS = (
    "id", "status", "score", "gap_days", "resolved_at",
    "later_title", "later_summary", "later_date", "site_id", "company_id",
    "sugg_thread_id", "sugg_parent_topic_id",
    "parent_title", "parent_summary", "parent_date",
    "thread_title", "thread_summary", "thread_date",
)


def sql_threads() -> str:
    """See the module docstring for the `earlier` choice. `earliest_thread_topic`
    picks, per thread, the topic with the lowest `report_date` (ties broken by
    `id` for determinism) -- the topic that anchored the thread.

    Visibility uses `deleted_predicates.visible_topics_predicate`, imported
    (not copied) -- the same predicate `repositories/threads.py` applies to
    every read of `topics` in this feature (`candidate_corpus`, `list_pending`,
    `thread_facts`), so a future change to what "deleted" means reaches this
    export too:

    - `t` (the LATER topic, `topic_id`): visibility is ANDed into `WHERE`,
      the same place `list_pending` puts its own `_VISIBLE_T` check on `t`. A
      row whose later topic was deleted is excluded but not separately
      counted (would need a second query per set).
    - `p` (`parent_topic_id`): visibility is ANDed into the `LEFT JOIN`'s
      `ON` clause, exactly like `list_pending`'s
      `LEFT JOIN topics p ON p.id = s.parent_topic_id AND {_VISIBLE_P}`. A
      deleted parent makes `p` come back NULL, which the `orphaned_parent`
      mapper branch already catches and counts -- no new exclusion bucket
      needed.
    - `earliest_thread_topic`: visibility is ANDed into the CTE's own
      `WHERE`, mirroring `candidate_corpus`'s pattern -- a deleted topic is
      never a candidate to be picked as a thread's earliest. If deletion
      empties a thread entirely, `et` comes back NULL and the existing
      `orphaned_thread` mapper branch counts it."""
    visible_t = visible_topics_predicate("t")
    visible_p = visible_topics_predicate("p")
    visible_et = visible_topics_predicate("t")
    return (
        "WITH earliest_thread_topic AS ("
        "    SELECT DISTINCT ON (thread_id) thread_id, id, title, summary, report_date "
        "    FROM topics t "
        f"    WHERE thread_id IS NOT NULL AND {visible_et} "
        "    ORDER BY thread_id, report_date ASC, id ASC"
        ") "
        "SELECT s.id, s.status, s.score, s.gap_days, s.resolved_at, "
        "       t.title AS later_title, t.summary AS later_summary, "
        "       t.report_date AS later_date, t.site_id AS site_id, "
        "       si.company_id AS company_id, "
        "       s.thread_id AS sugg_thread_id, s.parent_topic_id AS sugg_parent_topic_id, "
        "       p.title AS parent_title, p.summary AS parent_summary, "
        "       p.report_date AS parent_date, "
        "       et.title AS thread_title, et.summary AS thread_summary, "
        "       et.report_date AS thread_date "
        "FROM topic_thread_suggestions s "
        "JOIN topics t ON t.id = s.topic_id "
        "JOIN sites si ON si.id = t.site_id "
        f"LEFT JOIN topics p ON p.id = s.parent_topic_id AND {visible_p} "
        "LEFT JOIN earliest_thread_topic et ON et.thread_id = s.thread_id "
        "WHERE s.status IN ('confirmed','rejected') "
        f"AND {visible_t}"
    )


WORK_CLASS_COLUMNS = (
    "id", "human_verdict", "category", "classifier_verdict",
    "classifier_confidence", "created_at", "topic_id", "title", "summary",
    "site_id", "company_id",
)


def sql_work_class() -> str:
    """`classification_feedback.topic_id` carries no FK, so a topic that has
    since disappeared leaves the LEFT JOIN empty rather than cascading the
    feedback row away -- that case is visible and counted (`topic_missing`).

    Visibility uses `deleted_predicates.visible_topics_predicate("t")`
    (imported, not copied), ANDed into the `topics` `LEFT JOIN`'s `ON`
    clause -- a soft-deleted topic then comes back NULL exactly like a
    physically-missing one, and the existing `topic_missing` mapper branch
    (`rec["topic_id"] is None`) counts both without a second query or a new
    exclusion bucket."""
    visible_t = visible_topics_predicate("t")
    return (
        "SELECT cf.id, cf.human_verdict, "
        "       COALESCE(cf.topic_category, t.category) AS category, "
        "       cf.classifier_verdict, cf.classifier_confidence, cf.created_at, "
        "       t.id AS topic_id, t.title, t.summary, t.site_id, si.company_id "
        "FROM classification_feedback cf "
        f"LEFT JOIN topics t ON t.id = cf.topic_id AND {visible_t} "
        "LEFT JOIN sites si ON si.id = t.site_id "
        "WHERE cf.human_verdict IN ('confirm_non_work','reject_is_work','missed_personal')"
    )


NAME_ALIASES_COLUMNS = ("wrong_term", "right_term", "kind")


def sql_name_aliases(company_ids) -> str:
    """`company_ids` come from rows this same transaction already read back
    from the Data API (trusted uuid strings), not user input -- string
    interpolation here matches the practice in
    `scripts/verify_programme_schema.py`."""
    ids = ",".join(f"'{cid}'" for cid in company_ids)
    return (
        "SELECT wrong_term, right_term, kind FROM name_aliases "
        f"WHERE status='active' AND company_id IN ({ids})"
    )


USERS_COLUMNS = ("first_name", "last_name")


def sql_users(company_ids) -> str:
    ids = ",".join(f"'{cid}'" for cid in company_ids)
    return f"SELECT first_name, last_name FROM users WHERE company_id IN ({ids})"


COMPANIES_COLUMNS = ("name",)


def sql_companies(company_ids) -> str:
    ids = ",".join(f"'{cid}'" for cid in company_ids)
    return f"SELECT name FROM companies WHERE id IN ({ids})"


SITES_COLUMNS = ("name",)


def sql_sites(company_ids) -> str:
    """Site names for the exported companies, protected from the generic
    two-word masking pass the same way company names already are (fix wave 2
    I1 -- previously only company names were protected, so subcontractor and
    site names were masked).

    `archived_at IS NULL` mirrors the default guard
    `repositories/sites.py`'s `list_company_sites`/`list_all_sites` apply --
    an archived site's name is still a legitimate masking-protection term,
    but this follows the repo's own "current" convention rather than
    diverging from it silently. `sites` has no `redactions`-style tombstone
    (that mechanism only covers `topics`/`recordings`, see
    `deleted_predicates.py`), so nothing from that module applies here."""
    ids = ",".join(f"'{cid}'" for cid in company_ids)
    return f"SELECT name FROM sites WHERE company_id IN ({ids}) AND archived_at IS NULL"


PROGRAMME_TASK_NAMES_COLUMNS = ("name",)


def sql_programme_task_names(company_ids) -> str:
    """Programme task names for the exported companies' sites, protected
    from the generic two-word masking pass the same way company/site names
    are (fix wave 2 I1 -- task names are exactly the signal the eval's
    programme_match set exists to compare).

    `removed_in_version IS NULL` mirrors the guard `repositories/
    programme_tasks.py` applies everywhere it reads "current" tasks (the
    `idx_ptasks_window` index, `list_tasks`'s default) -- a task superseded
    by a later import is not a term this export needs to protect. If a
    company has no programmes/tasks at all, this simply returns zero rows
    (skip quietly, per the brief) -- there is no separate "programme data
    absent" branch to write. `programme_tasks`/`sites`/`programmes` carry no
    `redactions`-style tombstone (see `sql_sites` above), so nothing from
    `deleted_predicates` applies here either."""
    ids = ",".join(f"'{cid}'" for cid in company_ids)
    return (
        "SELECT DISTINCT pt.name FROM programme_tasks pt "
        "JOIN programmes p ON p.id = pt.programme_id "
        "JOIN sites s ON s.id = p.site_id "
        f"WHERE s.company_id IN ({ids}) AND pt.removed_in_version IS NULL"
    )


# ---------------------------------------------------------------------------
# RDS Data API record decoding -- pure.
# ---------------------------------------------------------------------------

def decode_field(value):
    """One Data API typed-value dict -> a plain Python value."""
    if not isinstance(value, dict):
        return value
    if value.get("isNull"):
        return None
    for key in ("stringValue", "longValue", "doubleValue", "booleanValue"):
        if key in value:
            return value[key]
    return None


def record_to_dict(columns, record) -> dict:
    """One Data API record (a list of typed-value dicts, positional) plus the
    column names in the same order as the SELECT -> a plain dict."""
    return {col: decode_field(field) for col, field in zip(columns, record)}


# ---------------------------------------------------------------------------
# Row mappers -- pure. One Data API record (already decoded to a plain dict)
# -> one output row, or an exclusion marker `{"_excluded": reason}`.
# ---------------------------------------------------------------------------

_LABEL_PROGRAMME_MATCH = {"confirmed": "yes", "rejected": "no"}
_LABEL_THREADS = {"confirmed": "yes", "rejected": "no"}
_LABEL_WORK_CLASS = {
    "confirm_non_work": "yes",
    "missed_personal": "yes",
    "reject_is_work": "no",
}


def map_programme_match_row(rec: dict) -> dict:
    label = _LABEL_PROGRAMME_MATCH.get(rec.get("state"))
    if label is None:
        return {"_excluded": "unmapped_state"}
    features = {
        "observation": {
            "title": rec.get("topic_title"),
            "summary": rec.get("topic_summary"),
            "date": rec.get("report_date"),
        },
        "task": {
            "name": rec.get("task_name"),
            "status": rec.get("task_status_before"),
            "progress_pct": rec.get("task_progress_before"),
        },
    }
    baseline = {
        "confidence": rec.get("confidence"),
        "suggested_status": rec.get("suggested_status"),
        "suggested_progress": rec.get("suggested_progress"),
        "task_id": rec.get("task_id"),
    }
    return {
        "set": "programme_match",
        "id": rec.get("id"),
        "label": label,
        "features": features,
        "site_id": rec.get("site_id"),
        "company_id": rec.get("company_id"),
        "decided_at": rec.get("decided_at"),
        "baseline": baseline,
    }


def map_threads_row(rec: dict) -> dict:
    label = _LABEL_THREADS.get(rec.get("status"))
    if label is None:
        return {"_excluded": "unmapped_status"}

    if rec.get("sugg_parent_topic_id") is not None:
        if rec.get("parent_title") is None:
            return {"_excluded": "orphaned_parent"}
        earlier = {
            "title": rec.get("parent_title"),
            "summary": rec.get("parent_summary"),
            "date": rec.get("parent_date"),
        }
    elif rec.get("sugg_thread_id") is not None:
        if rec.get("thread_title") is None:
            return {"_excluded": "orphaned_thread"}
        earlier = {
            "title": rec.get("thread_title"),
            "summary": rec.get("thread_summary"),
            "date": rec.get("thread_date"),
        }
    else:
        # migration 0032's CHECK forbids this; defensive only.
        return {"_excluded": "malformed_target"}

    features = {
        "earlier": earlier,
        "later": {
            "title": rec.get("later_title"),
            "summary": rec.get("later_summary"),
            "date": rec.get("later_date"),
        },
        "gap_days": rec.get("gap_days"),
    }
    baseline = {"score": rec.get("score")}
    return {
        "set": "threads",
        "id": rec.get("id"),
        "label": label,
        "features": features,
        "site_id": rec.get("site_id"),
        "company_id": rec.get("company_id"),
        "decided_at": rec.get("resolved_at"),
        "baseline": baseline,
    }


def map_work_class_row(rec: dict) -> dict:
    if rec.get("topic_id") is None:
        return {"_excluded": "topic_missing"}
    label = _LABEL_WORK_CLASS.get(rec.get("human_verdict"))
    if label is None:
        return {"_excluded": "unmapped_verdict"}
    features = {
        "title": rec.get("title"),
        "summary": rec.get("summary"),
        "category": rec.get("category"),
    }
    baseline = {
        "classifier_verdict": rec.get("classifier_verdict"),
        "classifier_confidence": rec.get("classifier_confidence"),
    }
    return {
        "set": "work_class",
        "id": rec.get("id"),
        "label": label,
        "features": features,
        "site_id": rec.get("site_id"),
        "company_id": rec.get("company_id"),
        "decided_at": rec.get("created_at"),
        "baseline": baseline,
    }


MAPPERS = {
    "programme_match": map_programme_match_row,
    "threads": map_threads_row,
    "work_class": map_work_class_row,
}


# ---------------------------------------------------------------------------
# Alias fixture builder -- pure.
# ---------------------------------------------------------------------------

def _clean_term(term):
    if term is None:
        return None
    term = str(term).strip()
    return term or None


def _is_word_char(ch: str) -> bool:
    return ch.isalnum() or ch == "_"


def _anchorable(term: str) -> bool:
    """A term `state.py`'s `\\b...\\b` regex can actually anchor on: it must
    start and end with a word character (single-character terms count)."""
    if not term:
        return False
    return _is_word_char(term[0]) and _is_word_char(term[-1])


def build_alias_rows(alias_rows: list, user_rows: list, company_rows: list,
                      site_rows: list | None = None,
                      task_rows: list | None = None) -> list:
    """(a) active `name_aliases` rows as-is, (b) for each user WITH a last
    name, three person rows sharing one `alias_group` (first name, last
    name, full name -> first name) so all three collapse to the SAME
    placeholder in `state.py` -- closing the "surname only" gap where the
    full name mapped to the first name but the surname and full name
    themselves were never masked; a user with no last name still gets a
    single ungrouped row mapping their name to itself, exactly as before,
    (c) one company row per company name, site name and programme task name.
    De-duplicates on (wrong_term, right_term, kind, alias_group); drops terms
    that are blank after stripping or would not anchor with a word-boundary
    match in `state.py`.

    `alias_group` is omitted from a row entirely when it is `None`, so an
    ungrouped row (every non-person row, and a user with no last name) has
    the exact same shape it always did -- backwards compatible with any
    fixture written before this fix."""
    out = []
    seen = set()

    def _add(wrong, right, kind, group=None):
        wrong = _clean_term(wrong)
        right = _clean_term(right)
        if wrong is None or right is None:
            return
        if not _anchorable(wrong) or not _anchorable(right):
            return
        key = (wrong, right, kind, group)
        if key in seen:
            return
        seen.add(key)
        row = {"wrong_term": wrong, "right_term": right, "kind": kind}
        if group is not None:
            row["alias_group"] = group
        out.append(row)

    for row in alias_rows:
        _add(row.get("wrong_term"), row.get("right_term"), row.get("kind"))

    for index, row in enumerate(user_rows):
        first = _clean_term(row.get("first_name"))
        if not first:
            continue
        last = _clean_term(row.get("last_name"))
        if last:
            group = f"user-{index}"
            _add(first, first, "person", group)
            _add(last, last, "person", group)
            _add(f"{first} {last}", first, "person", group)
        else:
            _add(first, first, "person")

    for row in company_rows:
        name = _clean_term(row.get("name"))
        if name is None:
            continue
        _add(name, name, "company")

    for row in (site_rows or []):
        name = _clean_term(row.get("name"))
        if name is None:
            continue
        _add(name, name, "company")

    for row in (task_rows or []):
        name = _clean_term(row.get("name"))
        if name is None:
            continue
        _add(name, name, "company")

    return out


# ---------------------------------------------------------------------------
# Counts -- pure.
# ---------------------------------------------------------------------------

def summarize_set(set_name: str, rows: list, exclusions: dict, database: str) -> dict:
    n = len(rows)
    positives = sum(1 for r in rows if r["label"] == "yes")
    negatives = sum(1 for r in rows if r["label"] == "no")
    summary = {
        "n": n,
        "positives": positives,
        "negatives": negatives,
        "descriptive_only": n < MIN_N_FOR_CONCLUSIONS,
        "database": database,
        "exported_at": datetime.now(timezone.utc).isoformat(),
        "exclusions": exclusions,
    }
    # Fix wave 3, I5: recomputed from the MERGED rows (this export's fresh DB
    # rows plus any surviving owner rows) so a re-export never drops the
    # breakdown `import_labels.py` had already written.
    if any(r.get("label_source") for r in rows):
        summary["label_source_breakdown"] = _label_source_breakdown(rows)
    return summary


# ---------------------------------------------------------------------------
# I/O: the RDS Data API runner. Everything above this line is pure and
# unit-tested without a database or the aws CLI.
# ---------------------------------------------------------------------------

def _aws(args: list):
    result = subprocess.run(["aws"] + args, capture_output=True, text=True)
    if result.returncode != 0:
        raise RuntimeError(result.stderr.strip()[:4000])
    return json.loads(result.stdout) if result.stdout.strip() else {}


def _begin_transaction(database: str, profile: str, region: str) -> str:
    return _aws([
        "rds-data", "begin-transaction",
        "--resource-arn", CLUSTER, "--secret-arn", SECRET,
        "--database", database, "--profile", profile, "--region", region,
        "--output", "json",
    ])["transactionId"]


def _execute(database: str, tx: str, sql: str, profile: str, region: str) -> dict:
    return _aws([
        "rds-data", "execute-statement",
        "--resource-arn", CLUSTER, "--secret-arn", SECRET,
        "--database", database, "--transaction-id", tx, "--sql", sql,
        "--profile", profile, "--region", region, "--output", "json",
    ])


def _rollback(tx: str, profile: str, region: str) -> dict:
    return _aws([
        "rds-data", "rollback-transaction",
        "--resource-arn", CLUSTER, "--secret-arn", SECRET,
        "--transaction-id", tx, "--profile", profile, "--region", region,
        "--output", "json",
    ])


def _process(result: dict, columns: tuple, mapper) -> tuple:
    rows = []
    exclusions: dict = {}
    for record in result.get("records", []):
        rec = record_to_dict(columns, record)
        mapped = mapper(rec)
        if "_excluded" in mapped:
            reason = mapped["_excluded"]
            exclusions[reason] = exclusions.get(reason, 0) + 1
            continue
        rows.append(mapped)
    return rows, exclusions


def _write_jsonl(path: Path, rows: list) -> None:
    with open(path, "w", encoding="utf-8") as fh:
        for row in rows:
            fh.write(json.dumps(row, sort_keys=True))
            fh.write("\n")


def _load_jsonl(path: Path) -> list:
    if not path.exists():
        return []
    rows = []
    for line in path.read_text(encoding="utf-8").splitlines():
        if line.strip():
            rows.append(json.loads(line))
    return rows


def _label_source_breakdown(rows: list) -> dict:
    breakdown: dict = {}
    for row in rows:
        source = row.get("label_source") or "unknown"
        breakdown[source] = breakdown.get(source, 0) + 1
    return breakdown


def owner_row_topic_ids(row: dict) -> list:
    """Topic ids an owner-labelled row's deletion-predicate recheck needs to
    verify are still visible -- fix wave 4, B9: both topics for `threads`,
    the one topic for `work_class` (`sample_batch.py` writes a `topic_ids`
    list on every owner-labelled batch row for exactly this purpose). A
    DB-sourced row needs no recheck here (its own SELECT already applied the
    visibility predicate this same transaction); an owner row written before
    this fix (no `topic_ids` field) has nothing to recheck and is kept, not
    dropped."""
    if row.get("label_source") != "owner":
        return []
    return [tid for tid in (row.get("topic_ids") or []) if tid]


def apply_owner_deletion_predicate(rows: list, visible_topic_ids: set) -> tuple:
    """`(kept_rows, n_dropped)` -- fix wave 4, B9: re-applies the SAME
    deletion/visibility predicate every DB-sourced row already passed
    through its export SELECT to owner-labelled rows too, on EVERY export
    (not just once, at labelling time). A topic visible when the owner
    labelled it can be soft-deleted later; without this recheck, that row
    would sit in `{set}.jsonl` forever, immune to `merge_export_rows`
    always keeping `label_source == "owner"` rows. A row with no ids to
    check (DB-sourced, or an owner row predating `topic_ids`) is kept
    unconditionally; an owner row is dropped only if ANY of its topic ids is
    no longer visible."""
    kept = []
    n_dropped = 0
    for row in rows:
        ids = owner_row_topic_ids(row)
        if ids and not all(tid in visible_topic_ids for tid in ids):
            n_dropped += 1
            continue
        kept.append(row)
    return kept, n_dropped


def sql_visible_topic_ids(topic_ids) -> str:
    """Which of `topic_ids` are still visible under `visible_topics_predicate`
    -- `topic_ids` are uuids this script already read back from its own
    fixture files (owner-labelled rows), interpolated the same trusted way
    `sql_name_aliases` interpolates its own already-fetched company ids."""
    visible_t = visible_topics_predicate("t")
    ids = ",".join(f"'{_sql_quote(tid)}'" for tid in topic_ids)
    return f"SELECT t.id FROM topics t WHERE t.id IN ({ids}) AND {visible_t}"


def _sql_quote(value) -> str:
    return str(value).replace("'", "''")


def merge_export_rows(existing_rows: list, new_rows: list) -> list:
    """Fix wave 3, I5: `_write_jsonl` used to open `{set}.jsonl` with `"w"`,
    so a re-export silently wiped out every row `import_labels.py` had
    already merged in from an owner-labelled batch. This keeps any existing
    row whose `label_source == "owner"` -- an owner override always wins on
    an id collision -- and replaces every other id with this export's fresh
    DB-sourced row (tagged `label_source: "db"` here; the mapper functions
    never set it, so `test_*_row_has_exact_keys_and_shape`'s exact-key
    assertions on `map_*_row`'s OWN output are unaffected). Sorted by id for
    a stable diff, same convention as `import_labels.merge_rows`."""
    tagged = []
    for row in new_rows:
        row = dict(row)
        row.setdefault("label_source", "db")
        tagged.append(row)
    owner_by_id = {r["id"]: r for r in existing_rows if r.get("label_source") == "owner"}
    merged = {r["id"]: r for r in tagged}
    merged.update(owner_by_id)
    return [merged[key] for key in sorted(merged, key=str)]


def _write_json(path: Path, payload) -> None:
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(payload, fh, indent=2, sort_keys=True)
        fh.write("\n")


def run_export(database: str, *, profile: str = DEFAULT_PROFILE,
                region: str = DEFAULT_REGION, out_dir: Path | None = None) -> dict:
    """Runs all three set queries plus the alias lookups inside ONE
    transaction, always rolled back in `finally` -- this cannot write, even
    if every statement in it is a SELECT.

    Trusts its caller: the `--database fieldsight` / `--allow-prod` gate
    lives in `main()`, not here. Calling `run_export("fieldsight", ...)`
    directly bypasses that gate."""
    out_dir = out_dir or FIXTURES_DIR
    out_dir.mkdir(parents=True, exist_ok=True)

    tx = _begin_transaction(database, profile, region)
    try:
        counts: dict = {}
        company_ids: set = set()

        # Fix wave 3, I5: merge each set's fresh DB rows with any existing
        # file's owner-labelled rows (`merge_export_rows`) BEFORE writing --
        # `_write_jsonl` opening with "w" used to wipe out every row
        # `import_labels.py` had merged in from an owner-labelled batch.
        pm_result = _execute(database, tx, sql_programme_match(), profile, region)
        pm_rows, pm_excl = _process(pm_result, PROGRAMME_MATCH_COLUMNS,
                                    map_programme_match_row)
        pm_existing = _load_jsonl(out_dir / "programme_match.jsonl")
        pm_merged = merge_export_rows(pm_existing, pm_rows)

        th_result = _execute(database, tx, sql_threads(), profile, region)
        th_rows, th_excl = _process(th_result, THREADS_COLUMNS, map_threads_row)
        th_excl.setdefault("deleted_topic_rows", "invisible (FK cascades)")
        th_existing = _load_jsonl(out_dir / "threads.jsonl")
        th_merged = merge_export_rows(th_existing, th_rows)

        wc_result = _execute(database, tx, sql_work_class(), profile, region)
        wc_rows, wc_excl = _process(wc_result, WORK_CLASS_COLUMNS, map_work_class_row)
        wc_existing = _load_jsonl(out_dir / "work_class.jsonl")
        wc_merged = merge_export_rows(wc_existing, wc_rows)

        # Fix wave 4, B9: re-apply the deletion/visibility predicate to
        # OWNER-labelled rows too, on every export -- `merge_export_rows`
        # keeps them unconditionally (that is what protects them from
        # `import_labels.py`'s work being silently overwritten), but a
        # topic visible when the owner labelled it can be soft-deleted
        # later, and nothing else ever rechecks it. One query for every
        # owner row's topic id(s) across all three sets, inside the same
        # rolled-back transaction.
        all_owner_topic_ids: set = set()
        for rows in (pm_merged, th_merged, wc_merged):
            for row in rows:
                all_owner_topic_ids.update(owner_row_topic_ids(row))

        visible_topic_ids: set = set()
        if all_owner_topic_ids:
            vis_result = _execute(
                database, tx, sql_visible_topic_ids(all_owner_topic_ids), profile, region)
            visible_topic_ids = {
                decode_field(r[0]) for r in vis_result.get("records", [])
            }

        pm_merged, pm_owner_dropped = apply_owner_deletion_predicate(pm_merged, visible_topic_ids)
        th_merged, th_owner_dropped = apply_owner_deletion_predicate(th_merged, visible_topic_ids)
        wc_merged, wc_owner_dropped = apply_owner_deletion_predicate(wc_merged, visible_topic_ids)

        _write_jsonl(out_dir / "programme_match.jsonl", pm_merged)
        pm_counts = summarize_set("programme_match", pm_merged, pm_excl, database)
        pm_counts["owner_rows_dropped_deleted"] = pm_owner_dropped
        counts["programme_match"] = pm_counts
        # Fix wave 4, B10: company ids for the alias lookups must include
        # OWNER rows too (`pm_merged`, not just the fresh `pm_rows`) -- an
        # owner-labelled batch can be the only source for a company's rows
        # in this export, and without this its users/sites/tasks would never
        # be protected by `build_alias_rows`.
        company_ids.update(r["company_id"] for r in pm_merged if r.get("company_id"))

        _write_jsonl(out_dir / "threads.jsonl", th_merged)
        th_counts = summarize_set("threads", th_merged, th_excl, database)
        th_counts["owner_rows_dropped_deleted"] = th_owner_dropped
        counts["threads"] = th_counts
        company_ids.update(r["company_id"] for r in th_merged if r.get("company_id"))

        _write_jsonl(out_dir / "work_class.jsonl", wc_merged)
        wc_counts = summarize_set("work_class", wc_merged, wc_excl, database)
        wc_counts["owner_rows_dropped_deleted"] = wc_owner_dropped
        counts["work_class"] = wc_counts
        company_ids.update(r["company_id"] for r in wc_merged if r.get("company_id"))

        alias_rows: list = []
        user_rows: list = []
        company_rows: list = []
        site_rows: list = []
        task_rows: list = []
        if company_ids:
            ar = _execute(database, tx, sql_name_aliases(company_ids), profile, region)
            alias_rows = [record_to_dict(NAME_ALIASES_COLUMNS, r) for r in ar.get("records", [])]
            ur = _execute(database, tx, sql_users(company_ids), profile, region)
            user_rows = [record_to_dict(USERS_COLUMNS, r) for r in ur.get("records", [])]
            cr = _execute(database, tx, sql_companies(company_ids), profile, region)
            company_rows = [record_to_dict(COMPANIES_COLUMNS, r) for r in cr.get("records", [])]
            sr = _execute(database, tx, sql_sites(company_ids), profile, region)
            site_rows = [record_to_dict(SITES_COLUMNS, r) for r in sr.get("records", [])]
            tr = _execute(database, tx, sql_programme_task_names(company_ids), profile, region)
            task_rows = [record_to_dict(PROGRAMME_TASK_NAMES_COLUMNS, r) for r in tr.get("records", [])]

        aliases = build_alias_rows(alias_rows, user_rows, company_rows, site_rows, task_rows)
        _write_json(out_dir / "name_aliases.json", aliases)

        counts["route_note"] = ROUTE_NOTE
        _write_json(out_dir / "counts.json", counts)
        return counts
    finally:
        _rollback(tx, profile, region)


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--database", default=DEFAULT_DATABASE)
    parser.add_argument("--allow-prod", action="store_true",
                        help="required to target --database fieldsight")
    parser.add_argument("--profile", default=DEFAULT_PROFILE)
    parser.add_argument("--region", default=DEFAULT_REGION)
    args = parser.parse_args(argv)

    if args.database == PROD_DATABASE and not args.allow_prod:
        print(
            f"refusing --database {PROD_DATABASE!r} without --allow-prod "
            "(this is prod)",
            file=sys.stderr,
        )
        return 2

    counts = run_export(args.database, profile=args.profile, region=args.region)
    print(json.dumps(counts, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    sys.exit(main())
