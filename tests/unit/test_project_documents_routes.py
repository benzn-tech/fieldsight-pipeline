"""The project-document routes: who may write (R5), keys under the SITE's company (R6),
archived projects (R8), the flag-off 404, through dispatch (so the route order is under
test too) and a fake s3. The SQL is pinned in tests/integration/test_project_docs_pg.py."""
import contextlib
import json
from datetime import datetime, timedelta, timezone

import pytest

oa = pytest.importorskip("lambda_org_api", reason="requires psycopg (installed in CI)")
from botocore.exceptions import ClientError  # noqa: E402

HOME = "c0000000-0000-0000-0000-000000000001"
OTHER = "c0000000-0000-0000-0000-000000000002"
SITE = "51000000-0000-0000-0000-000000000001"        # HOME's project
EXT = "52000000-0000-0000-0000-000000000002"         # OTHER's project (external reach)
DOC = "d0000000-0000-0000-0000-000000000001"
NOW = datetime(2026, 10, 16, tzinfo=timezone.utc)

GM = {"id": "u-gm", "company_id": HOME, "global_role": "gm", "cognito_sub": "s-gm"}
PM = {"id": "u-pm", "company_id": HOME, "global_role": "pm", "cognito_sub": "s-pm"}
SM = {"id": "u-sm", "company_id": HOME, "global_role": "site_manager", "cognito_sub": "s-sm"}
WORKER = {"id": "u-wk", "company_id": HOME, "global_role": "worker", "cognito_sub": "s-wk"}
ADMIN = {"id": "u-ad", "company_id": HOME, "global_role": "admin", "cognito_sub": "s-ad"}
OTHER_ADMIN = {"id": "u-oa", "company_id": OTHER, "global_role": "admin", "cognito_sub": "s-oa"}
PLATFORM = {"id": "u-pa", "company_id": "c-op", "global_role": "platform_admin",
            "cognito_sub": "s-pa"}
SITES = {SITE: {"id": SITE, "company_id": HOME, "archived_at": None},
         EXT: {"id": EXT, "company_id": OTHER, "archived_at": None}}


def _missing():
    return ClientError({"Error": {"Code": "404", "Message": "missing"}}, "HeadObject")


class _S3:
    def __init__(self):
        self.objects, self.puts, self.presigned = {}, {}, []

    def generate_presigned_url(self, op, Params, ExpiresIn):
        self.presigned.append((op, Params, ExpiresIn))
        return "https://s3.example/%s/%s" % (op, Params["Key"])

    def head_object(self, Bucket, Key):
        if Key not in self.objects:
            raise _missing()
        return {"ContentLength": self.objects[Key]}

    def put_object(self, Bucket, Key, Body, **kw):
        self.puts[Key] = json.loads(Body)
        return {}


class _Conn:
    """The request's connection, only as far as `transaction()`: the repository's writes
    take advisory / row locks that hold only inside a transaction, so every write must
    run inside one (the _Docs writers assert it)."""

    def __init__(self):
        self.in_tx = 0

    @contextlib.contextmanager
    def transaction(self):
        self.in_tx += 1
        try:
            yield
        finally:
            self.in_tx -= 1


def _in_tx(conn):
    assert conn.in_tx, "a project_docs write ran outside conn.transaction()"


