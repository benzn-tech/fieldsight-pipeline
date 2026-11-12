"""Integration: POST /api/org/decision-cards/{card_id}/answer (spec 2026-11-12 rev 2 §5, §6).

The real dispatch against real Postgres: which answers write a decision_ledger row, first
answer wins (retry-safe for the same answer, 409 otherwise), every 400/404 decided before any
write, and the hub's reaction to an answer (a remind frees the card key, a decision silences
the subject). The two-connection test commits its seed rows and cleans up in `finally`; it
answers only with `remind`, which writes no ledger row (the ledger trigger rejects DELETE).

Skipped without TEST_DATABASE_URL -- a skip is NOT a pass.
"""
import datetime as dt
import json
import threading
import time
import uuid

import psycopg
import pytest

import lambda_decision_scan as lds
from repositories import action_items, companies, memberships, sites, topics, users
from repositories import decision_cards as dc
from repositories import site_events as se

pytestmark = pytest.mark.integration

org = pytest.importorskip("lambda_org_api")

UTC = dt.timezone.utc
NOW = dt.datetime(2026, 11, 10, 1, 0, tzinfo=UTC)          # 14:00 NZDT, Tue 10 Nov
DATE = "2026-11-10"
ANSWERED = {"status": "answered", "applied": None}


class FakeS3:
    def get_object(self, Bucket, Key):
        from botocore.exceptions import ClientError
        raise ClientError({"Error": {"Code": "NoSuchKey", "Message": "x"}}, "GetObject")


def _setenv(monkeypatch, companies_csv):
    monkeypatch.setenv("SITE_DECISION_LOOP", "shadow")
    monkeypatch.setenv("DECISION_INBOX", "companies")
    monkeypatch.setenv("DECISION_INBOX_COMPANIES", companies_csv)
    monkeypatch.delenv("COMPANY_STATUS_ENFORCE", raising=False)
    monkeypatch.delenv("NIGHTLY_REMATCH", raising=False)
    monkeypatch.setattr(org, "s3", lambda: FakeS3())
    monkeypatch.setattr(org, "S3_BUCKET", "data-bucket")
    monkeypatch.setattr(org, "_inbox_now", lambda: NOW)


def _tag():
    return uuid.uuid4().hex[:8]


def _user(conn, co, role="worker"):
    t = _tag()
    u = users.upsert_user(conn, f"sub-dia-{t}", f"dia-{t}@example.com",
                          company_id=co["id"], global_role=role)
    return users.get_user_by_sub(conn, u["cognito_sub"])


@pytest.fixture
def world(db, monkeypatch):
    monkeypatch.setattr(db, "commit", lambda: None)
    co = companies.create_company(db, f"Co-dia-{_tag()}")
    site = sites.create_site(db, co["id"], f"Site-dia-{_tag()}")
    pm = _user(db, co)
    memberships.add_membership(db, pm["id"], site["id"], "pm")
    sm = _user(db, co)
    memberships.add_membership(db, sm["id"], site["id"], "site_manager")
    _setenv(monkeypatch, str(co["id"]))
    return co, site, pm, sm


def _card(db, co, site, recips, **over):
    card = {"card_key": f"key:{_tag()}", "rule_id": "todo.overdue", "purpose": "overdue",
            "subject_ref": str(uuid.uuid4()), "kind": "choose",
            "question": "'Order the rebar' was due 8 Nov 2026, still open", "consequence": "c",
            "options": [{"id": "remind", "label": "Remind Mike", "recommended": True},
                        {"id": "done", "label": "Mark done", "recommended": False},
                        {"id": "new_date", "label": "Set a new date", "recommended": False},
                        {"id": "not_needed", "label": "No longer needed",
                         "recommended": False}],
            "evidence": [], "urgency": "normal", "deadline_at": None,
            "timeout_default": "nothing changes",
            "recipients": [{"user_id": str(u["id"]), "role": role} for u, role in recips],
            "event_id": None, "company_id": co["id"], "site_id": site["id"]}
    card.update(over)
    return dc.insert_open(db, card)


