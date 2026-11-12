"""Integration: GET /api/org/decision-cards, the decision-card inbox (spec 2026-11-12 rev 2 §2, §4).

The real dispatch against real Postgres: flags, allow-list, still-a-recipient (site
membership roles, external members, company admins), exclusions, inactive companies, the
cap, the live digest count, and the deletion redaction shared with the shadow route.
S3 is a fake holding the deletion mirror.

Skipped without TEST_DATABASE_URL -- a skip is NOT a pass.
"""
import datetime as dt
import json
import uuid

import pytest

import deletion_mirror
from repositories import action_items, companies, memberships, sites, topics, users
from repositories import decision_cards as dc
from repositories import site_events as se

pytestmark = pytest.mark.integration

org = pytest.importorskip("lambda_org_api")

UTC = dt.timezone.utc
NOW = dt.datetime(2026, 11, 10, 1, 0, tzinfo=UTC)          # 14:00 NZDT, Tue 10 Nov
DATE = "2026-11-09"
FRESH_DEADLINE = dt.date(2026, 11, 8)                       # overdue, not stale on NOW's day
STALE_DEADLINE = dt.date(2026, 11, 3)                       # stale (is_late) on NOW's day


class FakeS3:
    def __init__(self):
        self.objs = {}

    def get_object(self, Bucket, Key):
        import io
        from botocore.exceptions import ClientError
        if (Bucket, Key) not in self.objs:
            raise ClientError({"Error": {"Code": "NoSuchKey", "Message": "x"}}, "GetObject")
        return {"Body": io.BytesIO(self.objs[(Bucket, Key)])}


class Allow:
    """Sets DECISION_INBOX_COMPANIES to the companies added so far."""

    def __init__(self, monkeypatch):
        self.mp, self.ids = monkeypatch, []

    def add(self, co):
        self.ids.append(str(co["id"]))
        self.mp.setenv("DECISION_INBOX_COMPANIES", ",".join(self.ids))
        return co


@pytest.fixture
def env(db, monkeypatch):
    monkeypatch.setenv("SITE_DECISION_LOOP", "shadow")
    monkeypatch.setenv("DECISION_INBOX", "companies")
    monkeypatch.setenv("DECISION_INBOX_COMPANIES", "")
    monkeypatch.delenv("COMPANY_STATUS_ENFORCE", raising=False)
    monkeypatch.setattr(db, "commit", lambda: None)
    fake = FakeS3()
    monkeypatch.setattr(org, "s3", lambda: fake)
    monkeypatch.setattr(org, "S3_BUCKET", "data-bucket")
    monkeypatch.setattr(org, "LAKE_BUCKET", "lake-bucket")
    monkeypatch.setattr(org, "_inbox_now", lambda: NOW)
    return fake


@pytest.fixture
def allow(monkeypatch):
    return Allow(monkeypatch)


def _tag():
    return uuid.uuid4().hex[:8]


def _user(db, co, role="worker"):
    t = _tag()
    u = users.upsert_user(db, f"sub-dib-{t}", f"dib-{t}@example.com",
                          company_id=co["id"], global_role=role)
    return users.get_user_by_sub(db, u["cognito_sub"])


def _world(db, allow, name=None):
    co = allow.add(companies.create_company(db, f"Co-dib-{_tag()}"))
    site = sites.create_site(db, co["id"], name or f"Site-dib-{_tag()}")
    pm = _user(db, co)
    memberships.add_membership(db, pm["id"], site["id"], "pm")
    return co, site, pm


def _event(db, co, site, type_="todo.overdue", payload=None):
    row, _ = se.insert(db, {
        "company_id": co["id"], "site_id": site["id"], "source": "action_items",
        "type": type_, "subject_ref": f"x|{_tag()}", "bucket": "",
        "occurred_at": NOW, "received_at": NOW, "summary": None, "link": None,
        "payload": payload, "dedupe_key": f"dib:{_tag()}", "status": "done"})
    return row["event_id"]


