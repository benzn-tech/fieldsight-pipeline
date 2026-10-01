"""Unit: voiceprint identity fields, merge-check, merge and rename (spec 2026-10-01).

What a connection double can and cannot prove here: it proves routing, the gates, validation,
the 409 and that a failure UNWINDS the transaction instead of returning from inside it (a
`return error()` inside `with conn:` commits). It proves nothing about the SQL itself, so the
statements' text and order are pinned below and the real statements run in
tests/integration/test_voiceprint_merge.py.
"""
import json

import pytest

org = pytest.importorskip("lambda_org_api", reason="requires psycopg (installed in CI)")

CALLER = {
    "id": "u-1", "cognito_sub": "sub-1", "company_id": "c-1",
    "email": "a@x.nz", "first_name": "Ada", "last_name": "L", "folder_name": "Ada_L",
    "avatar_s3_key": None, "global_role": "admin", "created_at": "2026-08-13",
}
SRC = "11111111-1111-4111-8111-111111111111"
TGT = "22222222-2222-4222-8222-222222222222"


class FakeConn:
    """Records how each `conn.transaction()` block ended."""

    def __init__(self):
        self.tx = []        # None = committed/released, else the exception type

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def transaction(self):
        outer = self

        class _Tx:
            def __enter__(self):
                return self

            def __exit__(self, et, ev, tb):
                outer.tx.append(et)
                return False
        return _Tx()

    def cursor(self, row_factory=None):
        return self

    def execute(self, sql, params=None):
        return self

    def fetchone(self):
        return None

    def fetchall(self):
        return []


def _ev(method, path, body=None, query=None):
    return {"httpMethod": method, "path": f"/api/org{path}", "queryStringParameters": query,
            "body": json.dumps(body) if body is not None else None,
            "requestContext": {"authorizer": {"claims": {"sub": "sub-1"}}}}


def _b(res):
    return json.loads(res["body"])


@pytest.fixture
def wired(monkeypatch):
    conn = FakeConn()
    calls = {"merge": [], "rename": []}
    monkeypatch.setattr(org, "SPEAKER_IDENTITY_MODE", "shadow")
    monkeypatch.setattr(org, "get_connection", lambda *a, **k: conn)
    monkeypatch.setattr(org.users, "get_user_by_sub", lambda c, sub: dict(CALLER))
    profiles = {SRC: {"id": SRC, "display_name": "Ben Lin", "status": "confirmed"},
                TGT: {"id": TGT, "display_name": "Ben Lin", "status": "confirmed"}}
    monkeypatch.setattr(org.voiceprints, "get_profile",
                        lambda c, co, vid: profiles.get(vid))
    state = {"verdict": "alike"}
    monkeypatch.setattr(
        org.voiceprints, "merge_check",
        lambda c, co, s, t: {"verdict": state["verdict"], "similarity": 0.2718,
                             "source_samples": 3, "target_samples": 3})

    def merge(c, co, s, t, by):
        calls["merge"].append((co, s, t, by))
        if state.get("boom"):
            raise RuntimeError("db down")
        return {"samplesMoved": 4, "samplesDropped": 1}
    monkeypatch.setattr(org.voiceprints, "merge_profiles", merge)

    def rename(c, co, vid, name):
        calls["rename"].append((co, vid, name))
        return vid in profiles and profiles[vid]["status"] != "withdrawn"
    monkeypatch.setattr(org.voiceprints, "rename_profile", rename)
    return conn, calls, profiles, state


# ---- gates -------------------------------------------------------------------------------

ROUTES = [("GET", f"/voiceprints/{SRC}/merge-check", None, {"into": TGT}),
          ("POST", f"/voiceprints/{SRC}/merge", {"into": TGT}, None),
          ("PATCH", f"/voiceprints/{SRC}", {"displayName": "Ben"}, None)]


@pytest.mark.parametrize("method,path,body,query", ROUTES)
def test_off_means_404(wired, monkeypatch, method, path, body, query):
    monkeypatch.setattr(org, "SPEAKER_IDENTITY_MODE", "off")
    assert org.lambda_handler(_ev(method, path, body, query), None)["statusCode"] == 404
    assert wired[1]["merge"] == [] and wired[1]["rename"] == []


@pytest.mark.parametrize("method,path,body,query", ROUTES)
def test_a_worker_is_refused(wired, monkeypatch, method, path, body, query):
    monkeypatch.setattr(org.users, "get_user_by_sub",
                        lambda c, sub: dict(CALLER, global_role="worker"))
    assert org.lambda_handler(_ev(method, path, body, query), None)["statusCode"] == 403
    assert wired[1]["merge"] == [] and wired[1]["rename"] == []


