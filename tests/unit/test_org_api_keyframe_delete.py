"""DELETE /api/org/media/keyframe (video-keyframe Q7). Reuses the make_event /
CALLER idiom from test_lambda_org_api.py; monkeypatches org.keyframes,
org._topic_authority and org.s3 collaborators so the handler logic (refuse
non-keyframe, ACL, transaction order, idempotent re-delete, S3-after-commit) is
asserted in isolation."""
import json

import pytest

org = pytest.importorskip("lambda_org_api", reason="requires psycopg (installed in CI)")


CALLER = {
    "id": "u-uuid-1", "cognito_sub": "sub-1", "company_id": "c-uuid-1",
    "email": "a@x.nz", "first_name": "Ada", "last_name": "L",
    "avatar_s3_key": None, "global_role": "admin", "created_at": "2026-07-04",
}

KF_KEY = "users/Ben_UCPK/pictures/2026-07-23/Benl1_2026-07-23_10-16-00_kf_s101534.jpg"


def body_of(res):
    return json.loads(res["body"])


class _Cursor:
    def __init__(self, conn):
        self.conn = conn
        self.sql = None
        self.params = None

    def execute(self, sql, params=None):
        self.sql = sql
        self.params = params
        self.conn.executed.append((sql, params))
        low = sql.lower()
        if "delete from topic_photos" in low:
            self.conn.order.append("delete_row")
        return self

    def fetchall(self):
        if "from topic_photos" in self.sql.lower():
            return list(self.conn.photo_rows)
        return []

    def fetchone(self):
        if "from topics" in self.sql.lower():
            return self.conn.topic_row
        return None

    @property
    def rowcount(self):
        if "delete from topic_photos" in self.sql.lower():
            return self.conn.deleted_rowcount
        return 0


class KFConn:
    """Handles the topic_photos SELECT/DELETE and the telemetry topics SELECT;
    records commit ordering into a shared `order` list."""

    def __init__(self, photo_rows=(), topic_row=None, order=None):
        self.photo_rows = list(photo_rows)
        self.topic_row = topic_row
        self.deleted_rowcount = len(self.photo_rows)
        self.executed = []
        self.committed = False
        self.order = order if order is not None else []

    def cursor(self, *a, **k):
        return _Cursor(self)

    def execute(self, sql, params=None):
        return _Cursor(self).execute(sql, params)

    def commit(self):
        self.committed = True
        self.order.append("commit")


class FakeS3:
    def __init__(self, order, raise_on_delete=False):
        self.order = order
        self.raise_on_delete = raise_on_delete
        self.deleted = []

    def delete_object(self, Bucket, Key):
        self.order.append("s3_delete")
        if self.raise_on_delete:
            raise Exception("s3 boom")
        self.deleted.append(Key)
        return {}


@pytest.fixture
def wired(monkeypatch):
    """Default: authorized topic, recording keyframes collaborators, a FakeS3.
    Returns a bag the tests read (order list, recorded tombstone/event args)."""
    order = []
    bag = {"order": order, "tombstones": [], "events": [],
           "authority": ("t-1", None)}

    def add_tombstone(conn, s3_key, company_id, topic_id, deleted_by):
        order.append("tombstone")
        bag["tombstones"].append((s3_key, company_id, topic_id, deleted_by))
        # True = a NEW tombstone row; False = ON CONFLICT DO NOTHING (already
        # tombstoned, e.g. the row was resurrected by a re-extraction).
        return bag.get("tombstone_is_new", True)

    def record_event(conn, event, **kw):
        order.append("event")
        bag["events"].append((event, kw))
        return {"id": "e-1"}

    def get_tombstone(conn, s3_key):
        return bag.get("tombstone_row")

    monkeypatch.setattr(org.keyframes, "add_tombstone", add_tombstone)
    monkeypatch.setattr(org.keyframes, "record_event", record_event)
    monkeypatch.setattr(org.keyframes, "get_tombstone", get_tombstone)

    # authorized topic: (row, None); tests override for deny/404.
    def _authority(conn, caller, tid):
        row, err = bag["authority"]
        if err is not None:
            return None, err
        return {"id": tid, "company_id": "c-uuid-1", "site_id": "s-1"}, None

    monkeypatch.setattr(org, "_topic_authority", _authority)

    s3 = FakeS3(order)
    monkeypatch.setattr(org, "s3", lambda: s3)
    bag["s3"] = s3
    bag["monkeypatch"] = monkeypatch
    return bag


def _call(conn, body):
    return org.delete_keyframe_endpoint(conn, dict(CALLER), body)


# --------------------------------------------------------------------------
# Refuse: never let this route touch a real user photo
# --------------------------------------------------------------------------

