"""Unit: every read path that returns topics or their children carries the LIVE-row arm
(Track B Task 2 -- migration 0071's `topics.superseded_at`).

Task 3, next, stops DELETEing a source key's prior topics on re-extraction and marks them
`superseded_at` instead, leaving the row (and its action_items/findings/chunks children) in
the table. This is the same shape of defect `tests/unit/test_deleted_read_paths.py` was
written for -- a dozen call sites, one predicate, and a read that forgets it passes every
test written today and shows a duplicate the day Task 3 lands. That file covers the
DELETION tombstone; this one covers the separate SUPERSEDED arm (a superseded topic carries
no redaction row -- it is not deleted, it is the stale half of one source key's two passes).

Two proof styles, deliberately not the same one everywhere -- a double records SQL text
without parsing it (search_sql's own tests use the same posture: "assert on text where a
double cannot type-check"):

  * FILTERED / _MARKERS: source-text scanning (mirrors test_deleted_read_paths.py) for the
    read paths that build their predicate INLINE, via a shared helper call or a module-level
    constant computed from one.
  * a small FakeConn section spot-checks the functions that gained a runtime TOGGLE this
    task (`include_superseded`), because text-scanning alone cannot tell "the arm is present"
    from "the arm is present and actually turned off when asked".

Real SQL execution against a real Postgres is `tests/integration/test_superseded_topics_invisible.py`
-- text is not SQL, and a query that types the right words but names the wrong alias has
happened in this file's neighbours before (topics.py:140's CHILD_OF_VISIBLE_TOPIC comment).

UNFILTERED lists every site this task deliberately left without the live arm, one phrase of
reason each -- the WRITE / lock / idempotency / enumeration / sweep paths Ruling R2 carves
out. Adding to it must be a decision, not a reflex, same rule as test_deleted_read_paths.py's
EXEMPT.
"""
import inspect

from deleted_predicates import (
    CHILD_OF_UNDELETED_TOPIC,
    CHILD_OF_VISIBLE_TOPIC,
    LIVE_TOPIC_PREDICATE,
    SUPERSEDED_TOPIC_PREDICATE,
    visible_chunks_predicate,
    visible_topics_predicate,
)
from repositories import (
    action_items,
    chunks,
    content,
    findings,
    redactions,
    rollup,
    search_sql,
    threads,
    topics,
)
import lambda_item_writer
import lambda_keyframe
import lambda_org_api
import photo_collapse


# ---------------------------------------------------------------------------------------
# The building blocks, checked directly.
# ---------------------------------------------------------------------------------------

def test_live_topic_predicate_names_the_right_column():
    assert LIVE_TOPIC_PREDICATE.format(alias="t") == "t.superseded_at IS NULL"


def test_superseded_topic_predicate_is_the_complement():
    assert SUPERSEDED_TOPIC_PREDICATE.format(alias="t") == "t.superseded_at IS NOT NULL"


def test_child_of_visible_topic_carries_the_live_arm():
    sql = CHILD_OF_VISIBLE_TOPIC.format(alias="action_items")
    assert "superseded_at IS NULL" in sql
    assert "action_items.topic_id" in sql, "must correlate on the CHILD's own topic_id"


def test_topics_child_of_visible_topic_is_the_shared_one():
    """Moved into deleted_predicates (R2) -- topics.py must not hold a second copy."""
    assert topics.CHILD_OF_VISIBLE_TOPIC is CHILD_OF_VISIBLE_TOPIC


def test_child_of_undeleted_topic_carries_no_live_arm():
    """Fix round 1: get_topic_full's photos child must stay unfiltered for supersession
    (R3) while still honouring a deletion tombstone. If this predicate ever gains
    `superseded_at`, get_topic_full's photos vanish from the reindex read the moment a
    topic is superseded -- exactly the regression this pins against."""
    sql = CHILD_OF_UNDELETED_TOPIC.format(alias="topic_photos")
    assert "superseded_at" not in sql
    assert "scope = 'deleted'" in sql and "reverted_at IS NULL" in sql
    assert "topic_photos.topic_id" in sql, "must correlate on the CHILD's own topic_id"


