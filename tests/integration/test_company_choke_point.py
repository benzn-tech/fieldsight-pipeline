"""Integration: the live-company choke point (spec 2026-11-01-company-offboarding §5.2, §6,
Task 4) against real Postgres.

World: company A (site sa) and company B (site sb), external memberships both ways:
  * bw  -- B worker, home member of sb, EXTERNAL worker on sa;
  * bad -- B admin, EXTERNAL pm on sa;
  * aw  -- A worker, home member of sa, EXTERNAL worker on sb;
  * pa  -- platform_admin.
bw recorded on both sites. With COMPANY_STATUS_ENFORCE on and A suspended, B's people
lose sa everywhere (reach, /me, /sites, capture, voice, own-folder media) and keep sb in
full; platform_admin keeps everything; reactivating A restores it; flag off changes
nothing. The `sites WHERE company_id = ANY(...)` read runs here against real Postgres, and
so does the skipped-query shape (no inactive company -> None, no predicate).

Nothing is committed (the `db` fixture rolls back), so no company_status_events cleanup.
Skipped without TEST_DATABASE_URL -- a skip is NOT a pass.
"""
import contextlib
import json
import uuid

import psycopg
import pytest

import tenancy_status
from repositories import companies, memberships, recordings, scope, sites, users

pytestmark = pytest.mark.integration

org = pytest.importorskip("lambda_org_api")
vr = pytest.importorskip("lambda_voice_resolve")

FLAG = "COMPANY_STATUS_ENFORCE"
DATE = "2026-10-30"
SID_A = "sid" + "a" * 32
SID_B = "sid" + "b" * 32


def _user(db, co, role, tag, folder=None):
    u = users.upsert_user(db, f"sub-cp-{tag}", f"cp-{tag}@example.com",
                          company_id=co["id"], global_role=role)
    if folder:
        users.set_folder_name(db, u["cognito_sub"], folder)
    return users.get_user_by_sub(db, u["cognito_sub"])


@pytest.fixture
def w(db):
    t = uuid.uuid4().hex[:8]
    a = companies.create_company(db, f"Co-A-{t}")
    b = companies.create_company(db, f"Co-B-{t}")
    p = companies.create_company(db, f"Co-P-{t}")
    sa = sites.create_site(db, a["id"], f"Site-A-{t}")
    sb = sites.create_site(db, b["id"], f"Site-B-{t}")
    bw = _user(db, b, "worker", f"bw-{t}", folder=f"BW_{t}")
    bad = _user(db, b, "admin", f"bad-{t}")
    aw = _user(db, a, "worker", f"aw-{t}", folder=f"AW_{t}")
    pa = _user(db, p, "platform_admin", f"pa-{t}")
    memberships.ensure_membership(db, bw["id"], sb["id"], "worker")
    memberships.add_external_membership(db, bw["id"], sa["id"], "worker")
    memberships.add_external_membership(db, bad["id"], sa["id"], "pm")
    memberships.ensure_membership(db, aw["id"], sa["id"], "worker")
    memberships.add_external_membership(db, aw["id"], sb["id"], "worker")
    folder = f"BW_{t}"
    rec_a = f"users/{folder}/audio/{DATE}/dev_10-00-00_{SID_A}_c0.wav"
    rec_b = f"users/{folder}/audio/{DATE}/dev_11-00-00_{SID_B}_c0.wav"
    photo_a = f"users/{folder}/pictures/{DATE}/IMG_a.jpg"
    photo_b = f"users/{folder}/pictures/{DATE}/IMG_b.jpg"
    for key, site, co, kind in ((rec_a, sa, a, "audio"), (rec_b, sb, b, "audio"),
                                (photo_a, sa, a, "photo"), (photo_b, sb, b, "photo")):
        recordings.insert_pending(db, co["id"], bw["id"], site["id"], kind, key,
                                  uuid.uuid4().hex, f"{DATE}T01:00:00Z")
    return dict(a=a, b=b, sa=str(sa["id"]), sb=str(sb["id"]), bw=bw, bad=bad, aw=aw, pa=pa,
                folder=folder, rec_a=rec_a, rec_b=rec_b, photo_a=photo_a, photo_b=photo_b)


@pytest.fixture
def flag_on(monkeypatch):
    monkeypatch.setenv(FLAG, "on")


@pytest.fixture
def flag_off(monkeypatch):
    monkeypatch.delenv(FLAG, raising=False)


