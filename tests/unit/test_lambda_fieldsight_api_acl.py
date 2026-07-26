"""ACL regression tests for src/lambda_fieldsight_api.py.

These pin the OVERLOADED-SENTINEL class of defect closed. The live prod
leak (2026-07-23): get_report_history used `allowed_folders = []` to mean
BOTH "admin/gm -- no filter" (:789) and "this caller can access nothing"
(:790-792), and `if allowed_folders:` (:802) is falsy for both -- so an
Aurora-only account (absent from the DynamoDB fieldsight-users table ->
role='viewer', display_name='' -> get_accessible_users returns []) got
EVERY report key in the bucket. Signed in as Ben_UCPK (own report count in
S3: 1), GET /api/reports/history?limit=200 returned 88 keys spanning every
user folder in the lake.

find_any_report (:264-291) carries the identical idiom at :277 and is
worse: when exactly one report survives the filter it returns the full
report BODY (:288).

get_presigned_url (:377-406) is the content half of the same attack: a key
whose owner could not be derived left target_user = None, and the `and` at
:400 short-circuited, so NO permission check ran at all.

Style mirrors tests/unit/test_lambda_fieldsight_api_ask.py.
"""
import datetime
import json
import os

import pytest

os.environ.setdefault("AWS_ACCESS_KEY_ID", "testing")
os.environ.setdefault("AWS_SECRET_ACCESS_KEY", "testing")
os.environ.setdefault("AWS_DEFAULT_REGION", "ap-southeast-2")

fapi = pytest.importorskip("lambda_fieldsight_api", reason="requires boto3 (installed in CI)")


ADMIN_CALLER = {
    "sub": "sub-admin-1", "email": "a@x.nz", "name": "Ada Admin",
    "role": "admin", "display_name": "Ada_Admin", "device_id": "",
    "sites": [], "managed_sites": [], "company_id": "c-1",
}

# THE BUG'S CALLER: an Aurora-provisioned account that get_caller_identity
# could not resolve -- absent from DynamoDB fieldsight-users AND from
# config/user_mapping.json, so it keeps the :86 seed values verbatim.
VIEWER_CALLER = {
    "sub": "sub-ucpk-1", "email": "ben@ucpk.nz", "name": "Ben UCPK",
    "role": "viewer", "display_name": "", "device_id": "",
    "sites": [], "managed_sites": [], "company_id": "",
}

WORKER_CALLER = {
    "sub": "sub-worker-1", "email": "w@x.nz", "name": "Ben Test",
    "role": "worker", "display_name": "Ben Test", "device_id": "Benl1",
    "sites": ["s-1"], "managed_sites": [], "company_id": "c-1",
}

SITE_MANAGER_CALLER = {
    "sub": "sub-sm-1", "email": "sm@x.nz", "name": "Sam Manager",
    "role": "site_manager", "display_name": "Sam Manager", "device_id": "",
    "sites": ["s-1"], "managed_sites": ["s-1"], "company_id": "c-1",
}

MAPPING = {
    "mapping": {
        "Benl1": {"name": "Ben Test",   "role": "worker",       "sites": ["s-1"]},
        "Dev2":  {"name": "Sam Manager", "role": "site_manager", "sites": ["s-1"]},
        "Dev3":  {"name": "Ada Worker",  "role": "worker",       "sites": ["s-1"]},
        "Dev4":  {"name": "Otto Other",  "role": "worker",       "sites": ["s-2"]},
    },
    "sites": {"s-1": {"name": "Site One"}, "s-2": {"name": "Site Two"}},
}

# The prod lake's real shape, in miniature: per-user reports, ownerless
# summary rollups, and ownerless site rollups.
ALL_KEYS = [
    "reports/2026-07-20/Ben_Test/daily_report.json",
    "reports/2026-07-20/Ada_Worker/daily_report.json",
    "reports/2026-07-20/Otto_Other/daily_report.json",
    "reports/2026-07-20/Sam_Manager/daily_report.json",
    "reports/2026-07-20/summary_report.json",
    "reports/2026-07-20/sites/s-1/site_report.json",
    "reports/2026-07-20/Ben_Test/daily_report_debug.json",   # excluded by :800
]

_TS = datetime.datetime(2026, 7, 20, 12, 0, 0)


class FakePaginator:
    def __init__(self, keys):
        self.keys = keys

    def paginate(self, **kwargs):
        prefix = kwargs.get("Prefix", "")
        delimiter = kwargs.get("Delimiter")
        if delimiter == "/":
            seen, cps = set(), []
            for k in self.keys:
                rest = k[len(prefix):]
                if "/" in rest:
                    d = rest.split("/")[0]
                    if d not in seen:
                        seen.add(d)
                        cps.append({"Prefix": prefix + d + "/"})
            yield {"CommonPrefixes": cps}
            return
        yield {"Contents": [{"Key": k, "LastModified": _TS, "Size": 100}
                            for k in self.keys if k.startswith(prefix)]}


