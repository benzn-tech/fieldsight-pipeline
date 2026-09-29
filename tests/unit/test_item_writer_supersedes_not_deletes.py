"""Unit: re-extraction issues UPDATE, never DELETE (Track B Task 3).

Before this task, lambda_item_writer/lambda_ingest re-extraction physically DELETEd a
source key's prior topics rows (CASCADEing action_items/findings/topic_photos with them)
before re-inserting. This replays the defect Task 3 fixes: the SQL the extraction path, the
group-merge path, and the ingest paths actually issue against a real connection double must
be an `UPDATE topics SET superseded_at` — the row and its children survive — and must never
be a `DELETE FROM topics`, on any of those paths.

Every other test in test_lambda_item_writer.py/test_lambda_ingest.py monkeypatches
`topics.supersede_topics_for_source[_prefix]` away entirely (right for those tests, which
are about everything AROUND the supersede call), so none of them would notice a regression
back to a DELETE inside that function. This file is the one that does not stub it out — it
drives the REAL repositories.topics function against a SQL-recording connection double and
reads the actual statement text.

The real supersede functions are captured at import time, before any fixture in either
imported module gets a chance to monkeypatch `topics.supersede_topics_for_source[_prefix]`
away -- same posture as test_lambda_ingest.py's own `_real_embed_from_sidecar` capture.
"""
import pytest

from repositories.topics import (
    supersede_topics_for_source as _real_supersede_topics_for_source,
    supersede_topics_for_source_prefix as _real_supersede_topics_for_source_prefix,
)

from tests.unit.test_lambda_item_writer import (  # noqa: F401
    EXTRACTION_KEY, iw, wired,
)
from tests.unit.test_lambda_ingest import (  # noqa: F401
    REPORT_KEY, ing,
    FakeS3 as _IngestFakeS3, make_report,
)
from tests.unit.test_lambda_ingest import wired as ingest_wired  # noqa: F401


class _BareCur:
    """conn.execute(...) -> this. `row` scripts what .fetchone() answers (None unless the
    test needs the I-4 report_already_ingested probe to read as a hit)."""

    def __init__(self, row=None):
        self._row = row

    def fetchone(self):
        return self._row

    def fetchall(self):
        return []


class _Cur:
    """conn.cursor(row_factory=...).execute(...) -> this -- the calling convention
    supersede_topics_for_source[_prefix]'s RETURNING clause uses (and, incidentally, the
    same one redactions.is_source_deleted uses for its own SELECT, which lands in the same
    recorded list -- harmless, neither of those statements contains the text this file
    greps for)."""

    def __init__(self, conn):
        self.conn = conn

    def execute(self, sql, params=None):
        self.conn.cursor_sql.append((sql, params))
        return self

    def fetchall(self):
        return []

    def fetchone(self):
        return None


class _Conn:
    """Records every statement issued through EITHER calling convention -- bare
    `conn.execute(...)` (the advisory lock, the I-4 probe, _warn_if_discarding_checkoffs'
    own count) into `.executed`, and `conn.cursor(...).execute(...)` (every real
    repositories.topics call) into `.cursor_sql` -- so a test can grep the actual SQL text
    without caring which convention a given call used.

    `report_already_ingested` mirrors test_lambda_item_writer.FakeConn's own flag, to drive
    the authority-flip branch (lambda_item_writer.py's :892 supersede) the same way that
    file's own I-4 tests do."""

    def __init__(self, report_already_ingested=False):
        self.executed = []
        self.cursor_sql = []
        self.report_already_ingested = report_already_ingested

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        return False

    def execute(self, sql, params=None):
        self.executed.append((sql, params))
        return _BareCur({"?column?": 1} if self.report_already_ingested else None)

    def cursor(self, row_factory=None):
        return _Cur(self)


def _all_sql(conn):
    return " ".join(sql for sql, _params in conn.cursor_sql + conn.executed)


