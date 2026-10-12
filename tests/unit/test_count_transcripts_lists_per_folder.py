"""Task 8b (ruling C3, revised): count_transcripts_for_date finds the folders under
transcripts/ with one delimiter listing and then lists transcripts/{folder}/{date}/ per folder.
It never walks every object under transcripts/, and it does not depend on the directory:
legacy folders (Ben_Lin, Ben_UCPK_) still hold transcripts and are in no directory entry."""
import pytest

rg = pytest.importorskip("lambda_report_generator",
                         reason="requires the report generator's dependencies (installed in CI)")

D = "2026-10-01"
KEYS = [
    f"transcripts/Ada_L/{D}/a1.json", f"transcripts/Ada_L/{D}/a2.json",
    f"transcripts/Ada_L/{D}/notes.txt",
    f"transcripts/Bo_K/{D}/b1.json",
    "transcripts/Bo_K/2026-09-30/old.json",
    f"transcripts/Ghost_Z/{D}/g1.json",          # a folder no directory entry knows
]


class _Paginator:
    def __init__(self, calls):
        self.calls = calls

    def paginate(self, Bucket, Prefix, Delimiter):
        self.calls.append((Prefix, Delimiter))
        folders = sorted({k.split("/")[1] for k in KEYS})
        yield {"CommonPrefixes": [{"Prefix": f"transcripts/{f}/"} for f in folders]}


@pytest.fixture
def s3(monkeypatch):
    listed, delimited = [], []

    def lst(bucket, prefix):
        listed.append(prefix)
        return [{"key": k} for k in KEYS if k.startswith(prefix)]

    class _C:
        def get_paginator(self, name):
            assert name == "list_objects_v2"
            return _Paginator(delimited)
    monkeypatch.setattr(rg, "list_s3_objects", lst)
    monkeypatch.setattr(rg, "s3_client", _C())
    return listed, delimited


def test_counts_every_folder_including_ones_no_directory_knows(s3):
    listed, delimited = s3
    assert rg.count_transcripts_for_date("b", D) == 4
    assert delimited == [("transcripts/", "/")]
    assert sorted(listed) == [f"transcripts/{f}/{D}/" for f in ("Ada_L", "Bo_K", "Ghost_Z")]


def test_never_walks_the_whole_prefix(s3):
    listed, _ = s3
    rg.count_transcripts_for_date("b", D)
    assert "transcripts/" not in listed


def test_an_empty_day_is_zero(s3):
    assert rg.count_transcripts_for_date("b", "2026-01-01") == 0