class FakeS3:
    def __init__(self, keys=None):
        self.keys = keys if keys is not None else list(ALL_KEYS)
        self.presigned = []
        self.got = []          # every get_object ATTEMPT, recorded before raising

    def get_paginator(self, _op):
        return FakePaginator(self.keys)

    # find_any_report uses the non-paginated call directly (:279).
    def list_objects_v2(self, Bucket=None, Prefix=""):
        return {"Contents": [{"Key": k, "LastModified": _TS, "Size": 100}
                             for k in self.keys if k.startswith(Prefix)]}

    def head_object(self, Bucket=None, Key=None):
        """get_dates probes one HEAD per (date, folder) pair inside a bare
        `except:`. Without this method every probe raised AttributeError,
        `dates` came back {} for EVERY scoped caller, and
        test_dates_worker_sees_only_own_dates passed vacuously (a `<=`
        subset assertion that the empty set satisfies) -- leaving the
        positive path, 'a scoped caller can still RECEIVE dates',
        completely unproven."""
        if Key not in self.keys:
            raise KeyError(Key)
        return {"ContentLength": 100, "LastModified": _TS}

    def get_object(self, Bucket=None, Key=None):
        """Body reads are RECORDED in self.got, then rejected.

        The recording is the load-bearing half. find_any_report wraps its
        body read in `except Exception: pass` and AssertionError IS an
        Exception, so the raise alone is swallowed there -- the buggy
        pre-fix code run against this fake still returned 200. Tests that
        must prove no content escaped therefore assert `fake.got == []`,
        which works through a bare except. The raise only fails loudly in
        callers that do not swallow."""
        self.got.append(Key)
        raise AssertionError(f"unexpected report body read: {Key}")

    def generate_presigned_url(self, _op, Params, ExpiresIn):
        self.presigned.append(Params["Key"])
        return "https://example.invalid/signed"


def wire(monkeypatch, keys=None):
    fake = FakeS3(keys)
    monkeypatch.setattr(fapi, "s3_client", fake)
    monkeypatch.setattr(fapi, "load_user_mapping", lambda: MAPPING)
    return fake


def body_of(res):
    return json.loads(res["body"])


def keys_of(res):
    return [r["key"] for r in body_of(res)["reports"]]


# ---------------------------------------------------------------
# S-1: get_report_history -- the deny-all regression gate.
# ---------------------------------------------------------------

def test_history_caller_with_no_accessible_folders_gets_zero_reports(monkeypatch):
    """THE REGRESSION GATE (plan requirement 3).

    A caller whose accessible-folder set is EMPTY must receive ZERO
    reports -- never the unfiltered bucket. This is the exact live prod
    leak: an Aurora-only account resolved to role='viewer',
    display_name='' -> get_accessible_users -> [] -> `if allowed_folders:`
    falsy -> whole-lake listing. If this test ever goes red, the
    overloaded sentinel has been reintroduced.
    """
    wire(monkeypatch)
    res = fapi.get_report_history({"limit": "200"}, VIEWER_CALLER)
    assert res["statusCode"] == 200
    assert body_of(res)["reports"] == []


def test_history_deny_all_is_not_the_same_value_as_unrestricted(monkeypatch):
    """The sentinel itself, asserted directly -- deny-all and unrestricted
    must never again be equal. `[] == []` was the whole bug."""
    monkeypatch.setattr(fapi, "load_user_mapping", lambda: MAPPING)
    assert fapi.accessible_folder_scope(ADMIN_CALLER) is None          # unrestricted
    assert fapi.accessible_folder_scope(VIEWER_CALLER) == set()        # deny-all
    assert fapi.accessible_folder_scope(ADMIN_CALLER) != fapi.accessible_folder_scope(VIEWER_CALLER)


def test_history_admin_still_sees_everything(monkeypatch):
    """No regression for the legacy DynamoDB accounts (blast radius)."""
    wire(monkeypatch)
    res = fapi.get_report_history({"limit": "200"}, ADMIN_CALLER)
    got = keys_of(res)
    assert "reports/2026-07-20/Otto_Other/daily_report.json" in got
    assert "reports/2026-07-20/summary_report.json" in got
    assert not any("_debug" in k for k in got)          # :800 still applies


