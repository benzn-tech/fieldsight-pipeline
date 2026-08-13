"""Unit: the endpoint a customer presses "delete" on.

Plan: docs/superpowers/plans/2026-08-14-user-deletes-a-recording.md phase 7.

Everything before this phase was inert: nothing could create a `scope='deleted'` row, so
every filter had nothing to filter. This is the key, and it is the only phase that changes
production behaviour.

The invariants the whole feature rests on are asserted here, because this is the one place
that could violate them:

* **no S3 object is ever deleted** — the audio and transcripts stay for analysis, which is
  the customer's own stated requirement;
* **the counts are reported, including zero** — "the request succeeded" and "anything was
  hidden" are otherwise the same observation, and a delete that silently matched nothing is
  the worst outcome of all: the customer is told it worked;
* **the caller may only delete what they own or administer** — `folder` comes from the
  request body, so without a check any authenticated account can hide another company's
  day, and `revert_batch` is company-guarded so that company cannot undo it. The first
  version of this endpoint had no authorization at all and no test asked for any; the
  four `test_authz_*` cases below exist because a review, not the suite, caught it.
* **the mirror is written after the commit, and merges** — see the last two tests.
"""
import json
import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(
    os.path.abspath(__file__)))), "src"))

org = pytest.importorskip("lambda_org_api")

CALLER = {"company_id": "c-1", "id": "u-1", "global_role": "admin"}
REC = {"folder": "Ben", "date": "2026-08-14", "sessionBase": "sid-a"}


class _S3:
    """Records every call. `delete_object` is a landmine: reaching for it is the one thing
    this feature must never do."""

    def __init__(self, existing=None):
        self.calls = []
        self.written = {}
        self.existing = existing or {}

    def put_object(self, **kw):
        self.calls.append(("put_object", kw.get("Key")))
        self.written[kw["Key"]] = json.loads(kw["Body"])

    def delete_object(self, **kw):
        raise AssertionError("this feature must never delete an S3 object")

    def get_object(self, **kw):
        self.calls.append(("get_object", kw.get("Key")))
        key = kw.get("Key")
        if key in self.existing:
            class B:
                def __init__(self, d):
                    self.d = d

                def read(self):
                    return json.dumps(self.d).encode()
            return {"Body": B(self.existing[key])}
        raise KeyError(key)


class _NoopCtx:
    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


class _Conn:
    """Records commits, so the ORDER of commit vs mirror write is observable.

    It has to be: `lambda_handler` opens the connection with `with get_connection() as
    conn`, and the heartbeat has already run a statement by the time the endpoint is
    reached, so a nested `conn.transaction()` is a savepoint that commits nothing. The
    first version of this endpoint trusted it and wrote the mirror inside the still-open
    transaction."""

    def __init__(self):
        self.events = []

    def transaction(self):
        return _NoopCtx()

    def commit(self):
        self.events.append("commit")

    def cursor(self, row_factory=None):
        class C:
            def execute(self, *a, **k):
                return self

            def fetchall(self):
                return []

            def fetchone(self):
                return None
        return C()


def _allow_all(monkeypatch, company="c-1"):
    monkeypatch.setattr(org, "_can_delete_folder", lambda conn, caller, f: (True, company))


def _stub_writes(monkeypatch, topics_found=()):
    monkeypatch.setattr(org.topics, "list_topics_for_source_prefix",
                        lambda *a, **k: list(topics_found))
    monkeypatch.setattr(org.redactions, "create_recording_tombstone",
                        lambda *a, **k: {"id": "r-1"})
    monkeypatch.setattr(org.redactions, "create_redaction", lambda *a, **k: {"id": "r-2"})


def test_the_flag_is_a_real_gate(monkeypatch):
    """Off by default. A customer-facing delete that ships enabled by a merge is not a
    feature flag, it is an accident waiting for a deploy."""
    assert hasattr(org, "ENABLE_USER_DELETION")
    monkeypatch.setattr(org, "ENABLE_USER_DELETION", False)
    res = org.delete_recordings_endpoint(object(), CALLER, {"recordings": [REC]})
    assert res["statusCode"] == 403
    assert "ENABLE_USER_DELETION" in res["body"], "say which switch, or nobody can turn it on"


# ---- authorization -----------------------------------------------------

def test_authz_a_stranger_cannot_delete_another_folder(monkeypatch):
    """`folder` is request-supplied. Without this check any provisioned account — including
    a browse-only user of an unrelated company — hides someone else's day, and cannot be
    undone by them because `revert_batch` is company-guarded."""
    monkeypatch.setattr(org, "ENABLE_USER_DELETION", True)
    monkeypatch.setattr(org, "_s3_client", _S3())
    monkeypatch.setattr(org, "_can_delete_folder", lambda conn, caller, f: (False, None))
    calls = []
    monkeypatch.setattr(org.redactions, "create_recording_tombstone",
                        lambda *a, **k: calls.append(a))

    res = org.delete_recordings_endpoint(_Conn(), CALLER, {"recordings": [REC]})
    body = json.loads(res["body"])
    assert body["results"][0]["topics_hidden"] == 0
    assert "not permitted" in body["results"][0]["error"]
    assert calls == [], "a refused recording must not write a tombstone"


