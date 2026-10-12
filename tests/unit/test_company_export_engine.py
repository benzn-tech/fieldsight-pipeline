"""company_export.run against canned owner pages and a FakeS3 with real multipart semantics."""
import io
import json
import uuid
import zipfile
from datetime import datetime, timezone

import pytest
from botocore.exceptions import ClientError

import company_export as ce
from tests.unit.export_fakes import FakeS3

CO = str(uuid.uuid4())
OTHER = str(uuid.uuid4())
EXPORT = str(uuid.uuid4())
LAKE = "lake"
NOW = datetime(2026, 10, 12, 1, 2, 3, tzinfo=timezone.utc)
PREFIX = f"exports/{CO}/{EXPORT}/"


class NoTombs:
    def excluded(self, conn, keys):
        return {}


class SomeTombs:
    def __init__(self, mapping):
        self.mapping = mapping

    def excluded(self, conn, keys):
        return {k: v for k, v in self.mapping.items() if k in keys}


def bucket_for(kind):
    return LAKE


def row(key, company=CO, kind="transcript", size=None, site=None):
    return {"s3_key": key, "company_id": company, "kind": kind, "size_bytes": size, "site_id": site}


def key(i):
    return f"transcripts/Ada/2026-10-01/t{i:03d}.json"


def setup(monkeypatch, rows, payload=None, filter_company=True):
    """Canned owners_for_company honouring after_key/limit; objects pre-loaded in a FakeS3."""
    rows = sorted(rows, key=lambda r: r["s3_key"])

    def owners(conn, company_id, *, after_key=None, limit=1000):
        out = [r for r in rows if (not filter_company or r["company_id"] == company_id) and
               (after_key is None or r["s3_key"] > after_key)]
        return out[:limit]
    monkeypatch.setattr(ce.lake_objects, "owners_for_company", owners)
    s3 = FakeS3(min_part=1)
    for r in rows:
        data = (payload or {}).get(r["s3_key"], (r["s3_key"] * 40).encode())
        s3.objects[(LAKE, r["s3_key"])] = data
    return s3


@pytest.fixture(autouse=True)
def _table_present(monkeypatch):
    monkeypatch.setattr(ce, "_table_exists", lambda conn: True)


def run(s3, **kw):
    kw.setdefault("bucket_for", bucket_for)
    kw.setdefault("export_id", EXPORT)
    kw.setdefault("now", NOW)
    kw.setdefault("tombstones", NoTombs())
    kw.setdefault("sink_part_bytes", 4096)
    return ce.run(object(), s3, CO, **kw)


def zip_members(s3, part_key):
    z = zipfile.ZipFile(io.BytesIO(s3.objects[(LAKE, part_key)]))
    return {i.filename: z.read(i) for i in z.infolist()}


def manifest(s3):
    return json.loads(s3.objects[(LAKE, PREFIX + "manifest.json")])


def test_two_parts_when_part_bytes_is_small(monkeypatch):
    s3 = setup(monkeypatch, [row(key(i)) for i in range(6)])
    out = run(s3, part_bytes=2000)          # each object is ~1.1 KB
    assert out["done"] and len(out["parts"]) >= 2
    assert out["parts"] == [PREFIX + f"part-{n:04d}.zip" for n in range(1, len(out["parts"]) + 1)]
    m = manifest(s3)
    assert [p["key"] for p in m["parts"]] == out["parts"]
    assert sum(p["objects"] for p in m["parts"]) == 6 == len(m["objects"])


def test_sink_never_buffers_more_than_one_s3_part(monkeypatch):
    peaks = []
    real = ce._MultipartSink

    class Spy(real):
        def __init__(self, *a, **k):
            super().__init__(*a, **k)
            peaks.append(self)
    monkeypatch.setattr(ce, "_MultipartSink", Spy)
    big = {key(i): bytes(range(256)) * 400 for i in range(5)}      # 100 KB each
    s3 = setup(monkeypatch, [row(key(i)) for i in range(5)], big)
    s3.min_part = 4096
    run(s3, part_bytes=10**9, chunk_size=64 * 1024)
    assert peaks and all(s.peak <= 4096 for s in peaks)             # never above one S3 part
    assert s3.max_part_len <= 4096 and len(s3.part_lens) > 100      # streamed, many parts
    assert not s3.uploads                                           # nothing left open


