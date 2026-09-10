"""Unit: a closed action is not the most overdue thing on site.

org-api serves `status` on every action item -- it is one of the five editable
columns (`repositories/action_items.py:_EDITABLE`), and ticking something off on
the Today page is what writes it. `report_sections._actions` did not read the
field.

Two consequences, and the second is worse than the first:

1. A job somebody had already closed was listed beside the ones still owed, with
   nothing on the row to tell them apart.
2. Closed items are the ones most likely to have a deadline in the PAST -- that
   is why they got done. Ranked by days-from-today, a finished job with last
   Tuesday's date sorted ABOVE everything genuinely outstanding, so the top of
   the action list was the work already finished.

Closed rows are kept, not dropped: "we said we would do this, and it is done" is
the good half of a daily report, and removing it would make a report of a
productive day shorter than one of an idle day.
"""
import report_sections

DATE = "2026-09-11"


def _topic(*items):
    return {"topic_title": "T", "action_items": list(items)}


def _act(action, **kw):
    row = {"action": action}
    row.update(kw)
    return row


def _actions(report):
    for section in report_sections.build(report):
        if section["title"] == "Actions":
            return section
    return None


def _rows(*topics):
    return _actions({"date": DATE, "topics": list(topics)})["rows"]


def test_status_reaches_the_row():
    rows = _rows(_topic(_act("Order the steel", status="done")))
    assert rows[0]["status"] == "done"


def test_an_item_with_no_status_is_open_not_blank():
    """The daily report generator's own extraction has no status field at all --
    everything it writes is newly said and therefore open. A blank would rank
    as neither open nor closed and read as an unknown state on the page."""
    rows = _rows(_topic(_act("Order the steel")))
    assert rows[0]["status"] == "open"


def test_status_is_a_declared_field_so_a_renderer_can_find_it():
    """`sections` is consumed by field name; a value on the row that is not in
    `fields` is invisible to the table renderer on the other side."""
    assert "status" in _actions({"date": DATE, "topics": [_topic(_act("x"))]})["fields"]


def test_a_finished_item_with_a_past_deadline_does_not_lead_the_list():
    """The exact failure: done last Tuesday sorts as five days overdue."""
    rows = _rows(_topic(
        _act("Fix the handrail", deadline="2026-09-04", status="done"),
        _act("Chase the RFI", deadline="2026-09-12"),
    ))
    assert [r["action"] for r in rows] == ["Chase the RFI", "Fix the handrail"]


def test_closed_sorts_below_even_an_undated_open_item():
    """Undated open work still has to be done; closed work does not."""
    rows = _rows(_topic(
        _act("Sign off the pour", deadline="2026-09-01", status="completed"),
        _act("Someone should look at the gate"),
    ))
    assert [r["status"] for r in rows] == ["open", "completed"]


def test_the_closed_vocabulary_covers_what_the_column_actually_holds():
    for word in ("done", "completed", "complete", "closed", "cancelled",
                 "canceled", "resolved", "DONE", " Done "):
        rows = _rows(_topic(
            _act("Closed thing", deadline="2026-09-01", status=word),
            _act("Open thing"),
        ))
        assert rows[0]["action"] == "Open thing", word


def test_an_unrecognised_status_is_treated_as_open():
    """"in_progress", "blocked", "pending" are not finished. Guessing the other
    way would hide live work at the bottom of the list."""
    for word in ("in_progress", "blocked", "pending", "open"):
        rows = _rows(_topic(
            _act("Live thing", status=word),
            _act("Really done", deadline="2026-09-01", status="done"),
        ))
        assert rows[0]["action"] == "Live thing", word


def test_raised_again_after_being_closed_is_open():
    """One action named by two topics, closed in one and open in the other,
    means somebody brought it back. Marking it done while a later mention is
    still asking for it is the one direction that loses work."""
    rows = _rows(
        _topic(_act("Replace the barrier", status="done")),
        _topic(_act("Replace the barrier")),
    )
    assert len(rows) == 1
    assert rows[0]["status"] == "open"


def test_closed_in_every_mention_stays_closed():
    rows = _rows(
        _topic(_act("Replace the barrier", status="done")),
        _topic(_act("Replace the barrier", status="closed")),
    )
    assert len(rows) == 1
    assert rows[0]["status"] in ("done", "closed")
