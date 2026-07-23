import pytest
from repositories import companies, users, content_edits

pytestmark = pytest.mark.integration

# content_edits.row_id is `uuid NOT NULL` (migration 0019_content_edits.sql:17).
# These tests originally passed the placeholders "row-1"/"row-2", which are not
# valid UUIDs, so every run died on InvalidTextRepresentation -- they have never
# passed in CI. Fixed literals keep the assertions deterministic.
ROW_1 = "11111111-1111-4111-8111-111111111111"
ROW_2 = "22222222-2222-4222-8222-222222222222"


def test_list_content_edits_resolves_actor_name(db):
    co = companies.create_company(db, "CE-Co")
    editor = users.upsert_user(db, "sub-ce-1", "ed@ce.nz", company_id=co["id"],
                               first_name="Bailey", last_name="Lin")
    content_edits.append_content_edit(
        db, co["id"], "topics", ROW_1, "topic_title",
        "Old title", "New title", editor["id"], "site_manager")

    edits = content_edits.list_content_edits(db, co["id"], "topics", ROW_1)
    assert len(edits) == 1
    assert edits[0]["actor_name"] == "Bailey Lin"
    assert edits[0]["before_text"] == "Old title"
    assert edits[0]["after_text"] == "New title"


def test_list_content_edits_actor_name_null_when_user_absent(db):
    co = companies.create_company(db, "CE-Co2")
    content_edits.append_content_edit(
        db, co["id"], "topics", ROW_2, "topic_title",
        "A", "B", None, "admin")  # no actor_user_id
    edits = content_edits.list_content_edits(db, co["id"], "topics", ROW_2)
    assert len(edits) == 1
    assert edits[0]["actor_name"] is None