class _Docs:
    """In-memory stand-in with the repository's signatures."""

    def __init__(self):
        self.rows = {}

    def get_document(self, conn, doc_id):
        return self.rows.get(str(doc_id))

    def insert_document(self, conn, *, doc_id, site_id, company_id, title, filename,
                        source_key, content_type, uploaded_by, series_id=None):
        _in_tx(conn)
        if series_id is not None:
            live = [r for r in self.rows.values() if r["series_id"] == series_id
                    and r["site_id"] == site_id and r["deleted_at"] is None]
            if not live:
                return None
            rev = max(r["revision"] for r in self.rows.values()
                      if r["series_id"] == series_id) + 1
        else:
            series_id, rev = "5e000000-0000-0000-0000-00000000000%d" % (len(self.rows) + 1), 1
        row = dict(id=doc_id, company_id=company_id, site_id=site_id, series_id=series_id,
                   revision=rev, title=title, filename=filename, source_key=source_key,
                   content_type=content_type, status="pending", reason=None, pages=None,
                   chars=None, chunks=None, uploaded_by=uploaded_by, uploaded_by_name="G M",
                   created_at=NOW, requested_at=NOW, indexed_at=None, superseded_at=None,
                   deleted_at=None)
        self.rows[doc_id] = row
        return row

    def list_current(self, conn, site_id):
        return [r for r in self.rows.values() if r["site_id"] == site_id and not r["deleted_at"]]

    def list_series(self, conn, site_id, series_id):
        return [r for r in self.list_current(conn, site_id) if r["series_id"] == series_id]

    def rename(self, conn, doc_id, title):
        _in_tx(conn)
        self.rows[doc_id]["title"] = title
        return True

    def soft_delete(self, conn, doc_id):
        _in_tx(conn)
        self.rows[doc_id]["deleted_at"] = NOW
        return True

    def mark_pending(self, conn, doc_id):
        """As the repository: False for an indexed or deleted document."""
        _in_tx(conn)
        row = self.rows[doc_id]
        if row["status"] == "indexed" or row["deleted_at"] is not None:
            return False
        row.update(status="pending", reason=None, requested_at=NOW)
        return True


@pytest.fixture
def world(monkeypatch):
    fake, docs, recorded = _S3(), _Docs(), []
    monkeypatch.setattr(oa, "s3", lambda: fake)
    monkeypatch.setattr(oa, "LAKE_BUCKET", "lake")
    monkeypatch.setenv("PROJECT_DOCUMENTS", "true")
    monkeypatch.setattr(oa, "project_docs", docs)
    monkeypatch.setattr(oa.sites, "get_site", lambda conn, sid: SITES.get(str(sid)))
    reach = {"u-gm": {SITE}, "u-pm": {SITE}, "u-sm": {SITE}, "u-wk": {SITE},
             "u-pa": {SITE, EXT}, "u-ad": {SITE}, "u-oa": {EXT}}
    ext_roles = {}
    monkeypatch.setattr(oa, "_allowed_site_ids", lambda conn, caller: reach[caller["id"]])
    monkeypatch.setattr(oa.memberships, "external_site_roles",
                        lambda conn, uid: ext_roles.get(uid, {}))
    monkeypatch.setattr(oa.lake_objects, "record_best_effort",
                        lambda conn, key, **kw: recorded.append((key, kw)) or True)
    return {"s3": fake, "docs": docs, "reach": reach, "ext": ext_roles, "recorded": recorded}


def _routed(monkeypatch, caller, method, path, body=None, qs=None):
    monkeypatch.setattr(oa.users, "get_user_by_sub", lambda conn, sub: dict(caller))
    monkeypatch.setattr(oa.device_heartbeat, "parse_headers", lambda h: None)
    monkeypatch.setattr(oa.device_heartbeat, "record", lambda *a, **kw: None)
    ev = {"httpMethod": method, "path": "/api/org" + path, "queryStringParameters": qs,
          "body": json.dumps(body) if body is not None else None,
          "requestContext": {"authorizer": {"claims": {"sub": caller["cognito_sub"]}}}}
    resp = oa.dispatch(_Conn(), ev, method, path)
    return resp["statusCode"], json.loads(resp["body"])


def _base(site=SITE):
    return f"/sites/{site}/documents"


def _upload(w, monkeypatch, caller=GM, site=SITE, ext="pdf", head_size=1000, **body):
    """upload-url (declared size 1000), the PUT (simulated: the object exists with
    `head_size` bytes -- S3 does not hold the uploader to the declared size), then create."""
    st, up = _routed(monkeypatch, caller, "POST", _base(site) + "/upload-url",
                     {"filename": "Spec." + ext, "contentType": oa.project_doc_types.EXT_TYPES[ext],
                      "size": 1000})
    assert st == 200, up
    w["s3"].objects[up["sourceKey"]] = head_size
    payload = {"sourceKey": up["sourceKey"], "filename": "Spec." + ext}
    payload.update(body)
    return _routed(monkeypatch, caller, "POST", _base(site), payload), up


