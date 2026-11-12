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
    monkeypatch.setattr(rr.item_history, "session_recorded_at", lambda conn, key: None)
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


# --- later-recorded candidates (first_seen_recorded_at vs this session's start) ----------

UTC = datetime.timezone.utc
START = datetime.datetime(2026, 11, 10, 4, 0, tzinfo=UTC)


def _ordered(monkeypatch, rows, started=START):
    monkeypatch.setattr(rr.item_history, "find_items", lambda conn, **kw: rows)
    monkeypatch.setattr(rr.item_history, "session_recorded_at", lambda conn, key: started)


def _sid(n):
    return uuid.UUID(int=n)


def test_candidate_first_heard_after_this_session_started_is_dropped(monkeypatch):
    rows = [_row(stable_id=_sid(1), first_seen_recorded_at=START - datetime.timedelta(hours=3)),
            _row(stable_id=_sid(2), first_seen_recorded_at=START + datetime.timedelta(minutes=1)),
            _row(stable_id=_sid(3), first_seen_recorded_at=None)]
    _ordered(monkeypatch, rows)
    body = _build()
    assert [c["stable_id"] for c in body["candidates"]] == [str(_sid(1)), str(_sid(3))]


def test_same_start_instant_is_kept(monkeypatch):
    _ordered(monkeypatch, [_row(first_seen_recorded_at=START)])
    assert len(_build()["candidates"]) == 1


def test_unknown_session_start_keeps_every_candidate(monkeypatch):
    rows = [_row(stable_id=_sid(1), first_seen_recorded_at=START + datetime.timedelta(hours=5))]
    _ordered(monkeypatch, rows, started=None)
    assert len(_build()["candidates"]) == 1


def test_only_later_candidates_means_no_request(monkeypatch):
    _ordered(monkeypatch, [_row(first_seen_recorded_at=START + datetime.timedelta(hours=1))])
    assert _build() is None


def test_order_is_by_recording_instant_across_timezones(monkeypatch):
    # 17:00 NZDT is 04:00Z: an aware non-UTC instant equal to START is not "later".
    nz = datetime.timezone(datetime.timedelta(hours=13))
    _ordered(monkeypatch, [_row(first_seen_recorded_at=datetime.datetime(
        2026, 11, 10, 17, 0, tzinfo=nz))])
    assert len(_build()["candidates"]) == 1


def test_start_lookup_failure_sends_nothing(monkeypatch, caplog):
    monkeypatch.setattr(rr.item_history, "find_items", lambda conn, **kw: [_row()])

    def boom(conn, key):
        raise RuntimeError("db down")
    monkeypatch.setattr(rr.item_history, "session_recorded_at", boom)
    conn = FakeConn()
    assert _build(conn=conn) is None
    assert conn.savepoints == 1


def test_results_key_round_trip():
    assert rr.RESULTS_PREFIX == "reconcile_results/"
    assert (rr.results_key("reconcile_requests/S/2026-11-12/ab.json")
            == "reconcile_results/S/2026-11-12/ab.json")
    with pytest.raises(ValueError):
        rr.results_key("match_requests/S/2026-11-12/ab.json")
