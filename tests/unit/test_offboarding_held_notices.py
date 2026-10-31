"""Company offboarding, Task 9 (spec 2026-11-01 §5.5): an external-member notice about a
site whose company is suspended / exited is HELD -- not sent, not moved to sent/, and
not expired -- and goes out after reactivation.

The backlog lambda has no database; the hold reads the published directory
(`inactive_companies`, written by org-api on every status change). A held notice is
copied onto itself once half-way to NOTICE_EXPIRY, so its 24 h clock restarts and it can
never be consumed by the expiry while held -- nor in the minutes after reactivation."""
import json
import logging
from datetime import datetime, timedelta, timezone

import pytest

bl = pytest.importorskip("lambda_extraction_backlog")
import directory  # noqa: E402

NOW = datetime(2026, 11, 1, 12, 0, tzinfo=timezone.utc)
KEY = "notices/external_member/m-9.json"
NOTICE = {"to_email": "ext@home.nz", "to_name": "Eve", "site_name": "Tower B",
          "inviting_company": "Site Co", "role": "pm", "site_id": "s-B",
          "site_company_id": "co-B", "created_at": "2026-11-01T11:50:00+00:00"}
DOC = {"version": 2, "people": {}, "sites": {"s-B": {"slug": "b", "company_id": "co-B"}}}


class S3:
    def __init__(self, objs):
        self.objs = dict(objs)      # key -> (LastModified, body dict)
        self.puts = []

    def get_paginator(self, name):
        outer = self

        class _P:
            def paginate(self, Bucket=None, Prefix=""):
                yield {"Contents": [{"Key": k, "LastModified": v[0]}
                                    for k, v in sorted(outer.objs.items()) if k.startswith(Prefix)]}
        return _P()

    def get_object(self, Bucket=None, Key=None):
        body = json.dumps(self.objs[Key][1]).encode()
        return {"Body": type("B", (), {"read": lambda s: body})()}

    def copy_object(self, Bucket=None, Key=None, CopySource=None, **kw):
        if Key == CopySource["Key"]:
            # In place: S3 refuses it without REPLACE; LastModified becomes now.
            assert kw.get("MetadataDirective") == "REPLACE"
            self.puts.append(Key)
            self.objs[Key] = (NOW, self.objs[Key][1])
        else:
            self.objs[Key] = self.objs[CopySource["Key"]]

    def delete_object(self, Bucket=None, Key=None):
        self.objs.pop(Key, None)


class Sender:
    def __init__(self):
        self.sent = []

    def send(self, to, subject, body_text, body_html=None):
        self.sent.append(to)
        return "id"


@pytest.fixture
def published(monkeypatch):
    """The directory the backlog reads; tests set `inactive_companies` on it."""
    doc = json.loads(json.dumps(DOC))
    monkeypatch.setattr(bl.directory, "load", lambda client, bucket: doc)
    return doc


def _run(s3, now=NOW):
    snd = Sender()
    out = bl.send_external_member_notices(s3, now=now, sender=snd)
    return out, snd


def test_held_while_the_site_company_is_inactive(monkeypatch, published):
    monkeypatch.setenv("COMPANY_STATUS_ENFORCE", "on")
    published["inactive_companies"] = {"co-B": "suspended"}
    s3 = S3({KEY: (NOW - timedelta(minutes=5), NOTICE)})
    out, snd = _run(s3)
    assert out == (0, 0, 0) and snd.sent == []
    assert KEY in s3.objs and not any(k.startswith("notices/sent/") for k in s3.objs)


def test_a_held_notice_never_expires_and_goes_out_after_reactivation(monkeypatch, published):
    monkeypatch.setenv("COMPANY_STATUS_ENFORCE", "on")
    published["inactive_companies"] = {"co-B": "exited"}
    # Older than the 24 h expiry: a notice that is not held would be deleted now.
    s3 = S3({KEY: (NOW - timedelta(hours=30), NOTICE)})
    out, snd = _run(s3)
    assert out == (0, 0, 0) and snd.sent == []
    assert KEY in s3.objs, "a held notice must not be consumed by the expiry"
    assert s3.puts == [KEY] and s3.objs[KEY][0] == NOW, "its clock restarts"
    assert s3.objs[KEY][1] == NOTICE, "same notice, unchanged"
    # A young held notice is left alone (no copy every tick).
    s3.puts.clear()
    _run(s3, now=NOW + timedelta(hours=1))
    assert s3.puts == []
    # Reactivated (the directory is republished without the company): sent next tick.
    published.pop("inactive_companies")
    out, snd = _run(s3, now=NOW + timedelta(hours=13))
    assert out == (1, 0, 0) and snd.sent == ["ext@home.nz"]
    assert KEY not in s3.objs and "notices/sent/external_member/m-9.json" in s3.objs


def test_active_site_company_sends_as_before(monkeypatch, published):
    monkeypatch.setenv("COMPANY_STATUS_ENFORCE", "on")
    published["inactive_companies"] = {"co-OTHER": "suspended"}
    s3 = S3({KEY: (NOW - timedelta(minutes=5), NOTICE)})
    out, snd = _run(s3)
    assert out == (1, 0, 0) and snd.sent == ["ext@home.nz"]