# ---- the switch ----------------------------------------------------------------------

@pytest.mark.parametrize("method,suffix,body", [
    ("GET", "", None),
    ("POST", "/upload-url", {"filename": "a.pdf", "contentType": "application/pdf", "size": 1}),
    ("POST", "", {"sourceKey": "x", "filename": "a.pdf"}),
    ("PATCH", "/" + DOC, {"title": "x"}),
    ("DELETE", "/" + DOC, None),
    ("GET", "/" + DOC + "/file", None),
    ("POST", "/" + DOC + "/retry", None),
])
def test_THE_off_means_404_not_found_on_every_route_and_no_s3_call(world, monkeypatch, method,
                                                                    suffix, body):
    monkeypatch.setenv("PROJECT_DOCUMENTS", "false")
    st, out = _routed(monkeypatch, GM, method, _base() + suffix, body)
    assert (st, out) == (404, {"error": "not found"})
    assert world["s3"].presigned == [] and world["s3"].puts == {}


# ---- upload --------------------------------------------------------------------------

def test_upload_writes_the_row_the_request_and_the_lake_index_under_the_sites_company(world, monkeypatch):
    (st, out), up = _upload(world, monkeypatch)
    assert st == 202 and out["status"] == "pending" and out["revision"] == 1
    assert up["sourceKey"] == f"project_doc_sources/{HOME}/{up['documentId']}.pdf"
    op, params, expires = world["s3"].presigned[0]
    assert (op, params["ContentType"], expires) == ("put_object", "application/pdf", 900)
    req = world["s3"].puts[f"project_doc_requests/{HOME}/{out['id']}.json"]
    assert req["id"] == out["id"] == up["documentId"]
    assert (req["companyId"], req["siteId"], req["sourceKey"], req["filename"]) == (
        HOME, SITE, up["sourceKey"], "Spec.pdf")
    kinds = {k: kw["kind"] for k, kw in world["recorded"]}
    assert kinds[up["sourceKey"]] == "project_doc_source"
    assert kinds[f"project_doc_vectors/{HOME}/{out['id']}.json"] == "project_doc"
    assert all(kw["site_id"] == SITE and kw["company_id"] == HOME for _, kw in world["recorded"])


def test_a_platform_admin_uploading_to_another_companys_project_uses_that_company(world, monkeypatch):
    (st, out), up = _upload(world, monkeypatch, caller=PLATFORM, site=EXT)
    assert st == 202 and up["sourceKey"].startswith(f"project_doc_sources/{OTHER}/")
    assert world["docs"].rows[out["id"]]["company_id"] == OTHER


@pytest.mark.parametrize("payload,why", [
    ({"filename": "plan.dwg", "contentType": "application/octet-stream", "size": 1}, "Upload a Word"),
    ({"filename": "a.pdf", "contentType": "text/plain", "size": 1}, "contentType"),
    ({"filename": "a.pdf", "contentType": "application/pdf", "size": 50 * 1024 * 1024 + 1}, "50 MB"),
    ({"filename": "a.pdf", "contentType": "application/pdf"}, "size"),
])
def test_upload_url_refuses_before_any_signature(world, monkeypatch, payload, why):
    st, out = _routed(monkeypatch, GM, "POST", _base() + "/upload-url", payload)
    assert st == 400 and why in out["error"] and world["s3"].presigned == []


def test_a_key_of_another_company_is_refused(world, monkeypatch):
    key = f"project_doc_sources/{OTHER}/{DOC}.pdf"
    world["s3"].objects[key] = 10
    st, out = _routed(monkeypatch, GM, "POST", _base(), {"sourceKey": key, "filename": "a.pdf"})
    assert st == 400 and world["docs"].rows == {}


