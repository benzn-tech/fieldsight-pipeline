"""Selection logic of the 2026-10-27 owner-run lake operations (no AWS, no database).

scripts/prod_lake_reassign_folder.py (A) and scripts/prod_lake_delete_folders.py (B). What is
proven here is WHICH rows/keys they pick; the SQL itself runs only against the real database.
"""
import re

import pytest

import scripts.lake_ops_common as common
import scripts.prod_lake_delete_folders as dl
import scripts.prod_lake_reassign_folder as ra

FOLDERS = ["MPI1", "Benl1", "Ben_UCPK_"]

IN_SCOPE = [
    "users/MPI1/video/2026-05-19/mpi1_2026-05-19_10-56-09.mp4",
    "audio_segments/MPI1/2026-05-19/x_off0.0_to1.0_srcmp4.wav",
    "reports/2026-04-24/Benl1/daily_report.json",
    "embeddings/2026-07-17/Ben_UCPK_/vectors.json",     # folder mid-path
    "users/Ben_UCPK_/audio/2026-07-17/ben_ucpk_2026-07-17_20-19-53.wav",
    "MPI1/at-the-root.txt",
]
OUT_OF_SCOPE = [
    "users/Ben_UCPK/audio/2026-07-17/ben_ucpk_2026-07-17_20-19-53.wav",  # the real account
    "users/Ben_UCPK2/video/2026-08-09/x.mp4",
    "users/MPI2/video/2026-05-19/x.mp4",
    "users/Jarley_Trainor/audio/2026-07-15/Benl1_2026-07-15_09-18-10.wav",  # name only
    "session_rolling/ben_ucpk_2026-07-30_23-01-42/x.json",                 # lowercase
    "users/mpi1/video/x.mp4",                                              # case differs
    "users/Ben_UCPK_x/a.wav",
    "reports/2026-07-17/xBenl1/daily_report.json",
    "users/Ben_Lin/MPI1",                                                  # last segment = file
]


@pytest.mark.parametrize("key", IN_SCOPE)
def test_folder_segment_is_in_scope(key):
    assert common.key_in_scope(key, FOLDERS)
    assert re.search(common.folder_segment_regex(FOLDERS), key)


@pytest.mark.parametrize("key", OUT_OF_SCOPE)
def test_not_a_folder_segment_is_out_of_scope(key):
    assert not common.key_in_scope(key, FOLDERS)
    assert not re.search(common.folder_segment_regex(FOLDERS), key)


def test_name_only_matches_are_reported_not_selected():
    keys = [(k, 1, "e") for k in IN_SCOPE + OUT_OF_SCOPE]
    inside, excluded = dl.classify_s3(keys, FOLDERS)
    assert sorted(o["key"] for o in inside) == sorted(IN_SCOPE)
    assert "users/Jarley_Trainor/audio/2026-07-15/Benl1_2026-07-15_09-18-10.wav" in excluded
    assert "users/Ben_UCPK/audio/2026-07-17/ben_ucpk_2026-07-17_20-19-53.wav" not in excluded
    assert {o["prefix"] for o in inside} >= {"users/", "embeddings/", "reports/"}


def test_regex_escapes_and_validation():
    assert common.folder_segment_regex(["a.b"]) == r"(^|/)(a\.b)/"
    assert not re.search(common.folder_segment_regex(["a.b"]), "users/axb/k")
    for bad in ["", "a/b", "a b", "Ben_UCPK ", "%", "a*"]:
        with pytest.raises(ValueError):
            common.validate_folders([bad])
    assert common.validate_folders(["MPI1", "MPI1", "Benl1"]) == ["MPI1", "Benl1"]


# ---- (A) reassignment ---------------------------------------------------------------------

FP, SB = "fp-company", "southbase-company"
TEST2, ADMIN = "user-test2", "user-admin"


def _row(key, company, user, reason=None, deleted=None, size=10, day="2026-09-10"):
    return {"s3_key": key, "company_id": company, "user_id": user, "reason": reason,
            "deleted_at": deleted, "size_bytes": size, "day": day, "updated_at": "t0"}


def test_reassign_plan_keeps_southbase_rows_and_claims_only_unresolved():
    rows = [_row("a", FP, ADMIN), _row("b", FP, TEST2), _row("c", None, None, "unresolved"),
            _row("d", SB, ADMIN), _row("e", None, None, "split_day"),
            _row("f", FP, TEST2, "operator"), _row("g", None, None, "unresolved", deleted="x")]
    plan = ra.plan_reassign(rows, TEST2, FP)
    keys = {k: [r["s3_key"] for r in v] for k, v in plan.items()}
    assert keys == {"claim": ["c"], "set_user": ["a"], "pin_only": ["b"], "unchanged": ["f"],
                    "other_company": ["d"], "null_other_reason": ["e"]}
    assert [r["s3_key"] for r in ra.change_set(plan)] == ["c", "a", "b"]
    assert "d" not in {r["s3_key"] for r in ra.change_set(plan)}


def test_reassign_is_idempotent_after_apply():
    rows = [_row("a", FP, ADMIN), _row("c", None, None, "unresolved"), _row("d", SB, ADMIN)]
    applied = []
    for r in rows:
        r = dict(r)
        if r["s3_key"] != "d":
            r.update(company_id=FP, user_id=TEST2, reason="operator")
        applied.append(r)
    assert ra.change_set(ra.plan_reassign(applied, TEST2, FP)) == []


def test_reassign_fingerprint_sees_drift():
    a = [_row("a", FP, ADMIN)]
    b = [dict(a[0], updated_at="t1")]
    assert ra.fingerprint(a) != ra.fingerprint(b)


# ---- (B) delete ----------------------------------------------------------------------------

