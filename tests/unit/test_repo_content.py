import pytest

content = pytest.importorskip("repositories.content",
                              reason="requires psycopg (installed in CI)")


class FakeCursor:
    def __init__(self, row):
        self._row = row
        self.sql = None
        self.params = None

    def execute(self, sql, params=None):
        self.sql = sql
        self.params = params
        return self

    def fetchone(self):
        return self._row


class FakeConn:
    def __init__(self, row):
        self.cur = FakeCursor(row)

    def cursor(self, *a, **k):
        return self.cur


class ScanCursor:
    """Returns a canned row list per table, keyed off the SQL's FROM clause."""

    def __init__(self, by_table):
        self.by_table = by_table
        self.sql_log = []

    def execute(self, sql, params=None):
        self.sql_log.append(sql)
        table = sql.split(" FROM ")[1].split(" ")[0]
        self._rows = self.by_table.get(table, [])
        return self

    def fetchall(self):
        return self._rows


class ScanConn:
    def __init__(self, by_table):
        self.cur = ScanCursor(by_table)

    def cursor(self, *a, **k):
        return self.cur


def test_editable_allow_list_matches_spec():
    assert content.EDITABLE["topics"] == {"title", "summary"}
    assert content.EDITABLE["action_items"] == {"text", "responsible"}
    assert content.EDITABLE["findings"] == {
        "observation", "recommended_action", "entity_name", "entity_trade"}
    # Phase F Task 24 (D8 retirement, spec §8): safety_observations is no
    # longer editable -- findings is the single source of truth for
    # safety/quality content, so a correction must target the findings row
    # (Tasks 21/22 make that row the one SAFETY/QUALITY reads).
    assert "safety_observations" not in content.EDITABLE


def test_is_editable_rejects_enum_and_unknown_tables():
    assert content.is_editable("topics", "title")
    assert not content.is_editable("topics", "category")     # enum, excluded (§3)
    assert not content.is_editable("action_items", "status")  # task metadata
    assert not content.is_editable("recordings", "title")     # not an item-store table


def test_safety_observations_no_longer_editable():
    # Phase F Task 24: safety/quality corrections target findings, not the
    # legacy safety_observations table (removed from both EDITABLE and
    # _SELECT -- get_content_row must 404, same posture as any other
    # unknown table).
    assert not content.is_editable("safety_observations", "observation")
    conn = FakeConn({"id": "so-1", "observation": "x"})
    assert content.get_content_row(conn, "safety_observations", "so-1") is None


def test_get_content_row_joins_company_and_author():
    conn = FakeConn({"id": "t-1", "site_id": "s-1", "company_id": "co-1",
                     "author_user_id": "u-9", "title": "Slab pour", "summary": "x"})
    row = content.get_content_row(conn, "topics", "t-1")
    assert row["company_id"] == "co-1"
    assert row["author_user_id"] == "u-9"
    assert "join sites" in conn.cur.sql.lower()


def test_update_content_field_only_writes_whitelisted_column():
    conn = FakeConn({"id": "t-1", "title": "Corrected"})
    row = content.update_content_field(conn, "topics", "t-1", "title", "Corrected")
    assert row["title"] == "Corrected"
    assert "update topics set title" in conn.cur.sql.lower()
    assert conn.cur.params == ("Corrected", "t-1")


def test_update_content_field_rejects_non_whitelisted_field():
    conn = FakeConn(None)
    assert content.update_content_field(conn, "topics", "t-1", "category", "x") is None


# ----------------------------------------------------------
# is_propagatable / _PROPAGATE_EXTRA — propagation-only fields (findings.
# impact_note): rewritable by intra-topic propagation, but NOT individually
# editable (not in EDITABLE).
# ----------------------------------------------------------
def test_is_propagatable_true_for_editable_and_propagate_extra():
    assert content.is_propagatable("findings", "observation")   # EDITABLE
    assert content.is_propagatable("findings", "impact_note")   # _PROPAGATE_EXTRA


