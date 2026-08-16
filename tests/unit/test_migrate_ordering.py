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


def test_two_files_sharing_a_version_have_a_defined_order():
    """`schema_migrations` is keyed on the full filename, so a duplicate version number does
    NOT make one file silently skip — both apply. What it does remove is the ordering
    guarantee: `sorted(key=parse_version)` is stable, so among equal versions the order is
    whatever `os.listdir` happened to return, which is not defined.

    Two collisions already exist and shipped (0041_user_deletion / 0041_turn_name_display,
    then 0044_chunk_archive / 0044_speaker_name_rejections, all four applied on TEST). They
    were harmless because the pairs touch unrelated tables. The next pair might not be, and
    the failure would be a migration applied in the wrong order on ONE environment only —
    reproducible nowhere, because the input order differs per filesystem.

    Tie-breaking on the filename costs nothing when versions are unique: the version already
    decides, and a second key is only ever consulted for a tie."""
    a, b = "0044_chunk_archive.sql", "0044_speaker_name_rejections.sql"
    assert pending_versions([a, b], set()) == pending_versions([b, a], set()), (
        "the order of two same-version migrations depends on the order the directory "
        "listing happened to produce")


def test_the_version_still_decides_before_the_name():
    """The tie-break must not become the sort. `0009_x` runs before `0010_a` even though
    '0010_a' < '0009_x' is false only numerically — a plain string sort would get 0009/0010
    right but 0009/00100 wrong, and version numbers are not zero-padded forever."""
    files = ["0010_a.sql", "0009_x.sql", "0100_z.sql"]
    assert pending_versions(files, set()) == ["0009_x.sql", "0010_a.sql", "0100_z.sql"]
