"""Guards on migration 0030 — the device ledger's schema contract.

Unit tests here have no live Postgres, so these assert the migration text:
that it is idempotent, and that it declares exactly the columns
lambda_device_ledger and device_heartbeat depend on.
"""

from pathlib import Path

SQL = Path("src/migrations/0030_devices.sql").read_text(encoding="utf-8").lower()


def test_creates_devices_table_idempotently():
    assert "create table if not exists devices" in SQL


def test_asset_tag_is_the_unique_authoritative_identity():
    assert "asset_tag         text unique not null" in SQL


def test_device_uuid_is_nullable_and_distrustable():
    assert "device_uuid       text" in SQL
    assert "uuid_trusted      boolean not null default true" in SQL


def test_adds_device_id_to_recordings_idempotently():
    assert "alter table recordings add column if not exists device_id uuid" in SQL


def test_indexes_the_columns_the_report_reads():
    assert "create index if not exists devices_last_seen_idx" in SQL
    assert "devices (last_seen_at nulls first)" in SQL
    assert "create index if not exists recordings_device_created_idx" in SQL
