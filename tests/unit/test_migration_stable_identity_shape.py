"""Unit: migration 0071 has the shape the repositories and decision-record writers rely on.

Text checks only -- the SQL is applied for real by
tests/integration/test_stable_identity_schema.py (CI Postgres). The version guard matters
because two version collisions have already shipped (0041, 0044) and the runner orders ties
by filename, so a second 0071 from a parallel branch would apply in an order nobody chose.
"""
import os

MIGRATIONS = os.path.join(os.path.dirname(__file__), "..", "..", "src", "migrations")
NAME = "0071_stable_identity.sql"


def _sql():
    with open(os.path.join(MIGRATIONS, NAME), encoding="utf-8") as fh:
        return fh.read()


def _flat():
    return " ".join(_sql().split())


def test_no_other_migration_uses_version_0071():
    assert [f for f in os.listdir(MIGRATIONS) if f.startswith("0071_")] == [NAME]


def test_new_child_tables_cascade_on_site_id():
    """The 2026-09-07 review found a draft that dropped this -- a site delete must not
    leave topic_decisions/topic_questions rows pointing at a gone site."""
    sql = _flat()
    assert (
        "site_id uuid NOT NULL REFERENCES sites(id) ON DELETE CASCADE" in sql
    ), "topic_decisions/topic_questions must cascade on site_id"
    # Both new child tables carry it -- not just one.
    assert sql.count("site_id uuid NOT NULL REFERENCES sites(id) ON DELETE CASCADE") == 2


def test_new_child_tables_cascade_on_topic_id():
    sql = _flat()
    assert sql.count("topic_id uuid NOT NULL REFERENCES topics(id) ON DELETE CASCADE") == 2


def test_stable_id_columns_are_not_null_with_a_default():
    sql = _flat()
    for table_marker in (
        "ALTER TABLE action_items ADD COLUMN IF NOT EXISTS stable_id uuid NOT NULL "
        "DEFAULT gen_random_uuid();",
        "ALTER TABLE findings ADD COLUMN IF NOT EXISTS stable_id uuid NOT NULL "
        "DEFAULT gen_random_uuid();",
    ):
        assert table_marker in sql, table_marker
    # The two new child tables also carry a stable_id with the same shape (_flat()
    # collapses the SQL file's column-aligned whitespace to single spaces).
    assert sql.count("stable_id uuid NOT NULL DEFAULT gen_random_uuid()") == 4, (
        "action_items, findings, topic_decisions, topic_questions"
    )


def test_live_source_index_is_partial_on_not_superseded():
    sql = _flat()
    assert (
        "CREATE INDEX IF NOT EXISTS idx_topics_live_source ON topics (source_s3_key) "
        "WHERE superseded_at IS NULL" in sql
    )


def test_every_item_table_has_audience_defaulting_internal_with_two_value_check():
    sql = _flat()
    # findings and action_items get the column added with a matching separate CHECK
    # constraint (Postgres rejects an inline ADD COLUMN ... CHECK IF NOT EXISTS).
    assert (
        "ALTER TABLE findings ADD COLUMN IF NOT EXISTS audience text NOT NULL "
        "DEFAULT 'internal';" in sql
    )
    assert (
        "ALTER TABLE action_items ADD COLUMN IF NOT EXISTS audience text NOT NULL "
        "DEFAULT 'internal';" in sql
    )
    assert sql.count("CHECK (audience IN ('internal','owner'))") == 4, (
        "findings, action_items, topic_decisions, topic_questions"
    )


def test_findings_kind_defaults_to_observation():
    sql = _flat()
    assert (
        "ALTER TABLE findings ADD COLUMN IF NOT EXISTS kind text NOT NULL "
        "DEFAULT 'observation';" in sql
    )
    assert (
        "CHECK (kind IN ('observation','instruction_received','daywork_record',"
        "'delay_event'))" in sql
    )


def test_decision_records_subject_is_not_fk_bound():
    """The record must outlive the row it judged -- deleting the topic (CASCADE removes
    the child) must not touch decision_records. subject_stable_id is a bare uuid column,
    not a REFERENCES."""
    sql = _flat()
    assert "subject_stable_id uuid NOT NULL," in sql
    assert "subject_stable_id uuid NOT NULL REFERENCES" not in sql


def test_decision_records_company_cascades_but_site_is_nullable():
    sql = _flat()
    assert "company_id uuid NOT NULL REFERENCES companies(id) ON DELETE CASCADE" in sql
    assert "site_id uuid REFERENCES sites(id) ON DELETE CASCADE" in sql
