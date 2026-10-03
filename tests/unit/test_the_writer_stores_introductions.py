"""Unit: the writer stores `self_introductions` inside its own connection block (Task 5).

`_request_rebind`/`_request_match` are called AFTER `with get_connection() as conn:` has
closed the connection -- psycopg3's `with conn:` closes it on exit
(`lambda_item_writer.py`'s own comment beside the group-merge email says so, having
crashed on every successful merge once for exactly this reason). `store` must NOT repeat
that placement: it goes beside `location_markers.replace_for_day`, inside the block.

The doubles below are copied from `test_lambda_item_writer.py` rather than imported --
this test suite has no precedent for cross-importing a test module, and pytest's
`pythonpath` does not put `tests/unit/` on the path for that anyway. `ClosingFakeConn`
adds the one extra thing this file needs: whether the connection was still open at the
moment `store` was called.

The stub-count rule this plan asks for: `store` is monkeypatched here and called for REAL
in `tests/integration/test_speaker_intro_suggestions_repo.py`.
"""
import io
import json

import pytest

iw = pytest.importorskip("lambda_item_writer", reason="requires psycopg (installed in CI)")


class _FakeCursor:
    def __init__(self, row):
        self._row = row

    def fetchone(self):
        return self._row


class _FakeTransaction:
    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        return False


class ClosingFakeConn:
    def __init__(self, report_already_ingested=False):
        self.executed = []
        self.report_already_ingested = report_already_ingested
        self.closed = False

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        self.closed = True
        return False

    def execute(self, sql, params=None):
        self.executed.append((sql, params))
        return _FakeCursor({"?column?": 1} if self.report_already_ingested else None)

    def transaction(self):
        return _FakeTransaction()


class FakeS3:
    def __init__(self, objects=None):
        self.objects = objects or {}

    def get_object(self, Bucket, Key):
        body = self.objects[Key]
        raw = body.encode("utf-8") if isinstance(body, str) else body
        return {"Body": io.BytesIO(raw)}

    def get_paginator(self, op):
        assert op == "list_objects_v2"
        return _FakePaginator(self.objects)


class _FakePaginator:
    def __init__(self, objects):
        self.objects = objects

    def paginate(self, Bucket, Prefix):
        contents = [{"Key": k} for k in self.objects if k.startswith(Prefix)]
        yield {"Contents": contents}


EXTRACTION_KEY = "extractions/Jarley_Trainor/2026-07-06/Benl1_2026-07-06_10-00-00.json"


def make_extraction(**overrides):
    extraction = {
        "schema_version": 1,
        "user_folder": "Jarley_Trainor",
        "date": "2026-07-06",
        "session_base": "Benl1_2026-07-06_10-00-00",
        "source_transcripts": ["Benl1_2026-07-06_10-00-00.json"],
        "extracted_at": "2026-07-06T10:05:00Z",
        "declared_site": None,
        "topics": [{
            "topic_title": "Safety Briefing",
            "category": "safety",
            "summary": "Discussed PPE requirements.",
            "time_range": "10:00 - 10:05",
            "participants": ["Jarley Trainor"],
            "action_items": [
                {"action": "Order more hard hats", "responsible": "Bob", "deadline": "Friday"}
            ],
            "safety_flags": [
                {"risk_level": "medium", "observation": "Missing barrier tape",
                 "recommended_action": "Install tape"}
            ],
        }],
    }
    extraction.update(overrides)
    return extraction


@pytest.fixture
def wired(monkeypatch):
    conn = ClosingFakeConn()
    monkeypatch.setattr(iw, "get_connection", lambda *a, **k: conn)
    monkeypatch.setattr(iw, "_s3_client", FakeS3({EXTRACTION_KEY: json.dumps(make_extraction())}))
    monkeypatch.setattr(iw.companies, "get_company_by_name",
                        lambda conn, name: {"id": "co-1", "name": name})
    monkeypatch.setattr(iw.lambda_ingest, "resolve_site",
                        lambda conn, cid, report, user_folder: {"id": "site-1", "name": "Test Site"})
    monkeypatch.setattr(iw.lambda_ingest, "resolve_user", lambda conn, cid, user_folder: None)
    monkeypatch.setattr(iw.recordings, "site_for_media", lambda *a, **k: None)
    monkeypatch.setattr(iw.recordings, "site_for_day", lambda *a, **k: None)
    monkeypatch.setattr(iw.topics, "delete_topics_for_source", lambda *a, **k: 0)
    monkeypatch.setattr(iw.topics, "upsert_topic", lambda *a, **k: {"id": "topic-uuid-0"})
    monkeypatch.setattr(iw.findings, "insert_findings", lambda *a, **k: [])
    monkeypatch.setattr(iw.match_request, "emit", lambda *a, **k: None)
    monkeypatch.setattr(iw.keyframe_request, "emit", lambda *a, **k: None)
    monkeypatch.setattr(iw, "EMIT_KEYFRAME_REQUESTS", False)
    monkeypatch.conn = conn  # for tests that want to read `.closed` directly
    return monkeypatch


INTRO = {"source_filename": "Benl1_2026-07-06_10-00-00.json", "speaker_label": "spk_0",
        "start_sec": 0.0, "end_sec": 6.0, "heard_name": "Petros",
        "company_name": "Cassidy", "quote": "Hi, this is Petros from Cassidy"}


