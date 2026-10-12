"""drain_lake_manifest against a real Postgres, with manifests written by the REAL lake_put.

A format mismatch between the writer (lake_put) and the reader (the drain) fails here because no
manifest line is hand-written except the deliberately malformed ones.
"""
import io
import json
import uuid

import pytest

import lake_put as lp
import lambda_org_api as api
from repositories import companies, lake_objects, meeting_session, recordings, sites, users

pytestmark = pytest.mark.integration

BUCKET = "lakebkt"
DAY = "2026-10-01"


class FakeS3:
    def __init__(self, page=1000):
        self.objects = {}
        self.page = page
        self.log = []
        self.delete_errors = set()

    def put_object(self, Bucket, Key, Body, **kw):
        self.objects[(Bucket, Key)] = Body if isinstance(Body, bytes) else Body.encode()
        return {"ETag": "x"}

    def list_objects_v2(self, Bucket, Prefix="", MaxKeys=1000, ContinuationToken=None, **kw):
        keys = sorted(k for (b, k) in self.objects if b == Bucket and k.startswith(Prefix))
        start = int(ContinuationToken or 0)
        n = min(MaxKeys, self.page)
        chunk = keys[start:start + n]
        more = start + n < len(keys)
        out = {"Contents": [{"Key": k} for k in chunk], "IsTruncated": more}
        if more:
            out["NextContinuationToken"] = str(start + n)
        return out

    def get_object(self, Bucket, Key):
        return {"Body": io.BytesIO(self.objects[(Bucket, Key)])}

    def delete_objects(self, Bucket, Delete):
        self.log.append("delete")
        errs = []
        for o in Delete["Objects"]:
            if o["Key"] in self.delete_errors:
                errs.append({"Key": o["Key"], "Code": "AccessDenied"})
            else:
                self.objects.pop((Bucket, o["Key"]), None)
        return {"Errors": errs} if errs else {}

    def manifests(self):
        return sorted(k for (_b, k) in self.objects if k.startswith("lake_manifest/"))


class Conn:
    """Delegates to the test's connection but never really commits (the db fixture rolls back),
    and records the commit so its order against the deletes is observable."""

    def __init__(self, c, s3=None):
        self._c, self._s3 = c, s3
        self.commits = 0

    def __getattr__(self, n):
        return getattr(self._c, n)

    def commit(self):
        self.commits += 1
        if self._s3 is not None:
            self._s3.log.append("commit")


def _world(db):
    tag = uuid.uuid4().hex[:6]
    a = companies.create_company(db, f"DrA-{tag}")
    b = companies.create_company(db, f"DrB-{tag}")
    sa = sites.create_site(db, a["id"], "Tower A")
    sb = sites.create_site(db, b["id"], "Tower B")
    folder = f"Ada_{tag}"
    u = users.upsert_user(db, f"sub-{tag}", f"{tag}@a.nz", company_id=a["id"],
                          first_name="Ada", last_name="L", global_role="worker")
    users.set_folder_name(db, f"sub-{tag}", folder)
    ada = users.get_by_id(db, u["id"])
    rec_key = f"users/{folder}/video/{DAY}/clip.mp4"
    recordings.insert_pending(db, a["id"], ada["id"], sa["id"], "video", rec_key, uuid.uuid4().hex,
                              "2026-10-01T01:00:00Z")
    return dict(a=a, b=b, sa=sa, sb=sb, ada=ada, folder=folder, rec_key=rec_key, tag=tag)


def _put(s3, key, hint=None, body=b"{}"):
    lp.lake_put(BUCKET, key, body, owner_hint=hint, client=s3)


def _row(db, key):
    return db.execute("SELECT company_id, site_id, user_id, kind, folder, day::text, reason, "
                      "size_bytes, source FROM lake_objects WHERE s3_key=%s", (key,)).fetchone()


def _count(db):
    return db.execute("SELECT count(*) FROM lake_objects").fetchone()[0]


