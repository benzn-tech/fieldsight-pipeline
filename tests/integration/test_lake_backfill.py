"""backfill_lake_objects against a real Postgres and a fake bucket that cannot be mutated.

The FakeS3 raises on put_object, delete_* and copy_*; put_object_tagging is allowed only when a
test opts in. Dry run is the default, a second apply run changes nothing, and a budgeted walk
resumes from its cursor without duplicating work.
"""
import uuid

import pytest

import lambda_org_api as api
from repositories import companies, recordings, sites, users

pytestmark = pytest.mark.integration

BUCKET = "lakebkt"
DAY = "2026-10-01"


class FakeS3:
    def __init__(self, keys=(), allow_tags=False):
        self.keys = sorted(keys)
        self.allow_tags = allow_tags
        self.tags = {}
        self.attempts = []          # every tagging call, even a forbidden one that gets swallowed

    def list_objects_v2(self, Bucket, Prefix="", MaxKeys=1000, ContinuationToken=None,
                        StartAfter=None, **kw):
        keys = [k for k in self.keys if k.startswith(Prefix) and (not StartAfter or k > StartAfter)]
        start = int(ContinuationToken or 0)
        chunk = keys[start:start + MaxKeys]
        more = start + MaxKeys < len(keys)
        out = {"Contents": [{"Key": k, "Size": 10 + len(k)} for k in chunk], "IsTruncated": more}
        if more:
            out["NextContinuationToken"] = str(start + MaxKeys)
        return out

    def put_object_tagging(self, Bucket, Key, Tagging):
        self.attempts.append(Key)
        if not self.allow_tags:
            raise AssertionError("put_object_tagging without tag + apply")
        self.tags[Key] = {t["Key"]: t["Value"] for t in Tagging["TagSet"]}

    def __getattr__(self, name):
        if name.startswith(("put_object", "delete_", "copy_", "upload_")):
            def call(*a, **k):
                raise AssertionError(f"S3 mutation attempted: {name}")
            return call
        raise AttributeError(name)


class Conn:
    """The db fixture rolls back, so commit only records."""

    def __init__(self, c):
        self._c = c
        self.commits = 0

    def __getattr__(self, n):
        return getattr(self._c, n)

    def commit(self):
        self.commits += 1


def _world(db):
    tag = uuid.uuid4().hex[:6]
    a = companies.create_company(db, f"BfA-{tag}")
    b = companies.create_company(db, f"BfB-{tag}")
    sa = sites.create_site(db, a["id"], "Tower A")
    sb = sites.create_site(db, b["id"], "Tower B")
    folder = f"Ada_{tag}"
    u = users.upsert_user(db, f"sub-{tag}", f"{tag}@a.nz", company_id=a["id"],
                          first_name="Ada", last_name="L", global_role="worker")
    users.set_folder_name(db, f"sub-{tag}", folder)
    ada = users.get_by_id(db, u["id"])
    # Ada is home in A but recorded on B's site: that object belongs to B.
    rec_key = f"users/{folder}/video/{DAY}/clip.mp4"
    recordings.insert_pending(db, b["id"], ada["id"], sb["id"], "video", rec_key, uuid.uuid4().hex,
                              "2026-10-01T01:00:00Z")
    keys = {
        "rec": rec_key,
        "tr": f"transcripts/{folder}/{DAY}/x.json",
        "home": f"transcripts/{folder}/2026-10-05/z.json",
        "voice": f"voice/{a['id']}/{sb['id']}/msg.json",
        "orphan": f"transcripts/Nobody_{tag}/{DAY}/y.json",
        "minutes": f"meeting_minutes/{DAY}/Some Title.json",
        "weird": "weird/stuff/z.bin",
        "summary": f"reports/{DAY}/summary_report.json",
        "directory": "config/directory.json",
        "manifest": "lake_manifest/2026/10/01/m.jsonl",
        "marker": "transcripts/",
    }
    return dict(a=a, b=b, sa=sa, sb=sb, ada=ada, folder=folder, keys=keys, tag=tag)


