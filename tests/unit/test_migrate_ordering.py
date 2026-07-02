from db.migrate import pending_versions, parse_version


def test_parse_version_reads_numeric_prefix():
    assert parse_version("0003_dashboard_readmodel.sql") == 3


def test_pending_versions_orders_and_filters():
    files = ["0002_core.sql", "0001_extensions.sql", "0003_read.sql"]
    applied = {"0001_extensions.sql"}
    assert pending_versions(files, applied) == ["0002_core.sql", "0003_read.sql"]


def test_pending_versions_empty_when_all_applied():
    files = ["0001_extensions.sql"]
    assert pending_versions(files, {"0001_extensions.sql"}) == []
