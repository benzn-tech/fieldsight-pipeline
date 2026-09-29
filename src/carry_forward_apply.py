"""Carry-forward APPLICATION -- the psycopg/logging/EMF side of Track B's carry-forward
feature, shared by lambda_item_writer (Task 4/5) and lambda_ingest's report path (final wave,
Ruling R19).

Split out of lambda_item_writer.py on Ruling R19: lambda_ingest's report-sourced re-ingest
(`ingest_report`) also supersedes topics on every re-ingest but had no carry-forward, no
decisions/questions dual-write and no orphan metric, so a tick made on a report-sourced item
was silently orphaned. Both lambdas are packaged from `CodeUri: src/` (template.yaml
IngestFunction / ItemWriterFunction), so a plain top-level import works for either -- no new
CFN resource, no IAM.

`carry_forward.py` itself stays PURE (no psycopg, no boto3 -- Ruling R4/R9's design: the
caller reads the "old"/"new" pools out of Aurora, that module only ever sees plain dicts).
This module is the part that touches a real connection and stdout: the per-table loop, the
SAVEPOINT posture, the crash-fallback count, and the OrphanedHumanEdits report -- unchanged
from lambda_item_writer's original Task 4/5/10 implementation, only relocated so a second
caller can reuse it verbatim rather than copy it.
"""
import json
import logging
import os

import carry_forward
from repositories import action_items, findings, topic_decisions, topic_questions

logger = logging.getLogger()


def _carry_forward_one_table(conn, repo, old_topic_ids, new_topic_ids, site_id):
    """Match one child table's retired rows to their replacements and carry stable_id (plus
    any human edit) forward. Returns the number of human-touched OLD rows that found no
    successor -- the caller's contribution to the OrphanedHumanEdits metric/log.

    `repo` is `action_items`, `findings`, `topic_decisions` or `topic_questions` (Track B
    Task 4, extended to the last two by Task 5): all four expose
    list_for_carry_forward(conn, topic_ids, site_id) -> rows with a computed `human_touched`,
    and carry_identity(conn, new_id, old_row), with the identical shape -- which is what lets
    this be one function instead of four near-duplicates."""
    old_rows = repo.list_for_carry_forward(conn, old_topic_ids, site_id)
    if not old_rows:
        return 0
    new_rows = repo.list_for_carry_forward(conn, new_topic_ids, site_id)
    old_by_id = {r["id"]: r for r in old_rows}
    pairs, orphans = carry_forward.match(old_rows, new_rows)
    for old_id, new_id, _how in pairs:
        repo.carry_identity(conn, new_id, old_by_id[old_id])
    return sum(1 for oid in orphans if old_by_id[oid]["human_touched"])


def _carry_forward_children(conn, old_topic_ids, new_topic_ids, site_id, key):
    """Track B Task 4: carry a human's tick, status, reassignment or deadline edit from a row
    this pass just superseded to the row that replaced it, matched by TEXT -- the new row's
    id did not exist when the person made the edit, so stable_id can only be assigned
    afterwards, by finding which new row is "the same" commitment reworded.

    `old_topic_ids` / `new_topic_ids` are both narrowed to `site_id` inside
    `list_for_carry_forward` (Ruling R9): the pool is exactly THIS caller's retired topics and
    THIS pass's own new topics, never another pass's superseded rows and never another site's.
    `key` identifies the pass for the log/metric -- an extraction key for lambda_item_writer,
    a report key for lambda_ingest -- opaque here.

    A SAVEPOINT (`conn.transaction()` nested inside the caller's already-open transaction),
    not a bare try/except (Ruling R10): Postgres aborts the WHOLE enclosing transaction on any
    SQL error, and catching that in Python does not un-abort it. A bare try/except here would
    let the exception stop propagating while every later statement in the caller's pass --
    the photo rebind, the final-email lookup, the commit itself -- started failing too, so a
    carry_forward bug would silently take the whole write down with it. The SAVEPOINT makes
    "degrade, do not abort" (R10) actually true: on failure it rolls back only what
    carry_forward itself did, leaving the topics/action_items/findings already inserted above
    intact and committable, exactly like a matcher bug leaves the topics `_suggest_threads`
    was fed intact in lambda_item_writer today.

    Always reports, including zero (Ruling R5) -- an operator reading "0 for that key" is the
    point of an EMF line with a `key` property, not a lucky silence indistinguishable from a
    producer that never ran. On failure the metric is NOT zero: nothing was carried, so every
    human-touched old row is -- by definition -- an orphan this pass, and the count is
    recomputed by a fallback read after the SAVEPOINT has rolled back (R10: "the metric must
    not read 0 when carry-forward crashed")."""
    try:
        with conn.transaction():
            orphaned = (_carry_forward_one_table(conn, action_items, old_topic_ids,
                                                 new_topic_ids, site_id)
                       + _carry_forward_one_table(conn, findings, old_topic_ids,
                                                  new_topic_ids, site_id)
                       + _carry_forward_one_table(conn, topic_decisions, old_topic_ids,
                                                  new_topic_ids, site_id)
                       + _carry_forward_one_table(conn, topic_questions, old_topic_ids,
                                                  new_topic_ids, site_id))
    except Exception:
        logger.exception(
            "carry_forward failed for %s -- topics were written, no identity was carried "
            "forward this pass", key)
        orphaned = _count_human_touched_old(conn, old_topic_ids, site_id, key)
    _report_orphaned_human_edits(key, orphaned)


