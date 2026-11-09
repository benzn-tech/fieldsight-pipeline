"""Tests for src/reconcile_request.py -- cross-session status reconcile R0, Task 4.

`build` turns one FINAL extraction into the reconcile_requests/ artifact body (or None);
`emit` writes it under a deterministic key. All text is synthetic.
"""
import datetime
import hashlib
import json
import uuid

import pytest

rr = pytest.importorskip("reconcile_request")

KEY = "extractions/Synth_Folder/2026-11-10/sidabc.json"
SITE = {"id": "11111111-1111-1111-1111-111111111111", "company_id": "co-B"}
SID = uuid.UUID("22222222-2222-2222-2222-222222222222")

CANDIDATE_FIELDS = {"kind", "stable_id", "text", "status", "responsible", "deadline",
                    "first_seen_date", "recorded_by", "score", "matched_terms", "place"}
BODY_FIELDS = {"site_id", "date", "user_folder", "session_base", "extraction_key",
               "extracted_at", "rung", "candidates"}


class FakeConn:
    def __init__(self):
        self.savepoints = 0

    def transaction(self):
        conn = self

        class _T:
            def __enter__(self):
                conn.savepoints += 1
                return self

            def __exit__(self, *exc):
                return False
        return _T()


class FakeS3:
    def __init__(self):
        self.put_calls = []

    def put_object(self, **kwargs):
        if kwargs["Key"].startswith("lake_manifest/"):
            return
        self.put_calls.append(kwargs)


def _row(**over):
    row = {
        "kind": "action_item", "stable_id": SID, "item_id": "a1",
        "text": "Synthetic task", "responsible": "Pat", "deadline": datetime.date(2026, 11, 12),
        "status": "open",
        "first_seen": datetime.datetime(2026, 11, 10, 9, 0, tzinfo=datetime.timezone.utc),
        "source": {"user_folder": "Synth_Folder", "date": "2026-11-10",
                   "session_base": "sidother", "source_s3_key": "extractions/x.json"},
        "recorded_by": "Synth_Folder", "carried_from": None, "score": 0.0,
        "matched_terms": [], "place": None,
        "origin": {"source_stem": "s", "start_sec": 1.0, "end_sec": 2.0,
                   "speaker_label": "spk_0", "quote_status": None},
    }
    row.update(over)
    return row


def _extraction(**over):
    e = {"tier": "final", "extracted_at": "2026-11-10T10:05:00Z", "topics": []}
    e.update(over)
    return e


@pytest.fixture
def calls(monkeypatch):
    seen = []

    def fake_find_items(conn, **kw):
        seen.append(kw)
        return [_row()]
    monkeypatch.setattr(rr.item_history, "find_items", fake_find_items)
    return seen


def _build(conn=None, extraction=None, owner="co-B"):
    return rr.build(conn or FakeConn(), extraction or _extraction(), KEY, SITE, owner,
                    user_folder="Synth_Folder", date="2026-11-10", session_base="sidabc")


def test_candidates_serialise_uuid_and_date(calls):
    body = _build()
    round_trip = json.loads(json.dumps(body))
    cand = round_trip["candidates"][0]
    assert cand["stable_id"] == str(SID)
    assert cand["deadline"] == "2026-11-12"
    assert cand["first_seen_date"] == "2026-11-10"


def test_first_seen_date_coerces_a_date_object(monkeypatch):
    src = {"user_folder": "Synth_Folder", "date": datetime.date(2026, 11, 9),
           "session_base": "b", "source_s3_key": "k"}
    monkeypatch.setattr(rr.item_history, "find_items",
                        lambda conn, **kw: [_row(source=src, deadline=None)])
    cand = json.loads(json.dumps(_build()))["candidates"][0]
    assert cand["first_seen_date"] == "2026-11-09"
    assert cand["deadline"] is None


def test_emit_key_deterministic_and_fields_whitelisted(calls):
    body = _build()
    assert set(body) == BODY_FIELDS
    assert body["rung"] == "R0"
    assert body["site_id"] == SITE["id"]
    assert body["extraction_key"] == KEY
    assert body["extracted_at"] == "2026-11-10T10:05:00Z"
    assert (body["user_folder"], body["date"], body["session_base"]) == (
        "Synth_Folder", "2026-11-10", "sidabc")
    for cand in body["candidates"]:
        assert set(cand) == CANDIDATE_FIELDS
    text = json.dumps(body)
    assert "origin" not in text and "company_id" not in text and "co-B" not in text

    s3 = FakeS3()
    key = rr.emit(s3, "bucket", body)
    h = hashlib.sha256(KEY.encode("utf-8")).hexdigest()[:16]
    assert key == f"reconcile_requests/{SITE['id']}/2026-11-10/{h}.json"
    assert rr.emit(s3, "bucket", body) == key
    assert [c["Key"] for c in s3.put_calls] == [key, key]
    assert json.loads(s3.put_calls[0]["Body"]) == json.loads(json.dumps(body))
    assert s3.put_calls[0]["ContentType"] == "application/json"


def test_r0_filters_passed_to_find_items(calls):
    conn = FakeConn()
    _build(conn=conn)
    (kw,) = calls
    assert kw["company_id"] == "co-B"
    assert kw["site_id"] == SITE["id"]
    assert kw["kinds"] == ("action_item",)
    assert kw["statuses"] == {"action_item": ["open", "in_progress", "blocked"]}
    assert kw["since"] == kw["until"] == "2026-11-10"
    assert kw["recorded_by"] == "Synth_Folder"
    assert kw["exclude_source_keys"] == [KEY]
    assert kw["limit"] == rr.CANDIDATE_CAP == 40
    assert "query" not in kw
    assert conn.savepoints == 1


def test_emit_none_on_empty(monkeypatch):
    monkeypatch.setattr(rr.item_history, "find_items", lambda conn, **kw: [])
    assert _build() is None
    s3 = FakeS3()
    assert rr.emit(s3, "bucket", None) is None
    assert rr.emit(s3, "bucket", {"site_id": "s", "date": "d", "extraction_key": KEY,
                                  "candidates": []}) is None
    assert s3.put_calls == []


def test_build_none_on_live_tier(calls):
    assert _build(extraction=_extraction(tier="live")) is None
    assert calls == []


def test_build_none_when_incomplete(calls):
    assert _build(extraction=_extraction(incomplete=True)) is None
    assert calls == []


def test_build_none_when_find_items_raises(monkeypatch, caplog):
    def boom(conn, **kw):
        raise RuntimeError("db down")
    monkeypatch.setattr(rr.item_history, "find_items", boom)
    assert _build() is None


def test_build_logs_ids_and_counts_only(calls, caplog):
    caplog.set_level("INFO")
    _build()
    msgs = " ".join(r.getMessage() for r in caplog.records)
    assert f"reconcile site={SITE['id']} candidates=1" in msgs
    assert "Synthetic task" not in msgs
