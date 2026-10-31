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