def _set(db, co, status):
    db.execute("UPDATE companies SET status=%s WHERE id=%s", (status, co["id"]))


def _fresh(u):
    """A new caller dict -- the memo lives on the caller, one request each."""
    return {k: v for k, v in u.items() if not str(k).startswith("_")}


def _usable(db):
    assert db.info.transaction_status == psycopg.pq.TransactionStatus.INTRANS
    assert db.execute("SELECT 1").fetchone()[0] == 1


def _call(db, u, method, route, qs=None):
    ev = {"httpMethod": method, "path": "/api/org" + route, "queryStringParameters": qs,
          "body": None, "headers": {},
          "requestContext": {"authorizer": {"claims": {"sub": u["cognito_sub"]}}}}
    res = org.dispatch(db, ev, method, route)
    return res["statusCode"], json.loads(res["body"])


# ---- the helper ------------------------------------------------------------------------

def test_helper_reads_sites_of_inactive_companies(db, w, flag_on):
    assert tenancy_status.inactive_site_ids(db, _fresh(w["bw"])) is None   # nobody inactive
    _set(db, w["a"], "suspended")
    got = tenancy_status.inactive_site_ids(db, _fresh(w["bw"]))
    assert w["sa"] in {str(x) for x in got} and w["sb"] not in {str(x) for x in got}
    _set(db, w["a"], "exited")
    assert w["sa"] in {str(x) for x in tenancy_status.inactive_site_ids(db, _fresh(w["bw"]))}
    _usable(db)


def test_helper_none_for_platform_admin_and_flag_off(db, w, flag_on, monkeypatch):
    _set(db, w["a"], "suspended")
    assert tenancy_status.inactive_site_ids(db, _fresh(w["pa"])) is None
    monkeypatch.delenv(FLAG)
    assert tenancy_status.inactive_site_ids(db, _fresh(w["bw"])) is None


def test_helper_is_memoized_on_the_caller(db, w, flag_on):
    _set(db, w["a"], "suspended")
    caller = _fresh(w["bw"])
    first = tenancy_status.inactive_site_ids(db, caller)
    _set(db, w["a"], "active")     # same request: the first answer stands
    assert tenancy_status.inactive_site_ids(db, caller) == first
    assert tenancy_status.inactive_site_ids(db, _fresh(w["bw"])) is None   # next request


def test_helper_missing_column_is_no_filter_and_keeps_the_transaction(db, w, flag_on):
    _set(db, w["a"], "suspended")
    db.execute("ALTER TABLE companies RENAME COLUMN status TO status_gone")
    assert tenancy_status.inactive_site_ids(db, _fresh(w["bw"])) is None
    _usable(db)
    sc = scope.visible_scope(db, _fresh(w["bw"]))
    assert {w["sa"], w["sb"]} <= sc["site_ids"]


def test_drop_inactive():
    assert tenancy_status.drop_inactive({"x", "y"}, None) == {"x", "y"}
    assert tenancy_status.drop_inactive({"x", "y"}, {"x"}) == {"y"}
    assert tenancy_status.drop_inactive(["x", "y"], {"x"}) == ["y"]
    assert tenancy_status.drop_inactive({"x": 1, "y": 2}, {"x"}) == {"y": 2}
    u = uuid.uuid4()
    assert tenancy_status.drop_inactive([u], {str(u)}) == []


# ---- visible_scope ---------------------------------------------------------------------

def test_visible_scope_drops_the_inactive_companys_sites(db, w, flag_on):
    before = scope.visible_scope(db, _fresh(w["bw"]))
    assert {w["sa"], w["sb"]} <= before["site_ids"]
    _set(db, w["a"], "suspended")
    sc = scope.visible_scope(db, _fresh(w["bw"]))
    assert w["sa"] not in sc["site_ids"] and w["sb"] in sc["site_ids"]
    # A's own person external on B's site: B's site stays fully B's.
    _set(db, w["a"], "active")
    assert {w["sa"], w["sb"]} <= scope.visible_scope(db, _fresh(w["bw"]))["site_ids"]


def test_visible_scope_external_pm_site_dropped_for_an_all_tier_caller(db, w, flag_on):
    assert w["sa"] in scope.visible_scope(db, _fresh(w["bad"]))["external_pm_site_ids"]
    _set(db, w["a"], "suspended")
    sc = scope.visible_scope(db, _fresh(w["bad"]))
    assert w["sa"] not in sc["site_ids"] and w["sa"] not in sc["external_pm_site_ids"]
    assert w["sb"] in sc["site_ids"]