# ---------------------------------------------------------------------------
# lambda_item_writer -- the extraction path's own idempotent clear (:997-ish)
# ---------------------------------------------------------------------------

def test_extraction_path_issues_update_not_delete(wired):
    conn = _Conn()
    wired.setattr(iw, "get_connection", lambda *a, **k: conn)
    # The `wired` fixture's own default stubs this out (right for every other test in the
    # suite) -- restore the REAL function for this one, which exists to prove what THAT
    # function actually sends to the database.
    wired.setattr(iw.topics, "supersede_topics_for_source", _real_supersede_topics_for_source)

    iw.write_extraction_items("2026-07-06", "Jarley_Trainor", EXTRACTION_KEY)

    sql = _all_sql(conn)
    assert "UPDATE topics SET superseded_at" in sql
    assert "DELETE FROM topics" not in sql


def test_authority_flip_branch_also_issues_update_not_delete(wired):
    """The OTHER supersede call on this path (:892-ish) -- the late-extraction-under-the-
    flip branch that retires the nightly report's topics, not this extraction's own prior
    pass. Both must be UPDATEs; test_extraction_path_issues_update_not_delete above only
    ever exercises the second."""
    conn = _Conn(report_already_ingested=True)
    wired.setattr(iw, "get_connection", lambda *a, **k: conn)
    wired.setattr(iw.lambda_ingest, "AUTHORITY_FLIP", True)
    wired.setattr(iw.topics, "supersede_topics_for_source", _real_supersede_topics_for_source)

    iw.write_extraction_items("2026-07-06", "Jarley_Trainor", EXTRACTION_KEY)

    sql = _all_sql(conn)
    assert sql.count("UPDATE topics SET superseded_at") == 2, (
        "one supersede for the retired report topics (authority-flip branch), one for "
        "this extraction key's own idempotent clear -- both real"
    )
    assert "DELETE FROM topics" not in sql


# ---------------------------------------------------------------------------
# lambda_item_writer -- the group-merge path (_supersede_member_topics)
# ---------------------------------------------------------------------------

def test_group_path_supersedes_member_keys_with_update_not_delete():
    conn = _Conn()
    artifact = {
        "tier": "group", "groupId": "a" * 32,
        "mergedMembers": [
            f"extractions/A/2026-08-07/sid{'a' * 32}.json",
            f"extractions/B/2026-08-08/sid{'b' * 32}.json",
        ],
    }

    iw._supersede_member_topics(conn, artifact, "group:2026-08-07T10:00:00Z",
                                supersede=_real_supersede_topics_for_source)

    sql = _all_sql(conn)
    assert sql.count("UPDATE topics SET superseded_at") == len(artifact["mergedMembers"]), (
        "one real supersede call per member key"
    )
    assert "DELETE FROM topics" not in sql


# ---------------------------------------------------------------------------
# lambda_ingest -- the report-key clear and the extraction-prefix supersede
# ---------------------------------------------------------------------------

def test_ingest_report_key_clear_issues_update_not_delete(ingest_wired):
    conn = _Conn()
    ingest_wired.setattr(ing, "get_connection", lambda *a, **k: conn)
    ingest_wired.setattr(ing, "_s3_client",
                         _IngestFakeS3({REPORT_KEY: __import__("json").dumps(make_report())}))
    ingest_wired.setattr(ing.topics, "supersede_topics_for_source",
                         _real_supersede_topics_for_source)
    ingest_wired.setattr(ing.topics, "supersede_topics_for_source_prefix",
                         _real_supersede_topics_for_source_prefix)

    ing.ingest_report("2026-03-02", "Jarley_Trainor", REPORT_KEY)

    sql = _all_sql(conn)
    # One UPDATE for the report-key clear (always) and one for the extraction-prefix
    # supersession (AUTHORITY_FLIP off by default -> the non-defer branch runs it too).
    assert sql.count("UPDATE topics SET superseded_at") == 2
    assert "DELETE FROM topics" not in sql
