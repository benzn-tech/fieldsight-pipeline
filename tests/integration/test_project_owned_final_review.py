"""Final-review security fixes for project-owned tenancy (F1-F8) against a real Postgres.

A connection double never parses SQL, so the whitelist queries, the range-aware guard,
the per-site author tier and the audit insert are only proven here.

World: Eve is a worker of company A (home site Yard A) who is also an EXTERNAL worker on
company B's Tower B. B's admin can open Eve's folder (footprint on B's site) -- the whole
question of the review is what else comes along.
"""
import io
import json
import uuid

import pytest
from botocore.exceptions import ClientError

import lambda_org_api as api
from repositories import (companies, memberships, recordings, scope, sites, topics,
                          users)

pytestmark = pytest.mark.integration

DATE = "2026-10-08"
VERBATIM = {"executive_summary": "WHOLE DAY PROSE", "topics": []}
SID_HOME, SID_A, SID_B = "1" * 32, "a" * 32, "b" * 32


class _S3:
    def __init__(self, objects=None):
        self.objects = objects or {}

    def generate_presigned_url(self, op, Params=None, ExpiresIn=0):
        return "https://s3.example/" + Params["Key"]

    def put_object(self, **kw):
        return {}

    def get_object(self, Bucket=None, Key=None):
        if Key in self.objects:
            return {"Body": io.BytesIO(json.dumps(self.objects[Key]).encode())}
        raise ClientError({"Error": {"Code": "NoSuchKey"}}, "GetObject")


@pytest.fixture(autouse=True)
def _env(monkeypatch):
    monkeypatch.setattr(api, "GRADED_ROLES", True)
    monkeypatch.setattr(api, "s3", lambda: _S3())
    monkeypatch.setattr(api, "_get_lake_json", lambda key: dict(VERBATIM)
                        if key.endswith("daily_report.json") else None)
    monkeypatch.setattr(api, "_merged_keys_for", lambda conn, uid, date: [])
    monkeypatch.setattr(api, "_session_was_removed", lambda *a, **k: False)


def _user(db, co, role, tag, who):
    sub = f"sub-{who}-{tag}"
    row = users.upsert_user(db, sub, f"{who}-{tag}@x.nz", company_id=co["id"],
                            first_name=who.title(), last_name="Test", global_role=role)
    users.set_folder_name(db, sub, f"{who.title()}_Test_{tag}")
    return users.get_by_id(db, row["id"])


def _world(db):
    tag = uuid.uuid4().hex[:6]
    co_a = companies.create_company(db, f"HomeCo-{tag}")
    co_b = companies.create_company(db, f"SiteCo-{tag}")
    co_c = companies.create_company(db, f"ThirdCo-{tag}")
    site_a = sites.create_site(db, co_a["id"], "Yard A")
    site_b = sites.create_site(db, co_b["id"], "Tower B")
    eve = _user(db, co_a, "worker", tag, "eve")
    admin_a = _user(db, co_a, "admin", tag, "adma")
    admin_b = _user(db, co_b, "admin", tag, "admb")
    plat = _user(db, co_a, "platform_admin", tag, "plat")
    memberships.add_membership(db, eve["id"], site_a["id"], "worker")
    memberships.add_external_membership(db, eve["id"], site_b["id"], "worker")
    return dict(tag=tag, co_a=co_a, co_b=co_b, co_c=co_c, site_a=site_a, site_b=site_b,
                eve=eve, admin_a=admin_a, admin_b=admin_b, plat=plat, folder=eve["folder_name"])


def _rec(db, w, site, sid, date=DATE, kind="audio"):
    """A recording of Eve; site=None is a SITE-LESS one (home company A)."""
    ext = "wav" if kind == "audio" else "mp4"
    company = site["company_id"] if site else w["co_a"]["id"]
    return recordings.insert_pending(
        db, company_id=company, user_id=w["eve"]["id"],
        site_id=site["id"] if site else None, kind=kind,
        s3_key=f"users/{w['folder']}/{kind}/{date}/x_sid{sid}_c0000.{ext}",
        client_uuid=uuid.uuid4().hex, started_at=f"{date}T10:00:00Z")


