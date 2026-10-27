"""Unit: "Closed today" / "Closed this week" in the day report (owner, 2026-10-27).

Two finished statuses, and the reader must be able to tell them apart: `done` is
work that was done, `closed` is work that no longer needs doing (cancelled,
superseded, duplicate). Both leave the list; only one is a completion.

The rows come from the audit trail (content_edits), not from the status column,
because "closed THIS WEEK" needs the day each one happened and who did it. The SQL
itself is exercised against Postgres in
tests/integration/test_action_closed_status.py; this file covers the shaping.
"""
from datetime import date

import lambda_org_api as org
import report_sections as rs
from tests.unit.test_lambda_ingest import wired      # noqa: F401  (fixture)

SITE_ID = "11111111-1111-1111-1111-111111111111"
USER_ID = "22222222-2222-2222-2222-222222222222"
DAY = "2026-10-27"          # a Tuesday


def _closure(text, status, on, by="Sarah Lee"):
    return {"stable_id": "s-" + text, "text": text, "status": status,
            "closed_on": on, "closed_by": by}


def _sections(closures, day=DAY):
    return {s["title"]: s for s in rs.build({"report_date": day, "closures": closures})}


def test_today_and_this_week_are_separate_and_today_is_not_repeated():
    out = _sections([
        _closure("Order steel", "done", DAY),
        _closure("Book crane", "closed", "2026-10-24"),
    ])
    assert out["Closed today"]["kind"] == "entries"
    assert out["Closed today"]["items"] == [
        {"title": "Order steel", "status": "done", "note": "by Sarah Lee"}]
    assert out["Closed this week"]["items"] == [
        {"title": "Book crane", "status": "not needed", "note": "Sat 24 Oct · by Sarah Lee"}]


def test_done_and_closed_are_marked_differently():
    items = _sections([_closure("a", "done", DAY), _closure("b", "closed", DAY)])[
        "Closed today"]["items"]
    assert [i["status"] for i in items] == ["done", "not needed"]


def test_no_closures_key_means_no_sections():
    """The nightly generator and meeting minutes have no database: no key, no section,
    rather than an empty heading claiming nothing was closed."""
    titles = [s["title"] for s in rs.build({"report_date": DAY})]
    assert "Closed today" not in titles and "Closed this week" not in titles
    assert not [s for s in rs.build({"report_date": DAY, "closures": []})
                if s["title"].startswith("Closed")]


def test_unknown_status_and_blank_text_are_dropped():
    out = _sections([_closure("x", "open", DAY), _closure("", "done", DAY),
                     "not a dict", _closure("kept", "done", DAY, by=None)])
    assert out["Closed today"]["items"] == [{"title": "kept", "status": "done", "note": ""}]


def test_the_sections_sit_after_actions():
    report = {"report_date": DAY, "closures": [_closure("Order steel", "done", DAY)],
              "topics": [{"topic_title": "Slab", "action_items": [{"action": "Pour"}]}]}
    titles = [s["title"] for s in rs.build(report)]
    assert titles.index("Actions") < titles.index("Closed today")


# ------------------------------------------------------- org-api wiring

def test_report_closures_reads_a_seven_day_window_bounded_by_the_rows(wired):
    seen = {}

    def fake(conn, site_ids, start, end, company_id=None, author_ids=None, **kw):
        seen.update(site_ids=site_ids, start=start, end=end, company_id=company_id,
                    author_ids=author_ids)
        return [_closure("Order steel", "done", DAY)]

    wired.setattr(org.content_edits, "list_action_finishes", fake)
    rows = [{"site_id": SITE_ID, "user_id": USER_ID}]
    out = org._report_closures(object(), rows, DAY, "c-1")
    assert out[0]["text"] == "Order steel"
    assert seen == {"site_ids": [SITE_ID], "start": date(2026, 10, 21),
                    "end": date(2026, 10, 27), "company_id": "c-1",
                    "author_ids": [USER_ID]}


def test_report_closures_without_an_author_or_site_reads_nothing(wired):
    """No author = no bound. Never widen to "every recording on the site"."""
    wired.setattr(org.content_edits, "list_action_finishes",
                  lambda *a, **k: (_ for _ in ()).throw(AssertionError("queried")))
    assert org._report_closures(object(), [{"site_id": SITE_ID}], DAY, "c-1") is None
    assert org._report_closures(object(), [{"user_id": USER_ID}], DAY, "c-1") is None


def test_report_closures_is_never_fatal(wired):
    def boom(*a, **k):
        raise RuntimeError("db down")

    wired.setattr(org.content_edits, "list_action_finishes", boom)
    rows = [{"site_id": SITE_ID, "user_id": USER_ID}]
    assert org._report_closures(object(), rows, DAY, "c-1") is None


def test_render_without_a_connection_carries_no_closures():
    """reindex.py renders with conn=None: nothing to read, nothing added."""
    row = {"id": "t-1", "site_id": SITE_ID, "user_id": USER_ID, "site_name": "Alpha",
           "user_name": "Ada L", "category": "progress", "title": "Slab", "summary": "s",
           "time_range": "08:00 – 08:15", "participants": [], "action_items": [],
           "safety_observations": [], "findings": [], "photos": []}
    shaped = org.render_report_shape([row], None, DAY, "Ada_L")
    assert "closures" not in shaped
