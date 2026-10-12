"""lake_put / lake_upload_file: object put first, manifest line second, never raises for the manifest."""
import io
import json
import logging
import os
import re
import sys
from datetime import datetime, timezone

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "src"))

import lake_put  # noqa: E402

BUCKET = "b"
TENANT_KEY = "transcripts/Ben_Lin/2026-10-12/sidabc.json"
HINT = {"company_id": "c1", "site_id": "s1", "user_id": None, "folder": "Ben_Lin"}


class FakeS3:
    def __init__(self, fail_manifest=False):
        self.calls = []
        self.fail_manifest = fail_manifest
        self.object_error = None

    def put_object(self, **kw):
        self.calls.append(("put_object", kw))
        if kw["Key"].startswith("lake_manifest/"):
            if self.fail_manifest:
                raise RuntimeError("manifest boom")
        elif self.object_error:
            raise self.object_error
        return {"ETag": "etag-" + kw["Key"]}

    def upload_file(self, filename, bucket, key, ExtraArgs=None):
        self.calls.append(("upload_file", {"Filename": filename, "Bucket": bucket, "Key": key,
                                           "ExtraArgs": ExtraArgs}))

    def put_object_tagging(self, **kw):  # must never be used
        self.calls.append(("put_object_tagging", kw))

    def manifests(self):
        return [c for c in self.calls if c[0] == "put_object" and c[1]["Key"].startswith("lake_manifest/")]


@pytest.fixture(autouse=True)
def _env(monkeypatch):
    monkeypatch.delenv("LAKE_MANIFEST", raising=False)


def test_happy_path_object_then_manifest_in_same_bucket():
    s3 = FakeS3()
    resp = lake_put.lake_put(BUCKET, TENANT_KEY, b"hello", owner_hint=HINT, client=s3)
    assert resp == {"ETag": "etag-" + TENANT_KEY}
    assert s3.calls[0][1]["Key"] == TENANT_KEY
    assert len(s3.calls) == 2
    m = s3.calls[1][1]
    assert m["Bucket"] == BUCKET
    assert re.fullmatch(r"lake_manifest/\d{4}/\d{2}/\d{2}/[0-9a-f-]{36}\.jsonl", m["Key"])
    lines = m["Body"].decode().splitlines()
    assert len(lines) == 1
    d = json.loads(lines[0])
    assert d["v"] == 1 and d["key"] == TENANT_KEY and d["bucket"] == BUCKET and d["size"] == 5
    assert d["hint"] == {"company_id": "c1", "site_id": "s1", "folder": "Ben_Lin"}  # nulls dropped
    assert datetime.fromisoformat(d["t"]).tzinfo is not None


def test_never_tags_and_passes_kwargs_through_untouched():
    s3 = FakeS3()
    lake_put.lake_put(BUCKET, TENANT_KEY, b"x", owner_hint=HINT, client=s3,
                      ContentType="application/json", IfNoneMatch="*", IfMatch="e",
                      Metadata={"a": "b"})
    obj = s3.calls[0][1]
    assert obj == {"Bucket": BUCKET, "Key": TENANT_KEY, "Body": b"x", "ContentType": "application/json",
                   "IfNoneMatch": "*", "IfMatch": "e", "Metadata": {"a": "b"}}
    assert not [c for c in s3.calls if c[0] == "put_object_tagging"]
    assert all("Tagging" not in c[1] for c in s3.calls)


def test_object_put_raises_same_exception_and_no_manifest():
    s3 = FakeS3()
    err = RuntimeError("PreconditionFailed")
    s3.object_error = err
    with pytest.raises(RuntimeError) as ei:
        lake_put.lake_put(BUCKET, TENANT_KEY, b"x", owner_hint=HINT, client=s3, IfNoneMatch="*")
    assert ei.value is err
    assert s3.manifests() == []
    assert len(s3.calls) == 1


def test_manifest_failure_is_a_warning_and_returns_object_response(caplog):
    s3 = FakeS3(fail_manifest=True)
    with caplog.at_level(logging.WARNING, logger="lake_put"):
        resp = lake_put.lake_put(BUCKET, TENANT_KEY, b"x", owner_hint=HINT, client=s3)
    assert resp == {"ETag": "etag-" + TENANT_KEY}
    recs = [r for r in caplog.records if r.levelno == logging.WARNING and "manifest" in r.getMessage()]
    assert len(recs) == 1 and TENANT_KEY in recs[0].getMessage()


def test_kill_switch_read_at_call_time(monkeypatch):
    s3 = FakeS3()
    monkeypatch.setenv("LAKE_MANIFEST", "off")
    lake_put.lake_put(BUCKET, TENANT_KEY, b"x", owner_hint=HINT, client=s3)
    assert len(s3.calls) == 1 and s3.manifests() == []
    monkeypatch.setenv("LAKE_MANIFEST", "on")  # flipped back in the same process
    lake_put.lake_put(BUCKET, TENANT_KEY, b"x", owner_hint=HINT, client=s3)
    assert len(s3.manifests()) == 1