def _topic(db, w, site, title, sid):
    return topics.upsert_topic(db, site["id"], DATE, title, user_id=w["eve"]["id"],
                               source_s3_key=f"extractions/{w['folder']}/{DATE}/sid{sid}.json",
                               time_range="09:00 - 09:30")


def _timeline(db, caller, user=None):
    ev = {"queryStringParameters": {"date": DATE, **({"user": user} if user else {})}}
    res = api.get_timeline_compat(db, caller, ev)
    return res["statusCode"], json.loads(res["body"])


def _presign(db, caller, key):
    return api.get_org_media_presigned_url(db, caller, {"queryStringParameters": {"key": key}})


# ---------------------------------------------------------------- F1: the whitelist

def test_a_site_less_home_session_is_hidden_from_the_site_company_only(db):
    w = _world(db)
    _rec(db, w, None, SID_HOME)
    _rec(db, w, w["site_a"], SID_A)
    _rec(db, w, w["site_b"], SID_B)
    hb = api._session_hider(db, w["admin_b"], w["folder"], DATE)
    assert hb.hides("sid" + SID_HOME) and hb.hides("sid" + SID_A)
    assert not hb.hides("sid" + SID_B)
    assert hb.hides(None) and hb.hides("sid" + "9" * 32)        # unknown/row-less: hidden too
    ha = api._session_hider(db, w["admin_a"], w["folder"], DATE)
    assert not ha.hides("sid" + SID_HOME) and not ha.hides("sid" + SID_A)
    assert ha.hides("sid" + SID_B)
    assert api._session_hider(db, w["eve"], w["folder"], DATE) is None        # the person
    assert api._session_hider(db, w["plat"], w["folder"], DATE) is None       # platform_admin


def _audio_keys(w, sid):
    return [{"Key": f"audio_segments/{w['folder']}/{DATE}/Eve_{DATE}_10-00-00_sid{sid}"
                    f"_c0000_off0_to30.wav"}]


def test_audio_listing_is_a_whitelist(db, monkeypatch):
    w = _world(db)
    _rec(db, w, None, SID_HOME)
    _rec(db, w, w["site_b"], SID_B)
    monkeypatch.setattr(api, "_list_media_objects",
                        lambda prefix, what: _audio_keys(w, SID_HOME) + _audio_keys(w, SID_B))

    def names(caller):
        out = api._read_org_audio_segments(DATE, w["folder"], "", "", conn=db, caller=caller)
        return sorted(s["filename"].split("_sid")[1][:32] for s in out["segments"])
    assert names(w["admin_b"]) == [SID_B]                  # NOT the site-less home session
    assert names(w["admin_a"]) == [SID_HOME]
    assert names(w["eve"]) == sorted([SID_HOME, SID_B])
    assert names(w["plat"]) == sorted([SID_HOME, SID_B])


def test_transcript_listing_is_a_whitelist(db, monkeypatch):
    w = _world(db)
    _rec(db, w, None, SID_HOME)
    _rec(db, w, w["site_b"], SID_B)
    objs = {}
    keys = []
    for sid, words in ((SID_HOME, "home words"), (SID_B, "tower words")):
        k = f"transcripts/{w['folder']}/{DATE}/Eve_{DATE}_10-00-00_sid{sid}.json"
        objs[k] = {"results": {"transcripts": [{"transcript": words}], "audio_segments": []}}
        keys.append({"Key": k})
    monkeypatch.setattr(api, "_list_media_objects", lambda prefix, what: keys)
    monkeypatch.setattr(api, "s3", lambda: _S3(objs))

    def text(caller):
        return json.dumps(api._read_org_transcripts(DATE, w["folder"], "", "", conn=db,
                                                    caller=caller))
    assert "tower words" in text(w["admin_b"]) and "home words" not in text(w["admin_b"])
    assert "home words" in text(w["admin_a"]) and "tower words" not in text(w["admin_a"])
    assert "home words" in text(w["eve"]) and "tower words" in text(w["eve"])