def test_history_worker_sees_only_own_folder(monkeypatch):
    wire(monkeypatch)
    got = keys_of(fapi.get_report_history({"limit": "200"}, WORKER_CALLER))
    assert got == ["reports/2026-07-20/Ben_Test/daily_report.json"]


def test_history_site_manager_sees_self_plus_site_workers_only(monkeypatch):
    """BUG-25's rule preserved: self + role='worker' on the same site,
    never another site_manager, never another site's worker."""
    wire(monkeypatch)
    got = keys_of(fapi.get_report_history({"limit": "200"}, SITE_MANAGER_CALLER))
    assert "reports/2026-07-20/Sam_Manager/daily_report.json" in got   # self
    assert "reports/2026-07-20/Ben_Test/daily_report.json" in got      # worker, s-1
    assert "reports/2026-07-20/Ada_Worker/daily_report.json" in got    # worker, s-1
    assert "reports/2026-07-20/Otto_Other/daily_report.json" not in got   # s-2
    assert "reports/2026-07-20/summary_report.json" not in got            # ownerless


def test_history_ownerless_keys_excluded_for_scoped_callers(monkeypatch):
    """summary_report.json / sites/ have no owner folder -- a scoped
    caller must not receive them (they never matched `/{uf}/` anyway;
    this pins it so the rewrite can't relax it)."""
    wire(monkeypatch)
    got = keys_of(fapi.get_report_history({"limit": "200"}, WORKER_CALLER))
    assert not any("summary_report" in k or "/sites/" in k for k in got)


# ---------------------------------------------------------------
# S-1b (extension): find_any_report -- same idiom at :277.
# ---------------------------------------------------------------

def test_find_any_report_denies_caller_with_empty_scope(monkeypatch):
    """`accessible = get_accessible_users(caller)` then `if accessible:`
    (:277) has the identical fail-open shape -- and unlike history this
    one can return a full report BODY when exactly one key survives
    (:285-290). Deny-all must yield the 404 envelope, not a report.

    The 404 envelope alone is what catches a regression here: the body
    read sits under `except Exception: pass`, so FakeS3.get_object's
    AssertionError would be swallowed. `fake.got == []` is the assertion
    that actually proves no content was even fetched."""
    fake = wire(monkeypatch, keys=["reports/2026-07-20/Otto_Other/daily_report.json"])
    res = fapi.find_any_report("2026-07-20", VIEWER_CALLER)
    assert res["statusCode"] == 404
    assert "available_users" not in body_of(res)
    assert fake.got == []


def test_find_any_report_admin_unaffected(monkeypatch):
    wire(monkeypatch, keys=["reports/2026-07-20/Otto_Other/daily_report.json",
                            "reports/2026-07-20/Ben_Test/daily_report.json"])
    res = fapi.find_any_report("2026-07-20", ADMIN_CALLER)
    assert res["statusCode"] == 200
    assert sorted(body_of(res)["available_users"]) == ["Ben_Test", "Otto_Other"]


def test_find_any_report_admin_does_not_widen_to_unmapped_folders(monkeypatch):
    """NO SILENT WIDENING (the delta test_find_any_report_admin_unaffected
    cannot see, because both of its fixtures ARE in MAPPING).

    Before this branch admin/gm went through `get_accessible_users` too,
    which returns the config/user_mapping.json roster -- a NON-empty list
    for an admin, so the filter DID run and a folder absent from the
    mapping was dropped. If admin/gm get the bare `None` scope instead,
    this lake of exactly one unmapped folder collapses to len(reports)==1
    and find_any_report returns the full report BODY. A narrowing PR must
    not widen the one function that can serve content."""
    fake = wire(monkeypatch, keys=["reports/2026-07-20/Ben_UCPK/daily_report.json"])
    res = fapi.find_any_report("2026-07-20", ADMIN_CALLER)
    assert res["statusCode"] == 404
    assert "available_users" not in body_of(res)
    assert fake.got == []          # no body read attempted, let alone served


def test_find_any_report_admin_with_unreadable_mapping_stays_unrestricted(monkeypatch):
    """The empty-mapping edge of the parity fix, pinned deliberately.

    load_user_mapping falls back to {'mapping': {}} whenever the S3 read
    of config/user_mapping.json fails, so an admin's mapping-derived set
    can be empty for reasons that have nothing to do with authorisation.
    Turning that into deny-all would be a NEW availability regression
    caused by an unrelated S3 hiccup, and it is not what the old code did
    either (an empty list was falsy -> no filter). Admin/gm therefore fall
    back to unrestricted, exactly as before. Non-admins are unaffected:
    for them an empty set stays deny-all (see the VIEWER test above)."""
    wire(monkeypatch, keys=["reports/2026-07-20/Ben_UCPK/daily_report.json",
                            "reports/2026-07-20/Otto_Other/daily_report.json"])
    monkeypatch.setattr(fapi, "load_user_mapping", lambda: {"mapping": {}, "sites": {}})
    res = fapi.find_any_report("2026-07-20", ADMIN_CALLER)
    assert res["statusCode"] == 200
    assert sorted(body_of(res)["available_users"]) == ["Ben_UCPK", "Otto_Other"]