def test_missing_source_is_recorded_and_export_continues(monkeypatch):
    s3 = setup(monkeypatch, [row(key(i)) for i in range(3)])
    del s3.objects[(LAKE, key(1))]
    out = run(s3)
    assert out["done"]
    m = manifest(s3)
    st = {o["s3_key"]: o.get("status") for o in m["objects"]}
    assert st == {key(0): None, key(1): "missing", key(2): None}
    assert [o["part"] for o in m["objects"] if o.get("status") == "missing"] == [None]
    assert sorted(zip_members(s3, out["parts"][0])) == [f"transcript/{key(0)}", f"transcript/{key(2)}"]


def test_a_transient_read_error_aborts_instead_of_becoming_a_gap(monkeypatch):
    s3 = setup(monkeypatch, [row(key(i)) for i in range(3)])
    s3.fail_get[key(1)] = ClientError({"Error": {"Code": "SlowDown", "Message": "x"}}, "GetObject")
    with pytest.raises(ClientError):
        run(s3)
    assert not s3.uploads and s3.aborted           # the open multipart was aborted
    assert (LAKE, PREFIX + "manifest.json") not in s3.objects


def test_a_multipart_error_aborts_the_upload(monkeypatch):
    big = {key(i): b"z" * 20000 for i in range(3)}
    s3 = setup(monkeypatch, [row(key(i)) for i in range(3)], big)
    s3.fail_upload_part = (2, RuntimeError("boom"))
    with pytest.raises(RuntimeError):
        run(s3)
    assert s3.uploads == {} and len(s3.aborted) == 1 and s3.completed == []
    assert (LAKE, PREFIX + "manifest.json") not in s3.objects


def test_time_budget_returns_a_cursor_and_resume_has_no_duplicates(monkeypatch):
    s3 = setup(monkeypatch, [row(key(i)) for i in range(7)])
    t = iter(range(0, 1000))
    out1 = run(s3, time_budget_s=3, clock=lambda: next(t))
    assert out1["done"] is False and out1["next_cursor"] and 0 < out1["objects"] < 7
    seen1 = [o["s3_key"] for o in json.loads(s3.objects[(LAKE, PREFIX + "state.json")])["entries"]]
    out2 = run(s3, cursor=out1["next_cursor"], time_budget_s=10 ** 6)
    assert out2["done"] and out2["next_cursor"] is None
    m = manifest(s3)
    keys = [o["s3_key"] for o in m["objects"]]
    assert keys == [key(i) for i in range(7)] and seen1 == keys[:len(seen1)]     # no dup, no gap
    members = [n for p in out2["parts"] for n in zip_members(s3, p)]
    assert sorted(members) == sorted(f"transcript/{k}" for k in keys)
    assert out2["parts"][:len(out1["parts"])] == out1["parts"]


def test_resume_uses_the_saved_state_not_a_stale_cursor(monkeypatch):
    s3 = setup(monkeypatch, [row(key(i)) for i in range(5)])
    t = iter(range(0, 1000))
    out1 = run(s3, time_budget_s=2, clock=lambda: next(t))
    out2 = run(s3, cursor=key(0), time_budget_s=10 ** 6)       # stale cursor
    assert out2["done"]
    assert [o["s3_key"] for o in manifest(s3)["objects"]] == [key(i) for i in range(5)]
    assert out1["next_cursor"] != key(0)


def test_cursor_without_state_is_refused_and_a_done_export_is_not_rerun(monkeypatch):
    s3 = setup(monkeypatch, [row(key(0))])
    with pytest.raises(ValueError):
        run(s3, cursor=key(0))
    run(s3)
    writes = len(s3.written)
    again = run(s3)
    assert again["done"] and len(s3.written) == writes