def _card(db, co, site, recips, **over):
    card = {"card_key": f"key:{_tag()}", "rule_id": "checklist.gap", "purpose": "gap",
            "subject_ref": f"s|Scaffold|{_tag()}", "kind": "choose",
            "question": "'Rails' on the Scaffold checklist failed", "consequence": "c",
            "options": [{"id": "remind", "label": "Remind Sam to recheck", "recommended": True},
                        {"id": "accept", "label": "Accept with reason", "recommended": False,
                         "reason_required": True}],
            "evidence": [], "urgency": "normal", "deadline_at": None,
            "timeout_default": "nothing changes",
            "recipients": [{"user_id": str(u["id"]), "role": role} for u, role in recips],
            "event_id": None, "company_id": co["id"], "site_id": site["id"]}
    card.update(over)
    return dc.insert_open(db, card)


def _at(db, card, when):
    db.execute("UPDATE decision_cards SET created_at=%s WHERE card_id=%s",
               (when, card["card_id"]))


def _item(db, site, deadline, text="Order the rebar"):
    t = topics.upsert_topic(db, site["id"], DATE, f"T-{_tag()}",
                            source_s3_key=f"extractions/x/{DATE}/{_tag()}.json")
    return action_items.insert_item(db, t["id"], site["id"], text, responsible="Mike",
                                    deadline=deadline)


def _call(db, u, method="GET", route="/decision-cards", body=None):
    ev = {"httpMethod": method, "path": "/api/org" + route, "queryStringParameters": None,
          "body": None if body is None else json.dumps(body), "headers": {},
          "requestContext": {"authorizer": {"claims": {"sub": u["cognito_sub"]}}}}
    res = org.dispatch(db, ev, method, route)
    return res["statusCode"], json.loads(res["body"])


def _ids(body):
    return [c["card_id"] for c in body["cards"]]


# ---------------------------------------------------------------- flags

@pytest.mark.parametrize("var,value", [("DECISION_INBOX", "off"), ("DECISION_INBOX", ""),
                                       ("SITE_DECISION_LOOP", "off"),
                                       ("SITE_DECISION_LOOP", "")])
def test_either_flag_off_is_404(db, env, allow, monkeypatch, var, value):
    co, site, pm = _world(db, allow)
    _card(db, co, site, [(pm, "pm")])
    monkeypatch.setenv(var, value)
    assert _call(db, pm)[0] == 404


def test_on_returns_cards_digest_more(db, env, allow):
    co, site, pm = _world(db, allow)
    card = _card(db, co, site, [(pm, "pm")])
    _at(db, card, dt.datetime(2026, 11, 9, 22, 0, tzinfo=UTC))
    status, body = _call(db, pm)
    assert status == 200
    assert set(body) == {"cards", "digest", "more"}
    assert body["digest"] is None and body["more"] == 0
    [c] = body["cards"]
    assert c == {
        "card_id": str(card["card_id"]), "kind": "choose", "site_id": str(site["id"]),
        "site_name": site["name"], "created_at": "2026-11-09T22:00:00+00:00",
        "source_label": "Missing from the inspection", "sub_label": None,
        "question": "'Rails' on the Scaffold checklist failed", "item": None,
        # no originating event -> the source is unresolvable -> removed (fail closed):
        # the person-free remind label, exactly as the shadow route serves it
        "options": [{"option_id": "remind", "label": "Remind the inspector to recheck",
                     "recommended": True},
                    {"option_id": "accept", "label": "Accept with reason",
                     "recommended": False, "reason_required": True}],
        "evidence": []}


def test_nothing_for_you_is_200_empty(db, env, allow):
    co, site, pm = _world(db, allow)
    stranger = _user(db, co)
    _card(db, co, site, [(pm, "pm")])
    assert _call(db, stranger) == (200, {"cards": [], "digest": None, "more": 0})


# ---------------------------------------------------------------- still a recipient