def _gap_card(db, co, site, recips):
    return _card(db, co, site, recips, rule_id="checklist.gap", purpose="gap",
                 question="'Rails' on the Scaffold checklist failed",
                 options=[{"id": "remind", "label": "Remind Sam to recheck", "recommended": True},
                          {"id": "accept", "label": "Accept with reason", "recommended": False,
                           "reason_required": True},
                          {"id": "reassign", "label": "Reassign", "recommended": False}])


def _post(conn, u, card_id, body):
    route = f"/decision-cards/{card_id}/answer"
    ev = {"httpMethod": "POST", "path": "/api/org" + route, "queryStringParameters": None,
          "body": body if isinstance(body, str) or body is None else json.dumps(body),
          "headers": {}, "requestContext": {"authorizer": {"claims": {"sub": u["cognito_sub"]}}}}
    res = org.dispatch(conn, ev, "POST", route)
    return res["statusCode"], json.loads(res["body"])


def _ledger(db, card_id):
    return db.execute(
        "SELECT company_id, site_id, card_id, subject_ref, decision, kind, decided_by, "
        "authority_basis, evidence, source FROM decision_ledger WHERE card_id = %s",
        (card_id,)).fetchall()


def _state(db, card_id):
    return db.execute(
        "SELECT status, answered_by, answered_option, answered_at IS NOT NULL, answer_reason "
        "FROM decision_cards WHERE card_id = %s", (card_id,)).fetchone()


# ---------------------------------------------------------------- decisions

def test_done_writes_one_decided_ledger_row(db, world):
    co, site, pm, _ = world
    card = _card(db, co, site, [(pm, "pm")])
    assert _post(db, pm, card["card_id"], {"option_id": "done"}) == (200, ANSWERED)
    assert _state(db, card["card_id"]) == ("answered", pm["id"], "done", True, None)
    [row] = _ledger(db, card["card_id"])
    assert row == (co["id"], site["id"], card["card_id"], card["subject_ref"], "done",
                   "decided", pm["id"], "pm", [], "card")


def test_not_needed_is_declined_with_matched_role(db, world):
    co, site, _, sm = world
    card = _card(db, co, site, [(sm, "site_manager")])
    assert _post(db, sm, card["card_id"], {"option_id": "not_needed"})[0] == 200
    [row] = _ledger(db, card["card_id"])
    assert (row[4], row[5], row[7]) == ("not_needed", "declined", "site_manager")


def test_gap_accept_needs_a_reason_and_records_it(db, world):
    co, site, pm, _ = world
    card = _gap_card(db, co, site, [(pm, "pm")])
    for body in ({"option_id": "accept"}, {"option_id": "accept", "reason": "   "}):
        status, _ = _post(db, pm, card["card_id"], body)
        assert status == 400
    assert _state(db, card["card_id"])[0] == "open" and _ledger(db, card["card_id"]) == []
    status, body = _post(db, pm, card["card_id"],
                         {"option_id": "accept", "reason": "  Rail fixed on site  "})
    assert (status, body) == (200, ANSWERED)
    assert _state(db, card["card_id"]) == ("answered", pm["id"], "accept", True,
                                           "Rail fixed on site")
    [row] = _ledger(db, card["card_id"])
    assert (row[4], row[5]) == ("accept", "decided")
    # no originating event: the source is unresolvable, so the served evidence is []
    assert row[8] == [{"kind": "reason", "text": "Rail fixed on site"}]