def test_zip_is_readable_and_byte_identical(monkeypatch):
    payload = {key(i): bytes([i]) * (1500 + i * 700) + bytes(range(256)) for i in range(5)}
    s3 = setup(monkeypatch, [row(key(i), kind="transcript") for i in range(5)], payload)
    out = run(s3, part_bytes=4000)
    got = {}
    for p in out["parts"]:
        z = zipfile.ZipFile(io.BytesIO(s3.objects[(LAKE, p)]))
        assert z.testzip() is None
        assert all(i.compress_type == zipfile.ZIP_STORED for i in z.infolist())
        got.update(zip_members(s3, p))
    assert got == {f"transcript/{k}": v for k, v in payload.items()}
    assert [o["size"] for o in manifest(s3)["objects"]] == [len(payload[key(i)]) for i in range(5)]


def test_only_this_company_and_never_lake_wide_or_global(monkeypatch):
    rows = [row(key(0)), row(key(1), company=OTHER),
            row("reports/2026-10-01/summary_report.json", kind="lake_wide"),
            row("config/directory.json", kind="global"),
            row("redactions/Ada/2026-10-01/deleted_sessions.json", kind="redaction"),
            row("transcripts/Ada/2026-10-01/own2.json")]
    # a buggy repository that hands back another company's row must still not leak
    s3 = setup(monkeypatch, rows, filter_company=False)
    out = run(s3)
    m = manifest(s3)
    assert [o["s3_key"] for o in m["objects"]] == ["transcripts/Ada/2026-10-01/own2.json", key(0)]
    assert m["excluded"] == {"foreign_company": 1, "not_tenant": 2, "internal": 1}
    assert f"transcript/{key(1)}" not in {n for p in out["parts"] for n in zip_members(s3, p)}
    assert (LAKE, key(1)) not in s3.get_calls                    # never even read


def test_tombstoned_keys_are_not_read_or_exported_or_listed(monkeypatch):
    s3 = setup(monkeypatch, [row(key(i)) for i in range(3)])
    run(s3, tombstones=SomeTombs({key(1): "recording_sid"}))
    m = manifest(s3)
    assert [o["s3_key"] for o in m["objects"]] == [key(0), key(2)]
    assert m["excluded"] == {"recording_sid": 1}
    assert m["excluded_tombstoned"] == [{"s3_key": key(1), "reason": "recording_sid"}]
    assert (LAKE, key(1)) not in s3.get_calls


def test_writes_stay_under_the_export_prefix_and_nothing_is_deleted(monkeypatch):
    s3 = setup(monkeypatch, [row(key(i)) for i in range(4)])
    run(s3, part_bytes=1500)            # FakeS3 raises on delete_/copy_/list_
    assert s3.written and all(k.startswith(PREFIX) for _, k in s3.written)
    assert (LAKE, PREFIX + "manifest.json") in s3.written


def test_unsafe_member_names_are_recorded_not_written(monkeypatch):
    s3 = setup(monkeypatch, [row("transcripts/../../x/2026-10-01/a.json"), row(key(0))])
    run(s3)
    m = manifest(s3)
    assert sorted(o.get("status") or "ok" for o in m["objects"]) == ["ok", "unsafe_name"]


def test_an_empty_company_exports_a_manifest_and_no_parts(monkeypatch):
    s3 = setup(monkeypatch, [row(key(0), company=OTHER)])
    out = run(s3)
    assert out["done"] and out["parts"] == [] and manifest(s3)["objects"] == []


def test_export_id_must_be_a_uuid(monkeypatch):
    s3 = setup(monkeypatch, [row(key(0))])
    with pytest.raises(ValueError):
        run(s3, export_id="../other/x")
    with pytest.raises(ValueError):
        ce.run(object(), s3, "not-a-uuid", bucket_for=bucket_for, tombstones=NoTombs())


def test_a_fresh_export_gets_a_uuid(monkeypatch):
    s3 = setup(monkeypatch, [row(key(0))])
    out = run(s3, export_id=None)
    assert str(uuid.UUID(out["export_id"])) == out["export_id"]


# --- Tombstones: the pure matching rules -------------------------------------------------
SID = "sid" + "ab" * 16