def test_removed_from_site_loses_the_card(db, env, allow):
    co, site, pm = _world(db, allow)
    _card(db, co, site, [(pm, "pm")])
    assert len(_call(db, pm)[1]["cards"]) == 1
    memberships.archive_membership(db, pm["id"], site["id"])
    assert _call(db, pm)[1]["cards"] == []


def test_role_must_match_the_stored_role(db, env, allow):
    co, site, pm = _world(db, allow)
    sm = _user(db, co)
    memberships.add_membership(db, sm["id"], site["id"], "site_manager")
    _card(db, co, site, [(sm, "site_manager")])
    assert len(_call(db, sm)[1]["cards"]) == 1
    # demoted to worker on the site: the stored site_manager role no longer holds
    db.execute("UPDATE memberships SET role='worker' WHERE user_id=%s AND site_id=%s",
               (sm["id"], site["id"]))
    assert _call(db, sm)[1]["cards"] == []


def test_external_member_of_allow_listed_site_sees_it(db, env, allow):
    co, site, _ = _world(db, allow)
    home = companies.create_company(db, f"Co-ext-{_tag()}")              # NOT allow-listed
    ext = _user(db, home)
    memberships.add_external_membership(db, ext["id"], site["id"], "pm")
    card = _card(db, co, site, [(ext, "pm")])
    assert _ids(_call(db, ext)[1]) == [str(card["card_id"])]


@pytest.mark.parametrize("role,sees", [("admin", True), ("gm", True),
                                       ("regional_manager", True), ("pm", False),
                                       ("worker", False)])
def test_company_admin_roles(db, env, allow, role, sees):
    co, site, _ = _world(db, allow)
    u = _user(db, co, role)
    _card(db, co, site, [(u, "company_admin")], urgency="injury")
    assert bool(_call(db, u)[1]["cards"]) is sees


def test_company_admin_of_another_company_does_not_see(db, env, allow):
    co, site, _ = _world(db, allow)
    other = allow.add(companies.create_company(db, f"Co-oth-{_tag()}"))
    u = _user(db, other, "admin")
    _card(db, co, site, [(u, "company_admin")])
    assert _call(db, u)[1]["cards"] == []


def test_other_stored_role_not_served(db, env, allow):
    co, site, pm = _world(db, allow)
    _card(db, co, site, [(pm, "ops")])
    assert _call(db, pm)[1]["cards"] == []


def test_company_not_allow_listed_is_empty(db, env, allow, monkeypatch):
    co, site, pm = _world(db, allow)
    _card(db, co, site, [(pm, "pm")])
    monkeypatch.setenv("DECISION_INBOX_COMPANIES", str(uuid.uuid4()))
    assert _call(db, pm) == (200, {"cards": [], "digest": None, "more": 0})


# ---------------------------------------------------------------- universe

def test_fault_rule_and_backlog_cards_are_not_cards(db, env, allow):
    co, site, pm = _world(db, allow)
    keep = _card(db, co, site, [(pm, "pm")])
    _card(db, co, site, [(pm, "pm")], rule_id="hub.fault", kind="fault",
          options=[{"id": "acknowledge", "label": "Acknowledge"}])
    _card(db, co, site, [(pm, "pm")], rule_id="x.y", kind="rule")
    _card(db, co, site, [(pm, "pm")], rule_id="todo.overdue_backlog", kind="fact",
          options=[{"id": "open_list", "label": "Open the list"}])
    status, body = _call(db, pm)
    assert status == 200 and _ids(body) == [str(keep["card_id"])] and body["more"] == 0


def test_card_without_options_is_skipped(db, env, allow):
    co, site, pm = _world(db, allow)
    _card(db, co, site, [(pm, "pm")], options=[])
    _card(db, co, site, [(pm, "pm")], options=[{"label": "no id"}])
    assert _call(db, pm) == (200, {"cards": [], "digest": None, "more": 0})