def test_a_missing_upload_is_a_404_that_says_so(world, monkeypatch):
    key = f"project_doc_sources/{HOME}/{DOC}.pdf"
    st, out = _routed(monkeypatch, GM, "POST", _base(), {"sourceKey": key, "filename": "a.pdf"})
    assert st == 404 and "upload it again" in out["error"]


def test_an_oversized_upload_is_refused_at_head(world, monkeypatch):
    (st, out), _ = _upload(world, monkeypatch, head_size=50 * 1024 * 1024 + 1)
    assert st == 400 and "50 MB" in out["error"]


def test_the_same_key_twice_is_a_409(world, monkeypatch):
    (st, out), up = _upload(world, monkeypatch)
    st2, out2 = _routed(monkeypatch, GM, "POST", _base(),
                        {"sourceKey": up["sourceKey"], "filename": "Spec.pdf"})
    assert st2 == 409


def test_a_new_revision_counts_up_and_a_gone_series_is_404(world, monkeypatch):
    (st, first), _ = _upload(world, monkeypatch)
    (st, second), _ = _upload(world, monkeypatch, seriesId=first["seriesId"])
    assert st == 202 and second["revision"] == 2 and second["seriesId"] == first["seriesId"]
    for r in world["docs"].rows.values():
        r["deleted_at"] = NOW
    (st, out), _ = _upload(world, monkeypatch, seriesId=first["seriesId"])
    assert st == 404 and out["error"] == "document not found"


# ---- who may write (R5) --------------------------------------------------------------

@pytest.mark.parametrize("caller,expected", [(GM, 200), (PM, 200), (SM, 403), (WORKER, 403)])
def test_home_company_writes_follow_the_global_role(world, monkeypatch, caller, expected):
    st, out = _routed(monkeypatch, caller, "POST", _base() + "/upload-url",
                      {"filename": "a.pdf", "contentType": "application/pdf", "size": 1})
    assert st == expected
    if expected == 403:
        assert "managers" in out["error"] and world["s3"].presigned == []


@pytest.mark.parametrize("membership,expected", [("pm", 200), ("site_manager", 403), ("worker", 403)])
def test_on_an_external_project_the_membership_role_decides(world, monkeypatch, membership, expected):
    world["reach"]["u-wk"] = {SITE, EXT}
    world["ext"]["u-wk"] = {EXT: membership}
    st, _ = _routed(monkeypatch, WORKER, "POST", _base(EXT) + "/upload-url",
                    {"filename": "a.pdf", "contentType": "application/pdf", "size": 1})
    assert st == expected


def test_a_home_gm_is_not_a_writer_on_another_companys_project_without_pm_membership(world, monkeypatch):
    world["reach"]["u-gm"] = {SITE, EXT}
    world["ext"]["u-gm"] = {EXT: "site_manager"}
    st, _ = _routed(monkeypatch, GM, "POST", _base(EXT) + "/upload-url",
                    {"filename": "a.pdf", "contentType": "application/pdf", "size": 1})
    assert st == 403


def test_reads_are_for_anyone_who_sees_the_project(world, monkeypatch):
    _upload(world, monkeypatch)
    st, out = _routed(monkeypatch, WORKER, "GET", _base())
    assert st == 200 and len(out["documents"]) == 1 and out["canWrite"] is False
    st, out = _routed(monkeypatch, GM, "GET", _base())
    assert out["canWrite"] is True
    d = out["documents"][0]
    assert set(d) == {"id", "seriesId", "revision", "title", "filename", "contentType",
                      "status", "reason", "pages", "chars", "chunks", "uploadedBy",
                      "uploadedByName", "createdAt", "requestedAt", "indexedAt", "supersededAt"}
    assert "T" in d["createdAt"] and "T" in d["requestedAt"]   # Safari parses only ISO