def test_visible_scope_platform_admin_keeps_everything(db, w, flag_on):
    _set(db, w["a"], "suspended")
    assert {w["sa"], w["sb"]} <= scope.visible_scope(db, _fresh(w["pa"]))["site_ids"]


def test_visible_scope_flag_off_is_unchanged(db, w, flag_off):
    _set(db, w["a"], "suspended")
    assert {w["sa"], w["sb"]} <= scope.visible_scope(db, _fresh(w["bw"]))["site_ids"]
    assert w["sa"] in scope.visible_scope(db, _fresh(w["bad"]))["external_pm_site_ids"]


def test_a_person_external_on_b_keeps_b_visible_to_b(db, w, flag_on):
    """A's people external on B's sites: B's site stays B's (visible to B) when A exits."""
    _set(db, w["a"], "suspended")
    assert w["sb"] in scope.visible_scope(db, _fresh(w["bw"]))["site_ids"]
    assert w["sb"] in scope.visible_scope(db, _fresh(w["bad"]))["site_ids"]


# ---- /me and /sites --------------------------------------------------------------------

@pytest.mark.parametrize("graded", [True, False])
def test_me_and_sites_hide_the_inactive_site(db, w, flag_on, monkeypatch, graded):
    monkeypatch.setattr(org, "GRADED_ROLES", graded)
    status, me = _call(db, w["bw"], "GET", "/me")
    assert status == 200 and w["sa"] in me["site_ids"] and w["sa"] in me["site_roles"]
    status, body = _call(db, w["bw"], "GET", "/sites")
    assert status == 200 and w["sa"] in {s["id"] for s in body["sites"]}

    _set(db, w["a"], "suspended")
    status, me = _call(db, w["bw"], "GET", "/me")
    assert status == 200
    assert w["sa"] not in me["site_ids"] and w["sa"] not in me["site_roles"]
    assert w["sb"] in me["site_ids"] and w["sb"] in me["site_roles"]
    assert not any(str(k).startswith("_") for k in me)          # no memo leaks into /me
    status, body = _call(db, w["bw"], "GET", "/sites")
    ids = {s["id"] for s in body["sites"]}
    assert status == 200 and w["sa"] not in ids and w["sb"] in ids

    _set(db, w["a"], "active")                                    # reactivation restores
    status, me = _call(db, w["bw"], "GET", "/me")
    assert w["sa"] in me["site_ids"] and w["sa"] in me["site_roles"]
    _usable(db)


def test_me_flag_off_unchanged(db, w, flag_off, monkeypatch):
    monkeypatch.setattr(org, "GRADED_ROLES", True)
    _set(db, w["a"], "suspended")
    status, me = _call(db, w["bw"], "GET", "/me")
    assert status == 200 and w["sa"] in me["site_ids"] and w["sa"] in me["site_roles"]


def test_platform_admin_sites_list_keeps_the_inactive_site(db, w, flag_on, monkeypatch):
    monkeypatch.setattr(org, "GRADED_ROLES", True)
    _set(db, w["a"], "suspended")
    status, body = _call(db, w["pa"], "GET", "/sites")
    assert status == 200 and w["sa"] in {s["id"] for s in body["sites"]}


# ---- capture ---------------------------------------------------------------------------

def test_capture_onto_the_inactive_site_is_refused(db, w, flag_on):
    site, err = org._capture_site(db, _fresh(w["bw"]), w["sa"])
    assert err is None and str(site["id"]) == w["sa"]
    _set(db, w["a"], "suspended")
    site, err = org._capture_site(db, _fresh(w["bw"]), w["sa"])
    assert site is None and err["statusCode"] == 403
    site, err = org._capture_site(db, _fresh(w["bw"]), w["sb"])
    assert err is None and str(site["id"]) == w["sb"]
    site, err = org._capture_site(db, _fresh(w["pa"]), w["sa"])  # pa holds no membership:
    assert err is not None and err["statusCode"] == 403          # refused as before, not by us


def test_capture_flag_off_unchanged(db, w, flag_off):
    _set(db, w["a"], "suspended")
    site, err = org._capture_site(db, _fresh(w["bw"]), w["sa"])
    assert err is None and str(site["id"]) == w["sa"]


# ---- Site Voice (ws broadcast) ---------------------------------------------------------