def test_authz_is_per_recording_not_per_request(monkeypatch):
    """One unreachable folder must not throw away the recordings the caller does own —
    and must not quietly succeed for it either."""
    monkeypatch.setattr(org, "ENABLE_USER_DELETION", True)
    monkeypatch.setattr(org, "_s3_client", _S3())
    monkeypatch.setattr(org, "_can_delete_folder",
                        lambda conn, caller, f: (f == "Ben", "c-1"))
    _stub_writes(monkeypatch, [{"id": "t-1"}])

    res = org.delete_recordings_endpoint(_Conn(), CALLER, {"recordings": [
        REC, {"folder": "Someone", "date": "2026-08-14", "sessionBase": "sid-b"}]})
    results = json.loads(res["body"])["results"]
    assert results[0]["topics_hidden"] > 0 and "error" not in results[0]
    assert results[1]["topics_hidden"] == 0 and "error" in results[1]


def test_authz_a_pm_who_can_view_still_cannot_delete(monkeypatch):
    """`_can_delete_folder` is `_can_view_folder` AND an authority test. A pm reads a
    worker's day legitimately; erasing it from everyone's view is a different act."""
    monkeypatch.setattr(org, "_can_view_folder", lambda conn, caller, f: True)
    monkeypatch.setattr(org.scope, "visible_scope", lambda conn, caller: {
        "self_folder": "Pm_Folder", "user_scope": "SITE", "cross_company": False,
        "site_ids": set(), "author_ids": set()})
    allowed, company = org._can_delete_folder(object(), CALLER, "Worker_Folder")
    assert allowed is False and company is None


def test_authz_a_cross_company_delete_is_stamped_with_the_targets_company(monkeypatch):
    """`revert_batch` is company-guarded, so stamping a platform_admin's own company would
    leave the affected company unable to undo a delete made on their data."""
    monkeypatch.setattr(org, "_can_view_folder", lambda conn, caller, f: True)
    monkeypatch.setattr(org.scope, "visible_scope", lambda conn, caller: {
        "self_folder": "Admin_Folder", "user_scope": "ALL", "cross_company": True,
        "site_ids": set(), "author_ids": set()})
    monkeypatch.setattr(org.users, "get_by_folder_name_global",
                        lambda conn, f: {"id": "u-9", "company_id": "c-VICTIM"})
    allowed, company = org._can_delete_folder(object(), CALLER, "Other_Folder")
    assert allowed is True
    assert company == "c-VICTIM", "stamping the caller's company makes the delete un-undoable"


# ---- the invariants ----------------------------------------------------

def test_a_delete_that_matched_nothing_says_so(monkeypatch):
    """The worst outcome is a silent success: the customer is told their recording is gone
    and nothing was hidden. Zero is a number and it must be in the response."""
    monkeypatch.setattr(org, "ENABLE_USER_DELETION", True)
    monkeypatch.setattr(org, "_s3_client", _S3())
    _allow_all(monkeypatch)
    _stub_writes(monkeypatch, [])

    res = org.delete_recordings_endpoint(_Conn(), CALLER, {"recordings": [REC]})
    body = json.loads(res["body"])
    assert res["statusCode"] == 200
    assert body["results"][0]["topics_hidden"] == 0
    assert body["batch_id"]


def test_a_recording_without_a_session_cannot_hide_the_whole_day(monkeypatch):
    """`sessionBase` missing used to degrade the prefix to `extractions/{folder}/{date}/` —
    the whole day — while the mirror was written with an empty session list. The customer
    would lose recordings they never selected, and be told it succeeded."""
    monkeypatch.setattr(org, "ENABLE_USER_DELETION", True)
    monkeypatch.setattr(org, "_s3_client", _S3())
    _allow_all(monkeypatch)
    calls = []
    monkeypatch.setattr(org.redactions, "create_recording_tombstone",
                        lambda *a, **k: calls.append(a))

    res = org.delete_recordings_endpoint(
        _Conn(), CALLER, {"recordings": [{"folder": "Ben", "date": "2026-08-14"}]})
    body = json.loads(res["body"])
    assert "sessionBase" in body["results"][0]["error"]
    assert calls == []
    assert org._source_prefixes_for({"folder": "Ben", "date": "2026-08-14"}) == []


