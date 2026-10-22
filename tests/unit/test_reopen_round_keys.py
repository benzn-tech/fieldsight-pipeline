"""Late-chunk reopen, round 2+: every per-round S3 object gets its own key.

Task 4A reopens an idle-closed `sent`/`failed` session when a newer chunk
arrives (meeting_session.reopen_count += 1). The session then idle-closes and
is claimed again -- but every per-round object (extraction request, brief
request, finalize request, finalize result, stored brief) is write-once or
read-as-truth at a key built from the bare session id. Round 2 would either 412
(the conditional puts) or be answered by round 1's result ("already sent").

`-r{n}` is inserted after the sid (before `-notes`); `-updated` stays the group
merge's own suffix. Round 0 keys are byte-identical to before.
"""
import json

import pytest

fc = pytest.importorskip("lambda_finalize_claim", reason="requires psycopg (installed in CI)")
fin = pytest.importorskip("lambda_session_finalize", reason="requires boto3 (installed in CI)")
iw = pytest.importorskip("lambda_item_writer", reason="requires boto3 (installed in CI)")

import lake_keys
import session_scope

SID = "622a0e7fc93e4befafeda1d0442fdf35"
CTX = {"recipient": "a@b.c", "folder": "F", "date": "2026-10-22",
       "timeRange": "09:00 - 10:00", "siteName": "Site"}


class _FakeS3:
    def put_object(self, **kw):
        return {}


@pytest.fixture
def puts(monkeypatch):
    """Every key lake_put is asked to write, from either module."""
    keys = []

    def fake_put(bucket, key, body, **kw):
        keys.append(key)
        return {}

    import boto3
    monkeypatch.setattr(boto3, "client", lambda *a, **k: _FakeS3())
    monkeypatch.setattr(fc, "lake_put", fake_put)
    monkeypatch.setattr(fin, "lake_put", fake_put)
    return keys


def test_round_suffix():
    assert session_scope.round_suffix(0) == ""
    assert session_scope.round_suffix(None) == ""
    assert session_scope.round_suffix(2) == "-r2"


def _claim_round(monkeypatch, reopen_count):
    monkeypatch.setattr(fc.meeting_session, "claim_finalize",
                        lambda c, s, v: {"session_id": s, "user_id": "u", "version": v,
                                         "reopen_count": reopen_count})
    fc.finalize_claim("CONN", SID, 3, resolve_context=lambda c, r: dict(CTX),
                      read_rolling=lambda *a: {},
                      request_extraction=fc._request_extraction)


def _backstop_round(reopen_count):
    from datetime import datetime, timedelta, timezone
    now = datetime.now(timezone.utc)
    row = {"session_id": SID, "version": 3, "reopen_count": reopen_count,
           "finalizing_at": now - timedelta(hours=2)}
    return fc.backstop("CONN", list_waiting=lambda c: [row],
                       resolve_context=lambda c, r: dict(CTX),
                       read_rolling=lambda *a: {}, enqueue=fc._enqueue, now=now)


def _item_writer_key(reopen_count, follow_up=False):
    recorded = []
    ctx = {"kind": "final", "sessionId": SID, "recipient": "a@b.c",
           **({"followUp": True} if follow_up else {}),
           **({"reopened": reopen_count} if reopen_count else {})}
    iw._enqueue_final_email(ctx, put=lambda key, body, only_if_absent: recorded.append(key))
    return recorded[0]


def _worker_result_key(monkeypatch, puts, reopened):
    monkeypatch.setattr(fin, "_session_was_deleted", lambda a: False)
    art = {"kind": "final", "sessionId": SID, "recipient": "a@b.c", "date": "2026-10-22",
           "openTodos": [], **({"reopened": reopened} if reopened else {})}
    before = len(puts)
    fin.process_finalize_request(art, send=lambda *a: None, already_sent=lambda r: False,
                                 poll_brief=lambda *a, **k: None)
    return puts[before:]