def test_get_topic_full_photos_uses_the_deletion_only_child_predicate():
    """Guards the wiring, not just the predicate: a future edit that swaps
    CHILD_OF_UNDELETED_TOPIC back for the combined CHILD_OF_VISIBLE_TOPIC on this one
    line would re-introduce fix round 1's regression even though both predicates still
    exist correctly in deleted_predicates.py."""
    src = inspect.getsource(topics.get_topic_full)
    assert "CHILD_OF_UNDELETED_TOPIC.format(" in src
    # Not a bare "CHILD_OF_VISIBLE_TOPIC" check -- the docstring above names it
    # deliberately, to explain why it is NOT used. The regression this guards is a CALL,
    # so it looks for the call form specifically.
    assert "CHILD_OF_VISIBLE_TOPIC.format(" not in src, (
        "get_topic_full must not CALL the live-arm-carrying combined predicate (R3) -- "
        "that is why the site lives in UNFILTERED, not FILTERED")


def test_visible_topics_predicate_carries_the_live_arm():
    assert "superseded_at IS NULL" in visible_topics_predicate("t")


def test_visible_chunks_predicate_carries_the_live_arm_and_keeps_unassigned_chunks():
    sql = visible_chunks_predicate("c")
    assert "superseded_at IS NULL" in sql
    # An unbound transcript-window chunk (topic_id NULL) must stay visible -- the arm only
    # excludes a chunk whose topic exists and is superseded, never the unassigned bucket.
    assert "c.topic_id IS NULL" in sql


def test_threads_module_constants_carry_the_live_arm():
    """threads.py computes _VISIBLE_T/_VISIBLE_P at IMPORT time from visible_topics_predicate,
    so every function that interpolates them inherits the live arm without its own edit."""
    assert "superseded_at IS NULL" in threads._VISIBLE_T
    assert "superseded_at IS NULL" in threads._VISIBLE_P


def test_get_topic_visible_sql_constants_carry_the_live_arm():
    assert "superseded_at IS NULL" in topics._TOPIC_VISIBLE_SQL
    assert "superseded_at IS NULL" in topics._TOPIC_VISIBLE_ACTION_ITEMS_SQL


def test_search_sql_helpers_carry_the_live_arm():
    assert "superseded_at IS NULL" in search_sql.build_search_sql()
    assert "superseded_at IS NULL" in search_sql.build_latest_date_sql()
    assert "superseded_at IS NULL" in search_sql._scope_predicate("c")


# ---------------------------------------------------------------------------------------
# FILTERED: read paths whose SOURCE TEXT must show the live arm, directly or via a helper
# call / module constant built from one. Mirrors test_deleted_read_paths.py's AST scan, but
# a plain substring check is enough here -- these are leaves, not the walk that file does.
# ---------------------------------------------------------------------------------------

_MARKERS = (
    "superseded_at IS NULL", "superseded_at IS NOT NULL",
    "LIVE_TOPIC_PREDICATE", "SUPERSEDED_TOPIC_PREDICATE",
    "visible_topics_predicate(", "visible_chunks_predicate(",
    "CHILD_OF_VISIBLE_TOPIC", "_VISIBLE_T", "_VISIBLE_P",
    "_TOPIC_VISIBLE_SQL", "_TOPIC_VISIBLE_ACTION_ITEMS_SQL",
    "company_excluded_topic_ids", "build_search_sql(", "build_latest_date_sql(",
)

FILTERED = [
    (topics, "list_site_topics"),
    (topics, "list_contributor_folders_for_site_date"),
    (topics, "get_topic"),
    (topics, "list_day_topics_for_binding"),
    (topics, "list_extraction_topics_for_day"),
    (topics, "list_topics_for_date"),
    (topics, "list_report_dates"),
    (topics, "has_topics_in_range"),
    (topics, "report_date_counts"),
    (topics, "list_extraction_folder_names_for_date"),
    (topics, "list_topics_for_source_prefix"),
    (topics, "has_topics_for_source"),
    (topics, "has_topics_for_source_prefix"),
    (topics, "get_topic_visible"),
    (findings, "count_by_domain"),
    (redactions, "company_excluded_topic_ids"),
    (rollup, "portfolio_counts"),
    (threads, "candidate_corpus"),
    (threads, "thread_facts"),
    (threads, "facts_for_threads"),
    (threads, "list_pending"),
    (search_sql, "_scope_predicate"),
    (search_sql, "build_search_sql"),
    (search_sql, "build_latest_date_sql"),
    (chunks, "search_chunks"),
    (chunks, "latest_visible_date"),
]