# ---- merge-check -------------------------------------------------------------------------


@pytest.mark.parametrize("verdict,fragment", [
    ("alike", "sound like the same person"),
    ("unsure", "different devices can sound different"),
    ("different", "Merging them could make Ben Lin harder to recognise"),
])
def test_merge_check_says_it_in_plain_words_without_a_number(wired, verdict, fragment):
    wired[3]["verdict"] = verdict
    res = org.lambda_handler(_ev("GET", f"/voiceprints/{SRC}/merge-check", None,
                                 {"into": TGT}), None)
    body = _b(res)
    assert res["statusCode"] == 200 and body["verdict"] == verdict
    assert fragment in body["message"]
    assert set(body) == {"verdict", "message"}
    assert "0.27" not in res["body"], "a similarity number reached the customer"


def test_merge_check_404s_for_another_companys_or_a_withdrawn_profile(wired):
    wired[2][TGT]["status"] = "withdrawn"
    res = org.lambda_handler(_ev("GET", f"/voiceprints/{SRC}/merge-check", None,
                                 {"into": TGT}), None)
    assert res["statusCode"] == 404
    res = org.lambda_handler(_ev("GET", f"/voiceprints/{SRC}/merge-check", None,
                                 {"into": "33333333-3333-4333-8333-333333333333"}), None)
    assert res["statusCode"] == 404


@pytest.mark.parametrize("into,status", [("", 400), ("not-a-uuid", 400), (SRC, 400)])
def test_merge_check_validates_into(wired, into, status):
    res = org.lambda_handler(_ev("GET", f"/voiceprints/{SRC}/merge-check", None,
                                 {"into": into}), None)
    assert res["statusCode"] == status


# ---- merge -------------------------------------------------------------------------------


def test_merge_runs_in_one_transaction_as_the_caller(wired):
    conn, calls, _, _ = wired
    res = org.lambda_handler(_ev("POST", f"/voiceprints/{SRC}/merge", {"into": TGT}), None)
    assert res["statusCode"] == 200
    assert _b(res) == {"mergedInto": TGT, "samplesMoved": 4, "samplesDropped": 1}
    assert calls["merge"] == [("c-1", SRC, TGT, "u-1")]
    assert conn.tx == [None]


def test_different_without_confirm_is_409_and_changes_nothing(wired):
    conn, calls, _, state = wired
    state["verdict"] = "different"
    for body in ({"into": TGT}, {"into": TGT, "confirm": False},
                 {"into": TGT, "confirm": "true"}):          # only a real true counts
        res = org.lambda_handler(_ev("POST", f"/voiceprints/{SRC}/merge", body), None)
        assert res["statusCode"] == 409
        assert "Only merge if you are sure" in _b(res)["error"]
        assert "0.27" not in res["body"]
    assert calls["merge"] == []


def test_different_with_confirm_merges(wired):
    wired[3]["verdict"] = "different"
    res = org.lambda_handler(
        _ev("POST", f"/voiceprints/{SRC}/merge", {"into": TGT, "confirm": True}), None)
    assert res["statusCode"] == 200 and len(wired[1]["merge"]) == 1


def test_a_failure_unwinds_the_transaction_instead_of_returning_from_inside_it(wired):
    """`return error()` inside `with conn:` commits. Both failure paths must raise out of the
    transaction block so the savepoint rolls back."""
    conn, calls, _, state = wired
    state["verdict"] = "different"
    org.lambda_handler(_ev("POST", f"/voiceprints/{SRC}/merge", {"into": TGT}), None)
    assert conn.tx and conn.tx[-1] is not None, "the 409 returned from inside the transaction"

    state["verdict"], state["boom"] = "alike", True
    res = org.lambda_handler(_ev("POST", f"/voiceprints/{SRC}/merge", {"into": TGT}), None)
    assert res["statusCode"] == 500           # an unexpected failure is a 500, not a commit
    assert conn.tx[-1] is RuntimeError


def test_a_refused_merge_inside_the_transaction_is_404_and_rolled_back(wired, monkeypatch):
    conn = wired[0]

    def refuse(*a):
        raise org.voiceprints.MergeRefused("raced")
    monkeypatch.setattr(org.voiceprints, "merge_profiles", refuse)
    res = org.lambda_handler(_ev("POST", f"/voiceprints/{SRC}/merge", {"into": TGT}), None)
    assert res["statusCode"] == 404 and conn.tx[-1] is not None


