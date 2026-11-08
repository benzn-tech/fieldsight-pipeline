"""The confirmation email's rows carry a time, and the final request carries the
day's places (Track L Task 7 Step 1, backend half).

The Where column places a row by its `at`. On the path prod runs -- the rows
item-writer builds from the final extraction -- no row had one: only the brief's
action rows (SESSION_BRIEF, TEST) carried `at`. So each extraction-built row now
carries the START of its topic's `time_range` ("HH:MM"), and the final request
carries the day's markers from `location_markers.for_day`, attached by the step
that just stored this session's own markers.
"""
import pytest

iw = pytest.importorskip("lambda_item_writer", reason="requires psycopg (installed in CI)")

DATE = "2026-11-09"

ART = {"topics": [
    {"topic_title": "Formwork", "time_range": "09:05 - 09:12",
     "action_items": [{"action": "Strip the level 2 forms", "responsible": "Neil"},
                      {"action": "Order more ties", "responsible": None}]},
    {"topic_title": "Rebar", "summary": "Cover was short on the east side.",
     "time_range": "9:25 - 9:30", "action_items": []},
    {"topic_title": "No clock", "summary": "Said in passing.", "action_items": []},
]}


def test_THE_test_an_action_row_starts_when_its_topic_starts():
    rows = iw._final_email_rows(ART, DATE)
    assert [(r["text"], r["at"]) for r in rows] == [
        ("Strip the level 2 forms", "09:05"), ("Order more ties", "09:05")]


def test_a_topic_row_starts_when_its_topic_starts():
    rows = iw._topic_rows(ART)
    assert [(r["text"].split(" — ")[0], r["at"]) for r in rows] == [
        ("Rebar", "9:25"), ("No clock", None)]


def test_a_topic_with_no_time_range_gives_no_time_rather_than_a_guess():
    art = {"topics": [{"topic_title": "T", "action_items": [{"action": "Do it"}]}]}
    assert iw._final_email_rows(art, DATE)[0]["at"] is None


# ---- the day's places ride on the final request ---------------------------

class _Tx:
    def __init__(self, conn):
        self.conn = conn

    def __enter__(self):
        self.conn.savepoints += 1
        return self

    def __exit__(self, *a):
        return False


class _Conn:
    def __init__(self):
        self.savepoints = 0

    def transaction(self):
        return _Tx(self)


def test_the_days_markers_are_attached(monkeypatch):
    seen = {}
    markers = [{"at": "09:00", "location": "Level 2"}]

    def for_day(conn, company_id, folder, date):
        seen["args"] = (company_id, folder, date)
        return markers
    monkeypatch.setattr(iw.location_markers, "for_day", for_day)
    conn = _Conn()
    ctx = {"kind": "final"}
    iw._attach_day_markers(conn, ctx, "co-1", "Ben_Lin_Test", DATE)
    assert ctx["locationMarkers"] == markers
    assert seen["args"] == ("co-1", "Ben_Lin_Test", DATE)
    assert conn.savepoints == 1, "the read runs in its own savepoint"


def test_a_failed_read_leaves_the_key_absent_and_the_email_alone(monkeypatch, caplog):
    def boom(*a, **k):
        raise RuntimeError("db down")
    monkeypatch.setattr(iw.location_markers, "for_day", boom)
    ctx = {"kind": "final"}
    iw._attach_day_markers(_Conn(), ctx, "co-1", "F", DATE)
    assert "locationMarkers" not in ctx
    assert any("markers" in r.getMessage() for r in caplog.records)


def test_a_day_with_no_markers_adds_no_key(monkeypatch):
    monkeypatch.setattr(iw.location_markers, "for_day", lambda *a: [])
    ctx = {"kind": "final"}
    iw._attach_day_markers(_Conn(), ctx, "co-1", "F", DATE)
    assert "locationMarkers" not in ctx


def test_the_writer_attaches_them_after_storing_this_sessions_own():
    """The call site is inside `write_extraction_items` (no unit harness drives it
    to the email branch), so its ORDER is pinned from the source: after
    `replace_for_session` -- else this session's own places are missing -- and
    outside `_final_email_context`, whose tests pass a fake connection."""
    import inspect
    src = inspect.getsource(iw.write_extraction_items)
    stored = src.index("location_markers.replace_for_session(")
    built = src.index("final_email_ctx = _final_email_context(")
    attached = src.index("_attach_day_markers(")
    assert stored < built < attached
    assert "_attach_day_markers" not in inspect.getsource(iw._final_email_context)
