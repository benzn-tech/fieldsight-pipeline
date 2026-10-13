"""Every site's coordinate is published daily, not only when someone edits it.

Found 2026-09-29: config/site-coords.json had never been written on prod. The
publish ran on site save, and no site had been saved with a coordinate since it
shipped -- so every production daily report still had no weather, which is
the defect the publish existed to fix.

THE test is `a site nobody has edited is published by the schedule`.
"""
import json
import os

import pytest

import lambda_org_api as api

ROWS = [
    {"id": "s1", "slug": "uc-pk", "name": "UC PK", "latitude": -43.52, "longitude": 172.58},
    {"id": "s2", "slug": "sb1131-northbrook-wanaka", "name": "Northbrook", "latitude": -44.70,
     "longitude": 169.13},
    {"id": "s3", "slug": "mpi", "name": "MPI", "latitude": None, "longitude": None},
]


@pytest.fixture
def lake(monkeypatch):
    state = {"doc": None, "puts": []}

    class S3:
        def put_object(self, **kw):
            state["puts"].append(kw)
            state["doc"] = json.loads(kw["Body"].decode("utf-8"))

    monkeypatch.setattr(api, "s3", lambda: S3())
    monkeypatch.setattr(api, "_get_lake_json", lambda key: state["doc"])
    monkeypatch.setattr(api.sites, "list_all_sites", lambda conn, include_archived=False: list(ROWS))
    return state


def test_THE_a_site_nobody_has_edited_is_published_by_the_schedule(lake):
    out = api.republish_all_site_coords(conn=None)
    assert out == {"changed": True, "sites": 3, "placed": 2}
    assert set(lake["doc"]) == {"uc-pk", "sb1131-northbrook-wanaka"}
    assert lake["doc"]["uc-pk"]["latitude"] == -43.52
    assert lake["puts"][0]["Key"] == api.site_coords.KEY


def test_nothing_changed_means_nothing_written(lake):
    api.republish_all_site_coords(conn=None)
    out = api.republish_all_site_coords(conn=None)
    assert out["changed"] is False and len(lake["puts"]) == 1


def test_a_coordinate_removed_in_the_ui_is_removed_from_the_file(lake, monkeypatch):
    api.republish_all_site_coords(conn=None)
    ROWS_WITHOUT = [dict(ROWS[0], latitude=None, longitude=None)] + ROWS[1:]
    monkeypatch.setattr(api.sites, "list_all_sites",
                        lambda conn, include_archived=False: ROWS_WITHOUT)
    api.republish_all_site_coords(conn=None)
    assert "uc-pk" not in lake["doc"]


def test_the_schedule_reaches_it_and_an_api_request_does_not(monkeypatch):
    called = []

    class Conn:
        def __enter__(self): return self
        def __exit__(self, *a): return False

    monkeypatch.setattr(api, "get_connection", lambda: Conn())
    monkeypatch.setattr(api, "republish_all_site_coords", lambda conn: called.append(1) or {"ok": 1})
    monkeypatch.setattr(api, "dispatch", lambda conn, event, method, route: {"statusCode": 404})
    assert api.lambda_handler({"task": "republish_site_coords"}, None) == {"ok": 1}
    # An API Gateway event: the request body cannot put `task` at the top level.
    api.lambda_handler({"httpMethod": "POST", "path": "/api/org/sites",
                        "body": json.dumps({"task": "republish_site_coords"})}, None)
    assert called == [1]


def test_the_template_schedules_it_before_the_daily_reports():
    path = os.path.join(os.path.dirname(__file__), "..", "..", "src", "template.yaml")
    t = open(path, encoding="utf-8").read()
    i = t.index("SiteCoordsRepublish:")
    block = t[i:i + 500]
    assert "cron(30 15 * * ? *)" in block, "04:30 NZDT, half an hour before cron(0 16)"
    assert '"task": "republish_site_coords"' in block
    assert "cron(0 16 * * ? *)" in t