def test_manifest_false_argument_skips_the_line():
    s3 = FakeS3()
    lake_put.lake_put(BUCKET, TENANT_KEY, b"x", client=s3, manifest=False)
    assert len(s3.calls) == 1


@pytest.mark.parametrize("key", ["config/directory.json", "download_claims/abc",
                                 "lake_manifest/2026/10/12/x.jsonl"])
def test_global_keys_are_never_manifested(key):
    s3 = FakeS3()
    lake_put.lake_put(BUCKET, key, b"x", client=s3)
    assert len(s3.calls) == 1


def test_unknown_key_shape_is_recorded_for_the_drain_to_report():
    s3 = FakeS3()
    lake_put.lake_put(BUCKET, "users/a/b.wav", b"x", client=s3)
    assert len(s3.manifests()) == 1


def test_size_is_null_for_streams():
    s3 = FakeS3()
    lake_put.lake_put(BUCKET, TENANT_KEY, io.BytesIO(b"abc"), client=s3)
    assert json.loads(s3.manifests()[0][1]["Body"])["size"] is None


def test_upload_file_passes_extraargs_unchanged_and_writes_manifest(tmp_path):
    f = tmp_path / "a.wav"
    f.write_bytes(b"12345678")
    s3 = FakeS3()
    lake_put.lake_upload_file(BUCKET, TENANT_KEY, str(f), owner_hint=HINT, client=s3,
                              ExtraArgs={"ContentType": "audio/wav"})
    kind, kw = s3.calls[0]
    assert kind == "upload_file" and kw["ExtraArgs"] == {"ContentType": "audio/wav"}
    assert json.loads(s3.manifests()[0][1]["Body"])["size"] == 8


def test_upload_file_failure_raises_without_manifest(tmp_path):
    class Boom(FakeS3):
        def upload_file(self, *a, **k):
            raise OSError("disk")

    s3 = Boom()
    with pytest.raises(OSError):
        lake_put.lake_upload_file(BUCKET, TENANT_KEY, str(tmp_path / "x"), client=s3)
    assert s3.manifests() == []


def test_manifest_key_shape_uses_utc_date():
    k = lake_put.manifest_key(now=datetime(2026, 1, 2, 23, 59, tzinfo=timezone.utc), uid="u1")
    assert k == "lake_manifest/2026/01/02/u1.jsonl"


def _script_filters():
    root = os.path.join(os.path.dirname(__file__), "..", "..")
    text = open(os.path.join(root, "scripts", "wire-s3-events.sh"), encoding="utf-8").read()
    pat = r'\{"Name":"prefix","Value":"([^"]*)"\},\{"Name":"suffix","Value":"([^"]*)"\}'
    return re.findall(pat, text)


def test_manifest_key_matches_no_s3_notification_or_eventbridge_filter():
    filters = _script_filters()
    assert len(filters) >= 14  # the script's filters were actually found
    key = lake_put.manifest_key()
    for prefix, suffix in filters:
        assert not (key.startswith(prefix) and key.endswith(suffix)), (prefix, suffix)
    # The EventBridge rules match on the transcripts/ prefix; the manifest sits elsewhere.
    assert not key.startswith("transcripts/")
    # No filter prefix is a prefix of the manifest key at all (stricter than the pairs above).
    assert not any(key.startswith(p) for p, _ in filters)


def test_a_parser_crash_never_fails_the_writer(monkeypatch):
    """The object is already stored when the key is classified; a parser bug must not
    surface to the writer as a failed put."""
    import lake_put as lp
    calls = []

    class _S3:
        def put_object(self, **kw):
            calls.append(kw["Key"])
            return {"ETag": "e"}

    def boom(key):
        raise ValueError("parser bug")

    monkeypatch.setattr(lp.lake_keys, "parse", boom)
    monkeypatch.setenv("LAKE_MANIFEST", "on")
    assert lp.lake_put("b", "transcripts/Ada_L/2026-10-01/x.json", b"{}", client=_S3()) == {"ETag": "e"}
    assert calls[0] == "transcripts/Ada_L/2026-10-01/x.json"
    assert len(calls) == 2 and calls[1].startswith("lake_manifest/")


def test_a_trace_hints_only_a_real_folder_and_no_company(monkeypatch):
    """A trace's company_id is whatever its caller set (photo_rebind: a whole day's), not
    the site's, so it is never offered as an owner; "_glossary" is not a person."""
    import pipeline_trace
    seen = []
    monkeypatch.setattr(pipeline_trace, "lake_put",
                        lambda b, k, body, **kw: seen.append(kw["owner_hint"]))
    monkeypatch.setattr(pipeline_trace, "enabled", lambda: True)
    for folder in ("_glossary", "Ada_L"):
        pipeline_trace.begin("t", user_folder=folder, date="2026-10-01", company_id="c-home")
        pipeline_trace.event("step")
        pipeline_trace.flush(object(), "b")
    assert seen == [{"folder": None}, {"folder": "Ada_L"}], seen