@pytest.mark.parametrize("body,status", [
    ({}, 400), ({"into": "nope"}, 400), ({"into": SRC}, 400)])
def test_merge_validates_the_body(wired, body, status):
    res = org.lambda_handler(_ev("POST", f"/voiceprints/{SRC}/merge", body), None)
    assert res["statusCode"] == status and wired[1]["merge"] == []


def test_merge_404s_when_either_profile_is_not_in_this_company(wired):
    wired[2].pop(TGT)
    res = org.lambda_handler(_ev("POST", f"/voiceprints/{SRC}/merge", {"into": TGT}), None)
    assert res["statusCode"] == 404 and wired[1]["merge"] == []


def test_delete_route_still_withdraws(wired, monkeypatch):
    """The new PATCH/merge routes sit beside DELETE on the same path pattern."""
    monkeypatch.setattr(org.voiceprints, "withdraw", lambda c, co, vid: ["s1"])
    res = org.lambda_handler(_ev("DELETE", f"/voiceprints/{SRC}"), None)
    assert res["statusCode"] == 200 and _b(res)["samplesRemoved"] == 1


# ---- rename ------------------------------------------------------------------------------


def test_rename_trims_and_stores(wired):
    res = org.lambda_handler(
        _ev("PATCH", f"/voiceprints/{SRC}", {"displayName": "  Ben Lin (Cassidy) "}), None)
    assert res["statusCode"] == 200
    assert wired[1]["rename"] == [("c-1", SRC, "Ben Lin (Cassidy)")]
    assert wired[0].tx == [None]


@pytest.mark.parametrize("name", ["", "   ", "x" * 81, None, 5])
def test_rename_rejects_bad_names(wired, name):
    res = org.lambda_handler(_ev("PATCH", f"/voiceprints/{SRC}", {"displayName": name}), None)
    assert res["statusCode"] == 400 and wired[1]["rename"] == []


def test_rename_accepts_the_80_char_boundary(wired):
    res = org.lambda_handler(_ev("PATCH", f"/voiceprints/{SRC}", {"displayName": "x" * 80}),
                             None)
    assert res["statusCode"] == 200


def test_rename_404s_for_a_missing_or_withdrawn_profile(wired):
    wired[2][SRC]["status"] = "withdrawn"
    res = org.lambda_handler(_ev("PATCH", f"/voiceprints/{SRC}", {"displayName": "Ben"}),
                             None)
    assert res["statusCode"] == 404


# ---- listing -----------------------------------------------------------------------------


def _row(**kw):
    base = {"id": "vp-1", "display_name": "Ben Lin", "status": "confirmed", "user_id": None,
            "linked_on": None, "consent_at": None, "samples": 1, "human_samples": 1,
            "last_attempt_at": None, "last_attempt_outcome": None,
            "last_attempt_detail": None}
    base.update(kw)
    return base


def test_listing_carries_identity_fields(wired, monkeypatch):
    import datetime
    monkeypatch.setattr(org.voiceprints, "list_profiles", lambda c, co: [
        _row(user_id="u-9", linked_name="Ben Lin", linked_email="b@x.nz",
             heard_on=["A", "B", "C", "D"], employer_name="ABC Ltd",
             first_named_at=datetime.datetime(2026, 9, 1, 3, 0), first_named_by="Ann",
             merged_into=None),
        _row(id="vp-2", status="withdrawn", merged_into="vp-1", merged_into_name="Ben Lin")])
    rows = _b(org.lambda_handler(_ev("GET", "/voiceprints"), None))["voiceprints"]
    a, m = rows
    assert a["linkedAccount"] == {"name": "Ben Lin", "email": "b@x.nz"}
    assert a["heardOn"] == ["A", "B", "C"]
    assert a["firstNamed"] == {"at": "2026-09-01T03:00:00", "by": "Ann"}
    assert a["employer"] == "ABC Ltd" and a["mergedInto"] is None
    assert m["linkedAccount"] is None and m["heardOn"] == [] and m["employer"] is None
    assert m["mergedInto"] == {"id": "vp-1", "displayName": "Ben Lin"}


# ---- repository: verdicts and SQL text ------------------------------------------------------

vps = org.voiceprints


@pytest.mark.parametrize("sim,ns,nt,expected", [
    (0.50, 2, 2, "alike"), (0.499, 2, 2, "unsure"), (0.35, 2, 2, "unsure"),
    (0.349, 2, 2, "different"), (0.9, 0, 2, "unsure"), (0.9, 2, 0, "unsure"),
    (None, 2, 2, "unsure"), (float("nan"), 2, 2, "unsure"), (0.1, 0, 0, "unsure")])
