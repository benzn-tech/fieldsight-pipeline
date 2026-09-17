"""Unit: a history row says when its Word file was written, not only its size.

The Reports page now follows a regenerate to completion by polling history until
the report's `generated_at` moves past the value captured at the click. The
generator writes the JSON first and the .docx a moment later
(lambda_report_generator.py: `Saved: ...json` then `Saved: ...docx`). A poll that
lands between the two sees a new `generated_at` beside the OLD `docx_size`, stops,
and shows the refreshed report with a Word file from the previous generation.

`generated_at` is the JSON's LastModified. The row had no equivalent for the Word
file, so "both files are new" could not be asked. This adds `docx_generated_at`,
read from the same listing pass that already collects `docx_size` -- no extra
request per row.

ABSENT, NOT EMPTY, when there is no Word file -- the same rule `docx_key` and
`docx_size` already follow, because "no Word file" and "a backend that never sends
one" must stay distinguishable.
"""
import datetime

import pytest

org = pytest.importorskip(
    "lambda_org_api",
    reason="requires the org api lambda's dependencies (installed in CI)")

JSON_AT = datetime.datetime(2026, 9, 15, 3, 9, 57, tzinfo=datetime.timezone.utc)
DOCX_AT = datetime.datetime(2026, 9, 15, 3, 9, 58, tzinfo=datetime.timezone.utc)
OLD_AT = datetime.datetime(2026, 9, 11, 3, 26, 50, tzinfo=datetime.timezone.utc)

OBJECTS = [
    ("reports/2026-09-03/Ben_UCPK2/daily_report.json", 346775, JSON_AT),
    ("reports/2026-09-03/Ben_UCPK2/daily_report.docx", 40763, DOCX_AT),
    # A report mid-regeneration: new JSON, the Word file still from last time.
    ("reports/2026-09-02/Ben_UCPK2/daily_report.json", 16329, JSON_AT),
    ("reports/2026-09-02/Ben_UCPK2/daily_report.docx", 42289, OLD_AT),
    # No Word file at all.
    ("reports/2026-09-08/Ben_UCPK2/daily_report.json", 6095, JSON_AT),
]


class _Paginator:
    def paginate(self, Bucket=None, Prefix=""):
        items = [{"Key": k, "Size": s, "LastModified": t}
                 for k, s, t in OBJECTS if k.startswith(Prefix)]
        # Split across pages so a .json and its .docx can arrive separately.
        yield {"Contents": items[:2]}
        yield {"Contents": items[2:]}


class _S3:
    def get_paginator(self, _op):
        return _Paginator()


@pytest.fixture
def rows(monkeypatch):
    monkeypatch.setattr(org, "s3", lambda: _S3())
    res = org._read_org_report_history(None, 50)
    return {r["key"]: r for r in res["reports"]}


def test_a_row_says_when_its_word_file_was_written(rows):
    """THE test."""
    r = rows["reports/2026-09-03/Ben_UCPK2/daily_report.json"]
    assert r["docx_generated_at"] == DOCX_AT.isoformat()


def test_it_is_the_word_files_own_time_not_the_jsons(rows):
    """Mid-regeneration: the JSON is new and the Word file is not. The row must
    say so, or a poller cannot tell the regeneration is still unfinished."""
    r = rows["reports/2026-09-02/Ben_UCPK2/daily_report.json"]
    assert r["generated_at"] == JSON_AT.isoformat()
    assert r["docx_generated_at"] == OLD_AT.isoformat()
    assert r["docx_generated_at"] < r["generated_at"]


def test_no_word_file_means_no_field(rows):
    r = rows["reports/2026-09-08/Ben_UCPK2/daily_report.json"]
    assert "docx_generated_at" not in r
    assert "docx_key" not in r