def test_find_any_report_worker_sees_only_own_folder(monkeypatch):
    """A scoped caller with a NON-empty scope still filters correctly --
    the fix must not turn 'restricted' into 'unrestricted' either."""
    fake = wire(monkeypatch, keys=["reports/2026-07-20/Otto_Other/daily_report.json",
                                   "reports/2026-07-20/Ada_Worker/daily_report.json"])
    res = fapi.find_any_report("2026-07-20", WORKER_CALLER)
    assert res["statusCode"] == 404
    assert fake.got == []


# ---------------------------------------------------------------
# S-2: get_presigned_url -- ownerless artifacts must fail CLOSED.
# ---------------------------------------------------------------

def test_presign_summary_report_denied_for_scoped_caller(monkeypatch):
    """reports/{date}/summary_report.json splits to length 3, so the old
    `len(key_parts) > 3` guard (:396) left candidate=None -> target_user
    =None -> the `and` at :400 short-circuited and NO check ran. 22 such
    objects exist on prod, each a whole-company daily rollup."""
    fake = wire(monkeypatch)
    res = fapi.get_presigned_url(
        {"key": "reports/2026-07-20/summary_report.json"}, WORKER_CALLER)
    assert res["statusCode"] == 403
    assert fake.presigned == []          # never even reached the signer


def test_presign_site_rollup_denied_for_scoped_caller(monkeypatch):
    """'sites' was explicitly excluded at :397 -- 14 such objects on prod."""
    fake = wire(monkeypatch)
    res = fapi.get_presigned_url(
        {"key": "reports/2026-07-20/sites/s-1/site_report.json"}, SITE_MANAGER_CALLER)
    assert res["statusCode"] == 403
    assert fake.presigned == []


def test_presign_bare_prefix_denied_for_scoped_caller(monkeypatch):
    """'users/' alone has no owner segment -- fail closed by the same rule."""
    fake = wire(monkeypatch)
    assert fapi.get_presigned_url({"key": "users/"}, WORKER_CALLER)["statusCode"] == 403
    assert fake.presigned == []


def test_presign_date_prefix_only_denied_for_scoped_caller(monkeypatch):
    """reports/{date} (length 2) -- also ownerless, also denied."""
    fake = wire(monkeypatch)
    assert fapi.get_presigned_url(
        {"key": "reports/2026-07-20"}, WORKER_CALLER)["statusCode"] == 403
    assert fake.presigned == []


def test_presign_own_daily_report_still_works(monkeypatch):
    """DO NOT REGRESS: the per-user path already worked correctly."""
    fake = wire(monkeypatch)
    res = fapi.get_presigned_url(
        {"key": "reports/2026-07-20/Ben_Test/daily_report.json"}, WORKER_CALLER)
    assert res["statusCode"] == 200
    assert body_of(res)["url"].startswith("https://")
    assert fake.presigned == ["reports/2026-07-20/Ben_Test/daily_report.json"]


def test_presign_other_users_daily_report_still_denied(monkeypatch):
    fake = wire(monkeypatch)
    res = fapi.get_presigned_url(
        {"key": "reports/2026-07-20/Otto_Other/daily_report.json"}, WORKER_CALLER)
    assert res["statusCode"] == 403
    assert fake.presigned == []


def test_presign_own_media_key_still_works(monkeypatch):
    wire(monkeypatch)
    res = fapi.get_presigned_url(
        {"key": "users/Ben_Test/pictures/2026-07-20/a.jpg"}, WORKER_CALLER)
    assert res["statusCode"] == 200


def test_presign_other_users_media_key_still_denied(monkeypatch):
    fake = wire(monkeypatch)
    res = fapi.get_presigned_url(
        {"key": "users/Otto_Other/pictures/2026-07-20/a.jpg"}, WORKER_CALLER)
    assert res["statusCode"] == 403
    assert fake.presigned == []


def test_presign_ownerless_allowed_for_admin(monkeypatch):
    """Blast radius: admin/gm keep full reach -- they bypass the whole
    block at :386 and always did."""
    wire(monkeypatch)
    res = fapi.get_presigned_url(
        {"key": "reports/2026-07-20/summary_report.json"}, ADMIN_CALLER)
    assert res["statusCode"] == 200