def test_presign_of_a_site_less_home_session_is_refused_to_the_site_company(db):
    w = _world(db)
    _rec(db, w, None, SID_HOME)
    _rec(db, w, w["site_b"], SID_B)
    home = f"users/{w['folder']}/audio/{DATE}/x_sid{SID_HOME}_c0000.wav"
    tower = f"users/{w['folder']}/audio/{DATE}/x_sid{SID_B}_c0000.wav"
    assert _presign(db, w["admin_b"], home)["statusCode"] == 404
    assert _presign(db, w["admin_b"], tower)["statusCode"] == 200
    assert _presign(db, w["admin_a"], home)["statusCode"] == 200
    assert _presign(db, w["admin_a"], tower)["statusCode"] == 404
    assert _presign(db, w["eve"], home)["statusCode"] == 200
    assert _presign(db, w["plat"], home)["statusCode"] == 200


def test_the_verbatim_daily_report_needs_the_home_company_for_site_less_days(db):
    w = _world(db)
    _rec(db, w, None, SID_HOME)                         # only a site-less recording, no topics
    code, _ = _timeline(db, w["admin_b"], w["folder"])
    assert code == 404                                  # B must not get X's home-day prose
    code, body = _timeline(db, w["admin_a"], w["folder"])
    assert code == 200 and body.get("executive_summary") == "WHOLE DAY PROSE"
    code, body = _timeline(db, w["eve"])
    assert code == 200 and body.get("executive_summary") == "WHOLE DAY PROSE"


def test_a_day_with_site_less_work_is_clipped_for_the_site_company(db):
    w = _world(db)
    _topic(db, w, w["site_b"], "Tower B steel", SID_B)
    _rec(db, w, None, SID_HOME)                         # same day, home work, no topic
    code, body = _timeline(db, w["admin_b"], w["folder"])
    assert code == 200
    assert [t.get("title") or t.get("topic_title") for t in body["topics"]] == ["Tower B steel"]
    assert "WHOLE DAY PROSE" not in json.dumps(body)    # the prose covers the site-less recording
    # and the home admin sees no foreign site: prose served unclipped on a clean day
    code, body = _timeline(db, w["admin_a"], w["folder"])
    assert code == 404 or "WHOLE DAY PROSE" not in json.dumps(body)   # the day holds B rows


def test_the_report_history_entry_follows_the_same_rule(db):
    w = _world(db)
    _rec(db, w, None, SID_HOME)
    key = f"reports/{DATE}/{w['folder']}/daily_report.json"
    assert api._report_entry_guard(db, w["admin_b"])(key) is False
    assert api._report_entry_guard(db, w["admin_a"])(key) is True
    assert api._report_entry_guard(db, w["eve"])(key) is True
    assert api._report_entry_guard(db, w["plat"]) is None
    assert _presign(db, w["admin_b"], key)["statusCode"] == 404
    assert _presign(db, w["admin_a"], key)["statusCode"] == 200


# ---------------------------------------------------------------- F2: ranges, directory

def _week_world(db):
    w = _world(db)
    _rec(db, w, w["site_a"], SID_A, date=DATE)                         # end day: clean for A
    _rec(db, w, w["site_b"], SID_B, date="2026-10-04")                 # inside the week
    return w