def test_tombstones_match_sid_prefix_legacy_base_and_day_report():
    t = ce.Tombstones([f"extractions/Ada/2026-10-01/{SID}",
                       "extractions/Bob/2026-10-02/dev9_2026-10-02_10-00-00"])
    r = t._key_reason
    assert r(f"extractions/Ada/2026-10-01/{SID}.json")
    assert r(f"transcripts/Ada/2026-10-01/2026-10-01_10-00-00_{SID}_c0000.json") == "recording_sid"
    assert r(f"users/Other/video/2026-10-05/x_{SID}.mp4") == "recording_sid"     # sid is global
    assert r("transcripts/Bob/2026-10-02/dev9_2026-10-02_10-00-00.json") == "recording_base"
    assert r("reports/2026-10-01/Ada/daily_report.json") == "day_report"
    assert r("reports/2026-10-03/Ada/daily_report.json") is None                 # other day
    assert r("transcripts/Ada/2026-10-01/other-session.json") is None            # kept session
    assert r("extraction_requests/dev9_2026-10-02_10-00-00.json") == "recording_base"
    assert r("transcripts/Carol/2026-10-01/x.json") is None
    # R9g: a tombstoned folder/day withholds every session-less key of it, in any family
    assert r("traces/2026-10-01/Ada/trace.json") == "day_level_no_session"
    assert r("embeddings/2026-10-01/Ada/e.json") == "day_level_no_session"
    assert r("audio_segments/Ada/2026-10-01/batch_map.json") == "day_level_no_session"
    assert r("traces/2026-10-04/Ada/trace.json") is None                         # other day
    assert r("traces/2026-10-01/Carol/trace.json") is None                       # other folder


def test_tombstone_db_sources_are_photo_and_keyframe(monkeypatch):
    monkeypatch.setattr(ce.redactions, "deleted_photo_keys", lambda conn, keys=None, company_id=None:
                        {k for k in keys if k.endswith("IMG1.jpg")})
    monkeypatch.setattr(ce.keyframes, "tombstoned_subset", lambda conn, keys:
                        {k for k in keys if "kf" in k})
    t = ce.Tombstones([])
    ks = ["users/A/pictures/2026-10-01/IMG1.jpg", "users/A/pictures/2026-10-01/IMG2.jpg",
          "keyframes/A/kf1.jpg"]
    assert t.excluded(None, ks) == {ks[0]: "photo_tombstone", ks[2]: "keyframe_tombstone"}


# --- fix round 2 ---------------------------------------------------------------------------
HEX = "ab" * 16
TOMB = f"extractions/Ada/2026-10-01/sid{HEX}"


@pytest.mark.parametrize("key", [
    f"session_reports/Ada/2026-10-01/{HEX}/r.json",                 # session_report(s)
    f"session_report_results/Ada/2026-10-01/{HEX}/r.json",          # ... results
    f"session_report_requests/Ada/2026-10-01/{HEX}/r.json",         # ... requests
    f"session_rolling/Ada/2026-10-01/{HEX}/roll.json",
    f"session_brief/Ada/2026-10-01/sid{HEX}/brief.json",
    f"session_finalize_requests/{HEX}-updated.json",
    f"session_finalize_requests/{HEX}-notes.json",
    f"session_finalize_results/sid{HEX}.json",
    f"somethingnew/Ada/2026-10-01/{HEX}/x.json",                    # sid only as a directory
    f"transcripts/Ada/2026-10-01/UPPER_{HEX.upper()}.json",         # case differs
])
def test_every_spelling_of_a_tombstoned_session_is_withheld(key):
    t = ce.Tombstones([TOMB])
    assert t._key_reason(key) == "recording_sid", key
    other = key.replace(HEX, "cd" * 16).replace(HEX.upper(), ("cd" * 16).upper())
    assert t._key_reason(other) in (None, "day_level_no_session")   # another session: not a sid hit


def test_tombstone_stored_bare_hex_and_uppercase_normalise():
    t = ce.Tombstones([f"extractions/Ada/2026-10-01/{HEX.upper()}"])
    assert t._key_reason(f"session_rolling/Ada/2026-10-01/{HEX}/x.json") == "recording_sid"


def test_legacy_base_matches_across_days_and_folder_case():
    base = "20261002_083000"
    t = ce.Tombstones([f"extractions/Ada/2026-10-02/{base}"])
    assert t._key_reason(f"transcripts/Ada/2026-10-03/{base}_c0.json") == "recording_base"   # next day
    assert t._key_reason(f"transcripts/ada/2026-10-02/{base}_c0.json") == "recording_base"   # case
    assert t._key_reason(f"users/ADA/audio/2026-10-04/{base}.wav") == "recording_base"
    assert t._key_reason("transcripts/Ada/2026-10-03/20261003_090000_c0.json") is None
    assert t._key_reason(f"transcripts/Bob/2026-10-03/{base}_c0.json") is None            # other folder


