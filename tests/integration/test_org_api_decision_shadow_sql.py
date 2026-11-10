"""Integration: org-api decision-loop shadow review routes against real Postgres (Task 6).

The real dispatch and every query the routes run: list_shadow (company/site names),
get_shadow, set_label, shadow_summary_rows, users.display_names_for_ids and
decision_cards.evidence_sources (card -> originating event -> checklist report, for the
deletion-mirror check). S3 is a fake holding the deletion mirror.

Skipped without TEST_DATABASE_URL -- a skip is NOT a pass.
"""
import datetime as dt
import json
import uuid

import pytest

import deletion_mirror
from repositories import companies, sites, users
from repositories import decision_cards as dc
from repositories import site_events as se

pytestmark = pytest.mark.integration

org = pytest.importorskip("lambda_org_api")

UTC = dt.timezone.utc
NOW = dt.datetime(2026, 11, 10, 1, 0, tzinfo=UTC)
DATE = "2026-11-09"


class FakeS3:
    def __init__(self):
        self.objs = {}

    def get_object(self, Bucket, Key):
        import io
        from botocore.exceptions import ClientError
        if (Bucket, Key) not in self.objs:
            raise ClientError({"Error": {"Code": "NoSuchKey", "Message": "x"}}, "GetObject")
        return {"Body": io.BytesIO(self.objs[(Bucket, Key)])}


@pytest.fixture
def env(db, monkeypatch):
    monkeypatch.setenv("SITE_DECISION_LOOP", "shadow")
    monkeypatch.delenv("COMPANY_STATUS_ENFORCE", raising=False)
    monkeypatch.setattr(db, "commit", lambda: None)
    fake = FakeS3()
    monkeypatch.setattr(org, "s3", lambda: fake)
    monkeypatch.setattr(org, "S3_BUCKET", "data-bucket")
    monkeypatch.setattr(org, "LAKE_BUCKET", "lake-bucket")
    # The summary covers every card in the table: start from an empty one (rolled back).
    db.execute("DELETE FROM decision_cards")
    return fake


def _tag():
    return uuid.uuid4().hex[:8]


def _user(db, co, role="worker", first=None, last=None):
    t = _tag()
    u = users.upsert_user(db, f"sub-dsh-{t}", f"dsh-{t}@example.com",
                          company_id=co["id"], global_role=role)
    db.execute("UPDATE users SET first_name=%s, last_name=%s WHERE id=%s",
               (first, last, u["id"]))
    return users.get_user_by_sub(db, u["cognito_sub"])


def _world(db):
    co = companies.create_company(db, f"Co-dsh-{_tag()}")
    site = sites.create_site(db, co["id"], f"Site-dsh-{_tag()}")
    admin = _user(db, co, "platform_admin", "Op", "Erator")
    return co, site, admin


def _report(db, co, folder, session, date=DATE):
    return db.execute(
        "INSERT INTO checklist_reports (company_id, user_folder, report_date, session, "
        "template_name, check_name, start_at, request_id, result_key) "
        "VALUES (%s,%s,%s,%s,'Scaffold','scaffold',%s,%s,%s) RETURNING id",
        (co["id"], folder, date, session, f"10:{_tag()[:2]}", _tag(), f"k-{_tag()}")
    ).fetchone()[0]


def _event(db, co, site, payload):
    row, _ = se.insert(db, {
        "company_id": co["id"], "site_id": site["id"], "source": "checklist_reports",
        "type": "checklist.gap", "subject_ref": f"s|Scaffold|{_tag()}", "bucket": "",
        "occurred_at": NOW, "received_at": NOW, "summary": None, "link": None,
        "payload": payload, "dedupe_key": f"dsh:{_tag()}", "status": "done"})
    return row["event_id"]


def _card(db, co, site, **over):
    card = {"card_key": f"key:{_tag()}", "rule_id": "checklist.gap", "purpose": "gap",
            "subject_ref": f"s|Scaffold|{_tag()}", "kind": "choose",
            "question": "'Rails' on the Scaffold checklist failed",
            "consequence": "It stays open.",
            "options": [{"id": "remind", "label": "Remind", "recommended": True}],
            "evidence": [], "urgency": "normal", "deadline_at": None,
            "timeout_default": "nothing changes", "recipients": [], "event_id": None,
            "company_id": co["id"], "site_id": site["id"]}
    card.update(over)
    return dc.insert_open(db, card)


