"""idx_companies_name_ci: one company per name, ignoring case and spaces."""
import pytest
from psycopg.errors import UniqueViolation

from repositories import companies

pytestmark = pytest.mark.integration


@pytest.mark.parametrize("variant", ["uniqco", "UNIQCO", " UniqCo", "UniqCo  "])
def test_a_case_or_space_variant_cannot_be_inserted(db, variant):
    companies.create_company(db, "UniqCo")
    with pytest.raises(UniqueViolation):
        companies.create_company(db, variant)


def test_different_names_coexist(db):
    companies.create_company(db, "UniqA")
    companies.create_company(db, "UniqB")


def test_a_company_may_change_only_the_case_of_its_own_name(db):
    co = companies.create_company(db, "CaseCo")
    row = companies.update_company(db, co["id"], name="CASECO")
    assert row["name"] == "CASECO"


def test_renaming_onto_another_tenants_name_hits_the_index(db):
    companies.create_company(db, "TakenCo")
    other = companies.create_company(db, "FreeCo")
    with pytest.raises(UniqueViolation):
        companies.update_company(db, other["id"], name="takenco")
