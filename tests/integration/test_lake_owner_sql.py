"""The resolver's lookups against a real Postgres. A fake connection never parses SQL, so each
new repository function (and the C2 split-day verdict built on them) is proven here."""
import uuid

import pytest

import lake_owner
from repositories import (companies, lake_objects, meeting_session, recordings, sites, users)

pytestmark = pytest.mark.integration

DAY = "2026-10-01"


def _user(db, co, folder):
    tag = uuid.uuid4().hex[:8]
    u = users.upsert_user(db, f"sub-{tag}", f"{tag}@x.nz", company_id=co["id"],
                          first_name="A", last_name="B", global_role="worker")
    users.set_folder_name(db, f"sub-{tag}", folder)
    return users.get_by_id(db, u["id"])


def _rec(db, user, site, key, company=None):
    return recordings.insert_pending(
        db, company or user["company_id"], user["id"], site["id"] if site else None, "video", key,
        uuid.uuid4().hex, "2026-10-01T01:00:00Z")


def _world(db):
    tag = uuid.uuid4().hex[:6]
    a = companies.create_company(db, f"OwnA-{tag}")
    b = companies.create_company(db, f"OwnB-{tag}")
    sa = sites.create_site(db, a["id"], "Tower A", slug=f"tw-{tag}")
    sb = sites.create_site(db, b["id"], "Tower B", slug=f"tw-{tag}")  # same slug, other company
    folder = f"Ada_{tag}"
    ada = _user(db, a, folder)
    return dict(a=a, b=b, sa=sa, sb=sb, ada=ada, folder=folder, tag=tag)


def _k(w, kind="video", name="clip.mp4"):
    return f"users/{w['folder']}/{kind}/{DAY}/{name}"


def _owner(db, key, hint=None):
    return lake_owner.Resolver(db).resolve(key, hint)


# ---- recordings.owners_for_keys ------------------------------------------------------------
def test_owners_for_keys_returns_the_sites_current_company_after_a_site_move(db):
    w = _world(db)
    key = _k(w)
    mover = sites.create_site(db, w["a"]["id"], "Mover")
    _rec(db, w["ada"], mover, key, company=w["a"]["id"])
    got = recordings.owners_for_keys(db, [key, "users/none/x"])
    assert set(got) == {key}
    assert got[key]["company_id"] == w["a"]["id"] and got[key]["site_id"] == mover["id"]
    db.execute("UPDATE sites SET company_id=%s WHERE id=%s", (w["b"]["id"], mover["id"]))
    assert recordings.owners_for_keys(db, [key])[key]["company_id"] == w["b"]["id"]
    assert recordings.owners_for_keys(db, []) == {}


def test_a_recording_with_no_site_is_the_recordings_own_company(db):
    w = _world(db)
    key = _k(w)
    _rec(db, w["ada"], None, key)
    assert _owner(db, key)["company_id"] == str(w["a"]["id"])


# ---- recordings.site_companies_for_folder_days -----------------------------------------------
def test_folder_days_reports_sites_with_their_current_company_and_siteless(db):
    w = _world(db)
    _rec(db, w["ada"], w["sb"], _k(w, name="1.mp4"))
    _rec(db, w["ada"], None, _k(w, name="2.mp4"))
    _rec(db, w["ada"], w["sa"], f"users/{w['folder']}/video/2026-10-02/3.mp4")  # another day
    got = recordings.site_companies_for_folder_days(
        db, [(w["folder"], DAY), (w["folder"], "2026-10-02"), (w["folder"], "2026-10-03"),
             ("Nobody", DAY)])
    assert got[(w["folder"], DAY)] == {"sites": {w["sb"]["id"]: w["b"]["id"]}, "siteless": True}
    assert got[(w["folder"], "2026-10-02")] == {"sites": {w["sa"]["id"]: w["a"]["id"]},
                                                "siteless": False}
    assert (w["folder"], "2026-10-03") not in got and ("Nobody", DAY) not in got
    assert recordings.site_companies_for_folder_days(db, []) == {}