def test_a_weekly_document_is_hidden_when_any_day_in_range_is_outside_the_callers_sites(db):
    w = _week_world(db)
    weekly = f"reports/{DATE}/{w['folder']}/weekly_report.json"
    daily = f"reports/{DATE}/{w['folder']}/daily_report.json"
    # the END day is clean for A, so the old single-date guard let the weekly through
    assert api._user_report_hidden(db, w["admin_a"], daily) is False
    assert api._user_report_hidden(db, w["admin_a"], weekly) is True
    assert api._user_report_hidden(db, w["admin_b"], weekly) is True
    assert api._user_report_hidden(db, w["admin_b"], daily) is True    # B sees none of DATE
    assert api._user_report_hidden(db, w["eve"], weekly) is False
    assert api._user_report_hidden(db, w["plat"], weekly) is False
    assert _presign(db, w["admin_a"], weekly)["statusCode"] == 404
    assert _presign(db, w["admin_a"], daily)["statusCode"] == 200
    assert api._report_entry_guard(db, w["admin_a"])(weekly) is False


def test_a_weekly_document_is_served_when_the_whole_range_is_in_scope(db):
    w = _world(db)
    _rec(db, w, w["site_a"], SID_A, date=DATE)
    _rec(db, w, w["site_a"], SID_A, date="2026-10-05")
    _rec(db, w, w["site_b"], SID_B, date="2026-09-15")                 # outside the 7 days
    assert api._user_report_hidden(
        db, w["admin_a"], f"reports/{DATE}/{w['folder']}/weekly_report.json") is False
    # a monthly reaches back further and does see it
    assert api._user_report_hidden(
        db, w["admin_a"], f"reports/{DATE}/{w['folder']}/monthly_report.json") is True


# ---------------------------------------------------------------- F3: per-site tier

def _lone(db, w, role, who):
    return _user(db, w["co_a"], role, w["tag"], who)


def test_an_external_pm_role_does_not_raise_the_home_company_reach(db):
    w = _world(db)
    z = _lone(db, w, "worker", "zed")
    peer = _lone(db, w, "worker", "pat")
    memberships.add_membership(db, z["id"], w["site_a"]["id"], "worker")
    memberships.add_membership(db, peer["id"], w["site_a"]["id"], "worker")
    memberships.add_external_membership(db, z["id"], w["site_b"]["id"], "pm")
    sc = scope.visible_scope(db, dict(z))
    assert sc["user_scope"] == "SELF" and sc["author_ids"] == {str(z["id"])}
    assert sc["site_ids"] == {str(w["site_a"]["id"])}      # B adds nothing it cannot express
    assert api._can_view_folder(db, dict(z), peer["folder_name"]) is False


def test_a_home_pm_role_does_not_raise_the_external_reach(db):
    w = _world(db)
    z = _lone(db, w, "regional_manager", "rex")
    bob = _user(db, w["co_b"], "worker", w["tag"], "bob")
    memberships.add_membership(db, z["id"], w["site_a"]["id"], "pm")
    memberships.add_membership(db, bob["id"], w["site_b"]["id"], "worker")
    memberships.add_external_membership(db, z["id"], w["site_b"]["id"], "worker")
    sc = scope.visible_scope(db, dict(z))
    assert sc["user_scope"] == "SITE" and sc["author_ids"] is None     # home: unchanged
    assert str(w["site_b"]["id"]) not in sc["site_ids"]                # not SITE tier on B
    assert api._can_view_folder(db, dict(z), bob["folder_name"]) is False


def test_matching_tiers_still_share_the_reach(db):
    w = _world(db)
    pm = _lone(db, w, "pm", "pam")
    memberships.add_membership(db, pm["id"], w["site_a"]["id"], "pm")
    memberships.add_external_membership(db, pm["id"], w["site_b"]["id"], "pm")
    sc = scope.visible_scope(db, dict(pm))
    assert sc["site_ids"] == {str(w["site_a"]["id"]), str(w["site_b"]["id"])}
    assert sc["author_ids"] is None
    wk = _lone(db, w, "worker", "wes")
    memberships.add_membership(db, wk["id"], w["site_a"]["id"], "worker")
    memberships.add_external_membership(db, wk["id"], w["site_b"]["id"], "worker")
    sc = scope.visible_scope(db, dict(wk))
    assert sc["site_ids"] == {str(w["site_a"]["id"]), str(w["site_b"]["id"])}
    assert sc["author_ids"] == {str(wk["id"])}