def _drain(db, s3, **kw):
    conn = Conn(db, s3)
    kw.setdefault("buckets", [BUCKET])
    return api.drain_lake_manifest(conn, s3, **kw), conn


# ---- the headline: a non-VPC writer's manifest line becomes an owned row -----------------------------
def test_a_manifest_line_from_lake_put_is_drained_with_the_resolved_owner(db):
    w = _world(db)
    f = w["folder"]
    s3 = FakeS3()
    t_key = f"transcripts/{f}/{DAY}/x.json"
    _put(s3, t_key, {"folder": f}, body=b'{"a": 1}')
    _put(s3, w["rec_key"], {"folder": f})
    nouser = f"transcripts/Nobody_{w['tag']}/{DAY}/y.json"
    _put(s3, nouser, {"folder": f"Nobody_{w['tag']}", "company_id": str(w["b"]["id"])})
    _put(s3, "weird/stuff/z.bin")
    _put(s3, f"reports/{DAY}/summary_report.json")           # lake-wide: manifested, then skipped
    _put(s3, "config/directory.json")                         # global: never manifested
    assert len(s3.manifests()) == 5
    good_only = s3.manifests()

    res, conn = _drain(db, s3)

    r = _row(db, t_key)
    assert r[0] == w["a"]["id"] and r[1] == w["sa"]["id"] and r[2] == w["ada"]["id"]
    assert (r[3], r[4], r[5], r[7], r[8]) == ("transcript", f, DAY, 8, "manifest")
    assert _row(db, w["rec_key"])[0] == w["a"]["id"]
    nu = _row(db, nouser)
    assert nu[0] is None and nu[6] == "unresolved"            # the company hint did not rescue it
    wd = _row(db, "weird/stuff/z.bin")
    assert wd[0] is None and wd[3] == "weird"
    assert _row(db, f"reports/{DAY}/summary_report.json") is None
    assert res["manifests"] == 5 and res["lines"] == 5 and res["upserted"] == 4
    assert res["skipped"] == 1 and res["unknown_shape"] == 1 and res["unresolved"] == 1
    assert res["deleted_manifests"] == 5 and res["kept"] == 0 and res["table_missing"] is False
    assert s3.manifests() == [] and conn.commits == 1
    assert good_only  # sanity: they existed


def test_second_run_changes_nothing_when_the_same_lines_are_replayed(db):
    w = _world(db)
    s3 = FakeS3()
    key = f"transcripts/{w['folder']}/{DAY}/x.json"
    _put(s3, key, {"folder": w["folder"]})
    saved = dict(s3.objects)
    first, _ = _drain(db, s3)
    n = _count(db)
    assert first["upserted"] == 1 and s3.manifests() == []
    s3.objects.update(saved)                                   # the same manifest, again
    again, _ = _drain(db, s3)
    assert again["upserted"] == 0 and again["deleted_manifests"] == 1 and _count(db) == n
    empty, _ = _drain(db, s3)
    assert empty["manifests"] == 0 and empty["upserted"] == 0


# ---- R5e: what is never deleted -----------------------------------------------------------------------
def test_an_object_with_a_malformed_line_is_kept_and_clean_ones_are_deleted(db):
    w = _world(db)
    s3 = FakeS3()
    good = f"transcripts/{w['folder']}/{DAY}/good.json"
    clean = f"transcripts/{w['folder']}/{DAY}/clean.json"
    _put(s3, clean, {"folder": w["folder"]})
    mixed = ("lake_manifest/2026/10/01/mixed.jsonl")
    line = json.dumps({"v": 1, "bucket": BUCKET, "key": good, "size": 3, "t": "x", "hint": {}})
    s3.put_object(BUCKET, mixed, (line + "\n{not json\n").encode())
    s3.put_object(BUCKET, "lake_manifest/2026/10/01/v2.jsonl",
                  (json.dumps({"v": 2, "key": good}) + "\n").encode())   # unknown version
    res, _ = _drain(db, s3)
    assert res["malformed"] == 2 and res["deleted_manifests"] == 1 and res["kept"] == 2
    assert s3.manifests() == ["lake_manifest/2026/10/01/mixed.jsonl",
                              "lake_manifest/2026/10/01/v2.jsonl"]
    assert _row(db, good) is not None and _row(db, clean) is not None   # good lines still indexed
    again, _ = _drain(db, s3)
    assert again["upserted"] == 0 and again["kept"] == 2 and again["deleted_manifests"] == 0