def _count_human_touched_old(conn, old_topic_ids, site_id, key):
    """Fallback for `_carry_forward_children`'s except branch: every human-touched OLD row
    across all four child tables, unconditionally -- carry_forward crashed, so none of them
    found a successor this pass, regardless of which table or which pair was mid-flight when
    it failed. Runs AFTER the failed SAVEPOINT has already rolled back, as a plain read that
    was not itself part of what failed.

    Never raises further: if even this cannot run, there is no better number left to report,
    so it logs and answers 0 -- which undercounts, but a metric that also throws would take
    the whole pass down for real, the one outcome R10 exists to prevent."""
    try:
        return sum(1 for repo in (action_items, findings, topic_decisions, topic_questions)
                   for row in repo.list_for_carry_forward(conn, old_topic_ids, site_id)
                   if row["human_touched"])
    except Exception:
        logger.exception(
            "could not count human-touched rows for %s after carry_forward failed -- "
            "OrphanedHumanEdits will under-report for this pass", key)
        return 0


def _report_orphaned_human_edits(key, count):
    """Tell an operator about human-touched rows that carry_forward could not carry.

    Two channels, because they answer two different questions: the WARNING is for someone
    reading THIS pass's logs ("did we lose a tick just now?"), the metric is for someone
    watching the fleet ("is this getting worse?") -- and only the metric is non-zero-
    suppressed, so a dashboard can tell "nothing lost" from "this key never ran" (Ruling R5).

    Embedded Metric Format printed to stdout, not `put_metric_data`: both callers
    (ItemWriterFunction, IngestFunction) are in-VPC with no CloudWatch endpoint (CLAUDE.md
    BUG-36) -- a real API call here would blackhole to a timeout with zero logs, exactly like
    ExtractionBacklogFunction's working put_metric_data call would if it were deployed
    in-VPC. Modelled on lambda_transcribe._emit_failure_metric's `_aws` block. Never raises: a
    metric or a log line that fails must not take an already-committed pass down with it --
    this runs after the transaction line above, by which point the rows are durable either
    way.
    """
    if count:
        logger.warning(
            "carry_forward: %d human-touched rows had no successor (key=%s)",
            count, key)
    try:
        import time as _t
        print(json.dumps({
            "_aws": {
                "Timestamp": int(_t.time() * 1000),
                "CloudWatchMetrics": [{
                    "Namespace": "FieldSight/Pipeline",
                    "Dimensions": [["Stage"]],
                    "Metrics": [{"Name": "OrphanedHumanEdits", "Unit": "Count"}],
                }],
            },
            "Stage": os.environ.get("STAGE", "unknown"),
            "OrphanedHumanEdits": count,
            "key": key,
        }))
    except Exception:
        logger.warning("could not emit the OrphanedHumanEdits metric for %s", key)