@pytest.mark.parametrize("bad_key", [
    "users/Ben_UCPK/pictures/2026-07-23/Benl1_2026-07-23_10-17-30.jpg",  # real photo
    "users/x/pictures/d/evil.jpg",                                       # crafted non-kf
    "org-assets/logo.png",                                               # wrong prefix
    "users/x/video/d/Benl1_2026-07-23_10-16-00_kf_s101534.jpg",          # not /pictures/
    # regex anchor: `$` would also match before a trailing newline, letting a
    # key ending "...jpg\n" through the guard. \Z anchors at the true end.
    "users/Ben_UCPK/pictures/2026-07-23/Benl1_2026-07-23_10-16-00_kf_s101534.jpg\n",
])
def test_delete_refuses_non_keyframe_key(wired, bad_key):
    conn = KFConn(order=wired["order"])
    res = _call(conn, {"s3_key": bad_key})
    assert res["statusCode"] == 400
    # ZERO DB / S3 / collaborator calls -- refused before any lookup
    assert conn.executed == [] and wired["order"] == []
    assert wired["tombstones"] == [] and wired["events"] == []


@pytest.mark.parametrize("bad_key", [
    "users/x/../y/pictures/d/a_kf_s101534.jpg",   # dot-segment
    "users//x/pictures/d/a_kf_s101534.jpg",       # double slash
    "/users/x/pictures/d/a_kf_s101534.jpg",       # leading slash
])
def test_delete_rejects_non_canonical_key(wired, bad_key):
    conn = KFConn(order=wired["order"])
    res = _call(conn, {"s3_key": bad_key})
    assert res["statusCode"] == 403
    assert conn.executed == []


def test_delete_missing_s3_key_400(wired):
    conn = KFConn(order=wired["order"])
    assert _call(conn, {})["statusCode"] == 400
    assert _call(conn, None)["statusCode"] == 400


# --------------------------------------------------------------------------
# Happy path -- exact transaction order
# --------------------------------------------------------------------------

def test_delete_happy_path_order(wired):
    conn = KFConn(photo_rows=[{"id": "p-1", "topic_id": "t-1"}],
                  topic_row={"time_range": "10:15 – 10:17", "category": "safety",
                             "work_class": "work", "site_id": "s-1"},
                  order=wired["order"])
    res = _call(conn, {"s3_key": KF_KEY})
    assert res["statusCode"] == 200
    b = body_of(res)
    assert b["deleted"] is True and b["rows_removed"] == 1 and b["s3_deleted"] is True
    # tombstone -> event -> DELETE row -> commit -> S3 delete
    assert wired["order"] == ["tombstone", "event", "delete_row", "commit", "s3_delete"]
    assert conn.committed is True
    assert wired["s3"].deleted == [KF_KEY]
    # tombstone carries the durable key + resolved company + topic + actor
    assert wired["tombstones"] == [(KF_KEY, "c-uuid-1", "t-1", "u-uuid-1")]


def test_deleted_event_shape_is_structural_only(wired):
    conn = KFConn(photo_rows=[{"id": "p-1", "topic_id": "t-1"}],
                  topic_row={"time_range": "10:15 – 10:17", "category": "safety",
                             "work_class": "work", "site_id": "s-1"},
                  order=wired["order"])
    _call(conn, {"s3_key": KF_KEY})
    event, kw = wired["events"][0]
    assert event == "deleted"
    # exactly the 0024 structural columns -- no s3_key, no caption, no free text
    assert set(kw) == {"company_id", "site_id", "topic_category", "work_class",
                       "duration_min", "n_frames_generated", "frame_index"}
    assert kw["company_id"] == "c-uuid-1"
    assert kw["topic_category"] == "safety"
    assert kw["duration_min"] == 2          # 10:15-10:17
    assert kw["n_frames_generated"] == 1    # single frame for a 2-min window
    assert kw["frame_index"] == 0           # 10:16:00 is that frame


def test_deleted_event_degrades_to_nulls_on_unparseable_range(wired):
    conn = KFConn(photo_rows=[{"id": "p-1", "topic_id": "t-1"}],
                  topic_row={"time_range": None, "category": None,
                             "work_class": None, "site_id": None},
                  order=wired["order"])
    res = _call(conn, {"s3_key": KF_KEY})
    assert res["statusCode"] == 200          # NULLs, never a failed delete
    _, kw = wired["events"][0]
    assert kw["duration_min"] is None and kw["frame_index"] is None
    assert kw["n_frames_generated"] is None