def test_every_filtered_site_carries_a_live_arm_marker():
    offenders = []
    for mod, name in FILTERED:
        src = inspect.getsource(getattr(mod, name))
        if not any(marker in src for marker in _MARKERS):
            offenders.append(f"{mod.__name__}.{name}")
    assert not offenders, (
        "these read topics or their children without excluding a superseded pass, so a "
        "customer whose recording was re-extracted can be shown BOTH passes once Task 3 "
        "lands: " + ", ".join(sorted(offenders)) +
        " -- add the live arm, or move the site to UNFILTERED with a reason")


def test_the_filtered_sites_are_all_real():
    stale = [f"{mod.__name__}.{name}" for mod, name in FILTERED if not hasattr(mod, name)]
    assert not stale, f"FILTERED names functions that no longer exist: {stale}"


# ---------------------------------------------------------------------------------------
# UNFILTERED: deliberately left without the live arm. One phrase each. See the task-2
# report for the full table with file:line -- this is the enforced, importable copy.
# ---------------------------------------------------------------------------------------

UNFILTERED = {
    (topics, "get_topic_full"):
        "R3 -- single-topic reindex read; the reindex builder must be able to re-embed a "
        "topic's corrected content regardless of supersession status. Its photos child "
        "uses CHILD_OF_UNDELETED_TOPIC (deletion-only, no live arm) rather than the "
        "combined CHILD_OF_VISIBLE_TOPIC -- fix-round-1: the combined constant silently "
        "started filtering photos here once it grew the live arm, contradicting this "
        "same R3 ruling for every other child on this read",
    (threads, "get_suggestion"):
        "write-guard load for confirm/reject by a known suggestion id; reached only "
        "through the already-filtered pending queue (controller decision, fix round 1)",
    (topics, "list_expired_non_work"):
        "R3 -- non-work retention sweep; must still see a superseded pass to redact it "
        "(Task 3's integration test proves the sweep reaches both passes of a day)",
    (topics, "folders_for_session_base"):
        "gates a WRITE (session-ownership check ahead of the /speaker-names correction "
        "endpoint), not a display read; a superseded topic still proves which folder a "
        "session belongs to",
    (topics, "delete_topics_for_source"):
        "a DELETE, not a read; must touch every row under the key, superseded or not",
    (topics, "delete_topics_for_source_prefix"):
        "a DELETE, not a read; must touch every row under the prefix, superseded or not",
    (topics, "replace_day_photo_bindings"):
        "a day-wide rebind's DELETE reach, not a display read; the inner SELECT is "
        "deliberately unfiltered for the deletion arm too (see its own docstring)",
    (chunks, "restore_chunks_for_batch"):
        "undelete/restore path; its scalar subquery asks only whether the topic row still "
        "EXISTS (ON DELETE SET NULL emulation), never whether it is superseded",
    (chunks, "archive_chunks_for_session"):
        "delete-time MOVE, not a filtered read; must move every chunk that matches "
        "SESSION_CHUNK_PREDICATE regardless of its parent topic's supersession status",
    (action_items, "get_action_item"):
        "write-path single-row lookup by id (PATCH validation), not a display list",
    (content, "get_content_row"):
        "write-path single-row lookup by id (PATCH validation), not a display list",
    (content, "list_topic_content_fields"):
        "write-path lookup for intra-topic correction propagation, by an already-known "
        "topic id, not a display list",
    (lambda_item_writer, "write_extraction_items"):
        "write-path idempotency check (report_already_ingested) that must see every row "
        "under the key, live or superseded, to answer 'has this ever been ingested'",
    (lambda_item_writer, "_warn_if_discarding_checkoffs"):
        "audit count of rows about to be superseded; must count every existing row under "
        "the key, not just the live ones, or the count under-reports what is being lost",
    (lambda_keyframe, "process_request"):
        "write guard gating a topic_photos INSERT (keyframe generation), not a display "
        "read -- existence alone, not supersession, decides whether to proceed",
    (lambda_org_api, "_enqueue_content_reindex"):
        "write-adjacent single-row lookup by an already-validated, already-live row id, "
        "feeding a reindex enqueue -- not a display list",
    (lambda_org_api, "_compliance_key_context"):
        "write-adjacent single-row lookup by an already-validated row id, resolving "
        "compliance-mark rekey context for a content edit -- not a display list",
    (lambda_org_api, "_keyframe_deleted_event_fields"):
        "write-adjacent telemetry for a keyframe-deleted event, single row by known id "
        "-- not a display list",
    (photo_collapse, "_run"):
        "ops/diagnostic tool counting topic_photos binding rows directly, not a topic "
        "content display; must see every row to detect and collapse duplicates regardless "
        "of the parent topic's supersession status",
}


