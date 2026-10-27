"""Report bench, Task 7: read-only owner-folder collector (fake S3 only)."""
import importlib.util
import json
import os
import sys

import pytest

REPO = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
BENCH_DIR = os.path.join(REPO, "tools", "report-bench")


def _load(name):
    if name in sys.modules:
        return sys.modules[name]
    spec = importlib.util.spec_from_file_location(name, os.path.join(BENCH_DIR, name + ".py"))
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


col = _load("rb_collect")
common = _load("rb_common")
SECRET = "TURNSENTINEL words spoken on site"
CFG = {"confirmed": True, "folders": ["Ben_Lin_test2", "_summary"]}


class FakeS3:
    def __init__(self):
        self.calls = []

    def __getattr__(self, name):
        def fn(*a, **kw):
            self.calls.append(name)
            return {}
        return fn


@pytest.fixture
def confirmed(monkeypatch):
    monkeypatch.setattr(col, "load_owner_folders", lambda path=None: CFG)


def test_folder_guards(confirmed):
    with pytest.raises(PermissionError):
        col.check_folder("SomeCustomer")
    with pytest.raises(PermissionError):
        col.check_folder("_summary")              # listed, still refused
    col.check_folder("Ben_Lin_test2")


def test_unconfirmed_refuses_everything():
    cfg = col.load_owner_folders()
    assert cfg["confirmed"] is False               # ships unconfirmed
    for f in cfg["folders"]:
        with pytest.raises(PermissionError):
            col.check_folder(f, cfg)


def test_read_only_s3_blocks_writes():
    fake = FakeS3()
    ro = col.ReadOnlyS3(fake)
    for name in ("put_object", "delete_object", "copy_object", "upload_file",
                 "delete_objects", "copy"):
        with pytest.raises(PermissionError):
            getattr(ro, name)
    assert fake.calls == []
    ro.get_object(Bucket="b", Key="transcripts/Ben_Lin_test2/2026-10-01/x.json")
    assert fake.calls == ["get_object"]
    with pytest.raises(PermissionError):
        ro.get_object(Bucket="b", Key="reports/2026-10-01/_summary/daily_report_debug.json")
    with pytest.raises(PermissionError):
        ro.list_objects_v2(Bucket="b", Prefix="reports/2026-10-01/_summary/")


def test_never_write_prefixes():
    for k in ("transcripts/a", "extraction_requests/x", "extractions/y", "reports/z"):
        with pytest.raises(PermissionError):
            col.assert_never_write(k)
    col.assert_never_write("bench/ok")


def _sessions(tmp_path, folder="Ben_Lin_test2"):
    p = tmp_path / "sessions.json"
    p.write_text(json.dumps([{"folder": folder, "date": "2026-10-01",
                              "session_base": "sid123", "bucket": "b", "role": "set"}]),
                 encoding="utf-8")
    return str(p)


def test_collect_writes_only_under_inputs(tmp_path, confirmed):
    out = tmp_path / "out"
    seen = []

    def x_loader(s3, bucket, folder, date, base):
        seen.append(type(s3).__name__)
        return [{"abs_start_str": "09:00:00", "speaker": "spk_0", "text": SECRET,
                 "source_filename": "f.json"}], 2

    def r_loader(s3, bucket, folder, date):
        seen.append(type(s3).__name__)
        return {"kwargs": {"transcripts_with_photos": [{"text": SECRET}]}, "n_transcripts": 1}

    m = col.collect(_sessions(tmp_path), str(out), FakeS3(), x_loader, r_loader)
    assert seen == ["ReadOnlyS3", "ReadOnlyS3"]
    files = sorted(os.path.relpath(os.path.join(d, f), out)
                   for d, _, fs in os.walk(out) for f in fs)
    assert all(f.startswith("inputs" + os.sep) for f in files)
    turns_path = out / "inputs" / "X" / "sid123.turns.json"
    assert m["sid123"]["sha256"] == common.sha256_file(str(turns_path))
    assert m["sid123"]["n_segments"] == 2
    assert "Ben_Lin_test2_2026-10-01" in m["R"]


def test_collect_refuses_before_any_s3_call(tmp_path, confirmed):
    fake = FakeS3()
    with pytest.raises(ValueError):
        col.collect(_sessions(tmp_path), os.path.join(REPO, "tmp-out"), fake)
    with pytest.raises(PermissionError):
        col.collect(_sessions(tmp_path, "SomeCustomer"), str(tmp_path / "o"), fake)
    assert fake.calls == []


def test_default_x_loader_reads_through_the_wrapper(tmp_path, confirmed):
    class Pager:
        def paginate(self, **kw):
            assert kw["Prefix"] == "transcripts/Ben_Lin_test2/2026-10-01/"
            return [{"Contents": []}]

    class S3(FakeS3):
        def get_paginator(self, name):
            self.calls.append("get_paginator")
            return Pager()

    fake = S3()
    with pytest.raises(LookupError):
        col.load_x(col.ReadOnlyS3(fake), "b", "Ben_Lin_test2", "2026-10-01", "sid1")
    assert fake.calls == ["get_paginator"]


_ORIG_COLLECT = col.collect


def _collect_with_fakes(sessions, out, s3, with_r=True):
    return _ORIG_COLLECT(sessions, out, s3, x_loader=lambda *a: ([{"text": SECRET}], 1),
                         with_r=with_r)


def test_cli_stdout_has_no_turn_text(tmp_path, confirmed, monkeypatch, capsys):
    monkeypatch.setattr(col, "collect", _collect_with_fakes)
    fake_boto = type(sys)("boto3")
    fake_boto.Session = lambda profile_name=None: type(
        "S", (), {"client": lambda self, n: FakeS3()})()
    monkeypatch.setitem(sys.modules, "boto3", fake_boto)
    col.main(["--sessions", _sessions(tmp_path), "--out", str(tmp_path / "o"), "--no-r"])
    out = capsys.readouterr().out
    assert "sid123" in out and SECRET not in out


def test_debug_cmd_refuses_summary(tmp_path, confirmed):
    with pytest.raises(PermissionError):
        col.debug_fetch_command("b", "2026-10-01", "_summary", str(tmp_path))
    line = col.debug_fetch_command("b", "2026-10-01", "Ben_Lin_test2", str(tmp_path))
    assert line.startswith("aws s3 cp s3://b/reports/2026-10-01/Ben_Lin_test2/"
                           "daily_report_debug.json ")