def _recording_store(calls, conn):
    def _store(conn_arg, company_id, session_base, user_folder, session_date, intros):
        calls.append({"conn_closed_at_call": conn.closed,
                      "company_id": company_id, "session_base": session_base,
                      "user_folder": user_folder, "session_date": session_date,
                      "intros": intros})
        return {"inserted": len(intros), "skipped_named": 0, "skipped_no_sid": 0}
    return _store


def test_store_is_called_with_the_extractions_fields_while_the_connection_is_open(wired):
    calls = []
    wired.setattr(iw.speaker_intro_suggestions, "store", _recording_store(calls, wired.conn))
    wired.setattr(iw, "_s3_client", FakeS3({
        EXTRACTION_KEY: json.dumps(make_extraction(self_introductions=[INTRO], tier="final"))}))

    iw.write_extraction_items("2026-07-06", "Jarley_Trainor", EXTRACTION_KEY)

    assert len(calls) == 1
    call = calls[0]
    assert call["conn_closed_at_call"] is False, (
        "store() ran after the connection closed -- the same placement mistake "
        "_request_rebind/_request_match made, which raised on every run")
    assert call["company_id"] == "co-1"
    assert call["session_base"] == "Benl1_2026-07-06_10-00-00"
    assert call["user_folder"] == "Jarley_Trainor"
    assert call["session_date"] == "2026-07-06"
    assert call["intros"] == [INTRO]
    assert wired.conn.closed is True, "the connection must still close normally afterwards"


def test_missing_field_does_not_call_store(wired):
    calls = []
    wired.setattr(iw.speaker_intro_suggestions, "store", _recording_store(calls, wired.conn))
    wired.setattr(iw, "_s3_client", FakeS3({
        EXTRACTION_KEY: json.dumps(make_extraction(tier="final"))}))  # no self_introductions key

    iw.write_extraction_items("2026-07-06", "Jarley_Trainor", EXTRACTION_KEY)

    assert calls == []


def test_empty_self_introductions_list_does_not_call_store(wired):
    calls = []
    wired.setattr(iw.speaker_intro_suggestions, "store", _recording_store(calls, wired.conn))
    wired.setattr(iw, "_s3_client", FakeS3({
        EXTRACTION_KEY: json.dumps(make_extraction(self_introductions=[], tier="final"))}))

    iw.write_extraction_items("2026-07-06", "Jarley_Trainor", EXTRACTION_KEY)

    assert calls == []


def test_store_raising_does_not_fail_the_write_and_is_logged(wired, caplog):
    def _boom(conn, company_id, session_base, user_folder, session_date, intros):
        raise RuntimeError("db down")

    wired.setattr(iw.speaker_intro_suggestions, "store", _boom)
    wired.setattr(iw, "_s3_client", FakeS3({
        EXTRACTION_KEY: json.dumps(make_extraction(self_introductions=[INTRO], tier="final"))}))
    caplog.set_level("ERROR")

    result = iw.write_extraction_items("2026-07-06", "Jarley_Trainor", EXTRACTION_KEY)

    assert result["skipped"] is False
    assert result["topics"] == 1
    assert "introduction" in caplog.text.lower()


def test_a_live_tier_extraction_does_not_call_store(wired):
    """Belt and braces: a live artifact always carries `[]` (Task 2), but the writer
    does not trust that alone -- it is gated on tier too."""
    calls = []
    wired.setattr(iw.speaker_intro_suggestions, "store", _recording_store(calls, wired.conn))
    wired.setattr(iw, "_s3_client", FakeS3({
        EXTRACTION_KEY: json.dumps(make_extraction(self_introductions=[INTRO], tier="live"))}))

    iw.write_extraction_items("2026-07-06", "Jarley_Trainor", EXTRACTION_KEY)

    assert calls == []


def test_the_seam_the_writer_reads_the_same_keys_task2_writes(wired):
    """Task 2's artifact -> this writer's call carries the SAME (source_filename,
    speaker_label) pairs."""
    calls = []
    wired.setattr(iw.speaker_intro_suggestions, "store", _recording_store(calls, wired.conn))
    artifact = make_extraction(self_introductions=[INTRO], tier="final")
    wired.setattr(iw, "_s3_client", FakeS3({EXTRACTION_KEY: json.dumps(artifact)}))

    iw.write_extraction_items("2026-07-06", "Jarley_Trainor", EXTRACTION_KEY)

    sent_keys = {(i["source_filename"], i["speaker_label"]) for i in calls[0]["intros"]}
    artifact_keys = {(i["source_filename"], i["speaker_label"])
                     for i in artifact["self_introductions"]}
    assert sent_keys == artifact_keys


def test_the_writer_stores_markers_per_session_not_per_day():
    """Prod 2026-10-02: replacing the DAY with one session's markers let the last
    session to finish erase the others'. Pinned by source: the writer merges."""
    import inspect
    import lambda_item_writer as iw
    src = inspect.getsource(iw.write_extraction_items)
    assert "location_markers.replace_for_session(" in src
    assert "_parse_extraction_key(extraction_key)[2]" in src
    assert "location_markers.replace_for_day(" not in src