def test_a_missing_table_deletes_nothing(db):
    w = _world(db)
    s3 = FakeS3()
    _put(s3, f"transcripts/{w['folder']}/{DAY}/x.json", {"folder": w["folder"]})
    _put(s3, f"reports/{DAY}/summary_report.json")             # a skip-only object too
    before = s3.manifests()
    db.execute("DROP TABLE lake_objects")
    res, conn = _drain(db, s3)
    assert res["table_missing"] is True and res["deleted_manifests"] == 0
    assert s3.manifests() == before and "delete" not in s3.log and conn.commits == 0


def test_commit_happens_before_any_delete(db):
    w = _world(db)
    s3 = FakeS3()
    _put(s3, f"transcripts/{w['folder']}/{DAY}/a.json", {"folder": w["folder"]})
    _put(s3, f"transcripts/{w['folder']}/{DAY}/b.json", {"folder": w["folder"]})
    _drain(db, s3)
    assert s3.log[0] == "commit" and "delete" in s3.log and s3.log.count("commit") == 1


def test_a_failed_upsert_keeps_only_its_own_object(db, monkeypatch):
    w = _world(db)
    s3 = FakeS3()
    ok = f"transcripts/{w['folder']}/{DAY}/ok.json"
    bad = f"transcripts/{w['folder']}/{DAY}/bad.json"
    _put(s3, ok, {"folder": w["folder"]})
    _put(s3, bad, {"folder": w["folder"]})
    real = lake_objects.upsert_many

    def flaky(conn, rows, **kw):
        if any(r["key"] == bad for r in rows):
            raise RuntimeError("boom")
        return real(conn, rows, **kw)
    monkeypatch.setattr(lake_objects, "upsert_many", flaky)
    res, _ = _drain(db, s3)
    assert res["deleted_manifests"] == 1 and res["kept"] == 1
    assert _row(db, ok) is not None and _row(db, bad) is None
    kept = s3.manifests()
    assert len(kept) == 1 and bad in s3.objects[(BUCKET, kept[0])].decode()


def test_a_delete_that_errors_is_counted_as_kept(db):
    w = _world(db)
    s3 = FakeS3()
    _put(s3, f"transcripts/{w['folder']}/{DAY}/a.json", {"folder": w["folder"]})
    s3.delete_errors = set(s3.manifests())
    res, _ = _drain(db, s3)
    assert res["deleted_manifests"] == 0 and res["kept"] == 1 and len(s3.manifests()) == 1


def test_an_unreadable_manifest_object_is_kept(db):
    w = _world(db)
    s3 = FakeS3()
    _put(s3, f"transcripts/{w['folder']}/{DAY}/a.json", {"folder": w["folder"]})
    s3.put_object(BUCKET, "lake_manifest/2026/10/01/binary.jsonl", b"\xff\xfe\x00")
    res, _ = _drain(db, s3)
    assert res["malformed"] == 1 and res["deleted_manifests"] == 1
    assert s3.manifests() == ["lake_manifest/2026/10/01/binary.jsonl"]


