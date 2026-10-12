"""Task 8b (ruling C3): count_transcripts_for_date lists transcripts/{folder}/{date}/ once per
folder in config/directory.json people; it never lists the whole transcripts/ prefix unless the
directory is absent, and then it says so with a WARNING."""
import logging

import pytest

rg = pytest.importorskip("lambda_report_generator",
                         reason="requires the report generator's dependencies (installed in CI)")

D = "2026-10-01"
KEYS = [
    f"transcripts/Ada_L/{D}/a1.json", f"transcripts/Ada_L/{D}/a2.json",
    f"transcripts/Ada_L/{D}/notes.txt",
    f"transcripts/Bo_K/{D}/b1.json",
    "transcripts/Bo_K/2026-09-30/old.json",
    f"transcripts/Ghost_Z/{D}/g1.json",          # a folder the directory does not know
]


@pytest.fixture
def lister(monkeypatch):
    prefixes = []

    def lst(bucket, prefix):
        prefixes.append(prefix)
        return [{"key": k} for k in KEYS if k.startswith(prefix)]
    monkeypatch.setattr(rg, "list_s3_objects", lst)
    return prefixes


def _dir(monkeypatch, people):
    monkeypatch.setattr(rg.directory, "load",
                        lambda c, b: {"version": 1, "people": people, "sites": {}})


def test_counts_per_folder_and_never_lists_the_whole_prefix(monkeypatch, lister):
    _dir(monkeypatch, {"Ada_L": {}, "Bo_K": {}})
    assert rg.count_transcripts_for_date("b", D) == 3
    assert sorted(lister) == [f"transcripts/Ada_L/{D}/", f"transcripts/Bo_K/{D}/"]
    assert "transcripts/" not in lister


def test_absent_directory_falls_back_to_the_old_listing_with_a_warning(monkeypatch, lister, caplog):
    _dir(monkeypatch, {})
    with caplog.at_level(logging.WARNING):
        assert rg.count_transcripts_for_date("b", D) == 4   # the old walk also sees Ghost_Z
    assert lister == ["transcripts/"]
    assert any("directory absent" in r.message for r in caplog.records)


def test_an_empty_day_for_known_folders_is_zero_not_a_fallback(monkeypatch, lister):
    _dir(monkeypatch, {"Ada_L": {}})
    assert rg.count_transcripts_for_date("b", "2026-01-01") == 0
    assert lister == ["transcripts/Ada_L/2026-01-01/"]