# ---------------------------------------------------------------- F4: the home pin

def test_company_none_is_not_how_an_external_member_reaches_the_site(db):
    w = _world(db)
    cat = _user(db, w["co_c"], "worker", w["tag"], "cat")
    recordings.insert_pending(                                   # another tenant, site-less
        db, company_id=w["co_c"]["id"], user_id=cat["id"], site_id=None, kind="audio",
        s3_key=f"users/{cat['folder_name']}/audio/2026-10-07/x_sid{'c' * 32}_c0000.wav",
        client_uuid=uuid.uuid4().hex, started_at="2026-10-07T10:00:00Z")
    _rec(db, w, w["site_b"], SID_B)                              # Eve's work on B's site
    sites_ = [w["site_a"]["id"], w["site_b"]["id"]]
    got = recordings.range_stats(db, w["co_a"]["id"], "2026-10-07", DATE, sites_,
                                 external_site_ids=[str(w["site_b"]["id"])])
    assert got["sessions"] == 1                                  # B's session yes, C's no
    plain = recordings.range_stats(db, w["co_a"]["id"], "2026-10-07", DATE, sites_)
    assert plain["sessions"] == 0                                # pin intact without the waiver
    counts = recordings.upload_date_counts(db, w["co_a"]["id"], sites_, "2026-10-01",
                                           external_site_ids=[str(w["site_b"]["id"])])
    assert [c["date"] for c in counts] == [DATE]


def test_the_calendar_uploads_index_does_not_count_other_tenants(db, monkeypatch):
    w = _world(db)
    boss = _lone(db, w, "admin", "bo")
    memberships.add_external_membership(db, boss["id"], w["site_b"]["id"], "pm")
    cat = _user(db, w["co_c"], "worker", w["tag"], "cat")
    recordings.insert_pending(
        db, company_id=w["co_c"]["id"], user_id=cat["id"], site_id=None, kind="audio",
        s3_key=f"users/{cat['folder_name']}/audio/{DATE}/x_sid{'c' * 32}_c0000.wav",
        client_uuid=uuid.uuid4().hex, started_at=f"{DATE}T10:00:00Z")
    res = api.get_org_dates(db, dict(boss), {"queryStringParameters": {"uploads": "1"}})
    dates = json.loads(res["body"])["dates"]
    assert DATE not in dates or dates[DATE]["sessions"] == 0


# ---------------------------------------------------------------- F5: site company writes

def test_naming_a_speaker_needs_the_sessions_site_company(db):
    w = _world(db)
    _rec(db, w, w["site_b"], SID_B)
    _rec(db, w, w["site_a"], SID_A)
    g = api._session_site_company_guard
    assert g(db, w["admin_a"], w["folder"], DATE, "sid" + SID_B, "x")["statusCode"] == 403
    assert g(db, w["admin_a"], w["folder"], DATE, "sid" + SID_A, "x") is None
    assert g(db, w["admin_b"], w["folder"], DATE, "sid" + SID_B, "x") is None
    assert g(db, w["plat"], w["folder"], DATE, "sid" + SID_B, "x") is None
    # the recorder: refused for enrolment, allowed for regenerating their own session
    assert g(db, w["eve"], w["folder"], DATE, "sid" + SID_B, "x")["statusCode"] == 403
    assert g(db, w["eve"], w["folder"], DATE, "sid" + SID_B, "x", allow_recorder=True) is None
    # a session on no site is the home company's
    _rec(db, w, None, SID_HOME)
    assert g(db, w["admin_a"], w["folder"], DATE, "sid" + SID_HOME, "x") is None