def test_ledger_evidence_is_the_served_evidence_plus_reason(db, world, monkeypatch):
    co, site, pm, _ = world
    card = _gap_card(db, co, site, [(pm, "pm")])
    db.execute("UPDATE decision_cards SET evidence = %s::jsonb WHERE card_id = %s",
               (json.dumps([{"quote": "rails are loose", "at": None}]), card["card_id"]))
    monkeypatch.setattr(org, "_removed_source_cards", lambda conn, rows: (set(), {}))
    assert _post(db, pm, card["card_id"], {"option_id": "accept", "reason": "ok"})[0] == 200
    [row] = _ledger(db, card["card_id"])
    assert row[8] == [{"speaker_name": None, "said_at": None, "quote": "rails are loose",
                       "audio": None},
                      {"kind": "reason", "text": "ok"}]


@pytest.mark.parametrize("body", [{"option_id": "nope"}, {}, {"option_id": 3},
                                  {"option_id": "done", "reason": "x" * 501},
                                  {"option_id": "done", "reason": 7}, "not json", "[1]"])
def test_400s_write_nothing(db, world, body):
    co, site, pm, _ = world
    card = _card(db, co, site, [(pm, "pm")])
    assert _post(db, pm, card["card_id"], body)[0] == 400
    assert _state(db, card["card_id"]) == ("open", None, None, False, None)
    assert _ledger(db, card["card_id"]) == []


def test_reason_of_500_chars_is_accepted(db, world):
    co, site, pm, _ = world
    card = _card(db, co, site, [(pm, "pm")])
    assert _post(db, pm, card["card_id"], {"option_id": "done", "reason": "x" * 500})[0] == 200


# ---------------------------------------------------------------- non-decisions and the hub

def _scan_card(db, co, site, pm):
    t = topics.upsert_topic(db, site["id"], DATE, f"T-{_tag()}",
                            source_s3_key=f"extractions/x/{DATE}/{_tag()}.json")
    item = action_items.insert_item(db, t["id"], site["id"], "Order the rebar",
                                    responsible="Mike", deadline=dt.date(2026, 11, 8))
    lds.emit_todo_overdue(db, NOW)
    assert lds.process(db, NOW)["cards"] == 1
    [card] = db.cursor(row_factory=psycopg.rows.dict_row).execute(
        "SELECT card_id, subject_ref FROM decision_cards WHERE company_id = %s AND status='open'",
        (co["id"],)).fetchall()
    assert card["subject_ref"] == str(item["stable_id"])
    return card


def test_remind_writes_no_ledger_and_the_next_day_cards_again(db, world):
    co, site, pm, _ = world
    card = _scan_card(db, co, site, pm)
    assert _post(db, pm, card["card_id"], {"option_id": "remind"}) == (200, ANSWERED)
    assert _state(db, card["card_id"])[:3] == ("answered", pm["id"], "remind")
    assert _ledger(db, card["card_id"]) == []
    now2 = NOW + dt.timedelta(days=1)
    lds.emit_todo_overdue(db, now2)
    assert lds.process(db, now2)["cards"] == 1
    n_open = db.execute("SELECT count(*) FROM decision_cards WHERE company_id=%s AND "
                        "status='open' AND subject_ref=%s",
                        (co["id"], card["subject_ref"])).fetchone()[0]
    assert n_open == 1


def test_done_silences_later_events(db, world):
    co, site, pm, _ = world
    card = _scan_card(db, co, site, pm)
    assert _post(db, pm, card["card_id"], {"option_id": "done"})[0] == 200
    now2 = NOW + dt.timedelta(days=1)
    lds.emit_todo_overdue(db, now2)
    assert lds.process(db, now2)["cards"] == 0
    ev = db.execute("SELECT e.status, r.outcome FROM site_events e "
                    "JOIN run_events re ON re.event_id = e.event_id "
                    "JOIN event_runs r ON r.run_id = re.run_id "
                    "WHERE e.company_id=%s AND e.subject_ref=%s AND e.bucket='2026-11-11'",
                    (co["id"], card["subject_ref"])).fetchone()
    assert ev == ("done", "nothing")
    assert db.execute("SELECT count(*) FROM decision_cards WHERE company_id=%s AND "
                      "status='open'", (co["id"],)).fetchone()[0] == 0