def test_inactive_company_with_enforce_on_is_empty(db, env, allow, monkeypatch):
    co, site, pm = _world(db, allow)
    home = companies.create_company(db, f"Co-home-{_tag()}")
    ext = _user(db, home)
    memberships.add_external_membership(db, ext["id"], site["id"], "pm")
    _card(db, co, site, [(ext, "pm")])
    assert len(_call(db, ext)[1]["cards"]) == 1
    db.execute("UPDATE companies SET status='suspended' WHERE id=%s", (co["id"],))
    assert len(_call(db, ext)[1]["cards"]) == 1          # flag off: served like every read
    monkeypatch.setenv("COMPANY_STATUS_ENFORCE", "on")
    assert _call(db, ext) == (200, {"cards": [], "digest": None, "more": 0})


# ---------------------------------------------------------------- cap and order

def test_cap_three_and_more(db, env, allow):
    co, site, pm = _world(db, allow)
    cards = [_card(db, co, site, [(pm, "pm")]) for _ in range(5)]
    for i, c in enumerate(cards):
        _at(db, c, NOW - dt.timedelta(hours=5 - i))
    body = _call(db, pm)[1]
    assert _ids(body) == [str(c["card_id"]) for c in cards[:3]] and body["more"] == 2


def test_injury_cards_never_cut(db, env, allow):
    co, site, pm = _world(db, allow)
    normal = _card(db, co, site, [(pm, "pm")], deadline_at=NOW)
    injury = [_card(db, co, site, [(pm, "pm")], urgency="injury") for _ in range(4)]
    body = _call(db, pm)[1]
    assert set(_ids(body)) == {str(c["card_id"]) for c in injury}
    assert str(normal["card_id"]) not in _ids(body) and body["more"] == 1


# ---------------------------------------------------------------- digest

def test_digest_count_is_live(db, env, allow):
    co, site, pm = _world(db, allow, name="Alpha")
    items = [_item(db, site, STALE_DEADLINE) for _ in range(2)]
    _item(db, site, FRESH_DEADLINE)                                       # overdue, not stale
    _card(db, co, site, [(pm, "pm")], rule_id="todo.overdue_backlog", kind="fact",
          subject_ref=str(site["id"]), options=[{"id": "open_list", "label": "Open the list"}],
          question="2 to-dos on Alpha are more than 3 days overdue")
    body = _call(db, pm)[1]
    assert body["cards"] == [] and body["more"] == 0
    assert body["digest"] == {"count": 2, "title": "2 overdue to-dos more than 3 days old",
                              "href": "#/tasks?filter=overdue-old"}
    items += [_item(db, site, STALE_DEADLINE) for _ in range(3)]
    assert _call(db, pm)[1]["digest"]["count"] == 5                       # today's number
    db.execute("UPDATE action_items SET status='done' WHERE site_id=%s", (site["id"],))
    assert _call(db, pm)[1]["digest"] is None                             # backlog cleared


def test_digest_multi_site_title_and_only_the_callers_sites(db, env, allow):
    co, a, pm = _world(db, allow, name="Bravo")
    b = sites.create_site(db, co["id"], "Alpha")
    memberships.add_membership(db, pm["id"], b["id"], "pm")
    c = sites.create_site(db, co["id"], "Charlie")                        # pm not a recipient
    for site, n in ((a, 1), (b, 2), (c, 4)):
        for _ in range(n):
            _item(db, site, STALE_DEADLINE)
    for site in (a, b):
        _card(db, co, site, [(pm, "pm")], rule_id="todo.overdue_backlog", kind="fact",
              subject_ref=str(site["id"]), options=[{"id": "open_list", "label": "Open"}])
    d = _call(db, pm)[1]["digest"]
    assert d["count"] == 3
    assert d["title"] == "3 overdue to-dos more than 3 days old on Alpha, Bravo"