def test_is_propagatable_false_for_hard_excluded_impact_fields():
    for field in ("impact_severity", "impact_evidence", "impact_task_name",
                  "impact_matched_at"):
        assert not content.is_propagatable("findings", field)
    assert not content.is_propagatable("unknown_table", "impact_note")


def test_impact_note_is_not_individually_editable():
    # The whole point: propagatable != editable. impact_note must stay out of
    # EDITABLE/is_editable so patch_content and history never expose it as a
    # directly-editable field.
    assert not content.is_editable("findings", "impact_note")
    assert "impact_note" not in content.EDITABLE["findings"]


def test_update_content_field_writes_propagate_only_field():
    # apply_topic_correction writes through update_content_field too, so it
    # must accept the propagation-only extra even though is_editable rejects it.
    conn = FakeConn({"id": "f-1", "impact_note": "McCahon delayed the pour"})
    row = content.update_content_field(
        conn, "findings", "f-1", "impact_note", "McCahon delayed the pour")
    assert row["impact_note"] == "McCahon delayed the pour"
    assert "update findings set impact_note" in conn.cur.sql.lower()


# ----------------------------------------------------------
# list_topic_content_fields — the intra-topic propagation blast radius
# ----------------------------------------------------------
_SCAN = {
    "topics": [{"id": "t-1", "title": "Mackon slab", "summary": "Mackon poured it"}],
    "action_items": [{"id": "a-1", "text": "call Mackon", "responsible": "Mackon"}],
    "findings": [{"id": "f-1", "observation": "Mackon left the edge open",
                  "recommended_action": None, "entity_name": "Mackon",
                  "entity_trade": None,
                  "impact_note": "Mackon's delay pushed the pour back two days"}],
}


def test_list_topic_content_fields_covers_all_three_tables():
    cells = content.list_topic_content_fields(ScanConn(_SCAN), "t-1")
    assert {(c["table"], c["field"]) for c in cells} == {
        ("topics", "title"), ("topics", "summary"),
        ("action_items", "text"), ("action_items", "responsible"),
        ("findings", "observation"), ("findings", "entity_name"),
        # impact_note is propagation-only (_PROPAGATE_EXTRA): scanned/rewritten
        # here, but NOT in EDITABLE, so it stays out of patch_content/history.
        ("findings", "impact_note"),
    }
    assert [c["table"] for c in cells][:2] == ["topics", "topics"]   # stable order


def test_list_topic_content_fields_skips_null_cells():
    cells = content.list_topic_content_fields(ScanConn(_SCAN), "t-1")
    # recommended_action / entity_trade are NULL -> never previewed, so they
    # can never be rewritten either.
    assert all(c["field"] not in ("recommended_action", "entity_trade") for c in cells)
    assert all(c["value"] is not None for c in cells)


def test_list_topic_content_fields_selects_propagatable_columns_only():
    # impact_note IS now selected (propagation-only, _PROPAGATE_EXTRA), but the
    # hard-excluded impact_* fields (mirror/enum/jsonb/timestamp) never are.
    conn = ScanConn(_SCAN)
    content.list_topic_content_fields(conn, "t-1")
    joined = " ".join(conn.cur.sql_log)
    assert "impact_note" in joined
    for banned in ("impact_task_name", "impact_evidence", "impact_matched_at",
                   "severity", "domain", "status", "priority", "category"):
        assert banned not in joined


def test_list_topic_content_fields_scopes_children_by_topic_id():
    conn = ScanConn(_SCAN)
    content.list_topic_content_fields(conn, "t-1")
    sqls = {s.split(" FROM ")[1].split(" ")[0]: s for s in conn.cur.sql_log}
    assert "WHERE id=%s" in sqls["topics"]                   # the topic row itself
    assert "WHERE topic_id=%s" in sqls["action_items"]       # never the whole day
    assert "WHERE topic_id=%s" in sqls["findings"]