def test_a_project_out_of_reach_is_404_for_reads_and_writes(world, monkeypatch):
    st, out = _routed(monkeypatch, GM, "GET", _base(EXT))
    assert (st, out["error"]) == (404, "project not found")
    st, out = _routed(monkeypatch, GM, "POST", _base(EXT) + "/upload-url",
                      {"filename": "a.pdf", "contentType": "application/pdf", "size": 1})
    assert st == 404


def test_an_archived_project_is_read_only_for_its_writers(world, monkeypatch):
    SITES[SITE]["archived_at"] = NOW
    try:
        world["reach"]["u-gm"] = set()          # visible_scope drops archived sites
        st, out = _routed(monkeypatch, GM, "POST", _base() + "/upload-url",
                          {"filename": "a.pdf", "contentType": "application/pdf", "size": 1})
        assert (st, out["error"]) == (409, "This project is archived")
        st, _ = _routed(monkeypatch, WORKER, "POST", _base() + "/upload-url",
                        {"filename": "a.pdf", "contentType": "application/pdf", "size": 1})
        assert st == 403                         # not a writer: no archive hint, no write
        assert world["s3"].presigned == []
    finally:
        SITES[SITE]["archived_at"] = None


# ---- history, rename, delete, file, retry --------------------------------------------

def test_history_lists_a_series_and_an_unknown_series_is_404(world, monkeypatch):
    (_, first), _ = _upload(world, monkeypatch)
    _upload(world, monkeypatch, seriesId=first["seriesId"])
    st, out = _routed(monkeypatch, WORKER, "GET", _base(), qs={"series": first["seriesId"]})
    assert st == 200 and sorted(d["revision"] for d in out["documents"]) == [1, 2]
    st, _ = _routed(monkeypatch, WORKER, "GET", _base(), qs={"series": DOC})
    assert st == 404


def test_rename_and_delete(world, monkeypatch):
    (_, doc), _ = _upload(world, monkeypatch)
    st, out = _routed(monkeypatch, PM, "PATCH", f"{_base()}/{doc['id']}", {"title": "Structural"})
    assert st == 200 and world["docs"].rows[doc["id"]]["title"] == "Structural"
    st, _ = _routed(monkeypatch, PM, "PATCH", f"{_base()}/{doc['id']}", {"title": "  "})
    assert st == 400
    st, _ = _routed(monkeypatch, WORKER, "DELETE", f"{_base()}/{doc['id']}")
    assert st == 403
    st, out = _routed(monkeypatch, PM, "DELETE", f"{_base()}/{doc['id']}")
    assert st == 200 and world["docs"].rows[doc["id"]]["deleted_at"] is not None
    st, _ = _routed(monkeypatch, PM, "DELETE", f"{_base()}/{doc['id']}")
    assert st == 404


def test_a_document_of_another_project_is_404_through_this_one(world, monkeypatch):
    (_, doc), _ = _upload(world, monkeypatch, caller=PLATFORM, site=EXT)
    st, _ = _routed(monkeypatch, PLATFORM, "GET", f"{_base(SITE)}/{doc['id']}/file")
    assert st == 404


def test_file_presign_names_the_original_and_its_type(world, monkeypatch):
    """Review Focus 2: a presign without a disposition downloads as <uuid>.pdf."""
    (_, doc), up = _upload(world, monkeypatch)
    world["s3"].presigned.clear()
    st, out = _routed(monkeypatch, WORKER, "GET", f"{_base()}/{doc['id']}/file")
    assert st == 200 and out["expiresIn"] == 3600 and out["contentType"] == "application/pdf"
    op, params, expires = world["s3"].presigned[0]
    assert op == "get_object" and params["Key"] == up["sourceKey"] and expires == 3600
    assert params["ResponseContentType"] == "application/pdf"
    assert 'filename="Spec.pdf"' in params["ResponseContentDisposition"]
    assert params["ResponseContentDisposition"].startswith("inline")


