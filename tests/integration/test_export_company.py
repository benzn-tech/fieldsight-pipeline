"""export_company against a real Postgres: the selection SQL, the site-move re-ownership and the
tombstone queries only exist here (a fake connection never parses SQL)."""
import io
import json
import uuid
import zipfile
from datetime import datetime, timezone

import pytest

import company_export as ce
from repositories import companies, keyframes, lake_objects, redactions, sites
from tests.unit.export_fakes import FakeS3

pytestmark = pytest.mark.integration

LAKE = "lakebkt"
DAY = "2026-10-01"
NOW = datetime(2026, 10, 12, tzinfo=timezone.utc)
SID1, SID2, SID3 = ("sid" + c * 32 for c in "123")


def bucket_for(kind):
    return LAKE


def put(db, key, company, site=None, kind="transcript", **kw):
    assert lake_objects.upsert(db, key, company_id=company, site_id=site, kind=kind,
                               source="upload", **kw)


def world(db):
    a = companies.create_company(db, f"ExA-{uuid.uuid4().hex[:6]}")
    b = companies.create_company(db, f"ExB-{uuid.uuid4().hex[:6]}")
    moved = sites.create_site(db, b["id"], "Moved Tower")      # starts as B's
    stay_b = sites.create_site(db, b["id"], "B Tower")
    own_a = sites.create_site(db, a["id"], "A Tower")
    return a, b, moved, stay_b, own_a


def seed(db):
    a, b, moved, stay_b, own_a = world(db)
    A, B = str(a["id"]), str(b["id"])
    k = {
        "a_tr": f"transcripts/Ada/{DAY}/a.json",
        "a_site": f"match_requests/{own_a['id']}/{DAY}/r.json",
        "moved1": f"match_requests/{moved['id']}/{DAY}/m1.json",
        "moved2": f"weather/{moved['id']}/{DAY}/w.json",
        "b_tr": f"transcripts/Bob/{DAY}/b.json",
        "b_site": f"match_requests/{stay_b['id']}/{DAY}/r.json",
        "null_unres": f"transcripts/Nobody/{DAY}/n.json",
        "null_split": f"reports/{DAY}/Ada/split.json",
        "gone": f"transcripts/Ada/{DAY}/deleted-row.json",
        "wide": f"reports/{DAY}/summary_report.json",
        "glob": "config/directory.json",
    }
    put(db, k["a_tr"], a["id"])
    put(db, k["a_site"], a["id"], own_a["id"], kind="match")
    put(db, k["moved1"], b["id"], moved["id"], kind="match")       # B's when written ...
    put(db, k["moved2"], b["id"], moved["id"], kind="weather")
    put(db, k["b_tr"], b["id"])
    put(db, k["b_site"], b["id"], stay_b["id"], kind="match")
    assert lake_objects.upsert(db, k["null_unres"], company_id=None, kind="transcript",
                               source="upload", reason="unresolved")
    assert lake_objects.upsert(db, k["null_split"], company_id=None, kind="report",
                               source="upload", reason="split_day")
    put(db, k["gone"], a["id"])
    db.execute("UPDATE lake_objects SET deleted_at=now() WHERE s3_key=%s", (k["gone"],))
    # defensive: even if a row for a lake-wide/global key somehow got a company, never exported
    put(db, k["wide"], a["id"], kind="lake_wide")
    put(db, k["glob"], a["id"], kind="global")
    assert lake_objects.reassign_site(db, moved["id"], a["id"]) == 2   # ... A's after the move
    s3 = FakeS3({(LAKE, key): f"body:{key}".encode() for key in k.values()}, min_part=1)
    return A, B, k, s3


def members(s3, out):
    names = set()
    for p in out["parts"]:
        z = zipfile.ZipFile(io.BytesIO(s3.objects[(LAKE, p)]))
        names |= {i.filename for i in z.infolist()}
    return names


def manifest(s3, out):
    key = f"exports/{out['company_id']}/{out['export_id']}/manifest.json"
    return json.loads(s3.objects[(LAKE, key)])


def export(db, s3, company, **kw):
    kw.setdefault("export_id", str(uuid.uuid4()))
    kw.setdefault("now", NOW)
    return ce.run(db, s3, company, bucket_for=bucket_for, **kw)


def test_export_a_has_exactly_a_live_keys_including_the_moved_site(db):
    A, B, k, s3 = seed(db)
    out = export(db, s3, A)
    want = {k["a_tr"], k["a_site"], k["moved1"], k["moved2"]}
    m = manifest(s3, out)
    assert {o["s3_key"] for o in m["objects"]} == want
    assert members(s3, out) == {f"{o['kind']}/{o['s3_key']}" for o in m["objects"]}
    assert members(s3, out) == {f"transcript/{k['a_tr']}", f"match/{k['a_site']}",
                                f"match/{k['moved1']}", f"weather/{k['moved2']}"}
    # nothing of B, nothing unowned, nothing deleted, nothing lake-wide/global was even read
    for name in ("b_tr", "b_site", "null_unres", "null_split", "gone", "wide", "glob"):
        assert (LAKE, k[name]) not in s3.get_calls, name
    assert m["excluded"].get("not_tenant") == 2


def test_export_b_excludes_the_moved_site_and_never_includes_nulls(db):
    A, B, k, s3 = seed(db)
    out = export(db, s3, B)
    got = {o["s3_key"] for o in manifest(s3, out)["objects"]}
    assert got == {k["b_tr"], k["b_site"]}
    assert members(s3, out) == {f"transcript/{k['b_tr']}", f"match/{k['b_site']}"}


