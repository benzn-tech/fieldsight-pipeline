"""Unit: the gateway PRODUCTION actually calls must name the Word file.

The generator has always written `daily_report.docx` beside the JSON -- 181 of
them in prod. The Reports page's download button was labelled ".docx" and
presigned whatever `key` it was handed, so every download served raw JSON.

That was fixed on the LEGACY gateway, and the fix could not reach anybody.
`scripts/api/reports.js` routes `/reports/history` to ORG-API whenever
`timelineSource === 'aurora'` and an org base URL is set -- which is exactly
prod's Amplify config (`FS_TIMELINE_SOURCE=aurora`, `FS_ORG_BASEURL` set). Two
gateways serve this path, the repaired one is not the one production calls, and
the frontend's "fall back to .json when there is no docx_key" then fired on
every row.

So this is the same test as `test_report_history_carries_the_word_file.py`,
pointed at the other gateway -- and it is a test that exists because fixing one
of two implementations is this repo's oldest failure, and it caught the fix
itself this time.
"""
import datetime

import pytest

org = pytest.importorskip(
    "lambda_org_api",
    reason="requires the org api lambda's dependencies (installed in CI)")

_TS = datetime.datetime(2026, 9, 2, 4, 7, 9, tzinfo=datetime.timezone.utc)

# One day carrying both artifacts, one carrying only the JSON. The second is not
# hypothetical: Word generation disables itself when the python-docx layer is
# missing or built for the wrong runtime -- one WARNING, no error -- and prod has
# exactly one such day.
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
        # Two pages, deliberately splitting a .json from its .docx: the endpoint
        # must not depend on seeing them in the same page.
        items = [{"Key": k, "LastModified": _TS, "Size": s}
                 for k, s in self.keys.items() if k.startswith(Prefix)]
        mid = len(items) // 2
        yield {"Contents": items[:mid]}
        yield {"Contents": items[mid:]}


class _FakeS3:
    def get_paginator(self, _op):
        return _FakePaginator(KEYS)


@pytest.fixture
def s3ed(monkeypatch):
    monkeypatch.setattr(org, "s3", lambda: _FakeS3())
    return monkeypatch


def _by_key(res):
    """`_read_org_report_history` returns {"reports": [...]}, not a bare list --
    it is the endpoint's body, one layer up from the legacy helper's."""
    return {r["key"]: r for r in res["reports"]}


def test_a_report_with_a_word_file_names_it(s3ed):
    rows = _by_key(org._read_org_report_history(None, 50))
    r = rows["reports/2026-09-02/Ben_UCPK2/daily_report.json"]
    assert r["docx_key"] == "reports/2026-09-02/Ben_UCPK2/daily_report.docx"
    assert r["docx_size"] == 40243


def test_a_report_without_one_omits_the_key_entirely(s3ed):
    """ABSENT, not empty. A client has to be able to tell "this report has no
    Word file" from "this backend never sends one" -- and only absence says the
    second thing. An empty string would read as a key and presign to nothing."""
    r = _by_key(org._read_org_report_history(None, 50))[
        "reports/2026-09-08/Ben_UCPK2/daily_report.json"]
    assert "docx_key" not in r
    assert "docx_size" not in r


def test_the_docx_is_never_a_row_of_its_own(s3ed):
    """It is an attribute of the report, not another report. Listing it would
    double every row on the page and give half of them no JSON to open."""
    keys = [r["key"] for r in org._read_org_report_history(None, 50)["reports"]]
    assert not any(k.endswith(".docx") for k in keys)


def test_pairing_survives_the_pages_they_arrive_on(s3ed):
    """The fixture splits the listing so a .json and its .docx land in different
    pages. Pairing per page would silently lose one of the two real pairs."""
    rows = _by_key(org._read_org_report_history(None, 50))
    paired = [k for k, r in rows.items() if "docx_key" in r]
    assert len(paired) == 2, paired


def test_the_weekly_report_pairs_too(s3ed):
    """`_report.docx` matching must not be spelled `daily_report.docx` -- weekly
    and monthly are written by the same generator."""
    r = _by_key(org._read_org_report_history(None, 50))[
        "reports/2026-08-16/Ben_UCPK2/weekly_report.json"]
    assert r["docx_key"].endswith("weekly_report.docx")


def test_folder_scope_still_filters(s3ed):
    """The Word pass must not become a way past the ACL: an empty scope means
    deny-all and nothing may come back through the docx branch either."""
    assert org._read_org_report_history(set(), 50)["reports"] == []
    assert org._read_org_report_history({"Ben_UCPK2"}, 50)["reports"] != []