def test_retry_only_for_a_failed_or_stuck_document(world, monkeypatch):
    (_, doc), _ = _upload(world, monkeypatch)
    row = world["docs"].rows[doc["id"]]
    row["requested_at"] = datetime.now(timezone.utc)
    st, out = _routed(monkeypatch, GM, "POST", f"{_base()}/{doc['id']}/retry")
    assert st == 409                                   # pending, not yet stuck
    row["requested_at"] = datetime.now(timezone.utc) - timedelta(minutes=21)
    world["s3"].puts.clear()
    st, out = _routed(monkeypatch, GM, "POST", f"{_base()}/{doc['id']}/retry")
    assert st == 202 and f"project_doc_requests/{HOME}/{doc['id']}.json" in world["s3"].puts
    row.update(status="error", reason="x")
    st, _ = _routed(monkeypatch, GM, "POST", f"{_base()}/{doc['id']}/retry")
    assert st == 202 and row["status"] == "pending"
    row.update(status="unreadable", reason="protected")
    world["s3"].puts.clear()
    st, out = _routed(monkeypatch, GM, "POST", f"{_base()}/{doc['id']}/retry")
    assert (st, out["error"]) == (
        409, "This document can't be read as is — upload a new revision.")
    assert row["status"] == "unreadable" and world["s3"].puts == {}   # R9: a new revision
    row["status"] = "indexed"
    st, out = _routed(monkeypatch, GM, "POST", f"{_base()}/{doc['id']}/retry")
    assert (st, out["error"]) == (409, "This document is already indexed.")
    st, _ = _routed(monkeypatch, WORKER, "POST", f"{_base()}/{doc['id']}/retry")
    assert st == 403


def test_a_retry_that_loses_the_race_to_indexing_is_a_409_and_writes_no_request(world, monkeypatch):
    """The row read says error, but it was indexed before mark_pending's guarded UPDATE:
    mark_pending answers False, and a Retry must not re-request a searchable document."""
    (_, doc), _ = _upload(world, monkeypatch)
    row = world["docs"].rows[doc["id"]]
    row.update(status="error", reason="x")
    real_get = world["docs"].get_document

    def get_then_index(conn, doc_id):
        found = dict(real_get(conn, doc_id))
        row["status"] = "indexed"
        return found
    monkeypatch.setattr(world["docs"], "get_document", get_then_index)
    world["s3"].puts.clear()
    st, out = _routed(monkeypatch, GM, "POST", f"{_base()}/{doc['id']}/retry")
    assert (st, out["error"]) == (409, "This document is already indexed.")
    assert world["s3"].puts == {}


def _archived_reads(monkeypatch, caller, doc):
    return [_routed(monkeypatch, caller, "GET", _base())[0],
            _routed(monkeypatch, caller, "GET", _base(), qs={"series": doc["seriesId"]})[0],
            _routed(monkeypatch, caller, "GET", f"{_base()}/{doc['id']}/file")[0]]


def test_an_archived_project_is_readable_not_writable_for_those_who_see_it(world, monkeypatch):
    """R8: read-only, not hidden -- for exactly whom list_org_sites(include_archived) shows
    the archived project to (GRADED_ROLES off, same-company admin/gm): list, history and
    the file with canWrite false, even though the site has left their reach."""
    monkeypatch.setattr(oa, "GRADED_ROLES", False)
    (_, doc), _ = _upload(world, monkeypatch)
    SITES[SITE]["archived_at"] = NOW
    try:
        for uid in world["reach"]:
            world["reach"][uid] = set()
        for caller in (GM, ADMIN):
            st, out = _routed(monkeypatch, caller, "GET", _base())
            assert st == 200 and out["canWrite"] is False and len(out["documents"]) == 1
            st, out = _routed(monkeypatch, caller, "GET", _base(), qs={"series": doc["seriesId"]})
            assert st == 200 and len(out["documents"]) == 1
            st, out = _routed(monkeypatch, caller, "GET", f"{_base()}/{doc['id']}/file")
            assert st == 200 and out["url"].startswith("https://s3.example/get_object/")
            st, out = _routed(monkeypatch, caller, "POST", f"{_base()}/{doc['id']}/retry")
            assert (st, out["error"]) == (409, "This project is archived")
    finally:
        SITES[SITE]["archived_at"] = None


