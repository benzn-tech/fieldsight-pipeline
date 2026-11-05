"""Unit: the org-api day view's report carries a Locations section only for an
unclipped caller (Track L, Task 5).

A place plus a minute is a fact about where the target was, so a site-clipped
or cross-user caller must not get it. Kept apart from
test_report_sections_locations.py so the pure tests there never skip when
psycopg is absent.
"""
import json

import pytest

import report_sections as rs
DATE, USER = "2026-09-02", "Neil_Blunden"
PREFIX = "users/%s/pictures/%s/" % (USER, DATE)
MARKERS = [
    {"at": "17:02", "location": "level two"},
    {"at": "17:05", "location": "level three"},
]


def _name(hh, mm):
    return "neil_blunden_2026-09-02_%02d-%02d-00.jpg" % (hh, mm)


def _topic(title, time_range, photos=(), **kw):
    t = {"topic_title": title, "time_range": time_range, "category": "quality",
         "summary": "", "key_decisions": [], "action_items": [],
         "related_photos": list(photos)}
    t.update(kw)
    return t


TOPICS = [
    _topic("Framing on level two", "17:03 - 17:04", [_name(17, 3)],
           key_decisions=["Accept framing"],
           action_items=[{"action": "Send framing photos",
                          "responsible": "Neil", "priority": "high"}]),
    _topic("Bracing on level three", "17:06 - 17:07",
           [_name(17, 6), _name(17, 7)],
           open_questions=["Who supplies braces?"]),
]


def _titles(sections):
    return [s["title"] for s in sections]

org = pytest.importorskip("lambda_org_api", reason="requires psycopg (installed in CI)")

SITE_ID = "a1a1a1a1-a1a1-a1a1-a1a1-a1a1a1a1a1a1"
CALLER = {
    "id": "u-1", "cognito_sub": "sub-1", "company_id": "c-1", "email": "a@x.nz",
    "first_name": "Ada", "last_name": "L", "folder_name": "Ada_L",
    "avatar_s3_key": None, "global_role": "admin", "created_at": "2026-07-25",
}


class FakeConn:
    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


def _shape(rows, doc, date, user, conn=None, company_id=None, **kw):
    """What `render_report_shape` emits, sections built the way it builds them."""
    shaped = {"report_date": date, "user_name": user, "_report_metadata": {},
              "safety_observations": [{"observation": "Open edge", "risk_level": "high"}],
              "topics": [dict(t, topic_id=i, related_photos=list(t["related_photos"]))
                         for i, t in enumerate(TOPICS)]}
    shaped["sections"] = rs.build(shaped)
    return shaped


@pytest.fixture
def wired(monkeypatch):
    monkeypatch.setattr(org.topics, "has_topics_for_source_prefix",
                        lambda conn, prefix: prefix.startswith("extractions/"))
    monkeypatch.setattr(org.topics, "list_topics_for_source_prefix",
                        lambda conn, prefix: [{"site_id": SITE_ID}])
    monkeypatch.setattr(org, "_allowed_site_ids", lambda conn, caller: {SITE_ID})
    monkeypatch.setattr(org, "_get_lake_json", lambda key: None)
    monkeypatch.setattr(org, "render_report_shape", _shape)
    monkeypatch.setattr(org.recordings, "photo_list_for_day",
                        lambda conn, company, folder, date: [
                            {"s3_key": PREFIX + n, "taken_at": None}
                            for n in (_name(17, 3), _name(17, 6), _name(17, 7))])
    monkeypatch.setattr(org.redactions, "deleted_photo_keys",
                        lambda conn, company, keys=None: set())
    monkeypatch.setattr(org.location_markers, "for_day",
                        lambda conn, company, folder, date: list(MARKERS))
    return monkeypatch


def _render(**kw):
    return org._render_timeline_for_user(FakeConn(), dict(CALLER), DATE, USER, **kw)


def test_org_api_day_view_carries_locations_between_safety_and_photos(wired):
    res = _render()
    assert res["statusCode"] == 200
    body = json.loads(res["body"])
    assert _titles(body["sections"]) == ["Summary", "Actions", "Open Questions", "Decisions",
                                         "Safety", "Locations", "Photos"]
    # The markers ride a shallow copy into `build`: the served payload does not
    # gain a raw `location_markers` key.
    assert "location_markers" not in body


def test_org_api_clipped_caller_gets_no_locations_section(wired):
    """Markers and timed topics are all there, so the absence is the gate."""
    res = _render(cross_user_clip=True)
    assert res["statusCode"] == 200
    body = json.loads(res["body"])
    assert "Locations" not in _titles(body["sections"])
    assert "location_markers" not in body


def test_org_api_day_without_markers_keeps_its_sections(wired):
    wired.setattr(org.location_markers, "for_day",
                  lambda conn, company, folder, date: [])
    body = json.loads(_render()["body"])
    assert _titles(body["sections"]) == ["Summary", "Actions", "Open Questions", "Decisions",
                                         "Safety", "Photos"]