def test_the_source_prefix_has_no_reports_arm():
    """Report-sourced topics carry `reports/{date}/{folder}/daily_report.json` — no session
    base — so a per-session `reports/` prefix matched nothing while its docstring claimed
    it closed a door. A day-wide one would be worse: it would keep hiding the CLEAN report
    the next nightly run regenerates without the deleted session."""
    prefixes = org._source_prefixes_for(REC)
    assert prefixes == ["extractions/Ben/2026-08-14/sid-a"]


def test_no_s3_object_is_ever_deleted(monkeypatch):
    """The premise of the whole design, asserted where it could be broken."""
    s3 = _S3()
    monkeypatch.setattr(org, "ENABLE_USER_DELETION", True)
    monkeypatch.setattr(org, "_s3_client", s3)
    _allow_all(monkeypatch)
    _stub_writes(monkeypatch, [{"id": "t-1"}])

    org.delete_recordings_endpoint(_Conn(), CALLER, {"recordings": [REC]})
    assert all(c[0] != "delete_object" for c in s3.calls)
    assert any(c[0] == "put_object" and c[1].startswith("redactions/") for c in s3.calls), \
        "the S3 mirror is what the non-VPC lambdas read; without it they serve deleted content"


def test_the_mirror_is_written_after_the_commit(monkeypatch):
    """If the mirror lands first and the request then fails, S3 advertises a deletion no
    database row records — hidden content with no batch_id to undo it."""
    conn = _Conn()
    s3 = _S3()

    class _Watched(_S3):
        def put_object(self, **kw):
            conn.events.append("put_object")
            _S3.put_object(self, **kw)

    watched = _Watched()
    monkeypatch.setattr(org, "ENABLE_USER_DELETION", True)
    monkeypatch.setattr(org, "_s3_client", watched)
    _allow_all(monkeypatch)
    _stub_writes(monkeypatch, [{"id": "t-1"}])

    org.delete_recordings_endpoint(conn, CALLER, {"recordings": [REC]})
    assert "commit" in conn.events, "a nested transaction() is a savepoint; it commits nothing"
    assert conn.events.index("commit") < conn.events.index("put_object")
    assert s3.calls == []


def test_a_second_delete_the_same_day_does_not_unhide_the_first(monkeypatch):
    """`write_mirror` puts the whole document. Building it from only this request's
    recordings replaced the previous batch's sessions, and every reader with no database —
    the nightly report and its email — republished content the customer had deleted."""
    key = "redactions/Ben/2026-08-14/deleted_sessions.json"
    s3 = _S3(existing={key: {"sessions": ["sid-first"]}})
    monkeypatch.setattr(org, "ENABLE_USER_DELETION", True)
    monkeypatch.setattr(org, "_s3_client", s3)
    _allow_all(monkeypatch)
    _stub_writes(monkeypatch, [{"id": "t-1"}])

    org.delete_recordings_endpoint(_Conn(), CALLER, {"recordings": [REC]})
    assert s3.written[key]["sessions"] == ["sid-a", "sid-first"]


def test_undelete_restores_exactly_one_batch(monkeypatch):
    """`one revert restores exactly what one delete hid` is the only check that proves this
    feature is reversible, and it is unimplementable without the batch id."""
    monkeypatch.setattr(org, "ENABLE_USER_DELETION", True)
    monkeypatch.setattr(org, "_s3_client", _S3())
    reverted = {}

    def _revert(conn, b, c, **kw):
        # NOT `setdefault(...) or [...]`: setdefault returns the truthy batch id, the `or`
        # short-circuits, and the double hands back a string where the caller expects rows.
        reverted["b"] = b
        return [{"id": "r-1", "target_type": "recording",
                 "target_key": "extractions/Ben/2026-08-14/sid-a"}]

    monkeypatch.setattr(org.redactions, "revert_batch", _revert)

    res = org.undelete_recordings_endpoint(_Conn(), CALLER, {"batchId": "b-1"})
    assert json.loads(res["body"])["restored"] == 1
    assert reverted["b"] == "b-1"


def test_undelete_frees_only_its_own_sessions_from_the_mirror(monkeypatch):
    """The first version rewrote the day's mirror as `[]`, which un-hid every OTHER active
    batch for that day — an undelete that restores more than its delete hid."""
    key = "redactions/Ben/2026-08-14/deleted_sessions.json"
    s3 = _S3(existing={key: {"sessions": ["sid-a", "sid-other"]}})
    monkeypatch.setattr(org, "ENABLE_USER_DELETION", True)
    monkeypatch.setattr(org, "_s3_client", s3)
    monkeypatch.setattr(org.redactions, "revert_batch", lambda conn, b, c, **kw: [
        {"id": "r-1", "target_type": "recording",
         "target_key": "extractions/Ben/2026-08-14/sid-a"},
        {"id": "r-2", "target_type": "topic", "target_key": None},
    ])

    org.undelete_recordings_endpoint(_Conn(), CALLER, {"batchId": "b-1"})
    assert s3.written[key]["sessions"] == ["sid-other"]