def _voice(db, monkeypatch, u, site_id):
    @contextlib.contextmanager
    def _conn(*a, **k):
        yield db
    monkeypatch.setattr(vr, "get_connection", _conn)
    return vr.lambda_handler({"sub": u["cognito_sub"], "siteId": site_id,
                              "s3Key": f"voice/x/{uuid.uuid4().hex}.wav"}, None)


def test_voice_onto_the_inactive_site_is_refused(db, w, flag_on, monkeypatch):
    assert _voice(db, monkeypatch, w["bw"], w["sa"])["statusCode"] == 200
    _set(db, w["a"], "suspended")
    assert _voice(db, monkeypatch, w["bw"], w["sa"]) == {"statusCode": 403}
    assert _voice(db, monkeypatch, w["bw"], w["sb"])["statusCode"] == 200
    _usable(db)


def test_voice_flag_off_unchanged(db, w, flag_off, monkeypatch):
    _set(db, w["a"], "suspended")
    assert _voice(db, monkeypatch, w["bw"], w["sa"])["statusCode"] == 200


# ---- own-folder reads (the session whitelist) ------------------------------------------

def test_own_folder_media_on_the_inactive_site_is_hidden(db, w, flag_on):
    caller = _fresh(w["bw"])
    assert org._session_hider(db, caller, w["folder"], DATE) is None     # nobody inactive
    _set(db, w["a"], "suspended")
    caller = _fresh(w["bw"])
    h = org._session_hider(db, caller, w["folder"], DATE)
    assert h is not None
    assert h.hides(SID_A) and not h.hides(SID_B)
    assert h.hides_key(w["rec_a"]) and not h.hides_key(w["rec_b"])
    assert h.hides_key(w["photo_a"]) and not h.hides_key(w["photo_b"])
    assert not h.hides_key(f"users/{w['folder']}/pictures/{DATE}/IMG_unknown.jpg")
    kept = org._drop_hidden_media([w["rec_a"], w["rec_b"], w["photo_a"], w["photo_b"]],
                                  h, lambda k: k, "test")
    assert kept == [w["rec_b"], w["photo_b"]]
    # per-session lake objects (brief / rolling / session report status)
    assert org._session_out_of_reach(db, caller, w["folder"], DATE, SID_A)
    assert not org._session_out_of_reach(db, caller, w["folder"], DATE, SID_B)
    # day report naming the inactive site's session
    assert org._day_sessions_out_of_reach(db, caller, [w["folder"]], DATE, [SID_A])
    assert not org._day_sessions_out_of_reach(db, caller, [w["folder"]], DATE, [SID_B])
    _set(db, w["a"], "active")                                            # restored
    assert org._session_hider(db, _fresh(w["bw"]), w["folder"], DATE) is None
    _usable(db)


def test_own_folder_media_flag_off_and_platform_admin(db, w, flag_off, monkeypatch):
    _set(db, w["a"], "suspended")
    assert org._session_hider(db, _fresh(w["bw"]), w["folder"], DATE) is None
    monkeypatch.setenv(FLAG, "on")
    assert org._session_hider(db, _fresh(w["pa"]), w["folder"], DATE) is None


def test_someone_elses_folder_on_the_inactive_site_is_hidden_too(db, w, flag_on, monkeypatch):
    """A colleague's view of bw's folder (B admin, graded): the reach no longer holds sa."""
    monkeypatch.setattr(org, "GRADED_ROLES", True)
    h = org._session_hider(db, _fresh(w["bad"]), w["folder"], DATE)
    assert h is not None and not h.hides(SID_A)       # external pm on sa: in reach
    _set(db, w["a"], "suspended")
    h = org._session_hider(db, _fresh(w["bad"]), w["folder"], DATE)
    assert h is not None and h.hides(SID_A) and not h.hides(SID_B)


# ---- own-folder whole-day reads (follow-up ruling: read like another viewer) -----------
# A day of the person's own folder that touches a site of an inactive company is read
# exactly as another viewer would read it: site-clipped rows, no whole-day prose, no
# verbatim daily_report.json; photos / inspections / checklists / per-user reports pinned
# to what is not on the inactive site. Days touching no inactive site are unchanged.

from repositories import inspection_windows, topics  # noqa: E402