@pytest.mark.parametrize("n,sfx", [(1, "-r1"), (0, "")])
def test_round_two_keys_never_collide(monkeypatch, puts, n, sfx):
    _claim_round(monkeypatch, n)
    assert puts == [f"extraction_requests/{SID}{sfx}.json",
                    f"session_finalize_requests/brief-{SID}{sfx}.json"]
    del puts[:]
    assert _backstop_round(n) == [SID]
    assert puts == [f"session_finalize_requests/{SID}{sfx}.json"]
    assert _item_writer_key(n) == f"session_finalize_requests/{SID}{sfx}.json"
    assert _worker_result_key(monkeypatch, puts, n) == [
        f"session_finalize_results/{SID}{sfx}.json"]


def test_round_two_bodies_carry_reopened(monkeypatch):
    bodies = {}

    def fake_put(bucket, key, body, **kw):
        bodies[key] = json.loads(body)
        return {}

    import boto3
    monkeypatch.setattr(boto3, "client", lambda *a, **k: _FakeS3())
    monkeypatch.setattr(fc, "lake_put", fake_put)
    _claim_round(monkeypatch, 2)
    _backstop_round(2)
    assert bodies[f"extraction_requests/{SID}-r2.json"]["reopened"] == 2
    assert bodies[f"session_finalize_requests/brief-{SID}-r2.json"]["reopened"] == 2
    assert bodies[f"session_finalize_requests/{SID}-r2.json"]["reopened"] == 2
    bodies.clear()
    _claim_round(monkeypatch, 0)
    assert all("reopened" not in b for b in bodies.values())


def test_a_deleted_round_two_settles_on_its_round_key(monkeypatch, puts):
    """The deleted-recording skip is what reconcile settles a round on, so it
    must land where reconcile looks for round 2, not on round 1's result."""
    monkeypatch.setattr(fin, "_session_was_deleted", lambda a: True)
    fin.process_finalize_request({"kind": "final", "sessionId": SID, "recipient": "a@b.c",
                                  "reopened": 1}, send=lambda *a: None)
    assert puts == [f"session_finalize_results/{SID}-r1.json"]


def _final(**extra):
    return {"kind": "final", "sessionId": SID, "recipient": "a@b", "date": "2026-10-22",
            "openTodos": [], **extra}


def test_round_two_subject(monkeypatch):
    monkeypatch.setattr(fin, "_session_was_deleted", lambda a: False)
    sent, results = [], []
    out = fin.process_finalize_request(
        _final(reopened=1), send=lambda *a: sent.append(a),
        write_result=lambda rid, p: results.append(rid),
        already_sent=lambda r: r == SID, poll_brief=lambda *a, **k: None)
    assert out["status"] == "sent" and len(sent) == 1
    assert sent[0][1].startswith(
        "Updated — replaces the earlier summary: FieldSight — your site notes")
    assert results == [f"{SID}-r1"]

    sent.clear()
    out = fin.process_finalize_request(
        _final(), send=lambda *a: sent.append(a), write_result=lambda *a: None,
        already_sent=lambda r: r == SID, poll_brief=lambda *a, **k: None)
    assert out == {"status": "skipped", "reason": "already sent", "sessionId": SID}
    assert sent == []


def test_round_one_subject_is_unchanged(monkeypatch):
    monkeypatch.setattr(fin, "_session_was_deleted", lambda a: False)
    sent = []
    fin.process_finalize_request(
        _final(), send=lambda *a: sent.append(a), write_result=lambda *a: None,
        already_sent=lambda r: False, poll_brief=lambda *a, **k: None)
    assert sent[0][1].startswith("FieldSight — your site notes")


def test_round_two_follow_up_key(monkeypatch):
    monkeypatch.setattr(fin, "_session_was_deleted", lambda a: False)
    checked, results = [], []
    fin.process_finalize_request(
        _final(followUp=True, reopened=2), send=lambda *a: None,
        write_result=lambda rid, p: results.append(rid),
        already_sent=lambda r: checked.append(r) or False, poll_brief=lambda *a, **k: None)
    assert checked == [f"{SID}-r2-notes"]
    assert results == [f"{SID}-r2-notes"]
    assert _item_writer_key(2, follow_up=True) == \
        f"session_finalize_requests/{SID}-r2-notes.json"