def _row(db, key):
    return db.execute("SELECT company_id, site_id, kind, reason, size_bytes, source "
                      "FROM lake_objects WHERE s3_key=%s", (key,)).fetchone()


def _snapshot(db, keys):
    return db.execute("SELECT s3_key, company_id, site_id, reason, updated_at FROM lake_objects "
                      "WHERE s3_key = ANY(%s) ORDER BY s3_key", (list(keys),)).fetchall()


def _count(db):
    return db.execute("SELECT count(*) FROM lake_objects").fetchone()[0]


def _run(db, s3, **kw):
    kw.setdefault("buckets", [BUCKET])
    return api.backfill_lake_objects(Conn(db), s3, **kw)


def test_resolves_owners_leaves_unresolvable_null_and_reports_them(db):
    w = _world(db)
    k = w["keys"]
    res = _run(db, FakeS3(k.values()), apply=True)

    assert _row(db, k["rec"])[0] == w["b"]["id"]            # recorder home A, site B -> B
    assert _row(db, k["rec"])[1] == w["sb"]["id"]
    assert _row(db, k["voice"])[0] == w["b"]["id"]          # the site company, not the caller
    assert _row(db, k["tr"])[0] == w["b"]["id"]             # the only company that day
    assert _row(db, k["home"])[0] == w["a"]["id"]           # no recordings that day: home
    orphan = _row(db, k["orphan"])
    assert orphan[0] is None and orphan[3] == "unresolved"
    weird = _row(db, k["weird"])
    assert weird[0] is None and weird[2] == "weird" and weird[5] == "backfill"
    assert _row(db, k["rec"])[4] == 10 + len(k["rec"])      # size from the listing
    for skipped in ("minutes", "summary", "directory", "manifest", "marker"):
        assert _row(db, k[skipped]) is None

    assert res["walked"] == 11
    assert res["global_skipped"] == 2 and res["lake_wide"] == 2
    assert res["unknown_shape"] == 1 and res["tenant"] == 5
    assert res["resolved"] == 4 and res["unresolved"] == 2
    assert res["mismatch_voice_company"] == 1
    assert {s["key"]: s["reason"] for s in res["unresolved_sample"]} == {
        k["orphan"]: "unresolved", k["weird"]: "unresolved"}
    assert res["reasons"]["recording"] == 1 and res["reasons"]["unresolved"] == 2
    assert res["rows_changed"] == 6 and res["next_start_after"] is None
    assert res["table_missing"] is False


def test_unresolved_sample_is_capped_at_fifty(db):
    keys = [f"weird/{i:03d}.bin" for i in range(60)]
    res = _run(db, FakeS3(keys), apply=True)
    assert res["unresolved"] == 60 and len(res["unresolved_sample"]) == 50
    assert res["unresolved_sample"][0]["key"] == "weird/000.bin"


def test_dry_run_is_the_default_and_writes_nothing(db):
    w = _world(db)
    before = _count(db)
    conn = Conn(db)
    res = api.backfill_lake_objects(conn, FakeS3(w["keys"].values()), buckets=[BUCKET])
    assert res["dry_run"] is True and res["rows_changed"] == 0
    assert res["resolved"] == 4 and res["unresolved"] == 2     # it still reports
    assert _count(db) == before and conn.commits == 0
    # tag without apply is still a dry run: the FakeS3 raises on tagging
    s3 = FakeS3(w["keys"].values())
    res = api.backfill_lake_objects(conn, s3, buckets=[BUCKET], tag=True)
    assert res["tagged"] == 0 and _count(db) == before and s3.attempts == []


def test_tagging_needs_tag_and_apply_and_tags_only_resolved_objects(db):
    w = _world(db)
    k = w["keys"]
    off = FakeS3(k.values())
    assert _run(db, off, apply=True)["tagged"] == 0          # tag defaults off
    assert off.attempts == []
    s3 = FakeS3(k.values(), allow_tags=True)
    res = _run(db, s3, apply=True, tag=True)
    assert res["tagged"] == 4
    assert s3.tags[k["rec"]] == {"company": str(w["b"]["id"]), "site": str(w["sb"]["id"])}
    assert k["orphan"] not in s3.tags and k["weird"] not in s3.tags


