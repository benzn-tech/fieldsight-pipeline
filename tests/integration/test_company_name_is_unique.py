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