def test_speaker_routes_refuse_a_session_on_another_companys_site(db, monkeypatch):
    w = _world(db)
    _rec(db, w, w["site_b"], SID_B)
    monkeypatch.setattr(api, "SPEAKER_IDENTITY_MODE", "shadow")
    base = f"Eve_{DATE}_10-00-00_sid{SID_B}"
    res = api.speaker_match(db, w["admin_a"], base,
                            {"body": json.dumps({"user": w["folder"]})})
    assert res["statusCode"] == 403 and "another company" in json.loads(res["body"])["error"]
    ev = {"body": json.dumps({"user": w["folder"], "display_name": "Bob Test",
                              "source_filename": base + ".json", "start_sec": 1, "end_sec": 9})}
    res = api.speaker_corrections(db, w["admin_a"], base, ev)
    assert res["statusCode"] == 403
    res = api.regenerate_session(db, w["admin_a"], "sid" + SID_B,
                                 {"body": json.dumps({"user": w["folder"], "date": DATE})})
    assert res["statusCode"] == 403


# ---------------------------------------------------------------- F6: the day-level rung

def test_the_most_used_site_rung_never_leaves_the_home_company(db):
    w = _world(db)
    _rec(db, w, w["site_b"], "d" * 32)
    _rec(db, w, w["site_b"], "e" * 32)                  # B is the most-used site that day
    _rec(db, w, w["site_a"], SID_A)
    got = recordings.site_for_day(db, w["eve"]["id"], w["folder"], DATE)
    assert str(got["id"]) == str(w["site_a"]["id"])
    db.execute("DELETE FROM recordings WHERE site_id=%s", (w["site_a"]["id"],))
    assert recordings.site_for_day(db, w["eve"]["id"], w["folder"], DATE) is None
    # an explicit tag (the recording of THIS session) still reaches the foreign site
    got = recordings.site_for_media(db, w["eve"]["id"], w["folder"], DATE, f"x_sid{'d' * 32}_c0000")
    assert str(got["id"]) == str(w["site_b"]["id"])


# ---------------------------------------------------------------- F7: folder+sid objects

def test_brief_rolling_and_report_status_follow_the_whitelist(db):
    w = _world(db)
    _rec(db, w, None, SID_HOME)
    _rec(db, w, w["site_b"], SID_B)
    ev = {"queryStringParameters": {"date": DATE, "user": w["folder"], "requestId": "r" * 32}}

    def code(fn, caller, sid):
        return fn(db, caller, sid, ev)["statusCode"]
    for fn in (api.session_brief_read, api.session_rolling, api.session_report_status):
        assert code(fn, w["admin_b"], "sid" + SID_HOME) == 404       # site-less home session
        assert code(fn, w["admin_b"], "sid" + SID_B) == 200          # pending: not refused
        assert code(fn, w["admin_a"], "sid" + SID_HOME) == 200
        assert code(fn, w["admin_a"], "sid" + SID_B) == 404
        assert code(fn, w["eve"], "sid" + SID_HOME) == 200


# ---------------------------------------------------------------- F8: audit and ambiguity

def _audit(db, site):
    return db.execute("SELECT action, role FROM membership_audit WHERE site_id=%s ORDER BY at, id",
                      (site["id"],)).fetchall()


