"""GET /api/reports/history returned only the .json key, and the UI's button
is labelled "Download .docx".

The generator has written a Word file beside every report since the
python-docx layer landed -- 183 of them in production, `daily_report.docx`
39 KiB next to `daily_report.json` 25 KiB for the same day. The history
endpoint never mentioned it, so the download button presigned the JSON and
every "Download .docx" in the product's life handed the user raw JSON.

These tests drive the real handler against a fake S3 that lists both files.
"""
import datetime
import types

import pytest

fapi = pytest.importorskip(
    "lambda_fieldsight_api",
    reason="requires the api lambda's dependencies (installed in CI)")


_TS = datetime.datetime(2026, 9, 2, 4, 7, 9, tzinfo=datetime.timezone.utc)

# One day carrying both artifacts, one carrying only the JSON. The second is
# not hypothetical: Word generation disables itself when the python-docx layer
# is missing or built for the wrong runtime, and production has exactly one
# such day among sixty-one.
KEYS = {
    "reports/2026-09-02/Ben_UCPK2/daily_report.json": 25490,
    "reports/2026-09-02/Ben_UCPK2/daily_report.docx": 40243,
    "reports/2026-09-02/Ben_UCPK2/daily_report_debug.json": 93798,
    "reports/2026-08-16/Ben_UCPK2/weekly_report.json": 5583,
    "reports/2026-08-16/Ben_UCPK2/weekly_report.docx": 31002,
    "reports/2026-09-08/Ben_UCPK2/daily_report.json": 6095,
}


class _FakePaginator:
    def __init__(self, keys):
        self.keys = keys

    def paginate(self, Bucket=None, Prefix=""):
        # Two pages, deliberately splitting a .json from its .docx: the
        # endpoint must not depend on seeing them in the same page.
        items = [{"Key": k, "LastModified": _TS, "Size": s}
                 for k, s in self.keys.items() if k.startswith(Prefix)]
        mid = len(items) // 2
        yield {"Contents": items[:mid]}
        yield {"Contents": items[mid:]}


class _FakeS3:
    def __init__(self, keys):
        self.keys = keys

    def get_paginator(self, _op):
        return _FakePaginator(self.keys)


@pytest.fixture
def s3(monkeypatch):
    fake = _FakeS3(dict(KEYS))
    monkeypatch.setattr(fapi, "s3_client", fake)
    monkeypatch.setattr(fapi, "accessible_folder_scope", lambda caller: None)
    return fake


ADMIN = {"role": "admin", "sub": "admin-sub", "email": "a@b.c"}


def _rows(res):
    import json
    return json.loads(res["body"])["reports"]


def _by_key(rows, key):
    return next(r for r in rows if r["key"] == key)


def test_the_fixture_reaches_the_handler(s3):
    """Self-check. Every assertion below reads rows off this list; if the
    handler returned nothing they would all pass vacuously."""
    rows = _rows(fapi.get_report_history({"limit": "50"}, ADMIN))
    assert len(rows) == 3, rows


def test_a_report_with_a_word_file_carries_its_key(s3):
    rows = _rows(fapi.get_report_history({"limit": "50"}, ADMIN))
    r = _by_key(rows, "reports/2026-09-02/Ben_UCPK2/daily_report.json")
    assert r["docx_key"] == "reports/2026-09-02/Ben_UCPK2/daily_report.docx"
    assert r["docx_size"] == 40243


def test_the_word_key_is_found_across_pages(s3):
    """The .docx and its .json can land in different pages of the listing;
    a single-pass "is the previous key its docx" would miss them."""
    rows = _rows(fapi.get_report_history({"limit": "50"}, ADMIN))
    r = _by_key(rows, "reports/2026-08-16/Ben_UCPK2/weekly_report.json")
    assert r["docx_key"] == "reports/2026-08-16/Ben_UCPK2/weekly_report.docx"


def test_a_report_with_no_word_file_has_no_docx_key_at_all(s3):
    """ABSENT, not empty string, not the json key. A client has to be able to
    tell "no Word file for this report" from "this backend does not send
    one", and a falsy-but-present value collapses those."""
    rows = _rows(fapi.get_report_history({"limit": "50"}, ADMIN))
    r = _by_key(rows, "reports/2026-09-08/Ben_UCPK2/daily_report.json")
    assert "docx_key" not in r
    assert "docx_size" not in r


def test_the_word_files_do_not_become_rows_of_their_own(s3):
    """Otherwise the history list doubles in length and every report appears
    twice, once undownloadable."""
    rows = _rows(fapi.get_report_history({"limit": "50"}, ADMIN))
    assert all(r["key"].endswith(".json") for r in rows), rows
    assert len(rows) == 3


def test_the_size_shown_is_still_the_report_not_the_word_file(s3):
    rows = _rows(fapi.get_report_history({"limit": "50"}, ADMIN))
    r = _by_key(rows, "reports/2026-09-02/Ben_UCPK2/daily_report.json")
    assert r["size"] == 25490


def test_debug_artifacts_are_still_excluded(s3):
    rows = _rows(fapi.get_report_history({"limit": "50"}, ADMIN))
    assert not any("_debug" in r["key"] for r in rows)


def test_scoping_still_applies_and_a_word_file_cannot_smuggle_a_row_past_it(
        s3, monkeypatch):
    """The .docx branch runs BEFORE the folder-scope check, because it has to
    collect files for reports it may never emit. That must not become a way
    for an out-of-scope report to appear."""
    monkeypatch.setattr(fapi, "accessible_folder_scope", lambda caller: {"Someone_Else"})
    rows = _rows(fapi.get_report_history({"limit": "50"}, ADMIN))
    assert rows == []
