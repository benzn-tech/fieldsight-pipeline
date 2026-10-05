"""scripts/replay_extractions.py: the S3 event it builds, and that a dry run invokes nothing."""
import io
import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "scripts"))
import replay_extractions as rx  # noqa: E402


class FakeS3:
    def __init__(self, keys):
        self.keys, self.calls = keys, []

    def list_objects_v2(self, **kw):
        self.calls.append(kw)
        return {"Contents": [{"Key": k} for k in self.keys], "IsTruncated": False}


class FakeLambda:
    def __init__(self):
        self.invoked = []

    def invoke(self, **kw):
        self.invoked.append(kw)
        return {"Payload": io.BytesIO(b'{"ok": true}')}


KEYS = ["extractions/Deandre__Alberts/2026-10-05/a.json",
        "extractions/Deandre__Alberts/2026-10-05/b.json",
        "extractions/Deandre__Alberts/2026-10-05/a.txt"]


def test_the_event_is_what_the_bucket_would_send():
    assert rx.build_event("B", "k/x.json") == {
        "Records": [{"s3": {"bucket": {"name": "B"}, "object": {"key": "k/x.json"}}}]}


def test_dry_run_lists_and_never_invokes():
    s3, lam, lines = FakeS3(KEYS), FakeLambda(), []
    keys, results = rx.replay(s3, lam, "prod", "Deandre__Alberts", "2026-10-05",
                              apply=False, out=lines.append)
    assert lam.invoked == [] and results == []
    assert keys == KEYS[:2]                      # only .json
    assert s3.calls[0]["Prefix"] == "extractions/Deandre__Alberts/2026-10-05/"
    assert s3.calls[0]["Bucket"] == "fieldsight-data-509194952652"


def test_apply_invokes_the_env_item_writer_once_per_key():
    lam, lines = FakeLambda(), []
    rx.replay(FakeS3(KEYS), lam, "test", "F", "2026-10-05", apply=True, out=lines.append)
    assert [c["FunctionName"] for c in lam.invoked] == ["fieldsight-test-item-writer"] * 2
    import json
    sent = [json.loads(c["Payload"]) for c in lam.invoked]
    assert sent[0] == rx.build_event("fieldsight-data-test-509194952652", KEYS[0])
    assert any('{"ok": true}' in l for l in lines)   # response payload is printed


@pytest.mark.parametrize("argv", [
    ["--env", "prod", "--folder", "F", "--date", "2026-13-40"],
    ["--env", "prod", "--folder", "F", "--date", "yesterday"],
    ["--env", "prod", "--folder", "Dea ndre", "--date", "2026-10-05"],
    ["--env", "dev", "--folder", "F", "--date", "2026-10-05"],
])
def test_bad_arguments_are_refused(argv):
    with pytest.raises(SystemExit):
        rx.main(argv)