def _gap_card(db, co, site, folder, session, quote, date=DATE):
    rid = _report(db, co, folder, session, date)
    eid = _event(db, co, site, {"session": session, "report_date": date,
                                "report_id": str(rid), "quote": quote})
    return _card(db, co, site, event_id=eid, evidence=[{"quote": quote, "at": None}])


def _at(db, card, when):
    db.execute("UPDATE decision_cards SET created_at=%s WHERE card_id=%s",
               (when, card["card_id"]))


def _call(db, u, method, route, body=None, query=None):
    ev = {"httpMethod": method, "path": "/api/org" + route, "queryStringParameters": query,
          "body": None if body is None else json.dumps(body), "headers": {},
          "requestContext": {"authorizer": {"claims": {"sub": u["cognito_sub"]}}}}
    res = org.dispatch(db, ev, method, route)
    return res["statusCode"], json.loads(res["body"])


# ---------------------------------------------------------------- list

def test_list_carries_names_recipients_and_shape(db, env):
    co, site, admin = _world(db)
    pm = _user(db, co, "worker", "Pat", "Manager")
    nameless = _user(db, co, "worker", None, None)
    ghost = str(uuid.uuid4())
    card = _card(db, co, site, recipients=[
        {"user_id": str(pm["id"]), "role": "pm"},
        {"user_id": str(nameless["id"]), "role": "site_manager"},
        {"user_id": ghost, "role": "ops"}])
    _at(db, card, dt.datetime(2026, 11, 9, 22, 0, tzinfo=UTC))
    status, body = _call(db, admin, "GET", "/decision-shadow")
    assert status == 200
    [c] = body["cards"]
    assert c["card_id"] == str(card["card_id"])
    assert c["company_name"] == co["name"] and c["site_name"] == site["name"]
    assert c["created_at"] == "2026-11-09T22:00:00+00:00"
    assert c["recipients"] == [
        {"user_id": str(pm["id"]), "role": "pm", "display_name": "Pat Manager"},
        {"user_id": str(nameless["id"]), "role": "site_manager", "display_name": ""},
        {"user_id": ghost, "role": "ops", "display_name": ""}]
    assert set(c) == {"card_id", "company_name", "site_name", "rule_id", "kind", "purpose",
                      "urgency", "question", "consequence", "created_at", "shadow_label",
                      "label_note", "options", "evidence", "recipients"}


def test_list_label_filter_and_paging(db, env):
    co, site, admin = _world(db)
    a, b, c = (_card(db, co, site) for _ in range(3))
    for i, card in enumerate((a, b, c)):
        _at(db, card, NOW + dt.timedelta(minutes=i))
    dc.set_label(db, a["card_id"], "needed", None, admin["id"])
    status, body = _call(db, admin, "GET", "/decision-shadow", query={"label": "needed"})
    assert status == 200 and [x["card_id"] for x in body["cards"]] == [str(a["card_id"])]
    status, body = _call(db, admin, "GET", "/decision-shadow",
                         query={"limit": "1", "offset": "1"})
    assert [x["card_id"] for x in body["cards"]] == [str(b["card_id"])]


def test_deleted_session_drops_evidence(db, env):
    co, site, admin = _world(db)
    folder = f"F_{_tag()}"
    gone = _gap_card(db, co, site, folder, "sidaaaa1111", "rails are loose")
    kept = _gap_card(db, co, site, folder, "sidbbbb2222", "toe board missing")
    # The mirror holds the bare spelling; the card's session is the sid spelling.
    env.objs[("data-bucket", deletion_mirror.mirror_key(folder, DATE))] = json.dumps(
        {"sessions": ["aaaa1111"]}).encode()
    status, body = _call(db, admin, "GET", "/decision-shadow")
    assert status == 200
    by_id = {c["card_id"]: c for c in body["cards"]}
    assert by_id[str(gone["card_id"])]["evidence"] == []
    assert by_id[str(kept["card_id"])]["evidence"] == [
        {"quote": "toe board missing", "at": None}]
    # The label POST returns the same shape, so it drops the evidence too.
    status, card = _call(db, admin, "POST", f"/decision-shadow/{gone['card_id']}/label",
                         {"label": "wrong_evidence"})
    assert status == 200 and card["evidence"] == []
    assert card["company_name"] == co["name"]