VERBATIM = {"executive_summary": "WHOLE DAY PROSE", "topics": []}
QUIET = "2026-10-29"      # a day with work on B's site only
A_ONLY = "2026-10-28"     # a day with one recording on A's site and no topics
SID_C = "sid" + "c" * 32  # an A-site session known ONLY from a topics row


@pytest.fixture
def own(w, db, monkeypatch):
    monkeypatch.setattr(org, "GRADED_ROLES", True)
    monkeypatch.setattr(org, "_get_lake_json", lambda key: dict(VERBATIM)
                        if key.endswith("daily_report.json") else None)
    monkeypatch.setattr(org, "_merged_keys_for", lambda conn, uid, date: [])
    f = w["folder"]
    for site, title, sid, date in ((w["sa"], "A pour", SID_A, DATE),
                                   (w["sb"], "B steel", SID_B, DATE),
                                   (w["sb"], "B quiet", SID_B, QUIET),
                                   (w["sa"], "A topic-only", SID_C, DATE)):
        topics.upsert_topic(db, site, date, title, user_id=w["bw"]["id"],
                            source_s3_key=f"extractions/{f}/{date}/{sid}.json",
                            time_range="09:00 - 09:30")
    recordings.insert_pending(db, w["a"]["id"], w["bw"]["id"], w["sa"], "audio",
                              f"users/{f}/audio/{A_ONLY}/dev_09-00-00.wav",
                              uuid.uuid4().hex, f"{A_ONLY}T01:00:00Z")
    return w


def _timeline(db, u, date):
    res = org.get_timeline_compat(db, _fresh(u), {"queryStringParameters": {"date": date}})
    return res["statusCode"], json.loads(res["body"])


def _titles(body):
    return sorted(t.get("title") or t.get("topic_title") or "" for t in body.get("topics") or [])


def test_own_timeline_day_touching_the_inactive_site_is_clipped(db, own, flag_on):
    code, body = _timeline(db, own["bw"], DATE)
    assert code == 200 and _titles(body) == ["A pour", "A topic-only", "B steel"]
    assert body.get("photo_filenames")                       # the person's own day: photos
    _set(db, own["a"], "suspended")
    code, body = _timeline(db, own["bw"], DATE)
    assert code == 200 and _titles(body) == ["B steel"]
    assert "WHOLE DAY PROSE" not in json.dumps(body)
    assert "IMG_a.jpg" not in json.dumps(body) and not body.get("photo_filenames")
    _set(db, own["a"], "active")                                # restored
    code, body = _timeline(db, own["bw"], DATE)
    assert _titles(body) == ["A pour", "A topic-only", "B steel"]
    _usable(db)


def test_own_verbatim_day_on_the_inactive_site_is_withheld(db, own, flag_on):
    code, body = _timeline(db, own["bw"], A_ONLY)
    assert code == 200 and body.get("executive_summary") == "WHOLE DAY PROSE"
    _set(db, own["a"], "suspended")
    code, body = _timeline(db, own["bw"], A_ONLY)
    assert code == 404 and "WHOLE DAY PROSE" not in json.dumps(body)


def test_own_day_touching_no_inactive_site_is_unchanged(db, own, flag_on):
    before = _timeline(db, own["bw"], QUIET)
    _set(db, own["a"], "suspended")
    assert _timeline(db, own["bw"], QUIET) == before


def test_own_timeline_flag_off_and_platform_admin_unchanged(db, own, flag_off, monkeypatch):
    _set(db, own["a"], "suspended")
    code, body = _timeline(db, own["bw"], A_ONLY)
    assert code == 200 and body.get("executive_summary") == "WHOLE DAY PROSE"
    assert _titles(_timeline(db, own["bw"], DATE)[1]) == ["A pour", "A topic-only", "B steel"]
    monkeypatch.setenv(FLAG, "on")
    assert not org._own_range_touches_inactive(db, _fresh(own["pa"]), own["folder"], DATE, DATE)


def test_own_sessions_list_drops_the_inactive_sites_sessions(db, own, flag_on):
    def sessions():
        res = org.get_org_sessions(db, _fresh(own["bw"]), {"queryStringParameters": {"date": DATE}})
        assert res["statusCode"] == 200
        return {s["session_id"] for s in json.loads(res["body"])["sessions"]}
    assert {SID_A, SID_B, SID_C} <= {s[-35:] for s in sessions()}
    _set(db, own["a"], "suspended")
    got = {s[-35:] for s in sessions()}
    assert SID_B in got and SID_A not in got and SID_C not in got