def test_digest_backlog_card_of_removed_member_not_counted(db, env, allow):
    co, site, pm = _world(db, allow)
    _item(db, site, STALE_DEADLINE)
    _card(db, co, site, [(pm, "pm")], rule_id="todo.overdue_backlog", kind="fact",
          subject_ref=str(site["id"]), options=[{"id": "open_list", "label": "Open"}])
    memberships.archive_membership(db, pm["id"], site["id"])
    assert _call(db, pm)[1]["digest"] is None


# ---------------------------------------------------------------- overdue cards and deletion

def test_overdue_card_item_and_label(db, env, allow):
    co, site, pm = _world(db, allow)
    it = _item(db, site, FRESH_DEADLINE, text="Order the beams")
    eid = _event(db, co, site, payload={"text": "Order the beams", "deadline": "2026-11-08",
                                        "responsible": "Mike"})
    _card(db, co, site, [(pm, "pm")], rule_id="todo.overdue", event_id=eid,
          subject_ref=str(it["stable_id"]),
          question="'Order the beams' was due 8 Nov 2026, still open",
          options=[{"id": "remind", "label": "Remind Mike", "recommended": True},
                   {"id": "done", "label": "Mark done", "recommended": False}])
    [c] = _call(db, pm)[1]["cards"]
    assert c["source_label"] == "Overdue since 9 Nov"
    assert c["item"] == {"stable_id": str(it["stable_id"]), "title": "Order the beams"}
    # the item is no longer visible (non_work) -> the person-free copy and no item
    db.execute("UPDATE topics SET work_class = 'non_work' WHERE id = %s", (it["topic_id"],))
    [c] = _call(db, pm)[1]["cards"]
    assert c["item"] is None
    assert c["question"] == "A to-do that was removed was due 8 Nov 2026, still open"
    assert c["options"][0]["label"] == "Remind the owner"


def _report(db, co, folder, session):
    return db.execute(
        "INSERT INTO checklist_reports (company_id, user_folder, report_date, session, "
        "template_name, check_name, start_at, request_id, result_key) "
        "VALUES (%s,%s,%s,%s,'Scaffold','scaffold',%s,%s,%s) RETURNING id",
        (co["id"], folder, DATE, session, f"10:{_tag()[:2]}", _tag(), f"k-{_tag()}")
    ).fetchone()[0]


def test_deleted_session_same_copy_as_shadow_route(db, env, allow):
    co, site, pm = _world(db, allow)
    admin = _user(db, co, "platform_admin")
    folder = f"F_{_tag()}"
    rid = _report(db, co, folder, "sidaaaa1111")
    eid = _event(db, co, site, type_="checklist.gap",
                 payload={"session": "sidaaaa1111", "report_date": DATE,
                          "report_id": str(rid), "quote": "rails are loose"})
    card = _card(db, co, site, [(pm, "pm")], event_id=eid,
                 evidence=[{"quote": "rails are loose", "at": None}])
    [live] = _call(db, pm)[1]["cards"]
    assert live["evidence"] == [{"speaker_name": None, "said_at": None,
                                 "quote": "rails are loose", "audio": None}]
    env.objs[("data-bucket", deletion_mirror.mirror_key(folder, DATE))] = json.dumps(
        {"sessions": ["aaaa1111"]}).encode()
    [gone] = _call(db, pm)[1]["cards"]
    [shadow] = [c for c in _call(db, admin, route="/decision-shadow")[1]["cards"]
                if c["card_id"] == str(card["card_id"])]
    assert gone["evidence"] == [] == shadow["evidence"]
    assert gone["question"] == shadow["question"]
    assert [(o["option_id"], o["label"]) for o in gone["options"]] == \
        [(o["id"], o["label"]) for o in shadow["options"]]
    assert gone["options"][0]["label"] == "Remind the inspector to recheck"
    assert "Sam" not in json.dumps(gone)


# ---------------------------------------------------------------- bounded queries