# ---- users.homes_for_folders / sites.* / meeting_session.* ----------------------------------------
def test_homes_for_folders(db):
    w = _world(db)
    got = users.homes_for_folders(db, [w["folder"], "Nobody_Z"])
    assert got == {w["folder"]: {"id": w["ada"]["id"], "company_id": w["a"]["id"]}}
    assert users.homes_for_folders(db, []) == {}


def test_sites_companies_for_ids_and_live_by_slugs(db):
    w = _world(db)
    got = sites.companies_for_ids(db, [w["sa"]["id"], str(uuid.uuid4())])
    assert got == {str(w["sa"]["id"]): w["a"]["id"]}
    slug = w["sa"]["slug"]
    live = sites.live_by_slugs(db, [slug, "nope"])
    assert sorted(str(x["id"]) for x in live[slug]) == sorted([str(w["sa"]["id"]), str(w["sb"]["id"])])
    sites.archive_site(db, w["sb"]["id"], w["b"]["id"])
    live = sites.live_by_slugs(db, [slug])
    assert [str(x["id"]) for x in live[slug]] == [str(w["sa"]["id"])]
    assert str(w["sb"]["id"]) in sites.companies_for_ids(db, [w["sb"]["id"]])  # archived keeps owning


def _session(db, w, sid, site, group_id=None, user=None):
    u = user or w["ada"]
    return meeting_session.ensure_open(db, sid, u["company_id"], u["id"],
                                       site["id"] if site else None, "audio",
                                       "2026-10-01T01:00:00Z", group_id=group_id)


def test_owners_for_sids_prefers_the_sites_company(db):
    w = _world(db)
    s1, s2 = uuid.uuid4().hex, uuid.uuid4().hex
    _session(db, w, s1, w["sb"])
    _session(db, w, s2, None)
    got = meeting_session.owners_for_sids(db, [s1, s2, uuid.uuid4().hex])
    assert got[s1]["company_id"] == w["b"]["id"] and got[s1]["site_id"] == w["sb"]["id"]
    assert got[s2]["company_id"] == w["a"]["id"] and got[s2]["site_id"] is None
    assert len(got) == 2 and meeting_session.owners_for_sids(db, []) == {}


def test_members_for_groups_includes_the_lead_and_the_sites_company(db):
    w = _world(db)
    lead, member = uuid.uuid4().hex, uuid.uuid4().hex
    _session(db, w, lead, w["sa"])                       # the lead carries no group_id
    _session(db, w, member, w["sb"], group_id=lead)
    got = meeting_session.members_for_groups(db, [lead, "nogroup"])
    assert sorted((str(m["site_id"]), str(m["site_company_id"])) for m in got[lead]) == sorted([
        (str(w["sa"]["id"]), str(w["a"]["id"])), (str(w["sb"]["id"]), str(w["b"]["id"]))])
    assert "nogroup" not in got and meeting_session.members_for_groups(db, []) == {}


# ---- the whole resolver over real rows -------------------------------------------------------------
def test_end_to_end_rules_against_real_rows(db):
    w = _world(db)
    f = w["folder"]
    # rule 1 and rule 4 (one site that day)
    _rec(db, w["ada"], w["sa"], _k(w))
    o = _owner(db, f"transcripts/{f}/{DAY}/x.json")
    assert (o["company_id"], o["site_id"], o["user_id"], o["reason"]) == (
        str(w["a"]["id"]), str(w["sa"]["id"]), str(w["ada"]["id"]), "folder_day_site")
    assert _owner(db, _k(w))["reason"] == "recording"
    # rule 3
    sid = uuid.uuid4().hex
    _session(db, w, sid, w["sb"])
    o = _owner(db, f"session_finalize_results/{sid}.json")
    assert (o["company_id"], o["reason"]) == (str(w["b"]["id"]), "session")
    # rule 3b: a group across two companies is NULL
    lead, other = uuid.uuid4().hex, uuid.uuid4().hex
    _session(db, w, lead, w["sa"])
    _session(db, w, other, w["sb"], group_id=lead)
    assert _owner(db, f"extraction_requests/group-{lead}.json")["company_id"] is None
    # rule 2 slugs: the slug exists on two live sites -> ambiguous -> NULL (R5c)
    assert _owner(db, f"reports/{DAY}/sites/{w['sa']['slug']}/site_report.json")["company_id"] is None
    sites.archive_site(db, w["sb"]["id"], w["b"]["id"])
    o = _owner(db, f"reports/{DAY}/sites/{w['sa']['slug']}/site_report.json")
    assert o["company_id"] == str(w["a"]["id"])