def _inv(**db):
    base = {"users_rows": [], "unhandled": [], "lake_objects": [], "recordings": [],
            "topics": [], "report_chunks": [], "report_chunks_archive": []}
    base.update(db)
    return {"db": base, "s3": {"objects": [], "versions": []}, "owned": {}}


def test_owned_detects_live_user_recordings_and_owned_index_rows():
    recs = [{"s3_key": "users/Ben_UCPK_/audio/2026-07-17/a.wav", "user_exists": True},
            {"s3_key": "users/MPI1/video/2026-05-19/b.mp4", "user_exists": False}]
    lake = [{"s3_key": "users/Ben_UCPK_/audio/2026-07-17/a.wav", "folder": "Ben_UCPK_",
             "company_id": "sb", "user_id": "u"},
            {"s3_key": "users/MPI1/video/2026-05-19/b.mp4", "folder": "MPI1",
             "company_id": None, "user_id": None}]
    assert dl.owned_by_folder(recs, lake, FOLDERS) == {
        "Ben_UCPK_": {"recordings_with_live_user": 1, "lake_rows_with_owner": 1}}


def test_blockers():
    inv = _inv()
    assert dl.blockers(inv, False) == []
    inv["owned"] = {"Ben_UCPK_": {"recordings_with_live_user": 6}}
    assert dl.blockers(inv, False) and dl.blockers(inv, True) == []
    assert dl.blockers(_inv(unhandled=[{"table": "x", "column": "y", "rows": 1}]), True)
    assert dl.blockers(_inv(users_rows=[{"folder_name": "MPI1"}]), True)


def test_unhandled_sweep_hits():
    sweep = [{"table": "lake_objects", "column": "s3_key", "rows": 3},
             {"table": "action_items", "column": "responsible", "rows": 2},
             {"table": "voice_messages", "column": "s3_key", "rows": 1}]
    assert [h["table"] for h in dl.unhandled_hits(sweep)] == ["action_items", "voice_messages"]
    assert [h["table"] for h in dl.unhandled_hits(sweep, [("action_items", "responsible")])] == \
        ["voice_messages"]


def test_comparable_diff_catches_a_new_object_and_a_new_version():
    a = _inv()
    a["s3"]["objects"] = [{"key": "users/MPI1/x", "etag": "1"}]
    a["s3"]["versions"] = [{"key": "users/MPI1/x", "version_id": "v1"}]
    b = _inv()
    b["s3"]["objects"] = [{"key": "users/MPI1/x", "etag": "1"}, {"key": "users/MPI1/y", "etag": "2"}]
    b["s3"]["versions"] = a["s3"]["versions"] + [{"key": "users/MPI1/x", "version_id": "v2"}]
    assert dl.diff(dl.comparable(a, False), dl.comparable(b, False)) == ["s3"]
    c = _inv()
    c["s3"]["objects"] = a["s3"]["objects"]
    c["s3"]["versions"] = b["s3"]["versions"]
    assert dl.diff(dl.comparable(a, False), dl.comparable(c, False)) == []
    assert dl.diff(dl.comparable(a, True), dl.comparable(c, True)) == ["versions"]


def test_s3_summary_counts_versions():
    objs = [{"key": "users/MPI1/a", "size": 5, "folder": "MPI1", "prefix": "users/"}]
    vers = [{"key": "users/MPI1/a", "is_latest": True, "delete_marker": False, "size": 5},
            {"key": "users/MPI1/a", "is_latest": False, "delete_marker": False, "size": 4},
            {"key": "users/MPI1/gone", "is_latest": True, "delete_marker": True, "size": 0}]
    s = dl.s3_summary(objs, vers, ["x"])
    assert (s["objects"], s["bytes"], s["noncurrent_versions"], s["noncurrent_bytes"],
            s["delete_markers"], s["keys_with_only_old_versions"]) == (1, 5, 1, 4, 1, 1)
    assert s["per_prefix_folder"] == {"users/MPI1": {"objects": 1, "bytes": 5}}


# ---- the dry-run transaction is read only and never committed -------------------------------

class _FakeRds:
    def __init__(self):
        self.calls = []

    def begin_transaction(self, **kw):
        self.calls.append(("begin",))
        return {"transactionId": "tx"}

    def execute_statement(self, **kw):
        self.calls.append(("sql", kw["sql"]))
        if kw["sql"].startswith("SHOW transaction_read_only"):
            return {"formattedRecords": '[{"transaction_read_only": "on"}]'}
        return {"formattedRecords": "[]", "numberOfRecordsUpdated": 0}

    def rollback_transaction(self, **kw):
        self.calls.append(("rollback",))

    def commit_transaction(self, **kw):
        self.calls.append(("commit",))


def test_read_only_db_sets_read_only_first_and_refuses_commit():
    fake = _FakeRds()
    db = common.Db("fieldsight", read_only=True, client=fake)
    assert fake.calls[1] == ("sql", "SET TRANSACTION READ ONLY")
    with pytest.raises(RuntimeError):
        db.commit()
    db.rollback()
    assert ("commit",) not in fake.calls and fake.calls[-1] == ("rollback",)


def test_prod_needs_allow_prod():
    assert common.refuse_prod_without_flag("fieldsight", False)
    assert not common.refuse_prod_without_flag("fieldsight", True)
    assert not common.refuse_prod_without_flag("fieldsight_test", False)
    assert ra.main(["--database", "fieldsight", "--from-folder", "a", "--to-folder", "b",
                    "--expect-company", "c", "--manifest-out", "x"]) == 2
    assert dl.main(["--database", "fieldsight", "--folders", "MPI1", "--manifest-out", "x"]) == 2