def test_every_external_add_revive_role_change_and_archive_is_audited(db):
    w = _world(db)
    ghost = _user(db, w["co_c"], "worker", w["tag"], "ghost")

    def add(role):
        return api.add_external_member(db, dict(w["admin_b"]), str(w["site_b"]["id"]),
                                       {"email": ghost["email"], "role": role})
    assert add("worker")["statusCode"] == 201
    assert api.put_member_membership(
        db, dict(w["admin_b"]), ghost["cognito_sub"], str(w["site_b"]["id"]),
        {"role": "pm"})["statusCode"] == 200
    assert api.delete_member_membership(
        db, dict(w["admin_b"]), ghost["cognito_sub"], str(w["site_b"]["id"]))["statusCode"] == 200
    assert add("site_manager")["statusCode"] == 201
    rows = [r for r in _audit(db, w["site_b"])]
    assert [r[0] for r in rows] == ["external_add", "external_role", "external_archive",
                                    "external_revive"]
    assert [r[1] for r in rows] == ["worker", "pm", "pm", "site_manager"]
    who = db.execute("SELECT actor_user_id, target_user_id, site_company_id "
                     "FROM membership_audit WHERE action='external_add' AND site_id=%s",
                     (w["site_b"]["id"],)).fetchone()
    assert (str(who[0]), str(who[1]), str(who[2])) == (
        str(w["admin_b"]["id"]), str(ghost["id"]), str(w["co_b"]["id"]))


def test_an_email_shared_by_two_live_users_is_refused_not_guessed(db):
    w = _world(db)
    a = _user(db, w["co_c"], "worker", w["tag"], "twin1")
    b = _user(db, w["co_a"], "worker", w["tag"], "twin2")
    db.execute("UPDATE users SET email=%s WHERE id IN (%s,%s)", ("same@x.nz", a["id"], b["id"]))
    res = api.add_external_member(db, dict(w["admin_b"]), str(w["site_b"]["id"]),
                                  {"email": "same@x.nz", "role": "worker"})
    assert res["statusCode"] == 409 and "ambiguous email" in json.loads(res["body"])["error"]
    assert memberships.get_membership(db, a["id"], w["site_b"]["id"]) is None
    assert memberships.get_membership(db, b["id"], w["site_b"]["id"]) is None
    assert _audit(db, w["site_b"]) == []


def test_a_report_with_no_rows_behind_it_is_not_served_to_the_site_company(db):
    w = _world(db)
    daily = f"reports/{DATE}/{w['folder']}/daily_report.json"
    assert api._user_report_hidden(db, w["admin_b"], daily) is True     # nothing ties it to B
    assert api._user_report_hidden(db, w["admin_a"], daily) is False    # home company


def test_day_report_status_follows_the_whitelist(db, monkeypatch):
    w = _world(db)
    _rec(db, w, None, SID_HOME)
    _rec(db, w, w["site_b"], SID_B)
    monkeypatch.setattr(api, "_any_session_removed", lambda *a, **k: False)
    monkeypatch.setattr(api, "_presign_report_doc", lambda *a, **k: "https://doc")
    rid = "f" * 32

    def serve(sids):
        objs = {f"session_report_results/{w['folder']}/{DATE}/day/{rid}.json": {
            "status": "done", "docKey": "k", "sessionIds": sids}}
        monkeypatch.setattr(api, "s3", lambda: _S3(objs))
        ev = {"queryStringParameters": {"user": w["folder"], "requestId": rid}}
        return lambda caller: api.day_report_status(db, caller, DATE, ev)["statusCode"]
    both = serve(["sid" + SID_HOME, "sid" + SID_B])
    assert both(w["admin_b"]) == 404                  # names a site-less home session
    assert both(w["admin_a"]) == 404                  # names a session on B's site
    assert both(w["eve"]) == 200 and both(w["plat"]) == 200
    only_b = serve(["sid" + SID_B])
    assert only_b(w["admin_b"]) == 200 and only_b(w["admin_a"]) == 404
    only_home = serve(["sid" + SID_HOME])
    assert only_home(w["admin_a"]) == 200 and only_home(w["admin_b"]) == 404


# ---------------------------------------------------------------- deploy tolerance