def test_an_archived_project_stays_404_for_those_who_cannot_see_it(world, monkeypatch):
    """No sites listing shows platform_admin an archived project, nor anyone below
    admin/gm, nor another company's admin: 404 on list, history and file."""
    monkeypatch.setattr(oa, "GRADED_ROLES", False)
    (_, doc), _ = _upload(world, monkeypatch)
    SITES[SITE]["archived_at"] = NOW
    try:
        for caller in (PLATFORM, WORKER, SM, PM, OTHER_ADMIN):
            assert _archived_reads(monkeypatch, caller, doc) == [404, 404, 404], caller["id"]
    finally:
        SITES[SITE]["archived_at"] = None


def test_with_graded_roles_an_archived_project_is_404_for_every_reader(world, monkeypatch):
    """GRADED_ROLES on: list_org_sites lists visible_scope, which drops archived sites for
    every role, so the documents are 404 for admin/gm too; writes still say archived."""
    monkeypatch.setattr(oa, "GRADED_ROLES", True)
    (_, doc), _ = _upload(world, monkeypatch)
    SITES[SITE]["archived_at"] = NOW
    try:
        for caller in (GM, ADMIN, PLATFORM):
            assert _archived_reads(monkeypatch, caller, doc) == [404, 404, 404], caller["id"]
        st, out = _routed(monkeypatch, GM, "POST", f"{_base()}/{doc['id']}/retry")
        assert (st, out["error"]) == (409, "This project is archived")
    finally:
        SITES[SITE]["archived_at"] = None


@pytest.mark.parametrize("method,suffix,status,message", [
    ("GET", "/not-a-uuid", 404, "document not found"),
    ("GET", "/" + DOC + "/nope", 404, "document not found"),
    ("GET", "/" + DOC, 405, "method not allowed"),
    ("POST", "/" + DOC + "/file", 405, "method not allowed"),
    ("PUT", "", 405, "method not allowed"),
    ("GET", "/upload-url", 405, "method not allowed"),
])
def test_with_the_flag_on_a_fall_through_never_says_the_bare_not_found(
        world, monkeypatch, method, suffix, status, message):
    """The UI reads `{"error": "not found"}` as "feature off" and hides the section: a bad
    path or id under a real project must answer something else."""
    st, out = _routed(monkeypatch, GM, method, _base() + suffix)
    assert (st, out) == (status, {"error": message})


def test_an_external_pm_reads_and_writes_the_other_companys_project(world, monkeypatch):
    world["reach"]["u-wk"] = {SITE, EXT}
    world["ext"]["u-wk"] = {EXT: "pm"}
    (st, out), up = _upload(world, monkeypatch, caller=WORKER, site=EXT)
    assert st == 202 and up["sourceKey"].startswith(f"project_doc_sources/{OTHER}/")
    assert world["docs"].rows[out["id"]]["company_id"] == OTHER
    st, listed = _routed(monkeypatch, WORKER, "GET", _base(EXT))
    assert st == 200 and listed["canWrite"] is True and len(listed["documents"]) == 1
    st, home = _routed(monkeypatch, WORKER, "GET", _base(SITE))
    assert st == 200 and home["canWrite"] is False      # home role still decides at home


def test_a_concurrent_second_create_of_the_same_upload_is_a_409(world, monkeypatch):
    """Two creates racing past the get_document check: the primary key refuses the second."""
    (_, doc), up = _upload(world, monkeypatch)
    monkeypatch.setattr(world["docs"], "get_document", lambda conn, doc_id: None)

    def dup(conn, **kw):
        raise oa.UniqueViolation("duplicate key")
    monkeypatch.setattr(world["docs"], "insert_document", dup)
    st, out = _routed(monkeypatch, GM, "POST", _base(),
                      {"sourceKey": up["sourceKey"], "filename": "Spec.pdf"})
    assert (st, out["error"]) == (409, "This file was already added.")