def test_own_photos_drop_the_inactive_sites_photos(db, own, flag_on):
    names = lambda: sorted(p["s3_key"].rsplit("/", 1)[-1]  # noqa: E731
                           for p in org._day_photo_block(db, _fresh(own["bw"]), own["folder"], DATE))
    assert names() == ["IMG_a.jpg", "IMG_b.jpg"]
    _set(db, own["a"], "suspended")
    assert names() == ["IMG_b.jpg"]
    _set(db, own["a"], "active")
    assert names() == ["IMG_a.jpg", "IMG_b.jpg"]


def test_own_report_history_and_presign_guard(db, own, flag_on):
    f = own["folder"]
    day = f"reports/{DATE}/{f}/daily_report.json"
    quiet = f"reports/{QUIET}/{f}/daily_report.json"
    week_spanning = f"reports/2026-10-31/{f}/weekly_report.json"   # covers DATE
    keep = org._report_entry_guard(db, _fresh(own["bw"]))
    assert keep(day) and keep(quiet) and keep(week_spanning)
    _set(db, own["a"], "suspended")
    caller = _fresh(own["bw"])
    keep = org._report_entry_guard(db, caller)
    assert not keep(day) and not keep(week_spanning)
    assert keep(quiet)
    assert org._user_report_hidden(db, caller, day) is True       # the presign rule
    assert org._user_report_hidden(db, caller, quiet) is False


def test_topics_only_session_is_hidden_from_own_media(db, own, flag_on):
    _set(db, own["a"], "suspended")
    h = org._session_hider(db, _fresh(own["bw"]), own["folder"], DATE)
    assert h.hides(SID_C)                                      # known only from topics
    assert h.hides(SID_A) and not h.hides(SID_B)


def test_own_inspections_and_checklists_drop_the_inactive_sites_sessions(db, own, flag_on):
    f = own["folder"]
    for co, sid, name in ((own["a"], SID_A, "A check"), (own["b"], SID_B, "B check")):
        inspection_windows.replace_for_session(
            db, co["id"], f, DATE, f"dev_10-00-00_{sid}",
            [{"name": name, "start_at": "10:00:00"}])

    def checks():
        res = org.get_day_inspections(db, _fresh(own["bw"]), DATE,
                                      {"queryStringParameters": {}})
        assert res["statusCode"] == 200
        return sorted(r["name"] for r in json.loads(res["body"])["inspections"])

    def unmatched():
        res = org.get_day_checklist_reports(db, _fresh(own["bw"]), DATE,
                                            {"queryStringParameters": {}})
        assert res["statusCode"] == 200
        return sorted(c["checkName"] for c in json.loads(res["body"])["unmatched"])
    assert checks() == ["A check", "B check"] and unmatched() == ["A check", "B check"]
    _set(db, own["a"], "suspended")
    assert checks() == ["B check"] and unmatched() == ["B check"]


# ---- review fix 1: merged meetings carry the LEAD's site --------------------------------

def test_merged_meeting_on_the_inactive_site_is_dropped_everywhere(db, own, flag_on, monkeypatch):
    """bw's own device that day was on B's site only (QUIET); the merged meeting is the
    A person's lead session on A's site. The caller's own-rows check alone sees nothing
    on A -- the merged rows themselves must trigger the rule."""
    lead_key = f"extractions/{own['aw']['folder_name']}/{QUIET}/{SID_A}.json"
    topics.upsert_topic(db, own["sa"], QUIET, "A merged meeting", user_id=own["aw"]["id"],
                        source_s3_key=lead_key, time_range="10:00 - 10:30")
    monkeypatch.setattr(org, "_merged_keys_for", lambda conn, uid, date: [lead_key]
                        if date == QUIET else [])

    def live():
        res = org.list_live_items(db, _fresh(own["bw"]), {"queryStringParameters": {"date": QUIET}})
        assert res["statusCode"] == 200
        return sorted(t["title"] for t in json.loads(res["body"])["topics"])

    def picker():
        return sorted(r["title"] for r in org._day_report_rows(
            db, _fresh(own["bw"]), own["folder"], QUIET))

    def timeline():
        code, body = _timeline(db, own["bw"], QUIET)
        return code, _titles(body), "WHOLE DAY PROSE" in json.dumps(body)

    both = ["A merged meeting", "B quiet"]
    assert live() == both and picker() == both
    assert timeline()[:2] == (200, both)
    _set(db, own["a"], "suspended")
    assert live() == ["B quiet"] and picker() == ["B quiet"]
    code, titles, prose = timeline()
    assert code == 200 and titles == ["B quiet"] and not prose
    _set(db, own["a"], "active")
    assert live() == both and picker() == both and timeline()[:2] == (200, both)
    _usable(db)