def test_code_that_lands_before_the_migrations_behaves_as_no_external_members(db, caplog):
    """The deploy workflows apply migrations AFTER sam deploy: for a minute the new code
    runs against a schema without memberships.external (0085) or membership_audit (0086)."""
    from repositories import directory as directory_repo
    w = _world(db)
    db.execute("ALTER TABLE memberships DROP COLUMN external")        # rolled back with the test
    db.execute("DROP TABLE membership_audit")
    with caplog.at_level("WARNING"):
        assert memberships.external_site_roles(db, w["eve"]["id"]) == {}
        assert memberships.has_live_external(db, w["eve"]["id"]) is False
        assert memberships.external_site_companies(db, w["eve"]["id"]) == {}
        assert memberships.get_membership(db, w["eve"]["id"], w["site_a"]["id"])["external"] is False
        assert memberships.count_by_site(db, [w["site_a"]["id"]]) == {str(w["site_a"]["id"]): 1}
        rows = memberships.members_for_site(db, w["co_a"]["id"], w["site_a"]["id"])
        assert [r["external"] for r in rows] == [False]
        assert memberships.list_company_memberships(db, w["co_a"]["id"])
        assert memberships.list_all_memberships(db)
        assert directory_repo.live_memberships(db)
        assert directory_repo.external_user_ids(db) == set()
        sc = scope.visible_scope(db, dict(w["eve"]))
        assert sc["site_ids"] == {str(w["site_a"]["id"])} | {str(w["site_b"]["id"])}  # legacy reach
        memberships.record_audit(db, "external_add", dict(w["admin_b"]), w["eve"]["id"],
                                 w["site_b"], "worker")            # logs, does not raise
    assert "migration 0085" in caplog.text and "migration 0086" in caplog.text


# ---------------------------------------------------------------- third round

def test_regenerate_of_a_site_less_session_is_the_home_companys_alone(db, monkeypatch):
    w = _world(db)
    _rec(db, w, None, SID_HOME)
    monkeypatch.setattr(api, "S3_BUCKET", "lake")
    monkeypatch.setattr(api.boto3, "client", lambda *a, **k: _S3())
    monkeypatch.setattr(api, "_list_media_objects", lambda prefix, what: [])
    ev = {"body": json.dumps({"user": w["folder"], "date": DATE})}
    assert api.regenerate_session(db, w["admin_b"], "sid" + SID_HOME, ev)["statusCode"] == 404
    assert api.regenerate_session(db, w["admin_a"], "sid" + SID_HOME, ev)["statusCode"] == 202
    assert api.regenerate_session(db, w["plat"], "sid" + SID_HOME, ev)["statusCode"] == 202


def test_a_mixed_session_is_hidden_whole_from_others_and_visible_to_the_person(db):
    w = _world(db)
    sid = "9" * 32
    _rec(db, w, w["site_b"], sid)                            # rows on B's site ...
    _topic(db, w, w["site_a"], "Yard A pour", sid)           # ... and a topic on A's site
    for caller in (w["admin_a"], w["admin_b"]):              # each sees one half only
        h = api._session_hider(db, caller, w["folder"], DATE)
        assert h.hides("sid" + sid) is True
    key = f"users/{w['folder']}/audio/{DATE}/x_sid{sid}_c0000.wav"
    assert _presign(db, w["admin_a"], key)["statusCode"] == 404
    assert _presign(db, w["admin_b"], key)["statusCode"] == 404
    assert _presign(db, w["eve"], key)["statusCode"] == 200
    assert api._session_hider(db, w["eve"], w["folder"], DATE) is None


def test_the_observations_reads_survive_the_missing_external_column(db):
    from repositories import observations
    w = _world(db)
    db.execute("ALTER TABLE memberships DROP COLUMN external")
    obs = observations.create_observation(
        db, w["co_a"]["id"], "safety", "yard-a", w["eve"]["cognito_sub"], "Eve",
        "Loose edge", report_date=DATE)
    assert obs["author_folder"] == w["folder"]
    assert [o["id"] for o in observations.list_observations(db, w["co_a"]["id"])] == [obs["id"]]
