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


def test_since_filters_labels_and_p95_but_not_the_list(db, env):
    """Spec §12 rev 6: ?since=<NZ date> limits the summary (labels and p95) to cards created
    on or after that NZ day; the card list is unchanged; a malformed value is a 400."""
    co, site, admin = _world(db)
    rid = str(uuid.uuid4())
    recips = [{"user_id": rid, "role": "pm"}]
    old = dt.datetime(2026, 11, 10, 10, 30, tzinfo=UTC)    # Tue 10 Nov 23:30 NZ
    new = dt.datetime(2026, 11, 10, 11, 30, tzinfo=UTC)    # Wed 11 Nov 00:30 NZ
    for i in range(5):
        c = _card(db, co, site, recipients=recips)
        _at(db, c, old + dt.timedelta(seconds=i))
        dc.set_label(db, c["card_id"], "not_needed", None, admin["id"])
    for i, label in enumerate(("needed", None)):
        c = _card(db, co, site, recipients=recips)
        _at(db, c, new + dt.timedelta(seconds=i))
        if label:
            dc.set_label(db, c["card_id"], label, None, admin["id"])
    status, body = _call(db, admin, "GET", "/decision-shadow")
    assert status == 200 and body["summary"]["since"] is None
    assert (body["summary"]["labelled"], body["summary"]["cards_per_recipient_day_p95"]) == (
        6, 5.0)
    status, body = _call(db, admin, "GET", "/decision-shadow", query={"since": "2026-11-11"})
    assert status == 200 and len(body["cards"]) == 7
    s = body["summary"]
    assert (s["since"], s["labelled"], s["needed"], s["not_needed"]) == ("2026-11-11", 1, 1, 0)
    assert s["precision"] == 1.0 and s["cards_per_recipient_day_p95"] == 2.0
    status, body = _call(db, admin, "GET", "/decision-shadow", query={"since": "11-11-2026"})
    assert (status, body["error"]) == (400, "since must be YYYY-MM-DD")


# ---------------------------------------------------------------- removed sources (fix round)
# Aurora recording tombstones, multi-unit walk parts, person names and overdue item text.

from repositories import action_items, redactions, topics  # noqa: E402

NAMED = [{"id": "remind", "label": "Remind Tom to recheck", "recommended": True},
         {"id": "accept", "label": "Accept with reason", "recommended": False,
          "reason_required": True}]


def _tombstone(db, co, admin, folder, base, date=DATE):
    redactions.create_recording_tombstone(
        db, co["id"], f"extractions/{folder}/{date}/{base}", "deleted by the user",
        admin["id"], "platform_admin", batch_id=str(uuid.uuid4()))


def _walk_report(db, co, folder, session, walk_key, date=DATE):
    return db.execute(
        "INSERT INTO checklist_reports (company_id, user_folder, report_date, session, "
        "template_name, check_name, start_at, request_id, result_key, walk_key) "
        "VALUES (%s,%s,%s,%s,'Pre-line','pre-line',%s,%s,%s,%s) RETURNING id",
        (co["id"], folder, date, session, f"09:{_tag()[:2]}", _tag(), f"k-{_tag()}",
         walk_key)).fetchone()[0]


def _window(db, co, folder, session, walk_key, start_at, date=DATE):
    db.execute(
        "INSERT INTO inspection_windows (company_id, user_folder, report_date, session, "
        "name, start_at, end_source, walk_key) VALUES (%s,%s,%s,%s,'pre-line',%s,"
        "'recording_stop',%s)", (co["id"], folder, date, session, start_at, walk_key))


def _gap_card_on(db, co, site, rid, session, evidence=(), options=NAMED):
    eid = _event(db, co, site, {"session": session, "report_date": DATE,
                                "report_id": str(rid)})
    return _card(db, co, site, event_id=eid, evidence=list(evidence), options=options)


def _view(db, admin, card):
    status, body = _call(db, admin, "GET", "/decision-shadow")
    assert status == 200
    [c] = [x for x in body["cards"] if x["card_id"] == str(card["card_id"])]
    return c


