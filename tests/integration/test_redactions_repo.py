import pytest
from repositories import companies, sites, topics, redactions

pytestmark = pytest.mark.integration


def _seed(db):
    co = companies.create_company(db, "Red-Co")
    s = sites.create_site(db, co["id"], "Red-Site")
    return co, s


def test_create_excludes_and_revert_restores(db):
    co, s = _seed(db)
    personal = topics.upsert_topic(db, s["id"], "2026-07-21", "Lunch", work_class="non_work")
    work = topics.upsert_topic(db, s["id"], "2026-07-21", "Pour", work_class="work")
    manual = topics.upsert_topic(db, s["id"], "2026-07-21", "Family call", work_class="work")

    # non_work is auto-excluded even with no redaction
    excl = redactions.company_excluded_topic_ids(db, [s["id"]])
    assert personal["id"] in excl and work["id"] not in excl

    red = redactions.create_redaction(db, co["id"], manual["id"], "non_work", None, "site_manager")
    assert red["reverted_at"] is None and red["scope"] == "analysis"
    assert redactions.is_topic_redacted(db, manual["id"]) is True
    assert manual["id"] in redactions.company_excluded_topic_ids(db, [s["id"]])

    reverted = redactions.revert_redaction(db, red["id"], co["id"])
    assert reverted is not None and reverted["reverted_at"] is not None
    assert redactions.is_topic_redacted(db, manual["id"]) is False
    assert manual["id"] not in redactions.company_excluded_topic_ids(db, [s["id"]])
    # wrong company can neither revert nor re-revert
    other = companies.create_company(db, "Other-Co")
    red2 = redactions.create_redaction(db, co["id"], work["id"], "privacy", None, "admin")
    assert redactions.revert_redaction(db, red2["id"], other["id"]) is None
