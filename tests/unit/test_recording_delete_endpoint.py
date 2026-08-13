"""Unit: the endpoint a customer presses "delete" on.

Plan: docs/superpowers/plans/2026-08-14-user-deletes-a-recording.md phase 7.

Everything before this phase was inert: nothing could create a `scope='deleted'` row, so
every filter had nothing to filter. This is the key, and it is the only phase that changes
production behaviour.

The two invariants the whole feature rests on are asserted here, because this is the one
place that could violate them:

* **no S3 object is ever deleted** — the audio and transcripts stay for analysis, which is
  the customer's own stated requirement;
* **the counts are reported, including zero** — "the request succeeded" and "anything was
  hidden" are otherwise the same observation, and a delete that silently matched nothing is
  the worst outcome of all: the customer is told it worked.
"""
import json
import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(
    os.path.abspath(__file__)))), "src"))

org = pytest.importorskip("lambda_org_api")


class _S3:
    """Records every call. `delete_object` is a landmine: reaching for it is the one thing
    this feature must never do."""

    def __init__(self):
        self.calls = []

    def put_object(self, **kw):
        self.calls.append(("put_object", kw.get("Key")))

    def delete_object(self, **kw):
        raise AssertionError("this feature must never delete an S3 object")

    def get_object(self, **kw):
        self.calls.append(("get_object", kw.get("Key")))
        raise KeyError(kw.get("Key"))


def test_the_flag_is_a_real_gate(monkeypatch):
    """Off by default. A customer-facing delete that ships enabled by a merge is not a
    feature flag, it is an accident waiting for a deploy."""
    assert hasattr(org, "ENABLE_USER_DELETION")
    monkeypatch.setattr(org, "ENABLE_USER_DELETION", False)
    res = org.delete_recordings_endpoint(object(), {"company_id": "c-1", "id": "u-1",
                                                    "global_role": "admin"},
                                         {"recordings": [{"folder": "Ben", "date": "2026-08-14",
                                                          "sessionBase": "sid-a"}]})
    assert res["statusCode"] == 403
    assert "ENABLE_USER_DELETION" in res["body"], "say which switch, or nobody can turn it on"


def test_a_delete_that_matched_nothing_says_so(monkeypatch):
    """The worst outcome is a silent success: the customer is told their recording is gone
    and nothing was hidden. Zero is a number and it must be in the response."""
    monkeypatch.setattr(org, "ENABLE_USER_DELETION", True)
    monkeypatch.setattr(org, "_s3_client", _S3())
    monkeypatch.setattr(org.topics, "list_topics_for_source_prefix", lambda *a, **k: [])
    monkeypatch.setattr(org.redactions, "create_recording_tombstone",
                        lambda *a, **k: {"id": "r-1"})

    res = org.delete_recordings_endpoint(
        _Conn(), {"company_id": "c-1", "id": "u-1", "global_role": "admin"},
        {"recordings": [{"folder": "Ben", "date": "2026-08-14", "sessionBase": "sid-a"}]})
    body = json.loads(res["body"])
    assert res["statusCode"] == 200
    assert body["results"][0]["topics_hidden"] == 0
    assert body["batch_id"]


def test_no_s3_object_is_ever_deleted(monkeypatch):
    """The premise of the whole design, asserted where it could be broken."""
    s3 = _S3()
    monkeypatch.setattr(org, "ENABLE_USER_DELETION", True)
    monkeypatch.setattr(org, "_s3_client", s3)
    monkeypatch.setattr(org.topics, "list_topics_for_source_prefix",
                        lambda *a, **k: [{"id": "t-1"}])
    monkeypatch.setattr(org.redactions, "create_recording_tombstone",
                        lambda *a, **k: {"id": "r-1"})
    monkeypatch.setattr(org.redactions, "create_redaction", lambda *a, **k: {"id": "r-2"})

    org.delete_recordings_endpoint(
        _Conn(), {"company_id": "c-1", "id": "u-1", "global_role": "admin"},
        {"recordings": [{"folder": "Ben", "date": "2026-08-14", "sessionBase": "sid-a"}]})
    assert all(c[0] != "delete_object" for c in s3.calls)
    assert any(c[0] == "put_object" and c[1].startswith("redactions/") for c in s3.calls), \
        "the S3 mirror is what the non-VPC lambdas read; without it they serve deleted content"


def test_undelete_restores_exactly_one_batch(monkeypatch):
    """`one revert restores exactly what one delete hid` is the only check that proves this
    feature is reversible, and it is unimplementable without the batch id."""
    monkeypatch.setattr(org, "ENABLE_USER_DELETION", True)
    monkeypatch.setattr(org, "_s3_client", _S3())
    reverted = {}
    def _revert(conn, b, c):
        # NOT `setdefault(...) or [...]`: setdefault returns the truthy batch id, the `or`
        # short-circuits, and the double hands back a string where the caller expects rows.
        reverted["b"] = b
        return [{"id": "r-1", "target_key": "extractions/Ben/2026-08-14/"}]

    monkeypatch.setattr(org.redactions, "revert_batch", _revert)

    res = org.undelete_recordings_endpoint(
        _Conn(), {"company_id": "c-1", "id": "u-1", "global_role": "admin"},
        {"batchId": "b-1"})
    assert json.loads(res["body"])["restored"] == 1
    assert reverted["b"] == "b-1"


class _NoopCtx:
    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


class _Conn:
    def transaction(self):
        return _NoopCtx()

    def cursor(self, row_factory=None):
        class C:
            def execute(self, *a, **k):
                return self

            def fetchall(self):
                return []

            def fetchone(self):
                return None
        return C()
