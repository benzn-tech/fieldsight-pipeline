import pytest
from repositories import companies, classification_feedback as cf

pytestmark = pytest.mark.integration


def test_append_and_summary_metadata_only(db):
    co = companies.create_company(db, "CF-Co")
    tid = "11111111-1111-1111-1111-111111111111"
    r = cf.append_feedback(db, co["id"], tid, "confirm_non_work",
                           classifier_verdict="non_work", classifier_confidence=0.8,
                           topic_category="progress")
    assert r["human_verdict"] == "confirm_non_work" and r["topic_category"] == "progress"
    cf.append_feedback(db, co["id"], tid, "reject_is_work", classifier_verdict="non_work")
    cf.append_feedback(db, co["id"], tid, "missed_personal")
    s = cf.summary(db, co["id"])
    assert s == {"confirm_non_work": 1, "reject_is_work": 1, "missed_personal": 1, "precision": 0.5}
    # privacy invariant: the table exposes no verbatim-content column
    cols = {c[0] for c in db.execute(
        "SELECT column_name FROM information_schema.columns "
        "WHERE table_name='classification_feedback'").fetchall()}
    assert "observation" not in cols and "text" not in cols and "transcript" not in cols