def test_second_apply_run_changes_nothing(db):
    w = _world(db)
    s3 = FakeS3(w["keys"].values())
    first = _run(db, s3, apply=True)
    snap = _snapshot(db, w["keys"].values())
    total = db.execute("SELECT count(*), max(updated_at) FROM lake_objects").fetchone()
    second = _run(db, s3, apply=True)
    assert first["rows_changed"] == 6 and second["rows_changed"] == 0
    assert (second["resolved"], second["unresolved"]) == (first["resolved"], first["unresolved"])
    assert second["reasons"] == first["reasons"]
    assert _snapshot(db, w["keys"].values()) == snap          # including updated_at
    assert db.execute("SELECT count(*), max(updated_at) FROM lake_objects").fetchone() == total


def test_a_budgeted_walk_resumes_without_duplicating_work(db):
    w = _world(db)
    keys = list(w["keys"].values())
    full = _run(db, FakeS3(keys), apply=True, page_size=3)
    snap_full = _snapshot(db, keys)
    db.execute("DELETE FROM lake_objects WHERE s3_key = ANY(%s)", (keys,))

    changed, seen, cursor, runs = 0, 0, None, 0
    while True:
        res = _run(db, FakeS3(keys), apply=True, page_size=3, time_budget_s=0, start_after=cursor)
        runs += 1
        changed += res["rows_changed"]
        seen += res["walked"]
        cursor = res["next_start_after"]
        if cursor is None:
            break
        assert res["walked"] == 3                          # one page per run, then it stops
        assert runs < 20
    assert runs > 1 and seen == len(keys) == full["walked"]
    assert changed == full["rows_changed"]                 # nothing written twice
    assert [r[:4] for r in _snapshot(db, keys)] == [r[:4] for r in snap_full]


def test_prefixes_and_cursor_stay_monotone(db):
    w = _world(db)
    keys = list(w["keys"].values())
    pre = ["transcripts/", "users/"]
    one = _run(db, FakeS3(keys), prefixes=pre, page_size=2)
    assert one["walked"] == 5            # marker, tr, home, orphan under transcripts/ + the clip
    seen, cursor = 0, None
    for _ in range(20):                                    # bounded: a broken cursor must fail
        res = _run(db, FakeS3(keys), prefixes=pre, page_size=2, time_budget_s=0, start_after=cursor)
        seen += res["walked"]
        cursor = res["next_start_after"]
        if cursor is None:
            break
    assert cursor is None
    assert seen == one["walked"]


def test_two_buckets_resume_at_the_next_bucket(db):
    s3 = FakeS3(["weird/a.bin", "weird/b.bin"])
    res = api.backfill_lake_objects(Conn(db), s3, buckets=["b1", "b2"], page_size=10,
                                    time_budget_s=0)
    assert res["walked"] == 2 and res["next_start_bucket"] == "b2"
    assert res["next_start_after"] is None
    res = api.backfill_lake_objects(Conn(db), s3, buckets=["b1", "b2"], page_size=10,
                                    start_bucket="b2")
    assert res["walked"] == 2 and res["next_start_bucket"] is None
    bad = api.backfill_lake_objects(Conn(db), s3, buckets=["b1"], start_bucket="zz")
    assert "error" in bad and bad["walked"] == 0


def test_a_missing_table_is_reported_and_the_walk_stops_at_the_cursor(db):
    w = _world(db)
    db.execute("DROP TABLE lake_objects")
    res = _run(db, FakeS3(w["keys"].values()), apply=True, page_size=3)
    assert res["table_missing"] is True and res["rows_changed"] == 0
    assert res["walked"] < 11                                  # stopped part-way
    assert res["next_start_after"] is not None                 # resume point = the failed page
