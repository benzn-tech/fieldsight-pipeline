"""The records export README names, honestly, every table it does NOT export (M5 of the
final review): a company reading the README must be able to tell what is missing.

Pinned against the migrations: every table any migration creates is either exported
(company_records_export.TABLES) or listed in NOT_EXPORTED -- a new table cannot slip past
the README silently."""
import contextlib
import re
from datetime import datetime, timezone
from pathlib import Path

import company_records_export as cre

MIG = Path(__file__).resolve().parents[2] / "src" / "migrations"


def _migration_tables():
    out = set()
    for f in MIG.glob("*.sql"):
        for m in re.finditer(r"CREATE TABLE (?:IF NOT EXISTS )?([a-z_]+)",
                             f.read_text(encoding="utf-8"), re.I):
            out.add(m.group(1).lower())
    return out


def test_every_migration_table_is_exported_or_listed_as_not_included():
    exported = {s.table for s in cre.TABLES}
    listed = {t for t, _ in cre.NOT_EXPORTED}
    assert not exported & listed, "a table cannot be both exported and not included"
    missing = _migration_tables() - exported - listed
    assert not missing, f"tables neither exported nor named in the README: {sorted(missing)}"


class _Conn:
    @contextlib.contextmanager
    def transaction(self):
        yield

    def execute(self, sql, params=None):
        return type("R", (), {"fetchone": lambda s: ("Acme",)})()


def test_the_readme_names_every_table_not_included():
    state = {"tables": {}}
    text = cre._readme(_Conn(), "co-1", "ex-1", state,
                       datetime(2026, 11, 1, tzinfo=timezone.utc))
    tail = text.split("Not included (no file):", 1)[1]
    for table, why in cre.NOT_EXPORTED:
        assert f"- {table}: {why}" in tail
    for t in ("action_status_suggestions", "programme_progress_suggestions",
              "checklist_reports", "inspection_windows", "recordings", "topic_photos"):
        assert f"- {t}:" in tail