def test_c2_split_day_through_real_sql_and_a_second_day_unaffected(db):
    w = _world(db)
    f = w["folder"]
    _rec(db, w["ada"], w["sa"], _k(w, name="1.mp4"))
    _rec(db, w["ada"], w["sb"], _k(w, name="2.mp4"))
    for key in (f"reports/{DAY}/{f}/daily_report.json", f"transcripts/{f}/{DAY}/x.json",
                f"audio_segments/{f}/{DAY}/seg.json"):
        o = _owner(db, key)
        assert o["company_id"] is None and o["reason"] == "split_day", key
        assert o["user_id"] == str(w["ada"]["id"])
    # the recordings of that very day still resolve (rule 1)
    assert _owner(db, _k(w, name="2.mp4"))["company_id"] == str(w["b"]["id"])
    # another day with no recordings is the home company
    assert _owner(db, f"transcripts/{f}/2026-10-05/x.json")["company_id"] == str(w["a"]["id"])
    # one site company but two sites -> that company, no site
    sa2 = sites.create_site(db, w["a"]["id"], "Tower A2")
    other = f"users/{f}/video/2026-10-07/1.mp4"
    _rec(db, w["ada"], w["sa"], other)
    _rec(db, w["ada"], sa2, f"users/{f}/video/2026-10-07/2.mp4")
    o = _owner(db, f"transcripts/{f}/2026-10-07/x.json")
    assert (o["company_id"], o["site_id"]) == (str(w["a"]["id"]), None)


def test_a_moved_site_changes_the_verdict_for_the_same_key(db):
    w = _world(db)
    mover = sites.create_site(db, w["a"]["id"], "Mover")
    _rec(db, w["ada"], mover, _k(w), company=w["a"]["id"])
    key = f"transcripts/{w['folder']}/{DAY}/x.json"
    assert _owner(db, key)["company_id"] == str(w["a"]["id"])
    db.execute("UPDATE sites SET company_id=%s WHERE id=%s", (w["b"]["id"], mover["id"]))
    assert _owner(db, key)["company_id"] == str(w["b"]["id"])  # new Resolver: fresh cache


def test_reason_is_not_stamped_on_a_row_that_already_has_a_company(db):
    """A write without a company must not give a company-owned row a 'why it has none' reason."""
    w = _world(db)
    key = f"reports/{DAY}/{w['folder']}/daily_report.json"
    lake_objects.upsert(db, key, company_id=w["a"]["id"], kind="report", source="upload")
    lake_objects.upsert(db, key, company_id=None, kind="report", source="manifest",
                        reason="split_day")
    row = db.execute("SELECT company_id, reason FROM lake_objects WHERE s3_key=%s", (key,)).fetchone()
    assert row == (w["a"]["id"], None)
    k2 = key + "2"
    lake_objects.upsert(db, k2, company_id=None, kind="report", source="manifest",
                        reason="unresolved")
    lake_objects.upsert(db, k2, company_id=None, kind="report", source="manifest",
                        reason="split_day")
    assert db.execute("SELECT reason FROM lake_objects WHERE s3_key=%s", (k2,)).fetchone()[0] == "split_day"


def test_upsert_many_on_missing_none_is_distinguishable_from_zero_changed(db):
    w = _world(db)
    row = {"key": f"reports/{DAY}/{w['folder']}/x.json", "company_id": w["a"]["id"],
           "kind": "report", "source": "manifest"}
    assert lake_objects.upsert_many(db, [row], on_missing=None) == 1
    assert lake_objects.upsert_many(db, [row], on_missing=None) == 0       # present, unchanged
    db.execute("DROP TABLE lake_objects")
    assert lake_objects.upsert_many(db, [row], on_missing=None) is None    # missing
    assert lake_objects.upsert_many(db, [row]) == 0
