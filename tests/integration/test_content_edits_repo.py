import pytest
from repositories import companies, users, content_edits

pytestmark = pytest.mark.integration


def test_list_content_edits_resolves_actor_name(db):
    co = companies.create_company(db, "CE-Co")
    editor = users.upsert_user(db, "sub-ce-1", "ed@ce.nz", company_id=co["id"],
                               first_name="Bailey", last_name="Lin")
    content_edits.append_content_edit(
        db, co["id"], "topics", "row-1", "topic_title",
        "Old title", "New title", editor["id"], "site_manager")

    edits = content_edits.list_content_edits(db, co["id"], "topics", "row-1")
    assert len(edits) == 1
    assert edits[0]["actor_name"] == "Bailey Lin"
    assert edits[0]["before_text"] == "Old title"
    assert edits[0]["after_text"] == "New title"


def test_list_content_edits_actor_name_null_when_user_absent(db):
    co = companies.create_company(db, "CE-Co2")
    content_edits.append_content_edit(
        db, co["id"], "topics", "row-2", "topic_title",
        "A", "B", None, "admin")  # no actor_user_id
    edits = content_edits.list_content_edits(db, co["id"], "topics", "row-2")
    assert len(edits) == 1
    assert edits[0]["actor_name"] is None