def test_flag_off_sends_and_logs(monkeypatch, published, caplog):
    monkeypatch.setenv("COMPANY_STATUS_ENFORCE", "off")
    caplog.set_level(logging.WARNING)
    published["inactive_companies"] = {"co-B": "suspended"}
    s3 = S3({KEY: (NOW - timedelta(minutes=5), NOTICE)})
    out, snd = _run(s3)
    assert out == (1, 0, 0) and snd.sent == ["ext@home.nz"]
    lines = [r.getMessage() for r in caplog.records
             if "COMPANY_GATE_WOULD_SUPPRESS" in r.getMessage()]
    assert len(lines) == 1 and "where=external-member-notice" in lines[0]


def test_flag_off_still_expires_as_before(monkeypatch, published):
    monkeypatch.setenv("COMPANY_STATUS_ENFORCE", "off")
    published["inactive_companies"] = {"co-B": "suspended"}
    s3 = S3({KEY: (NOW - timedelta(hours=25), NOTICE)})
    out, snd = _run(s3)
    assert out == (0, 0, 1) and KEY not in s3.objs


def test_site_unknown_to_the_directory_uses_the_notice_company(monkeypatch, published):
    monkeypatch.setenv("COMPANY_STATUS_ENFORCE", "on")
    published["sites"] = {}
    published["inactive_companies"] = {"co-B": "suspended"}
    s3 = S3({KEY: (NOW - timedelta(minutes=5), NOTICE)})
    out, snd = _run(s3)
    assert out == (0, 0, 0) and KEY in s3.objs


def test_a_notice_written_before_this_change_is_sent(monkeypatch, published):
    monkeypatch.setenv("COMPANY_STATUS_ENFORCE", "on")
    published["inactive_companies"] = {"co-B": "suspended"}
    old = {k: v for k, v in NOTICE.items() if k not in ("site_id", "site_company_id")}
    s3 = S3({KEY: (NOW - timedelta(minutes=5), old)})
    out, snd = _run(s3)
    assert out == (1, 0, 0)


def test_no_inactive_company_reads_no_notice_body_before_the_expiry(monkeypatch, published):
    """Nobody inactive: the loop is the one it always was (an old notice expires
    without being read)."""
    monkeypatch.setenv("COMPANY_STATUS_ENFORCE", "on")
    s3 = S3({KEY: (NOW - timedelta(hours=25), NOTICE)})
    s3.get_object = lambda **kw: pytest.fail("read before the expiry")
    out, _ = _run(s3)
    assert out == (0, 0, 1)


def test_nothing_waiting_reads_no_directory(monkeypatch):
    monkeypatch.setattr(bl.directory, "load",
                        lambda *a: pytest.fail("no notice, no directory read"))
    assert bl.send_external_member_notices(S3({}), now=NOW, sender=Sender()) == (0, 0, 0)


def test_the_reader_is_the_shared_directory_module():
    assert bl.directory is directory


def test_the_backlog_function_may_read_the_directory():
    import re
    from pathlib import Path
    t = (Path(__file__).resolve().parents[2] / "src" / "template.yaml").read_text(encoding="utf-8")
    block = re.search(r"^  ExtractionBacklogFunction:\n(.*?)(?=^  [A-Za-z]\w*:\n)", t,
                      re.S | re.M).group(1)
    assert "${DataBucketName}/config/directory.json" in block
    assert "COMPANY_STATUS_ENFORCE: !Ref CompanyStatusEnforce" in block


class _BadBodyS3(S3):
    """A notice whose body is not JSON (or cannot be read)."""
    def __init__(self, objs, body=b"{not json", raise_on_read=False):
        super().__init__(objs)
        self._body, self._raise = body, raise_on_read

    def get_object(self, Bucket=None, Key=None):
        if self._raise:
            raise RuntimeError("unreadable")
        body = self._body
        return {"Body": type("B", (), {"read": lambda s: body})()}


@pytest.mark.parametrize("raise_on_read", [False, True], ids=["malformed", "unreadable"])
def test_flag_off_malformed_notice_still_expires_as_before(monkeypatch, published,
                                                           raise_on_read):
    """R1: the hold check must never change the flag-off outcome. A body that cannot be
    read / parsed while some company is inactive expires exactly as it did before."""
    monkeypatch.setenv("COMPANY_STATUS_ENFORCE", "off")
    published["inactive_companies"] = {"co-B": "suspended"}
    s3 = _BadBodyS3({KEY: (NOW - timedelta(hours=25), NOTICE)}, raise_on_read=raise_on_read)
    out, snd = _run(s3)
    assert out == (0, 0, 1) and KEY not in s3.objs


def test_flag_on_malformed_notice_falls_through_to_the_old_path(monkeypatch, published):
    monkeypatch.setenv("COMPANY_STATUS_ENFORCE", "on")
    published["inactive_companies"] = {"co-B": "suspended"}
    s3 = _BadBodyS3({KEY: (NOW - timedelta(hours=25), NOTICE)})
    out, snd = _run(s3)
    assert out == (0, 0, 1) and KEY not in s3.objs