# ---- dry run, paging, buckets -----------------------------------------------------------------------------
def test_dry_run_writes_and_deletes_nothing_but_counts(db):
    w = _world(db)
    s3 = FakeS3()
    _put(s3, f"transcripts/{w['folder']}/{DAY}/a.json", {"folder": w["folder"]})
    _put(s3, "weird/x")
    n = _count(db)
    res, conn = _drain(db, s3, dry_run=True)
    assert res["dry_run"] is True and res["upserted"] == 2 and res["unknown_shape"] == 1
    assert res["deleted_manifests"] == 0 and len(s3.manifests()) == 2
    assert _count(db) == n and conn.commits == 0 and "delete" not in s3.log


def test_max_objects_bounds_the_run_and_pagination_reaches_every_page(db):
    w = _world(db)
    s3 = FakeS3(page=2)
    for i in range(5):
        _put(s3, f"transcripts/{w['folder']}/{DAY}/n{i}.json", {"folder": w["folder"]})
    first, _ = _drain(db, s3, max_objects=3)
    assert first["manifests"] == 3 and first["deleted_manifests"] == 3 and len(s3.manifests()) == 2
    rest, _ = _drain(db, s3, max_objects=500)
    assert rest["manifests"] == 2 and s3.manifests() == []
    assert db.execute("SELECT count(*) FROM lake_objects WHERE folder=%s",
                      (w["folder"],)).fetchone()[0] == 5


def test_default_buckets_are_the_distinct_configured_ones(db, monkeypatch):
    w = _world(db)
    s3 = FakeS3()
    other = "otherbkt"
    lp.lake_put(other, f"transcripts/{w['folder']}/{DAY}/a.json", b"{}",
                owner_hint={"folder": w["folder"]}, client=s3)
    _put(s3, f"transcripts/{w['folder']}/{DAY}/b.json", {"folder": w["folder"]})
    monkeypatch.setattr(api, "S3_BUCKET", BUCKET)
    monkeypatch.setattr(api, "LAKE_BUCKET", other)
    res = api.drain_lake_manifest(Conn(db, s3), s3)
    assert res["manifests"] == 2 and res["deleted_manifests"] == 2
    monkeypatch.setattr(api, "LAKE_BUCKET", BUCKET)            # same physical bucket: listed once
    _put(s3, f"transcripts/{w['folder']}/{DAY}/c.json", {"folder": w["folder"]})
    assert api.drain_lake_manifest(Conn(db, s3), s3)["manifests"] == 1


# ---- the resolver rules, end to end through the drain ----------------------------------------------------
def test_c2_split_day_documents_are_stored_with_a_null_company_and_the_reason(db):
    w = _world(db)
    f = w["folder"]
    recordings.insert_pending(db, w["b"]["id"], w["ada"]["id"], w["sb"]["id"], "video",
                              f"users/{f}/video/{DAY}/two.mp4", uuid.uuid4().hex,
                              "2026-10-01T02:00:00Z")
    s3 = FakeS3()
    day_doc = f"reports/{DAY}/{f}/daily_report.json"
    _put(s3, day_doc, {"folder": f, "company_id": str(w["a"]["id"])})
    _put(s3, w["rec_key"], {"folder": f})
    res, _ = _drain(db, s3)
    r = _row(db, day_doc)
    assert r[0] is None and r[6] == "split_day" and r[2] == w["ada"]["id"]
    assert res["split_day"] == 1 and res["unresolved"] == 0
    assert _row(db, w["rec_key"])[0] == w["a"]["id"]           # the recording keeps its own site


def test_a_session_keyed_object_resolves_through_meeting_session(db):
    w = _world(db)
    sid = uuid.uuid4().hex
    meeting_session.ensure_open(db, sid, w["a"]["id"], w["ada"]["id"], w["sb"]["id"], "audio",
                                "2026-10-01T01:00:00Z")
    s3 = FakeS3()
    key = f"session_finalize_results/{sid}.json"
    _put(s3, key, {"company_id": str(w["a"]["id"])})            # a wrong hint
    _drain(db, s3)
    assert _row(db, key)[0] == w["b"]["id"] and _row(db, key)[1] == w["sb"]["id"]