def test_aurora_tombstone_drops_evidence_and_names(db, env):
    co, site, admin = _world(db)
    folder = f"F_{_tag()}"
    gone = _gap_card(db, co, site, folder, "sideeee1111", "rails are loose")
    db.execute("UPDATE decision_cards SET options=%s WHERE card_id=%s",
               (json.dumps(NAMED), gone["card_id"]))
    kept = _gap_card(db, co, site, folder, "sidffff2222", "toe board missing")
    db.execute("UPDATE decision_cards SET options=%s WHERE card_id=%s",
               (json.dumps(NAMED), kept["card_id"]))
    _tombstone(db, co, admin, folder, "eeee1111")       # bare spelling; no S3 mirror at all
    g, k = _view(db, admin, gone), _view(db, admin, kept)
    assert g["evidence"] == []
    assert g["options"][0]["label"] == "Remind the inspector to recheck"
    assert k["evidence"] == [{"quote": "toe board missing", "at": None}]
    assert k["options"][0]["label"] == "Remind Tom to recheck"


def test_reverted_tombstone_shows_again(db, env):
    co, site, admin = _world(db)
    folder = f"F_{_tag()}"
    card = _gap_card(db, co, site, folder, "sid1234abcd", "q")
    _tombstone(db, co, admin, folder, "sid1234abcd")
    assert _view(db, admin, card)["evidence"] == []
    db.execute("UPDATE redactions SET reverted_at = now() WHERE target_type = 'recording' "
               "AND target_key = %s", (f"extractions/{folder}/{DATE}/sid1234abcd",))
    assert _view(db, admin, card)["evidence"] == [{"quote": "q", "at": None}]


def test_gap_without_evidence_but_named_person_and_deleted_source(db, env):
    co, site, admin = _world(db)
    folder = f"F_{_tag()}"
    rid = _report(db, co, folder, "sid9999aaaa")
    card = _gap_card_on(db, co, site, rid, "sid9999aaaa")
    assert _view(db, admin, card)["options"][0]["label"] == "Remind Tom to recheck"
    _tombstone(db, co, admin, folder, "sid9999aaaa")
    c = _view(db, admin, card)
    assert c["options"][0]["label"] == "Remind the inspector to recheck"
    assert c["evidence"] == []
    # the stored row is untouched (read-time rewrite only)
    stored = db.execute("SELECT options FROM decision_cards WHERE card_id=%s",
                        (card["card_id"],)).fetchone()[0]
    assert stored[0]["label"] == "Remind Tom to recheck"


def test_walk_report_with_one_deleted_part_is_removed(db, env):
    co, site, admin = _world(db)
    folder, wk = f"F_{_tag()}", f"walk-{_tag()}"
    rid = _walk_report(db, co, folder, "sidopen0001", wk)
    _window(db, co, folder, "sidopen0001", wk, "09:00:00")
    _window(db, co, folder, "sidpart0002", wk, "10:00:00")
    card = _gap_card_on(db, co, site, rid, "sidopen0001", [{"quote": "q", "at": None}])
    rows = dc.evidence_sources(db, [card["card_id"]])
    assert rows[0]["walk_key"] == wk
    assert [p["session"] for p in rows[0]["walk_parts"]] == ["sidopen0001", "sidpart0002"]
    assert rows[0]["walk_parts"][0]["report_date"] == DATE
    assert _view(db, admin, card)["evidence"] == [{"quote": "q", "at": None}]
    _tombstone(db, co, admin, folder, "sidpart0002")    # a part, not the recorded session
    c = _view(db, admin, card)
    assert c["evidence"] == [] and c["options"][0]["label"] == "Remind the inspector to recheck"


