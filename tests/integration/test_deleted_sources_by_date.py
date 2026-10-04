"""deleted_source_prefixes(conn, None, date) must answer for THAT date.

The lake-wide summary is withheld on a day that has a deletion, because the summary is a
verbatim document and a deleted session's words would sit inside it. admin_disambiguation
asks `deleted_source_prefixes(conn, None, date)` -- every folder, one date. The date filter
was written `if folder and date:`, so with folder=None the date was IGNORED and the call
asked "has any recording, anywhere, on any day, ever been deleted?". Once one had, the
summary was withheld for every date (since 43179ad8, 2026-08-14); verified on TEST
2026-10-04, where even platform_admin got 404.

Integration, because this is a WHERE clause: only a database can say which rows it keeps.
Asserted by membership, not equality -- the local database is persistent and other tests'
committed tombstones may be present.
"""
import uuid

import pytest

from repositories import companies, redactions

pytestmark = pytest.mark.integration

DAY = "2026-08-14"
OTHER_DAY = "2026-08-15"


@pytest.fixture
def tombstone(db):
    co = companies.create_company(db, "DelByDate-" + uuid.uuid4().hex[:8])
    folder = "DelFolder" + uuid.uuid4().hex[:6]
    prefix = f"extractions/{folder}/{DAY}/sid-{uuid.uuid4().hex[:6]}"
    redactions.create_recording_tombstone(db, co["id"], prefix, "test", None, "admin")
    return folder, prefix


def test_every_folder_on_the_deleted_day_finds_it(db, tombstone):
    _, prefix = tombstone
    assert prefix in redactions.deleted_source_prefixes(db, None, DAY)


def test_every_folder_on_another_day_does_not(db, tombstone):
    """The bug: a deletion on one day withheld the summary on every other day."""
    _, prefix = tombstone
    assert prefix not in redactions.deleted_source_prefixes(db, None, OTHER_DAY)


def test_no_arguments_still_means_everything(db, tombstone):
    """lambda_ingest and the session-deleted check call it bare and want every prefix."""
    _, prefix = tombstone
    assert prefix in redactions.deleted_source_prefixes(db)


def test_folder_and_date_still_narrow_to_that_folder(db, tombstone):
    folder, prefix = tombstone
    assert prefix in redactions.deleted_source_prefixes(db, folder, DAY)
    assert prefix not in redactions.deleted_source_prefixes(db, "SomeoneElse", DAY)