def test_presign_absent_caller_denied_for_ownerless_key(monkeypatch):
    """ABSENT IDENTITY == UNRESTRICTED, the very shape this branch set out
    to abolish, still living in the function it just hardened: the guard
    read `if caller and caller.get('role') not in ('admin','gm')`, so
    caller=None skipped the ENTIRE permission block and got a signed URL
    for a whole-company rollup. Unreachable from lambda_handler today
    (it always passes a dict), which is exactly why it needs a test."""
    fake = wire(monkeypatch)
    res = fapi.get_presigned_url({"key": "reports/2026-07-20/summary_report.json"}, None)
    assert res["statusCode"] == 403
    assert fake.presigned == []


def test_presign_absent_caller_denied_for_owned_key(monkeypatch):
    """The same rule for a key whose owner IS derivable: with no identity
    there is nothing to authorise against, so deny (and never reach
    can_access_user_data, which would dereference caller['role'])."""
    fake = wire(monkeypatch)
    res = fapi.get_presigned_url(
        {"key": "users/Ben_Test/pictures/2026-07-20/a.jpg"}, None)
    assert res["statusCode"] == 403
    assert fake.presigned == []


def test_presign_disallowed_prefix_still_403(monkeypatch):
    wire(monkeypatch)
    assert fapi.get_presigned_url(
        {"key": "config/user_mapping.json"}, ADMIN_CALLER)["statusCode"] == 403


# ---------------------------------------------------------------
# S-3: get_dates -- same falsy-empty-list idiom at :331 and :350.
# ---------------------------------------------------------------

def test_dates_site_filter_with_no_accessible_users_returns_empty(monkeypatch):
    """`elif site:` yields [] when the caller can reach nobody on that
    site; `if user_folders:` was then falsy and EVERY date got marked
    hasReport=True, then enriched from the unscoped summary_report.json --
    leaking company-wide topic and safety counts per day."""
    wire(monkeypatch)
    res = fapi.get_dates({"months": "2", "site": "s-2"}, SITE_MANAGER_CALLER)
    assert res["statusCode"] == 200
    assert body_of(res)["dates"] == {}


def test_dates_admin_unfiltered_unchanged(monkeypatch):
    """Blast radius: admin/gm keep the union-across-all-users behaviour."""
    wire(monkeypatch)
    dates = body_of(fapi.get_dates({"months": "2"}, ADMIN_CALLER))["dates"]
    assert "2026-07-20" in dates
    assert dates["2026-07-20"]["hasReport"] is True


def test_dates_viewer_with_no_mapping_returns_empty(monkeypatch):
    """The final `else` branch currently yields [''] -- truthy, so it
    fails closed by ACCIDENT. Pin the intent so a future 'clean up the
    empty string' commit can't turn luck into a leak."""
    wire(monkeypatch)
    assert body_of(fapi.get_dates({"months": "2"}, VIEWER_CALLER))["dates"] == {}


def test_dates_worker_sees_only_own_dates(monkeypatch):
    """THE POSITIVE PATH (previously unproven anywhere in the suite).

    FakeS3 used to define no head_object, so every scoped lookup
    AttributeError'd inside get_dates' bare `except:`, `dates` was always
    {}, and `set(dates.keys()) <= {"2026-07-20"}` was satisfied by the
    empty set -- the test could not tell 'correctly scoped' from
    'totally broken'. Equality, so a scoped caller must actually RECEIVE
    its own date."""
    wire(monkeypatch)
    dates = body_of(fapi.get_dates({"months": "2"}, WORKER_CALLER))["dates"]
    assert set(dates.keys()) == {"2026-07-20"}
    assert dates["2026-07-20"]["hasReport"] is True


def test_dates_admin_site_filter_with_no_mapped_users_returns_empty(monkeypatch):
    """LIVE BEHAVIOUR CHANGE, pinned deliberately (correct, keep it).

    `elif site:` is evaluated BEFORE `elif role in ('admin','gm')`, so an
    admin passing ?site= takes the scoped branch. get_accessible_users
    filters against config/user_mapping.json's legacy-only site ids, so an
    Aurora-provisioned site yields [] -> the new deny-all early return ->
    {} , where before it fell through the falsy-empty-list hole and
    returned EVERY date enriched from the unscoped summary rollup. An
    admin asking about a site the legacy mapping has never heard of gets
    nothing rather than everything."""
    wire(monkeypatch)
    res = fapi.get_dates({"months": "2", "site": "s-aurora-99"}, ADMIN_CALLER)
    assert res["statusCode"] == 200
    assert body_of(res)["dates"] == {}