def test_merged_meeting_flag_off_unchanged(db, own, flag_off, monkeypatch):
    lead_key = f"extractions/{own['aw']['folder_name']}/{QUIET}/{SID_A}.json"
    topics.upsert_topic(db, own["sa"], QUIET, "A merged meeting", user_id=own["aw"]["id"],
                        source_s3_key=lead_key, time_range="10:00 - 10:30")
    monkeypatch.setattr(org, "_merged_keys_for", lambda conn, uid, date: [lead_key])
    _set(db, own["a"], "suspended")
    res = org.list_live_items(db, _fresh(own["bw"]), {"queryStringParameters": {"date": QUIET}})
    assert "A merged meeting" in {t["title"] for t in json.loads(res["body"])["topics"]}


# ---- final review I2: admin_disambiguation's single candidate ----------------------------
# B's admin opens a day with no ?user; the only candidate is bw (B's own person) who also
# worked on A's site. With the flag on and A inactive, the single-candidate render is
# clipped like any other viewer's: no A rows, no whole-day prose, no photo list, no
# verbatim fallback. Flag off it is the call it always was (the pre-existing view of an
# ACTIVE other company's rows on this path is NOT changed here -- reported separately).

def _disambiguate(db, u, date):
    res = org.admin_disambiguation(db, _fresh(u), date)
    return res["statusCode"], json.loads(res["body"])


@pytest.fixture
def single(own, db, monkeypatch):
    # The merged meeting: an A-site lead session bw attended. The lead row has no author
    # folder, so it adds no second candidate.
    lead_key = f"extractions/LEAD_{uuid.uuid4().hex[:6]}/{DATE}/{SID_A}.json"
    topics.upsert_topic(db, own["sa"], DATE, "A merged meeting", user_id=None,
                        source_s3_key=lead_key, time_range="10:00 - 10:30")
    monkeypatch.setattr(org, "_merged_keys_for", lambda conn, uid, date: [lead_key]
                        if date == DATE else [])
    monkeypatch.setattr(org, "_list_report_folders",
                        lambda date, conn=None, caller=None: [own["folder"]])
    return own


def test_single_candidate_day_of_an_inactive_site_is_clipped(db, single, flag_on):
    code, body = _disambiguate(db, single["bad"], DATE)
    assert code == 200 and body.get("user") == single["folder"]     # one candidate: rendered
    _set(db, single["a"], "suspended")
    code, body = _disambiguate(db, single["bad"], DATE)
    assert code == 200 and _titles(body) == ["B steel"]
    raw = json.dumps(body)
    assert "A pour" not in raw and "A merged meeting" not in raw and "A topic-only" not in raw
    assert "WHOLE DAY PROSE" not in raw
    assert not body.get("photo_filenames") and "IMG_a.jpg" not in raw
    assert single["sa"] not in raw
    _usable(db)


def test_single_candidate_verbatim_day_on_an_inactive_site_is_withheld(db, single, flag_on):
    code, body = _disambiguate(db, single["bad"], A_ONLY)
    assert code == 200 and body.get("executive_summary") == "WHOLE DAY PROSE"
    _set(db, single["a"], "suspended")
    code, body = _disambiguate(db, single["bad"], A_ONLY)
    assert code == 404 and "WHOLE DAY PROSE" not in json.dumps(body)


def test_single_candidate_flag_off_is_unchanged(db, single, flag_off):
    before = _disambiguate(db, single["bad"], DATE)
    before_v = _disambiguate(db, single["bad"], A_ONLY)
    _set(db, single["a"], "suspended")
    assert _disambiguate(db, single["bad"], DATE) == before
    assert _disambiguate(db, single["bad"], A_ONLY) == before_v
    assert before_v[1].get("executive_summary") == "WHOLE DAY PROSE"


def test_single_candidate_nobody_inactive_is_unchanged(db, single, flag_on, monkeypatch):
    on = _disambiguate(db, single["bad"], DATE)
    monkeypatch.delenv(FLAG, raising=False)
    assert _disambiguate(db, single["bad"], DATE) == on