def test_walk_part_in_s3_mirror_is_removed(db, env):
    co, site, admin = _world(db)
    folder, wk = f"F_{_tag()}", f"walk-{_tag()}"
    rid = _walk_report(db, co, folder, "sidopen0003", wk)
    _window(db, co, folder, "sidopen0003", wk, "09:00:00")
    _window(db, co, folder, "sidpart0004", wk, "10:00:00")
    card = _gap_card_on(db, co, site, rid, "sidopen0003", [{"quote": "q", "at": None}])
    env.objs[("data-bucket", deletion_mirror.mirror_key(folder, DATE))] = json.dumps(
        {"sessions": ["part0004"]}).encode()
    assert _view(db, admin, card)["evidence"] == []


def test_walk_report_with_unresolvable_parts_is_removed(db, env):
    co, site, admin = _world(db)
    folder, wk = f"F_{_tag()}", f"walk-{_tag()}"
    rid = _walk_report(db, co, folder, "sidopen0005", wk)
    # a window of the same walk key in ANOTHER folder is not this report's part
    _window(db, co, f"Other_{_tag()}", "sidopen0005", wk, "09:00:00")
    card = _gap_card_on(db, co, site, rid, "sidopen0005", [{"quote": "q", "at": None}])
    assert dc.evidence_sources(db, [card["card_id"]])[0]["walk_parts"] == []
    c = _view(db, admin, card)
    assert c["evidence"] == [] and c["options"][0]["label"] == "Remind the inspector to recheck"


def _overdue_world(db, co, site, text="Order the rebar"):
    t = topics.upsert_topic(db, site["id"], DATE, f"T-{_tag()}",
                            source_s3_key=f"extractions/OF/{DATE}/sid{_tag()}.json")
    item = action_items.insert_item(db, t["id"], site["id"], text, responsible="Mike",
                                    deadline=dt.date(2026, 11, 1))
    row, _ = se.insert(db, {
        "company_id": co["id"], "site_id": site["id"], "source": "action_items",
        "type": "todo.overdue", "subject_ref": str(item["stable_id"]), "bucket": DATE,
        "occurred_at": NOW, "received_at": NOW, "summary": None, "link": None,
        "payload": {"text": text, "deadline": "2026-11-01", "responsible": "Mike"},
        "dedupe_key": f"dsh:{_tag()}", "status": "done"})
    card = _card(db, co, site, rule_id="todo.overdue", purpose="overdue",
                 subject_ref=str(item["stable_id"]), event_id=row["event_id"],
                 question=f"'{text}' was due 1 Nov 2026, still open",
                 options=[{"id": "remind", "label": "Remind Mike", "recommended": True},
                          {"id": "done", "label": "Mark done", "recommended": False}])
    return t, item, card


def test_overdue_live_item_unchanged(db, env):
    co, site, admin = _world(db)
    _, _, card = _overdue_world(db, co, site)
    c = _view(db, admin, card)
    assert c["question"] == "'Order the rebar' was due 1 Nov 2026, still open"
    assert c["options"][0]["label"] == "Remind Mike"


@pytest.mark.parametrize("how", ["topic_tombstone", "recording_tombstone", "redacted",
                                 "non_work"])
def test_overdue_removed_item_has_no_text_and_no_person(db, env, how):
    co, site, admin = _world(db)
    t, item, card = _overdue_world(db, co, site, "Call Dave about the crane")
    if how == "topic_tombstone":
        redactions.create_redaction(db, co["id"], t["id"], "x", admin["id"], "platform_admin",
                                    target_type="topic", scope="deleted")
    elif how == "recording_tombstone":
        redactions.create_recording_tombstone(
            db, co["id"], t["source_s3_key"].rsplit(".", 1)[0], "x", admin["id"],
            "platform_admin", batch_id=str(uuid.uuid4()))
    elif how == "redacted":
        redactions.create_redaction(db, co["id"], t["id"], "x", admin["id"], "platform_admin",
                                    target_type="topic", scope="analysis")
    else:
        db.execute("UPDATE topics SET work_class = 'non_work' WHERE id = %s", (t["id"],))
    c = _view(db, admin, card)
    assert c["question"] == "A to-do that was removed was due 1 Nov 2026, still open"
    assert c["options"][0]["label"] == "Remind the owner"
    assert "Dave" not in json.dumps(c) and "Mike" not in json.dumps(c)