def test_paging_and_parts_and_resume_over_real_rows(db):
    A, B, k, s3 = seed(db)
    t = iter(range(10000))
    first = export(db, s3, A, page_size=1, part_bytes=10, time_budget_s=2, clock=lambda: next(t))
    assert first["done"] is False
    out = export(db, s3, A, export_id=first["export_id"], cursor=first["next_cursor"],
                 page_size=1, part_bytes=10)
    assert out["done"] and len(out["parts"]) >= 2
    keys = [o["s3_key"] for o in manifest(s3, out)["objects"]]
    assert keys == sorted(keys) and len(keys) == len(set(keys)) == 4


def test_user_deleted_content_is_not_exported(db):
    a, b, moved, stay_b, own_a = world(db)
    A = str(a["id"])
    keep = {"tr": f"transcripts/Ada/{DAY}/keep_{SID3}_c0.json",
            "photo2": "users/Ada/pictures/2026-10-05/IMG2.jpg",
            "reverted": f"transcripts/Ada/{DAY}/rev_{SID2}_c0.json"}
    gone = {"tr_sid": f"transcripts/Ada/{DAY}/x_{SID1}_c0.json",
            "audio": f"users/Ada/audio/{DAY}/x_{SID1}.wav",
            "extraction": f"extractions/Ada/{DAY}/{SID1}.json",
            "day_report": f"reports/{DAY}/Ada/daily_report.json",
            "photo1": "users/Ada/pictures/2026-10-05/IMG1.jpg",
            "keyframe": "users/Ada/pictures/2026-10-05/kf-1.jpg"}
    for kk in (*keep.values(), *gone.values()):
        put(db, kk, a["id"])
    # source 1+2: recording tombstone (prefix, sid, day report); source 3: photo; source 4: keyframe
    redactions.create_recording_tombstone(db, a["id"], f"extractions/Ada/{DAY}/{SID1}",
                                          "user delete", None, "worker")
    redactions.create_photo_tombstone(db, a["id"], gone["photo1"], "user delete", None, "worker")
    assert keyframes.add_tombstone(db, gone["keyframe"], a["id"], None, None)
    # a REVERTED tombstone hides nothing
    redactions.create_recording_tombstone(db, a["id"], f"extractions/Ada/{DAY}/{SID2}",
                                          "user delete", None, "worker")
    db.execute("UPDATE redactions SET reverted_at=now() WHERE target_key=%s",
               (f"extractions/Ada/{DAY}/{SID2}",))
    s3 = FakeS3({(LAKE, kk): b"x" for kk in (*keep.values(), *gone.values())}, min_part=1)
    out = export(db, s3, A)
    m = manifest(s3, out)
    assert {o["s3_key"] for o in m["objects"]} == set(keep.values())
    assert {n.split("/", 1)[1] for n in members(s3, out)} == set(keep.values())
    for kk in gone.values():
        assert (LAKE, kk) not in s3.get_calls
    assert {e["s3_key"] for e in m["excluded_tombstoned"]} == set(gone.values())
    assert sum(m["excluded"].values()) == len(gone)


def test_a_missing_object_is_recorded_not_fatal_on_real_rows(db):
    A, B, k, s3 = seed(db)
    del s3.objects[(LAKE, k["a_site"])]
    out = export(db, s3, A)
    st = {o["s3_key"]: o.get("status") for o in manifest(s3, out)["objects"]}
    assert st[k["a_site"]] == "missing" and st[k["a_tr"]] is None


def test_a_deleted_session_withholds_the_days_session_less_keys_but_not_the_live_session(db):
    """R9g: one deleted and one live session on the same folder/day."""
    a = companies.create_company(db, f"ExG-{uuid.uuid4().hex[:6]}")
    live_tr = f"transcripts/Ada/{DAY}/live_{SID3}_c0.json"
    live_ex = f"extractions/Ada/{DAY}/{SID3}.json"
    dead_tr = f"transcripts/Ada/{DAY}/dead_{SID1}_c0.json"
    day_trace = f"traces/{DAY}/Ada/trace.json"
    day_emb = f"embeddings/{DAY}/Ada/e.json"
    other_day_trace = f"traces/2026-10-09/Ada/trace.json"
    other_folder_trace = f"traces/{DAY}/Bob/trace.json"
    allk = [live_tr, live_ex, dead_tr, day_trace, day_emb, other_day_trace, other_folder_trace]
    for kk in allk:
        put(db, kk, a["id"])
    redactions.create_recording_tombstone(db, a["id"], f"extractions/Ada/{DAY}/{SID1}",
                                          "user delete", None, "worker")
    s3 = FakeS3({(LAKE, kk): b"x" for kk in allk}, min_part=1)
    out = export(db, s3, str(a["id"]))
    m = manifest(s3, out)
    assert {o["s3_key"] for o in m["objects"]} == {live_tr, live_ex, other_day_trace,
                                                   other_folder_trace}
    listed = {e["s3_key"]: e["reason"] for e in m["excluded_tombstoned"]}
    assert listed == {dead_tr: "recording_sid", day_trace: "day_level_no_session",
                      day_emb: "day_level_no_session"}
    for kk in (dead_tr, day_trace, day_emb):
        assert (LAKE, kk) not in s3.get_calls


def test_a_missing_lake_objects_table_is_an_error_not_an_empty_export(db):
    s3 = FakeS3({}, min_part=1)
    db.execute("DROP TABLE lake_objects CASCADE")      # rolled back with the test transaction
    with pytest.raises(ce.ExportError, match="lake_objects missing"):
        export(db, s3, str(uuid.uuid4()))
    assert not any(k.endswith("manifest.json") for _, k in s3.objects)
