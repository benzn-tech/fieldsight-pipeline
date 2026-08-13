"""Unit: a missing S3 object is not a failure, and the difference has to stay visible.

`fieldsight-prod-report-errors` sat permanently in ALARM because most days asked for a
`reports/{date}/summary_report.json` that was never written — those days had no content.
An alarm that is always red is worse than no alarm: the next real failure arrives at a
signal everyone has already learned to ignore.

The other half is the part that must NOT be lost. Absent and unreadable look identical to
the caller — both return None — and treating a permissions fault as "nothing there" is the
shape of several silent breakages in this project. So absence drops to INFO and everything
else stays at ERROR, and both halves are asserted here.
"""
import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(
    os.path.abspath(__file__)))), "src"))

gen = pytest.importorskip("lambda_report_generator")


class NoSuchKey(Exception):
    pass


class AccessDenied(Exception):
    pass


class _S3:
    def __init__(self, exc):
        self.exc = exc

    def get_object(self, **kw):
        raise self.exc


def test_a_missing_object_does_not_log_an_error(monkeypatch, caplog):
    monkeypatch.setattr(gen, "s3_client", _S3(NoSuchKey("The specified key does not exist")))
    with caplog.at_level("INFO"):
        assert gen.download_json_from_s3("b", "reports/2026-08-07/summary_report.json") is None
    assert not [r for r in caplog.records if r.levelname == "ERROR"], \
        "an empty day keeps the prod alarm red forever"
    assert any(r.levelname == "INFO" for r in caplog.records), \
        "silence is not the fix either — the absence still has to be visible"


def test_a_real_failure_still_logs_an_error(monkeypatch, caplog):
    """The half that must survive. A permissions fault returns None exactly like an empty
    day, so ERROR is the only thing left that tells them apart."""
    monkeypatch.setattr(gen, "s3_client", _S3(AccessDenied("not authorized")))
    with caplog.at_level("INFO"):
        assert gen.download_json_from_s3("b", "reports/2026-08-07/summary_report.json") is None
    assert [r for r in caplog.records if r.levelname == "ERROR"], \
        "a permissions fault now looks exactly like an empty day"
