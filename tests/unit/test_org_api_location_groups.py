"""Unit: the day payload carries `location_groups` (Track L, Task 4).

`location_groups` is the day's topics AND photos under the places he said out
loud, in visit order -- `location_grouping.group_day` on the payload's own
topics and the day's tombstone-filtered photos. It is ADDITIVE:

  * `photo_groups` stays exactly what it was. The marker-day test pins it to a
    literal list (a replay of the old output), never to something derived from
    `location_groups`.
  * It rides the same gate as the photo list: a site-clipped/cross-user caller
    gets neither key.
  * A day nobody announced a place on gets neither key -- absent, not [].
  * A failure in location grouping drops only `location_groups`.
"""
import json

import pytest

org = pytest.importorskip("lambda_org_api", reason="requires psycopg (installed in CI)")

SITE_ID = "a1a1a1a1-a1a1-a1a1-a1a1-a1a1a1a1a1a1"
DATE, USER = "2026-09-02", "Neil_Blunden"
CALLER = {
    "id": "u-1", "cognito_sub": "sub-1", "company_id": "c-1", "email": "a@x.nz",
    "first_name": "Ada", "last_name": "L", "folder_name": "Ada_L",
    "avatar_s3_key": None, "global_role": "admin", "created_at": "2026-07-25",
}
PREFIX = "users/%s/pictures/%s/" % (USER, DATE)

MARKERS = [
    {"at": "17:02", "location": "level two"},
    {"at": "17:05", "location": "level three"},
]


def _name(minute):
    return "neil_blunden_2026-09-02_17-%02d-00.jpg" % minute


def _at(minute):
    return {"s3_key": PREFIX + _name(minute), "taken_at": None}


PHOTOS = [_at(3), _at(4), _at(6), _at(7)]

# What `render_report_shape` emits: a positional `topic_id`, no top-level `id`.
TOPICS = [
    {"topic_id": 0, "topic_row_id": "row-a", "time_range": "17:03 - 17:04",
     "related_photos": [_name(3)]},
    {"topic_id": 1, "topic_row_id": "row-b", "time_range": "17:06 - 17:07",
     "related_photos": []},
]


class FakeConn:
    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


def body_of(res):
    return json.loads(res["body"])


@pytest.fixture
def wired(monkeypatch):
    """One in-scope Aurora topic set -> the rendered 200 path, with markers."""
    monkeypatch.setattr(org.topics, "has_topics_for_source_prefix",
                        lambda conn, prefix: prefix.startswith("extractions/"))
    monkeypatch.setattr(org.topics, "list_topics_for_source_prefix",
                        lambda conn, prefix: [{"site_id": SITE_ID}])
    monkeypatch.setattr(org, "_allowed_site_ids", lambda conn, caller: {SITE_ID})
    monkeypatch.setattr(org, "_get_lake_json", lambda key: None)
    monkeypatch.setattr(org, "render_report_shape",
                        lambda rows, doc, date, user, conn=None, company_id=None, **kw:
                        {"report_date": date, "_report_metadata": {},
                         "topics": [dict(t, related_photos=list(t["related_photos"]))
                                    for t in TOPICS]})
    monkeypatch.setattr(org.recordings, "photo_list_for_day",
                        lambda conn, company, folder, date: list(PHOTOS))
    monkeypatch.setattr(org.redactions, "deleted_photo_keys",
                        lambda conn, company, keys=None: set())
    monkeypatch.setattr(org.location_markers, "for_day",
                        lambda conn, company, folder, date: list(MARKERS))
    return monkeypatch


def _render(caller=None, **kw):
    return org._render_timeline_for_user(
        FakeConn(), dict(caller or CALLER), DATE, USER, **kw)


# --------------------------------------------------------------------------