def test_the_unfiltered_sites_are_all_real():
    stale = [f"{mod.__name__}.{name}" for (mod, name) in UNFILTERED if not hasattr(mod, name)]
    assert not stale, f"UNFILTERED documents functions that no longer exist: {stale}"


def test_every_unfiltered_site_has_a_real_reason():
    for (mod, name), reason in UNFILTERED.items():
        assert isinstance(reason, str) and len(reason) > 20, (
            f"{mod.__name__}.{name} needs a real reason, not a placeholder")


# ---------------------------------------------------------------------------------------
# FakeConn spot checks for the functions that gained a runtime toggle this task
# (`include_superseded`). Source-text scanning alone cannot tell "present" from "present
# and actually switched off when the caller asks for every row" -- these prove the branch.
# ---------------------------------------------------------------------------------------

class FakeCursor:
    def __init__(self, conn):
        self.conn = conn
        self._rows = []

    def execute(self, sql, params=None):
        self.conn.calls.append({"sql": sql, "params": params})
        self._rows = self.conn._pop_result()
        return self

    def fetchall(self):
        return self._rows

    def fetchone(self):
        return self._rows[0] if self._rows else None


class FakeConn:
    """`results` is consumed in call order: one entry per cursor().execute() call."""

    def __init__(self, results=None):
        self.calls = []
        self._results = list(results or [])

    def _pop_result(self):
        return self._results.pop(0) if self._results else []

    def cursor(self, row_factory=None):
        return FakeCursor(self)


def test_has_topics_for_source_defaults_to_the_live_arm():
    conn = FakeConn()
    assert topics.has_topics_for_source(conn, "extractions/f/2026-09-01/x.json") is False
    assert "superseded_at IS NULL" in conn.calls[0]["sql"]


def test_has_topics_for_source_include_superseded_drops_the_arm():
    conn = FakeConn()
    topics.has_topics_for_source(conn, "k", include_superseded=True)
    assert "superseded_at" not in conn.calls[0]["sql"]


def test_has_topics_for_source_prefix_defaults_to_the_live_arm():
    conn = FakeConn()
    topics.has_topics_for_source_prefix(conn, "extractions/f/2026-09-01/")
    assert "superseded_at IS NULL" in conn.calls[0]["sql"]


def test_has_topics_for_source_prefix_include_superseded_drops_the_arm():
    conn = FakeConn()
    topics.has_topics_for_source_prefix(conn, "extractions/f/2026-09-01/",
                                        include_superseded=True)
    assert "superseded_at" not in conn.calls[0]["sql"]


def test_list_topics_for_source_prefix_defaults_to_the_live_arm():
    conn = FakeConn()  # empty topic_rows -> one call, then short-circuits to []
    out = topics.list_topics_for_source_prefix(conn, "extractions/f/2026-09-01/")
    assert out == []
    assert "superseded_at IS NULL" in conn.calls[0]["sql"]


def test_list_topics_for_source_prefix_include_superseded_drops_the_arm():
    """The delete/undelete enumeration posture (lambda_org_api.py ~5082/~5095/~5252):
    must enumerate EVERY row under the prefix, or a superseded copy is left outside every
    batch and the undelete can never bring the day back to exactly what the delete hid."""
    conn = FakeConn()
    topics.list_topics_for_source_prefix(conn, "reports/2026-09-01/f/",
                                         include_superseded=True)
    assert "superseded_at" not in conn.calls[0]["sql"]


def test_company_excluded_topic_ids_carries_the_superseded_arm():
    conn = FakeConn()
    out = redactions.company_excluded_topic_ids(conn, ["site-1"])
    assert out == set()
    assert "superseded_at IS NOT NULL" in conn.calls[0]["sql"]


def test_portfolio_counts_computes_the_exclusion_set_first():
    """rollup.portfolio_counts never spells `superseded_at` itself -- it subtracts the ids
    `company_excluded_topic_ids` already computed with the arm. This pins the wiring: the
    first statement portfolio_counts issues must be that exclusion query."""
    conn = FakeConn()
    out = rollup.portfolio_counts(conn, ["site-1"])
    assert "site-1" in out
    assert conn.calls, "portfolio_counts must query at all for a non-empty site_ids"
    assert "superseded_at IS NOT NULL" in conn.calls[0]["sql"]