def test_evidence_without_a_resolvable_source_is_dropped(db, env):
    co, site, admin = _world(db)
    orphan = _card(db, co, site, evidence=[{"quote": "no report behind me", "at": None}])
    status, body = _call(db, admin, "GET", "/decision-shadow")
    assert status == 200
    [c] = [x for x in body["cards"] if x["card_id"] == str(orphan["card_id"])]
    assert c["evidence"] == []


def test_evidence_sources_is_batched_and_text_safe(db, env):
    co, site, _ = _world(db)
    folder = f"F_{_tag()}"
    g = _gap_card(db, co, site, folder, "sidcccc3333", "q")
    bad_eid = _event(db, co, site, {"session": "x", "report_id": "not-a-uuid"})
    bad = _card(db, co, site, event_id=bad_eid)
    plain = _card(db, co, site)
    rows = dc.evidence_sources(db, [g["card_id"], bad["card_id"], plain["card_id"]])
    assert [(r["card_id"], r["user_folder"], r["session"], str(r["report_date"]))
            for r in rows] == [(g["card_id"], folder, "sidcccc3333", DATE)]


# ---------------------------------------------------------------- label

def test_label_keeps_status_open(db, env):
    co, site, admin = _world(db)
    card = _card(db, co, site)
    status, body = _call(db, admin, "POST", f"/decision-shadow/{card['card_id']}/label",
                         {"label": "not_needed", "note": "already fixed", "status": "done"})
    assert status == 200
    assert body["shadow_label"] == "not_needed" and body["label_note"] == "already fixed"
    assert body["site_name"] == site["name"]
    row = db.execute("SELECT status, shadow_label, labelled_by, labelled_at FROM "
                     "decision_cards WHERE card_id=%s", (card["card_id"],)).fetchone()
    assert row[0] == "open" and row[1] == "not_needed"
    assert row[2] == admin["id"] and row[3] is not None


def test_label_unknown_card_404_sql(db, env):
    _, _, admin = _world(db)
    status, _ = _call(db, admin, "POST", f"/decision-shadow/{uuid.uuid4()}/label",
                      {"label": "needed"})
    assert status == 404


def test_non_platform_admin_gets_403_sql(db, env):
    co, site, _ = _world(db)
    _card(db, co, site)
    for role in ("admin", "gm", "pm"):
        u = _user(db, co, role, "X", role)
        assert _call(db, u, "GET", "/decision-shadow")[0] == 403


# ---------------------------------------------------------------- summary

def test_summary_counts_cover_all_cards(db, env):
    co, site, admin = _world(db)
    cards = [_card(db, co, site) for _ in range(5)]
    for card, label in zip(cards, ("needed", "needed", "not_needed", "wrong_evidence")):
        dc.set_label(db, card["card_id"], label, None, admin["id"])
    status, body = _call(db, admin, "GET", "/decision-shadow", query={"limit": "1"})
    assert status == 200 and len(body["cards"]) == 1
    s = body["summary"]
    assert (s["labelled"], s["needed"], s["not_needed"], s["wrong_evidence"]) == (4, 2, 1, 1)
    assert s["precision"] == pytest.approx(0.667, abs=1e-3)


def test_cards_per_recipient_day_p95(db, env):
    co, site, admin = _world(db)
    rid = str(uuid.uuid4())
    recips = [{"user_id": rid, "role": "pm"}]
    day1 = dt.datetime(2026, 11, 9, 20, 0, tzinfo=UTC)     # Tue 10 Nov 09:00 NZ
    day2 = dt.datetime(2026, 11, 10, 20, 0, tzinfo=UTC)    # Wed 11 Nov 09:00 NZ
    for i in range(4):
        _at(db, _card(db, co, site, recipients=recips), day1 + dt.timedelta(minutes=i))
    _at(db, _card(db, co, site, recipients=recips), day2)
    for i in range(6):   # fault cards are excluded from the gate
        _at(db, _card(db, co, site, kind="fault", recipients=recips),
            day2 + dt.timedelta(minutes=i))
    status, body = _call(db, admin, "GET", "/decision-shadow")
    assert status == 200
    p95 = body["summary"]["cards_per_recipient_day_p95"]
    assert p95 >= 3.5
    assert p95 == 4.0