def test_a_marker_day_carries_both_keys(wired):
    res = _render()
    assert res["statusCode"] == 200
    body = body_of(res)

    # REPLAY of the existing output on this fixture -- not derived from
    # location_groups. Raw marker text, filenames by their own time.
    assert body["photo_groups"] == [
        {"location": "level two", "filenames": [_name(3), _name(4)]},
        {"location": "level three", "filenames": [_name(6), _name(7)]},
    ]

    assert body["location_groups"] == [
        {"location": "Level 2", "kind": "level", "key": body["location_groups"][0]["key"],
         "reason": None, "order": 0, "topic_ids": [0],
         "photos": [_name(3)], "unbound_photos": [_name(4)]},
        {"location": "Level 3", "kind": "level", "key": body["location_groups"][1]["key"],
         "reason": None, "order": 1, "topic_ids": [1],
         "photos": [], "unbound_photos": [_name(6), _name(7)]},
    ]
    keys = [g["key"] for g in body["location_groups"]]
    assert all(keys) and keys[0] != keys[1]


def test_location_group_topic_ids_are_the_ids_topics_carry(wired):
    body = body_of(_render())
    payload_ids = [t["topic_id"] for t in body["topics"]]
    grouped = [i for g in body["location_groups"] for i in g["topic_ids"]]
    assert sorted(grouped) == sorted(payload_ids), "each topic placed exactly once"
    # ...and not the durable row id, which group_day would never see as `id`.
    assert not set(grouped) & {t["topic_row_id"] for t in body["topics"]}


def test_location_groups_see_only_the_tombstone_filtered_photos(wired):
    wired.setattr(org.redactions, "deleted_photo_keys",
                  lambda conn, company, keys=None: {PREFIX + _name(4)})
    body = body_of(_render())
    every = [n for g in body["location_groups"]
             for n in g["photos"] + g["unbound_photos"]]
    assert _name(4) not in every
    assert sorted(every) == sorted(body["photo_filenames"])


def test_markers_are_read_once_for_both_groupings(wired):
    calls = []

    def _count(conn, company, folder, date):
        calls.append(company)
        return list(MARKERS)

    wired.setattr(org.location_markers, "for_day", _count)
    body = body_of(_render())
    assert "photo_groups" in body and "location_groups" in body
    assert calls == ["c-1"]


def test_a_cross_user_caller_gets_neither_key(wired):
    """The gate is on the whole photo block: markers and timed topics are all
    there, so absence here is the gate and not a no-data coincidence."""
    called = []
    wired.setattr(org.location_grouping, "group_day",
                  lambda *a, **k: called.append(1) or [])
    res = _render(cross_user_clip=True)
    assert res["statusCode"] == 200
    body = body_of(res)
    assert "photo_groups" not in body
    assert "location_groups" not in body
    assert "photo_filenames" not in body
    assert called == []
    assert len(body["topics"]) == 2, "a gate on the field, not on the day"


def test_a_day_nobody_announced_a_place_has_neither_key(wired):
    wired.setattr(org.location_markers, "for_day",
                  lambda conn, company, folder, date: [])
    body = body_of(_render())
    assert "photo_groups" not in body
    assert "location_groups" not in body
    assert len(body["photo_filenames"]) == 4


def test_a_failing_group_day_keeps_photo_groups(wired):
    def _boom(*a, **k):
        raise RuntimeError("grouping blew up")

    wired.setattr(org.location_grouping, "group_day", _boom)
    res = _render()
    assert res["statusCode"] == 200
    body = body_of(res)
    assert "location_groups" not in body
    assert body["photo_groups"] == [
        {"location": "level two", "filenames": [_name(3), _name(4)]},
        {"location": "level three", "filenames": [_name(6), _name(7)]},
    ]


def test_a_marker_read_failure_drops_both_and_keeps_the_day(wired):
    def _boom(conn, company, folder, date):
        raise RuntimeError('relation "day_location_markers" does not exist')

    wired.setattr(org.location_markers, "for_day", _boom)
    res = _render()
    assert res["statusCode"] == 200
    body = body_of(res)
    assert "photo_groups" not in body and "location_groups" not in body
    assert len(body["photo_filenames"]) == 4