def test_a_site_report_with_an_ambiguous_slug_takes_the_company_hint_only_without_a_folder(db):
    w = _world(db)
    slug = f"dup-{w['tag']}"
    sites.set_slug(db, w["sa"]["id"], slug)
    sites.set_slug(db, w["sb"]["id"], slug)
    s3 = FakeS3()
    key = f"reports/{DAY}/sites/{slug}/site_report.json"
    _put(s3, key, {"company_id": str(w["a"]["id"])})
    _drain(db, s3)
    r = _row(db, key)
    assert r[0] == w["a"]["id"] and r[1] == w["sa"]["id"]   # F2: the candidate site too
    # and a stale hint never overrides the DB: a unique slug is the site's company
    sites.archive_site(db, w["sb"]["id"], w["b"]["id"])
    s3b = FakeS3()
    key2 = f"reports/2026-10-09/sites/{slug}/site_report.json"   # a fresh row, not the first one
    _put(s3b, key2, {"company_id": str(w["b"]["id"])})
    _drain(db, s3b)
    assert _row(db, key2)[0] == w["a"]["id"]


def test_voiceprint_company_in_the_key_is_stored(db):
    w = _world(db)
    s3 = FakeS3()
    key = f"voiceprint_requests/{w['b']['id']}/base1/req.json"
    _put(s3, key)
    _drain(db, s3)
    assert _row(db, key)[0] == w["b"]["id"]


def test_f3_the_newest_line_of_a_key_supplies_the_whole_hint(db):
    """Two lines for one key: the hint is the newest line's, not a key-by-key merge, whatever
    order the objects are listed in."""
    w = _world(db)
    s3 = FakeS3()
    key = f"reports/{DAY}/sites/nosuch-{w['tag']}/site_report.json"

    def line(t, hint, size):
        return (json.dumps({"v": 1, "bucket": BUCKET, "key": key, "size": size, "t": t,
                            "hint": hint}) + "\n").encode()
    # listing order puts the NEWER object first, the older one second
    s3.put_object(BUCKET, "lake_manifest/2026/10/01/a.jsonl",
                  line("2026-10-01T10:00:00+00:00", {"company_id": str(w["b"]["id"])}, 20))
    s3.put_object(BUCKET, "lake_manifest/2026/10/01/b.jsonl",
                  line("2026-10-01T09:00:00+00:00", {"company_id": str(w["a"]["id"])}, 10))
    _drain(db, s3)
    r = _row(db, key)
    assert r[0] == w["b"]["id"] and r[7] == 20
    # a newer line with an EMPTY hint does not inherit the older line's company
    key2 = key + "2"
    s3.put_object(BUCKET, "lake_manifest/2026/10/01/c.jsonl",
                  line("2026-10-01T09:00:00+00:00", {"company_id": str(w["a"]["id"])}, 1)
                  .replace(key.encode(), key2.encode()))
    s3.put_object(BUCKET, "lake_manifest/2026/10/01/d.jsonl",
                  line("2026-10-01T10:00:00+00:00", {}, 2).replace(key.encode(), key2.encode()))
    _drain(db, s3)
    assert _row(db, key2)[0] is None


def test_a_drained_line_never_replaces_a_stored_different_company(db):
    w = _world(db)
    s3 = FakeS3()
    key = f"transcripts/{w['folder']}/{DAY}/x.json"           # resolver: A (home, no recordings...)
    db.execute("INSERT INTO lake_objects (s3_key, company_id, kind, source) VALUES (%s,%s,'transcript','upload')",
               (key, w["b"]["id"]))
    _put(s3, key, {"folder": w["folder"]})
    res, _ = _drain(db, s3)
    assert _row(db, key)[0] == w["b"]["id"]
    assert res["conflict"] == 1 and res["conflict_sample"][0]["key"] == key
    assert res["conflict_sample"][0]["existing"] == str(w["b"]["id"])
