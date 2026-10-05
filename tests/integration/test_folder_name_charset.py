"""users_folder_name_charset (0081): the database refuses a folder the upload route
cannot write under. `Deandre'_Alberts` was stored on 2026-10-05; the upload route
wrote `Deandre__Alberts`; 45 minutes of recording had no directory row."""
import pytest
from psycopg.errors import CheckViolation

from repositories import companies, users

pytestmark = pytest.mark.integration


def _co(db):
    return companies.create_company(db, "FolderCharsetCo")["id"]


def test_an_apostrophe_in_a_folder_is_refused(db):
    with pytest.raises(CheckViolation):
        db.cursor().execute(
            "INSERT INTO users (company_id, email, first_name, last_name, global_role, "
            "folder_name, kind) VALUES (%s, '', 'Deandre''', 'Alberts', 'worker', %s, "
            "'field_only')", (_co(db), "Deandre'_Alberts"))


def test_the_hand_set_double_underscore_folder_is_accepted(db):
    row = users.upsert_field_only_user(db, _co(db), "Deandre__Alberts",
                                       "Deandre'", "Alberts", "worker")
    assert row["folder_name"] == "Deandre__Alberts"


def test_a_null_folder_is_accepted(db):
    users.upsert_user(db, "sub-nofolder", "nf@x.nz", company_id=_co(db),
                      first_name="No", last_name="Folder")


def test_set_folder_name_cannot_write_a_bad_key(db):
    users.upsert_user(db, "sub-bad", "bad@x.nz", company_id=_co(db),
                      first_name="Bad", last_name="Key")
    with pytest.raises(CheckViolation):
        users.set_folder_name(db, "sub-bad", "José Núñez")