def test_overdue_item_states_is_batched_and_text_safe(db, env):
    co, site, admin = _world(db)
    _, _, live = _overdue_world(db, co, site)
    bad = _card(db, co, site, rule_id="todo.overdue", purpose="overdue",
                subject_ref="not-a-uuid")
    ghost = _card(db, co, site, rule_id="todo.overdue", purpose="overdue",
                  subject_ref=str(uuid.uuid4()))
    rows = {r["card_id"]: r for r in dc.overdue_item_states(
        db, [live["card_id"], bad["card_id"], ghost["card_id"]])}
    assert (rows[live["card_id"]]["visible"], rows[live["card_id"]]["deadline"]) == (
        True, "2026-11-01")
    assert (rows[bad["card_id"]]["visible"], rows[bad["card_id"]]["deadline"]) == (False, None)
    assert rows[ghost["card_id"]]["visible"] is False
    # the transaction is still usable after the malformed subject_ref
    assert db.execute("SELECT 1").fetchone()[0] == 1
    c = _view(db, admin, bad)
    assert c["question"] == "A to-do that was removed is past its date, still open"


def test_overdue_superseded_pass_reads_the_live_row(db, env):
    """A re-extraction supersedes the old topic; the carried row (same stable_id) is live."""
    co, site, admin = _world(db)
    t, item, card = _overdue_world(db, co, site)
    t2 = topics.upsert_topic(db, site["id"], DATE, f"T-{_tag()}",
                             source_s3_key=f"extractions/OF/{DATE}/sid{_tag()}.json")
    new = action_items.insert_item(db, t2["id"], site["id"], "Order the rebar",
                                   deadline=dt.date(2026, 11, 1))
    db.execute("UPDATE action_items SET stable_id=%s WHERE id=%s",
               (item["stable_id"], new["id"]))
    db.execute("UPDATE topics SET superseded_at = now() WHERE id = %s", (t["id"],))
    assert _view(db, admin, card)["question"].startswith("'Order the rebar'")
    db.execute("UPDATE topics SET work_class = 'non_work' WHERE id = %s", (t2["id"],))
    assert _view(db, admin, card)["question"].startswith("A to-do that was removed")


def test_overdue_item_of_another_company_or_site_does_not_look_live(db, env):
    """Review M-3: the item is resolved on the card's own site and company. An item carrying
    the same stable_id elsewhere never makes the card look live."""
    co, site, admin = _world(db)
    t, item, card = _overdue_world(db, co, site, "Call Dave about the crane")
    # the card's own item is deleted ...
    redactions.create_redaction(db, co["id"], t["id"], "x", admin["id"], "platform_admin",
                                target_type="topic", scope="deleted")
    # ... while live items with the SAME stable_id sit on another company's site and on
    # another site of this company
    co2 = companies.create_company(db, f"Co-dsh-{_tag()}")
    elsewhere = [sites.create_site(db, co2["id"], f"Site-dsh-{_tag()}"),
                 sites.create_site(db, co["id"], f"Site-dsh-{_tag()}")]
    for other in elsewhere:
        t2 = topics.upsert_topic(db, other["id"], DATE, f"T-{_tag()}",
                                 source_s3_key=f"extractions/OF/{DATE}/sid{_tag()}.json")
        twin = action_items.insert_item(db, t2["id"], other["id"], "Call Dave about the crane",
                                        deadline=dt.date(2026, 11, 1))
        db.execute("UPDATE action_items SET stable_id=%s WHERE id=%s",
                   (item["stable_id"], twin["id"]))
    [st] = dc.overdue_item_states(db, [card["card_id"]])
    assert st["visible"] is False
    c = _view(db, admin, card)
    assert c["question"] == "A to-do that was removed was due 1 Nov 2026, still open"
    assert "Dave" not in json.dumps(c)
