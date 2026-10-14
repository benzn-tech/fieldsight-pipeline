"""The index-family filter against a real PostgreSQL.

Three chunks on one site: a nightly-report chunk (reports/ key, 10-12), an
extraction-built chunk (extractions/ key, 10-08) and a legacy chunk with a NULL
source_s3_key (10-11). A fake connection never parses SQL, so only the database
can say which family each of them belongs to -- notably that NULL belongs to
'report' (NOT LIKE alone would drop it, since NULL NOT LIKE x is NULL).

Under 'session', report-family rows are ALSO visible for any (site, user, day) that
has no session row (coverage fallback); the fixture's three rows sit on three
different days, so none of the report rows is covered by the one session row.
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
                                      source_family="session") == "2026-10-12"
    assert chunks.latest_visible_date(db, ids, on_or_before="2026-10-13",
                                      source_family="report") == "2026-10-12"
    assert chunks.latest_visible_date(db, ids, on_or_before="2026-10-13") == "2026-10-12"


def test_session_family_sees_uncovered_days_of_every_family(db, site):
    # 10-08 is covered by the session row; 10-11 / 10-12 have no session row, so
    # their report-family rows (NULL key included) fall back in.
    assert _texts(db, site, "session") == ["legacy null row", "report row", "session row"]


def test_null_key_row_belongs_to_the_report_family_only(db, site):
    assert _texts(db, site, "report") == ["legacy null row", "report row"]
    # ...and is only visible under 'session' because its day is uncovered.
    covered = chunks.insert_chunk(db, site["id"], "2026-10-11", "topic", "covering", _vec(),
                                  source_s3_key="extractions/Ben/2026-10-11/sid2.json")
    assert covered
    assert "legacy null row" not in _texts(db, site, "session")


def test_no_family_means_no_filter(db, site):
    assert _texts(db, site, None) == ["legacy null row", "report row", "session row"]


# ---- coverage fallback (session reads) -------------------------------------------------

def _user(db, company_id, tag):
    return db.execute(
        "INSERT INTO users (company_id, email, global_role, folder_name) "
        "VALUES (%s, %s, 'worker', %s) RETURNING id",
        (company_id, f"{tag}@x.com", tag)).fetchone()[0]


@pytest.fixture
def cov(db):
    co = companies.create_company(db, "Acme-cov")
    s = sites.create_site(db, co["id"], "S-cov")
    return {"site": s, "a": _user(db, co["id"], "usera"), "b": _user(db, co["id"], "userb")}


def _add(db, cov, day, text, user, key):
    chunks.insert_chunk(db, cov["site"]["id"], day, "topic", text, _vec(),
                        user_id=cov[user] if user else None, source_s3_key=key)


def test_covered_day_hides_its_report_rows_under_session(db, cov):
    _add(db, cov, "2026-10-08", "rep", "a", "reports/2026-10-08/a/daily_report.json")
    _add(db, cov, "2026-10-08", "ses", "a", "extractions/a/2026-10-08/s1.json")
    assert _texts(db, cov["site"], "session") == ["ses"]
    assert _texts(db, cov["site"], "report") == ["rep"]


def test_report_only_day_stays_visible_under_session(db, cov):
    _add(db, cov, "2026-09-10", "sept report", "a", "reports/2026-09-10/a/daily_report.json")
    assert _texts(db, cov["site"], "session") == ["sept report"]


def test_coverage_is_per_user(db, cov):
    _add(db, cov, "2026-10-08", "a rep", "a", "reports/2026-10-08/a/daily_report.json")
    _add(db, cov, "2026-10-08", "a ses", "a", "extractions/a/2026-10-08/s1.json")
    _add(db, cov, "2026-10-08", "b rep", "b", "reports/2026-10-08/b/daily_report.json")
    assert _texts(db, cov["site"], "session") == ["a ses", "b rep"]


def test_null_user_rows_cover_each_other_only(db, cov):
    # IS NOT DISTINCT FROM: an unattributed session row covers the unattributed report row
    # but NOT a user-attributed one (a plain '=' would cover neither).
    _add(db, cov, "2026-10-08", "null rep", None, "reports/2026-10-08/x/daily_report.json")
    _add(db, cov, "2026-10-08", "null ses", None, "extractions/x/2026-10-08/s1.json")
    _add(db, cov, "2026-10-08", "a rep", "a", "reports/2026-10-08/a/daily_report.json")
    assert _texts(db, cov["site"], "session") == ["a rep", "null ses"]


def test_latest_visible_date_reaches_a_report_only_day_under_session(db, cov):
    _add(db, cov, "2026-10-08", "ses", "a", "extractions/a/2026-10-08/s1.json")
    _add(db, cov, "2026-10-12", "late rep", "a", "reports/2026-10-12/a/daily_report.json")
    ids = [cov["site"]["id"]]
    assert chunks.latest_visible_date(db, ids, on_or_before="2026-10-13",
                                      source_family="session") == "2026-10-12"


def test_latest_visible_date_skips_a_covered_report_day_under_session(db, cov):
    # 10-12 holds a report row AND a session row, so it is visible via the session row;
    # the point is that a covered day never *adds* a date the session family lacks.
    _add(db, cov, "2026-10-08", "ses", "a", "extractions/a/2026-10-08/s1.json")
    _add(db, cov, "2026-10-08", "rep", "a", "reports/2026-10-08/a/daily_report.json")
    ids = [cov["site"]["id"]]
    assert chunks.latest_visible_date(db, ids, on_or_before="2026-10-13",
                                      source_family="session") == "2026-10-08"


def test_report_family_is_unchanged_by_coverage(db, cov):
    _add(db, cov, "2026-10-08", "rep", "a", "reports/2026-10-08/a/daily_report.json")
    _add(db, cov, "2026-10-08", "ses", "a", "extractions/a/2026-10-08/s1.json")
    _add(db, cov, "2026-10-09", "null key", "a", None)
    assert _texts(db, cov["site"], "report") == ["null key", "rep"]