def test_second_delete_of_resurrected_row_records_no_duplicate_event(wired):
    """Ratio integrity: in the s3_deleted:false window an item-writer
    re-extraction can re-bind the still-present object (photo_binding has no
    tombstone check), resurrecting the topic_photos row. A second user delete
    must NOT record a second 'deleted' event for ONE keyframe -- that would
    inflate the deleted/generated ratio this table exists to measure."""
    wired["tombstone_is_new"] = False          # ON CONFLICT DO NOTHING
    conn = KFConn(photo_rows=[{"id": "p-2", "topic_id": "t-1"}],
                  topic_row={"time_range": "10:15 – 10:17", "category": "safety",
                             "work_class": "work", "site_id": "s-1"},
                  order=wired["order"])
    res = _call(conn, {"s3_key": KF_KEY})
    assert res["statusCode"] == 200
    # the resurrected row is still removed and the object still deleted...
    assert wired["order"] == ["tombstone", "delete_row", "commit", "s3_delete"]
    # ...but NO second 'deleted' event
    assert wired["events"] == []


# --------------------------------------------------------------------------
# ACL
# --------------------------------------------------------------------------

def test_delete_denied_without_topic_authority(wired):
    wired["authority"] = (None, org.error("admin/gm, this site's pm/site_manager, "
                                          "or the author only", 403))
    conn = KFConn(photo_rows=[{"id": "p-1", "topic_id": "t-1"}], order=wired["order"])
    res = _call(conn, {"s3_key": KF_KEY})
    assert res["statusCode"] == 403
    # no tombstone, no event, no row delete, no S3 call
    assert wired["order"] == []
    assert wired["tombstones"] == [] and wired["events"] == []
    assert wired["s3"].deleted == []


def test_delete_cross_company_topic_404(wired):
    wired["authority"] = (None, org.error("topic not found", 404))
    conn = KFConn(photo_rows=[{"id": "p-1", "topic_id": "t-1"}], order=wired["order"])
    res = _call(conn, {"s3_key": KF_KEY})
    assert res["statusCode"] == 404
    assert wired["order"] == []


# --------------------------------------------------------------------------
# Unknown key + idempotent re-delete
# --------------------------------------------------------------------------

def test_delete_unknown_key_404(wired):
    # no topic_photos rows, no tombstone -> 404
    conn = KFConn(photo_rows=[], order=wired["order"])
    res = _call(conn, {"s3_key": KF_KEY})
    assert res["statusCode"] == 404
    assert wired["order"] == []


def test_redelete_is_idempotent(wired):
    # no rows, but a tombstone (same company) exists -> 200 already_deleted,
    # S3 delete re-attempted, NO second tombstone/event
    wired["tombstone_row"] = {"s3_key": KF_KEY, "company_id": "c-uuid-1"}
    conn = KFConn(photo_rows=[], order=wired["order"])
    res = _call(conn, {"s3_key": KF_KEY})
    assert res["statusCode"] == 200
    b = body_of(res)
    assert b["already_deleted"] is True and b["s3_deleted"] is True
    assert wired["order"] == ["s3_delete"]      # only the S3 re-attempt
    assert wired["tombstones"] == [] and wired["events"] == []


def test_redelete_cross_company_tombstone_404(wired):
    # tombstone belongs to another company; non-cross caller must 404 (parity)
    wired["tombstone_row"] = {"s3_key": KF_KEY, "company_id": "c-OTHER"}
    conn = KFConn(photo_rows=[], order=wired["order"])
    res = _call(conn, {"s3_key": KF_KEY})
    assert res["statusCode"] == 404
    assert wired["order"] == []


# --------------------------------------------------------------------------
# S3 failure after commit -- delete is already durable
# --------------------------------------------------------------------------

def test_s3_failure_still_returns_200_after_commit(wired):
    wired["s3"].raise_on_delete = True
    conn = KFConn(photo_rows=[{"id": "p-1", "topic_id": "t-1"}],
                  topic_row={"time_range": "10:15 – 10:17", "category": "safety",
                             "work_class": "work", "site_id": "s-1"},
                  order=wired["order"])
    res = _call(conn, {"s3_key": KF_KEY})
    assert res["statusCode"] == 200
    assert body_of(res)["s3_deleted"] is False
    # commit happened BEFORE the S3 attempt -- tombstone is source of truth
    assert conn.committed is True
    assert wired["order"] == ["tombstone", "event", "delete_row", "commit", "s3_delete"]


# --------------------------------------------------------------------------
# CORS -- the response header now advertises DELETE
# --------------------------------------------------------------------------

def test_cors_headers_include_delete():
    assert "DELETE" in org.ok({})["headers"]["Access-Control-Allow-Methods"]