def test_round_two_brief_is_its_own(monkeypatch):
    assert fin._brief_key("F", "2026-10-22", SID, 1).endswith(f"/sid{SID}/r1.json")
    assert fin._brief_key("F", "2026-10-22", SID).endswith(f"/sid{SID}/latest.json")

    read = []
    monkeypatch.setattr(fin, "SESSION_BRIEF", True, raising=False)
    monkeypatch.setattr(fin, "_session_was_deleted", lambda a: False)
    monkeypatch.setattr(fin, "_read_brief",
                        lambda f, d, s, reopened=0: read.append(
                            fin._brief_key(f, d, s, reopened)) or None)
    monkeypatch.setattr(fin, "BRIEF_WAIT_SECONDS", 0, raising=False)
    fin.process_finalize_request(
        _final(folder="F", reopened=1), send=lambda *a: None,
        write_result=lambda *a: None, already_sent=lambda r: False)
    assert read and all(k.endswith(f"/sid{SID}/r1.json") for k in read)
    assert not any(k.endswith("latest.json") for k in read)


def test_round_two_brief_is_stored_under_its_round(monkeypatch, puts):
    fin._store_brief("F", "2026-10-22", SID, {"sections": [1]}, reopened=1)
    assert sorted(puts) == sorted([
        f"session_brief/F/2026-10-22/sid{SID}/latest.json",
        f"session_brief/F/2026-10-22/sid{SID}/r1.json"])
    del puts[:]
    fin._store_brief("F", "2026-10-22", SID, {"sections": [1]})
    assert puts == [f"session_brief/F/2026-10-22/sid{SID}/latest.json"]


def test_brief_request_round_reaches_the_store(monkeypatch):
    stored = []
    monkeypatch.setattr(fin, "SESSION_BRIEF", True, raising=False)
    monkeypatch.setattr(fin, "_store_brief",
                        lambda f, d, s, b, reopened=0: stored.append(reopened))
    import lambda_extract_session as ex
    monkeypatch.setattr(ex, "gather_session_segments", lambda *a: ["k"])
    monkeypatch.setattr(ex, "assemble_deduped_turns", lambda *a: (["t"], []))
    fin._complete_summary({"sessionId": SID, "folder": "F", "date": "2026-10-22",
                           "reopened": 3},
                          summarize=lambda turns: {"sections": [1], "summary": "s"})
    assert stored == [3]


def _writer_ctx(monkeypatch, reopen_count):
    monkeypatch.setattr(iw.meeting_session, "get",
                        lambda c, s: {"session_id": s, "status": "finalizing",
                                      "user_id": "u", "reopen_count": reopen_count})
    monkeypatch.setattr(fc, "_resolve_context", lambda c, r: dict(CTX))
    return iw._final_email_context("CONN", f"sid{SID}", {"topics": []}, "2026-10-22")


def test_item_writer_carries_reopened(monkeypatch):
    assert _writer_ctx(monkeypatch, 1)["reopened"] == 1
    assert "reopened" not in _writer_ctx(monkeypatch, 0)


@pytest.mark.parametrize("prefix,tail", [
    ("session_finalize_requests", "-r1"), ("session_finalize_requests", "-r1-notes"),
    ("session_finalize_results", "-r2"), ("session_finalize_results", "-r2-notes"),
    ("extraction_requests", "-r1")])
def test_round_keys_still_name_their_session(prefix, tail):
    """The lake ownership index resolves these keys by the sid in them; a
    suffix it does not know would read `{sid}-r1` as no session at all."""
    p = lake_keys.parse(f"{prefix}/{SID}{tail}.json")
    assert p["kind"] == "session_marker" and p["sid"] == SID and p["session_base"] == SID