@pytest.mark.parametrize("name", ["transcript/a\\..\\x.json", "transcript/a//b.json",
                                  "transcript/a/b\x01.json", "transcript/../x", "/abs"])
def test_unsafe_member_names(name):
    assert ce._unsafe_name(name)
    assert not ce._unsafe_name("transcript/transcripts/Ada/2026-10-01/a.json")


def test_budget_guard_uses_row_size_and_resumes(monkeypatch):
    big = 100 * 1024 * 1024
    rows = [row(key(0), size=10), row(key(1), size=big), row(key(2), size=10)]
    s3 = setup(monkeypatch, rows)
    out1 = run(s3, time_budget_s=19, clock=lambda: 0)     # 100 MiB at 5 MiB/s = 20 s > 19 s left
    assert out1["done"] is False and out1["next_cursor"] == key(0)
    assert (LAKE, key(1)) not in s3.get_calls and not s3.uploads
    out2 = run(s3, cursor=out1["next_cursor"], time_budget_s=10 ** 6, clock=lambda: 0)
    assert out2["done"]
    assert [o["s3_key"] for o in manifest(s3)["objects"]] == [key(0), key(1), key(2)]


def test_budget_guard_always_makes_progress(monkeypatch):
    s3 = setup(monkeypatch, [row(key(0), size=10 ** 12)])
    out = run(s3, time_budget_s=1, clock=lambda: 0)
    assert out["done"] and out["objects"] == 1


def test_tombstones_are_reloaded_at_each_part(monkeypatch):
    s3 = setup(monkeypatch, [row(key(i)) for i in range(4)])
    loads = []

    class T:
        def __init__(self, dead):
            self.dead = dead

        def excluded(self, conn, keys):
            return {k: "recording_sid" for k in keys if k in self.dead}

    def load(conn):
        loads.append(1)
        return T({key(2)} if len(loads) > 1 else set())
    monkeypatch.setattr(ce.Tombstones, "load", staticmethod(load))
    out = ce.run(object(), s3, CO, bucket_for=bucket_for, export_id=EXPORT, now=NOW,
                 part_bytes=1, sink_part_bytes=4096)         # one object per part
    keys = [o["s3_key"] for o in manifest(s3)["objects"]]
    assert out["done"] and len(loads) >= 3
    assert key(2) not in keys and key(3) in keys and key(0) in keys


def test_lost_state_race_stops_without_writing(monkeypatch):
    s3 = setup(monkeypatch, [row(key(i)) for i in range(3)])
    t = iter(range(0, 1000))
    run(s3, time_budget_s=2, clock=lambda: next(t))          # state.json now exists
    state_key = (LAKE, PREFIX + "state.json")
    before = s3.objects[state_key]
    s3.race_on_put = lambda b, k: s3._stamp(b, k) if k.endswith("state.json") else None
    out = run(s3, time_budget_s=10 ** 6)
    assert out.get("lost_race") is True and out["done"] is False and out["next_cursor"] is None
    assert s3.objects[state_key] == before                   # the loser wrote nothing
    assert not s3.uploads


def test_first_state_write_is_if_none_match(monkeypatch):
    s3 = setup(monkeypatch, [row(key(0))])
    seen = []
    orig = s3.put_object
    s3.put_object = lambda **kw: (seen.append(dict(kw)), orig(**kw))[1]
    run(s3)
    st = [kw for kw in seen if kw["Key"].endswith("state.json")]
    assert st and st[0].get("IfNoneMatch") == "*"


def test_missing_table_is_an_error_not_an_empty_done_export(monkeypatch):
    s3 = setup(monkeypatch, [])
    monkeypatch.setattr(ce, "_table_exists", lambda conn: False)
    with pytest.raises(ce.ExportError, match="lake_objects missing"):
        run(s3)
    assert (LAKE, PREFIX + "manifest.json") not in s3.objects