def test_verdict_boundaries(sim, ns, nt, expected):
    assert vps.merge_verdict(sim, ns, nt) == expected


class RecConn:
    def __init__(self, locked):
        self.sql = []
        self.locked = locked
        self.rowcount = 1

    def cursor(self, row_factory=None):
        return self

    def execute(self, sql, params=None):
        self.sql.append(" ".join(sql.split()))
        self._last = sql
        return self

    def fetchall(self):
        return self.locked if "FOR UPDATE" in self._last else []

    def fetchone(self):
        return {"source_samples": 2, "target_samples": 2, "similarity": 0.6}


def _locked(**over):
    base = {"display_name": "T", "status": "confirmed", "user_id": None, "linked_by": None,
            "linked_at": None, "linked_on": None, "employer_name": None,
            "employer_source": None, "employer_set_by": None, "employer_set_at": None,
            "external_ref": None, "external_source": None}
    return dict(base)


def test_merge_statements_are_company_scoped_and_ordered():
    src = dict(_locked(), id=SRC, user_id="u1", external_ref="r", external_source="s")
    tgt = dict(_locked(), id=TGT)
    conn = RecConn([src, tgt])
    vps.merge_profiles(conn, "c-1", SRC, TGT, "u-1")
    writes = [s for s in conn.sql if not s.startswith("SELECT")]
    assert all("company_id" in s for s in conn.sql), "an unscoped statement"
    # The source is withdrawn before the target inherits its external ref (partial unique
    # index), and colliding samples are deleted before the rest move.
    idx = {k: next(i for i, s in enumerate(writes) if k in s) for k in (
        "SET status = 'withdrawn'", "DELETE FROM speaker_voiceprint_samples",
        "UPDATE speaker_voiceprint_samples", "UPDATE speaker_turn_names",
        "DELETE FROM speaker_name_proposals", "UPDATE speaker_name_proposals",
        "UPDATE site_attendance", "external_ref = %s")}
    assert idx["SET status = 'withdrawn'"] == 0
    assert idx["DELETE FROM speaker_voiceprint_samples"] < idx[
        "UPDATE speaker_voiceprint_samples"]
    assert idx["DELETE FROM speaker_name_proposals"] < idx["UPDATE speaker_name_proposals"]
    assert idx["external_ref = %s"] == len(writes) - 1
    turn = next(s for s in writes if s.startswith("UPDATE speaker_turn_names"))
    assert "voiceprint_id = %s" in turn.split("WHERE")[1] and "display_name = %s" in turn
    assert "lower(" not in turn.lower() and "display_name =" not in turn.split("WHERE")[1], (
        "turn names must be matched by voiceprint_id, never by name")


def test_merge_without_anything_to_inherit_writes_no_inheritance_update():
    conn = RecConn([dict(_locked(), id=SRC), dict(_locked(), id=TGT)])
    vps.merge_profiles(conn, "c-1", SRC, TGT, None)
    assert not any("external_ref = %s" in s or "employer_name = %s" in s or "user_id = %s" in s
                   for s in conn.sql)


def test_employer_is_inherited_as_a_pair():
    src = dict(_locked(), id=SRC, employer_name="ABC", employer_source="typed")
    conn = RecConn([src, dict(_locked(), id=TGT)])
    vps.merge_profiles(conn, "c-1", SRC, TGT, None)
    upd = next(s for s in conn.sql if "employer_name = %s" in s)
    assert "employer_source = %s" in upd and "employer_set_at = %s" in upd


def test_merge_check_sql_uses_live_samples_and_cosine_of_means():
    conn = RecConn([])
    out = vps.merge_check(conn, "c-1", SRC, TGT)
    sql = conn.sql[0]
    assert sql.count("quarantined_at IS NULL") == 2
    assert "avg(embedding)" in sql and "a.v <=> b.v" in sql and "1 -" in sql
    assert out["verdict"] == "alike"


def test_list_profiles_sql_has_the_identity_columns_in_one_statement():
    conn = RecConn([])
    conn.fetchall = lambda: []
    vps.list_profiles(conn, "c-1")
    assert len(conn.sql) == 1
    sql = conn.sql[0]
    for frag in ("split_part(hs.s3_key, '/', 2)", "hs.quarantined_at IS NULL", "LIMIT 3",
                 "COALESCE(p.asserted_by, p.consented_by)", "lu.email AS linked_email",
                 "p.merged_into", "merged_into_name", "p.employer_name"):
        assert frag in sql, frag
