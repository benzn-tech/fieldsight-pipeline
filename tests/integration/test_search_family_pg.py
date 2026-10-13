"""The index-family filter against a real PostgreSQL.

Three chunks on one site: a nightly-report chunk (reports/ key, 10-12), an
extraction-built chunk (extractions/ key, 10-08) and a legacy chunk with a NULL
source_s3_key (10-11). A fake connection never parses SQL, so only the database
can say which family each of them belongs to -- notably that NULL belongs to
'report' (NOT LIKE alone would drop it, since NULL NOT LIKE x is NULL).
"""
import pytest

from repositories import chunks, companies, sites

pytestmark = pytest.mark.integration


def _vec(hot=0, dim=1024):
    v = [0.0] * dim
    v[hot] = 1.0
    return v


@pytest.fixture
def site(db):
    co = companies.create_company(db, "Acme-family")
    s = sites.create_site(db, co["id"], "S1")
    chunks.insert_chunk(db, s["id"], "2026-10-12", "topic", "report row", _vec(),
                        source_s3_key="reports/2026-10-12/Ben/daily_report.json")
    chunks.insert_chunk(db, s["id"], "2026-10-08", "topic", "session row", _vec(),
                        source_s3_key="extractions/Ben/2026-10-08/sid1.json")
    chunks.insert_chunk(db, s["id"], "2026-10-11", "topic", "legacy null row", _vec())
    return s


def _texts(db, site, family):
    rows = chunks.search_chunks(db, _vec(), [site["id"]], k=10, source_family=family)
    return sorted(r["chunk_text"] for r in rows)


def test_latest_visible_date_per_family(db, site):
    ids = [site["id"]]
    assert chunks.latest_visible_date(db, ids, on_or_before="2026-10-13",
                                      source_family="session") == "2026-10-08"
    assert chunks.latest_visible_date(db, ids, on_or_before="2026-10-13",
                                      source_family="report") == "2026-10-12"
    assert chunks.latest_visible_date(db, ids, on_or_before="2026-10-13") == "2026-10-12"


def test_search_session_returns_only_the_extractions_row(db, site):
    assert _texts(db, site, "session") == ["session row"]


def test_null_key_row_belongs_to_the_report_family_only(db, site):
    assert _texts(db, site, "report") == ["legacy null row", "report row"]
    assert "legacy null row" not in _texts(db, site, "session")


def test_no_family_means_no_filter(db, site):
    assert _texts(db, site, None) == ["legacy null row", "report row", "session row"]