# ---------------------------------------------------------------- first answer wins

def test_same_user_retry_is_200_and_one_ledger_row(db, world):
    co, site, pm, _ = world
    card = _card(db, co, site, [(pm, "pm")])
    assert _post(db, pm, card["card_id"], {"option_id": "done"}) == (200, ANSWERED)
    assert _post(db, pm, card["card_id"], {"option_id": "done"}) == (200, ANSWERED)
    assert len(_ledger(db, card["card_id"])) == 1
    # the same user with a DIFFERENT option is stale
    assert _post(db, pm, card["card_id"], {"option_id": "not_needed"}) == (409, {"status": "stale"})
    assert len(_ledger(db, card["card_id"])) == 1


def test_other_user_after_answer_is_409(db, world):
    co, site, pm, sm = world
    card = _card(db, co, site, [(pm, "pm"), (sm, "site_manager")])
    assert _post(db, pm, card["card_id"], {"option_id": "done"})[0] == 200
    assert _post(db, sm, card["card_id"], {"option_id": "done"}) == (409, {"status": "stale"})
    assert len(_ledger(db, card["card_id"])) == 1


def test_superseded_between_get_and_post_is_409(db, world):
    co, site, pm, _ = world
    card = _gap_card(db, co, site, [(pm, "pm")])
    dc.supersede(db, co["id"], site["id"], card["card_key"])
    assert _post(db, pm, card["card_id"], {"option_id": "accept", "reason": "r"}) == \
        (409, {"status": "stale"})
    assert _ledger(db, card["card_id"]) == []
    assert _state(db, card["card_id"])[0] == "superseded"


# ---------------------------------------------------------------- 404s

def test_not_a_recipient_or_removed_is_404(db, world):
    co, site, pm, sm = world
    card = _card(db, co, site, [(pm, "pm")])
    assert _post(db, sm, card["card_id"], {"option_id": "done"})[0] == 404
    memberships.archive_membership(db, pm["id"], site["id"])
    assert _post(db, pm, card["card_id"], {"option_id": "done"})[0] == 404
    assert _state(db, card["card_id"])[0] == "open"


def test_unknown_malformed_or_other_company_card_is_404(db, world, monkeypatch):
    co, site, pm, _ = world
    assert _post(db, pm, uuid.uuid4(), {"option_id": "done"})[0] == 404
    assert _post(db, pm, "not-a-uuid", {"option_id": "done"})[0] == 404
    card = _card(db, co, site, [(pm, "pm")])
    monkeypatch.setenv("DECISION_INBOX_COMPANIES", str(uuid.uuid4()))
    assert _post(db, pm, card["card_id"], {"option_id": "done"})[0] == 404
    assert _state(db, card["card_id"])[0] == "open"


def test_404_is_decided_before_the_body(db, world):
    co, site, pm, sm = world
    card = _card(db, co, site, [(pm, "pm")])
    assert _post(db, sm, card["card_id"], "not json")[0] == 404        # existence not leaked


@pytest.mark.parametrize("rule_id,kind", [("todo.overdue_backlog", "fact"),
                                          ("hub.fault", "fault"), ("x.y", "rule")])
def test_digest_fault_rule_cards_are_404(db, world, rule_id, kind):
    co, site, pm, _ = world
    card = _card(db, co, site, [(pm, "pm")], rule_id=rule_id, kind=kind,
                 options=[{"id": "open_list", "label": "Open the list"},
                          {"id": "acknowledge", "label": "Acknowledge"}])
    assert _post(db, pm, card["card_id"], {"option_id": "open_list"})[0] == 404
    assert _state(db, card["card_id"])[0] == "open"