class _Counting:
    """Delegating connection that counts every statement a route executes."""

    def __init__(self, conn):
        self._conn, self.n = conn, 0

    def __getattr__(self, name):
        return getattr(self._conn, name)

    def execute(self, *a, **k):
        self.n += 1
        return self._conn.execute(*a, **k)

    def cursor(self, *a, **k):
        cur, outer = self._conn.cursor(*a, **k), self

        class _Cur:
            def __getattr__(self, name):
                return getattr(cur, name)

            def execute(self, *a2, **k2):
                outer.n += 1
                cur.execute(*a2, **k2)
                return self

            def fetchone(self):
                return cur.fetchone()

            def fetchall(self):
                return cur.fetchall()

        return _Cur()


def test_query_count_bounded(db, env, allow):
    co, a, pm = _world(db, allow)
    b = sites.create_site(db, co["id"], f"Site-dib-{_tag()}")
    memberships.add_membership(db, pm["id"], b["id"], "pm")
    for i in range(10):
        _card(db, co, a if i % 2 else b, [(pm, "pm")])
    for site in (a, b):
        _item(db, site, STALE_DEADLINE)
        _card(db, co, site, [(pm, "pm")], rule_id="todo.overdue_backlog", kind="fact",
              subject_ref=str(site["id"]), options=[{"id": "open_list", "label": "Open"}])
    caller = users.get_user_by_sub(db, pm["cognito_sub"])
    counting = _Counting(db)
    res = org.decision_cards_route(counting, caller, {"httpMethod": "GET"}, "GET",
                                   "/decision-cards")
    body = json.loads(res["body"])
    assert res["statusCode"] == 200 and len(body["cards"]) == 3 and body["more"] == 7
    assert body["digest"]["count"] == 2
    assert counting.n <= 8, counting.n


def test_digest_ignores_other_tenants_stale_items(db, env, allow):
    co, site, pm = _world(db, allow, name="Alpha")
    other_co = companies.create_company(db, f"Co-oth-{_tag()}")          # not allow-listed
    other_site = sites.create_site(db, other_co["id"], "Zulu")
    _item(db, site, STALE_DEADLINE)
    for _ in range(4):
        _item(db, other_site, STALE_DEADLINE)
    _card(db, co, site, [(pm, "pm")], rule_id="todo.overdue_backlog", kind="fact",
          subject_ref=str(site["id"]), options=[{"id": "open_list", "label": "Open"}])
    assert _call(db, pm)[1]["digest"]["count"] == 1
    # the site filter is in the SQL: the other tenant's rows never come back from it
    today = dt.date(2026, 11, 10)
    got = action_items.overdue_open(db, today, site_ids=[site["id"]])
    assert {str(r["site_id"]) for r in got} == {str(site["id"])} and len(got) == 1
    everything = action_items.overdue_open(db, today)                     # the scan's call
    assert sum(1 for r in everything if r["site_id"] == other_site["id"]) == 4
    assert action_items.overdue_open(db, today, site_ids=[]) == []


def test_overdue_open_site_filter_keeps_the_newest_row_rule(db, env, allow):
    """An item whose newest live row moved to another site is not counted under the old
    site: the filter runs after DISTINCT ON picks the newest row (same predicate)."""
    co, a, pm = _world(db, allow)
    b = sites.create_site(db, co["id"], f"Site-dib-{_tag()}")
    old = _item(db, a, STALE_DEADLINE)
    t = topics.upsert_topic(db, b["id"], "2026-11-09", f"T-{_tag()}",
                            source_s3_key=f"extractions/y/2026-11-09/{_tag()}.json")
    newer = action_items.insert_item(db, t["id"], b["id"], "moved", deadline=STALE_DEADLINE)
    db.execute("UPDATE action_items SET stable_id = %s, created_at = now() + interval '1 hour' "
               "WHERE id = %s", (old["stable_id"], newer["id"]))
    today = dt.date(2026, 11, 10)
    assert action_items.overdue_open(db, today, site_ids=[a["id"]]) == []
    moved = action_items.overdue_open(db, today, site_ids=[b["id"]])
    assert [str(r["site_id"]) for r in moved] == [str(b["id"])]