def test_flags_off_404(db, world, monkeypatch):
    co, site, pm, _ = world
    card = _card(db, co, site, [(pm, "pm")])
    monkeypatch.setenv("DECISION_INBOX", "off")
    assert _post(db, pm, card["card_id"], {"option_id": "done"})[0] == 404
    monkeypatch.setenv("DECISION_INBOX", "companies")
    monkeypatch.setenv("SITE_DECISION_LOOP", "off")
    assert _post(db, pm, card["card_id"], {"option_id": "done"})[0] == 404
    assert _state(db, card["card_id"])[0] == "open"


# ---------------------------------------------------------------- two connections

def _committed_world(url):
    with psycopg.connect(url) as c:
        co = companies.create_company(c, f"Co-dia2-{_tag()}")
        site = sites.create_site(c, co["id"], f"Site-dia2-{_tag()}")
        a, b = _user(c, co), _user(c, co)
        memberships.add_membership(c, a["id"], site["id"], "pm")
        memberships.add_membership(c, b["id"], site["id"], "site_manager")
        card = _card(c, co, site, [(a, "pm"), (b, "site_manager")])
    return co, site, a, b, card


def _cleanup(url, co, site, a, b, card):
    with psycopg.connect(url) as c:
        assert c.execute("SELECT count(*) FROM decision_ledger WHERE card_id=%s",
                         (card["card_id"],)).fetchone()[0] == 0
        c.execute("DELETE FROM decision_cards WHERE card_id=%s", (card["card_id"],))
        c.execute("DELETE FROM memberships WHERE site_id=%s", (site["id"],))
        c.execute("DELETE FROM users WHERE id = ANY(%s)", ([a["id"], b["id"]],))
        c.execute("DELETE FROM sites WHERE id=%s", (site["id"],))
        c.execute("DELETE FROM companies WHERE id=%s", (co["id"],))


def _wait_until_blocked(url, pid, timeout=10.0):
    end = time.monotonic() + timeout
    with psycopg.connect(url, autocommit=True) as c:
        while time.monotonic() < end:
            row = c.execute("SELECT wait_event_type FROM pg_stat_activity WHERE pid=%s",
                            (pid,)).fetchone()
            if row and row[0] == "Lock":
                return True
            time.sleep(0.05)
    return False


@pytest.mark.parametrize("a_commits,b_expect", [(True, (409, {"status": "stale"})),
                                                (False, (200, ANSWERED))])
def test_two_connections_first_answer_wins(migrated_db_url, monkeypatch, a_commits, b_expect):
    url = migrated_db_url
    co, site, a, b, card = _committed_world(url)
    _setenv(monkeypatch, str(co["id"]))
    conn_a, conn_b = psycopg.connect(url), psycopg.connect(url)
    result = {}
    try:
        # A holds the guarded UPDATE, uncommitted (a remind: no ledger row either way)
        assert dc.answer(conn_a, card["card_id"], co["id"], site["id"], a["id"], "remind",
                         None) is not None

        def run_b():
            try:
                result["b"] = _post(conn_b, b, card["card_id"], {"option_id": "remind"})
                conn_b.commit()
            except Exception as exc:  # noqa: BLE001 - surfaced by the assert below
                result["exc"] = exc

        t = threading.Thread(target=run_b)
        t.start()
        assert _wait_until_blocked(url, conn_b.info.backend_pid), "B never blocked on A"
        assert "b" not in result
        conn_a.commit() if a_commits else conn_a.rollback()
        t.join(timeout=10)
        assert not t.is_alive()
        assert "exc" not in result, result.get("exc")
        assert result["b"] == b_expect
        with psycopg.connect(url) as c:
            st = c.execute("SELECT status, answered_by FROM decision_cards WHERE card_id=%s",
                           (card["card_id"],)).fetchone()
        assert st == ("answered", a["id"] if a_commits else b["id"])
    finally:
        conn_a.close()
        conn_b.close()
        _cleanup(url, co, site, a, b, card)
