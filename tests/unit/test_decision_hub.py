"""Pure decision module: rules, late window, dedupe, card shapes, recipients.

No DB, no network. The scan Lambda (Task 4) and the replay judge (Task 7) both
call these functions, so they must be deterministic and import without
psycopg / boto3 / org modules.
"""
import ast
import pathlib
from datetime import date, datetime, timezone

import pytest

import nz_time
import decision_hub as hub
from decision_hub import Event
from decision_rules import RULES

SRC = pathlib.Path(__file__).resolve().parents[2] / "src"


def _utc(y, m, d, h=2):
    # 02:00 UTC in November is 15:00 NZDT: never near the NZ midnight.
    return datetime(y, m, d, h, 0, 0, tzinfo=timezone.utc)


def _nz_midnight(d):
    return nz_time.from_nz(datetime(d.year, d.month, d.day)).replace(tzinfo=timezone.utc)


def _overdue(received_at=None, payload=None, **kw):
    received_at = received_at or _utc(2026, 11, 6)
    base = dict(
        event_id="e-1", company_id="c-1", site_id="s-1", source="scan",
        type="todo.overdue", subject_ref="todo:42",
        occurred_at=received_at, received_at=received_at,
        payload=payload if payload is not None else
        {"text": "Order rebar", "deadline": "2026-11-01", "responsible": "Tom"},
        chain_id="ch-1", chain_depth=0,
    )
    base.update(kw)
    return Event(**base)


def _gap(report_date=date(2026, 11, 2), received_at=None, payload=None, **kw):
    received_at = received_at or _utc(2026, 11, 6)
    base = dict(
        event_id="e-2", company_id="c-1", site_id="s-1", source="scan",
        type="checklist.gap", subject_ref="rk:3:Cover to reinforcing",
        occurred_at=_nz_midnight(report_date), received_at=received_at,
        payload=payload if payload is not None else
        {"checklist": "Pre-pour", "item": "Cover to reinforcing",
         "kind": "not_covered", "session": "s1", "quote": None},
        chain_id="ch-2", chain_depth=0,
    )
    base.update(kw)
    return Event(**base)


# --- purity -----------------------------------------------------------------

def test_modules_import_only_stdlib_and_nz_time():
    allowed = {"nz_time", "decision_hub", "decision_rules", "dataclasses",
               "datetime", "typing", "__future__"}
    # A run-rule's handler is imported inside the function that calls it (call time, never
    # module load), so importing the rules still needs no database driver.
    lazy_ok = {"decision_rules.py": {"nightly_rematch"}}
    for name in ("decision_hub.py", "decision_rules.py"):
        tree = ast.parse((SRC / name).read_text(encoding="utf-8"))
        in_function = set()
        for fn in ast.walk(tree):
            if isinstance(fn, (ast.FunctionDef, ast.AsyncFunctionDef)):
                in_function |= {id(n) for n in ast.walk(fn)}
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                mods = [a.name.split(".")[0] for a in node.names]
            elif isinstance(node, ast.ImportFrom):
                mods = [(node.module or "").split(".")[0]]
            else:
                continue
            for m in mods:
                if id(node) in in_function and m in lazy_ok.get(name, set()):
                    continue
                assert m in allowed, f"{name} imports {m}"


# --- dedupe / late / match --------------------------------------------------

def test_dedupe_key_overdue_buckets_by_nz_day():
    e = _overdue()
    k1 = hub.dedupe_key(e, date(2026, 11, 6))
    k2 = hub.dedupe_key(e, date(2026, 11, 7))
    k1b = hub.dedupe_key(_overdue(event_id="other"), date(2026, 11, 6))
    assert k1 != k2
    assert k1 == k1b
    assert k1 == "c-1|s-1|todo.overdue|todo:42|2026-11-06"


def test_dedupe_key_gap_has_no_bucket():
    e = _gap()
    assert hub.dedupe_key(e, date(2026, 11, 6)) == hub.dedupe_key(e, date(2026, 11, 9))
    assert hub.dedupe_key(e, date(2026, 11, 6)) == \
        "c-1|s-1|checklist.gap|rk:3:Cover to reinforcing|"


def test_dedupe_key_unknown_type_has_empty_bucket():
    e = _overdue(type="mystery.thing")
    assert hub.dedupe_key(e, date(2026, 11, 6)).endswith("|mystery.thing|todo:42|")


def test_late_gap_after_7_days():
    rule = RULES["checklist.gap"]
    today = date(2026, 11, 10)
    assert hub.is_late(_gap(report_date=date(2026, 11, 2)), rule, today) is True
    assert hub.is_late(_gap(report_date=date(2026, 11, 3)), rule, today) is False


def test_overdue_is_late_after_its_first_three_overdue_days():
    """Rev 6 (option B): occurred_at = 00:00 NZ of deadline + 1 day; late_after_days = 2, so
    an item is carded on its first three overdue NZ days and late (digest) after that."""
    rule = RULES["todo.overdue"]
    assert rule.late_after_days == 2
    today = date(2026, 11, 10)

    def item(deadline):
        return _overdue(occurred_at=_nz_midnight(date.fromordinal(deadline.toordinal() + 1)),
                        received_at=_utc(2026, 11, 10))
    assert hub.is_late(item(date(2026, 11, 9)), rule, today) is False    # today - 1
    assert hub.is_late(item(date(2026, 11, 7)), rule, today) is False    # today - 3
    assert hub.is_late(item(date(2026, 11, 6)), rule, today) is True     # today - 4
    assert hub.is_late(item(date(2025, 1, 1)), rule, today) is True


def _backlog(payload=None, **kw):
    received_at = _utc(2026, 11, 10)
    base = dict(
        event_id="e-3", company_id="c-1", site_id="s-1", source="action_items",
        type="todo.overdue_backlog", subject_ref="s-1",
        occurred_at=received_at, received_at=received_at,
        payload=payload if payload is not None else
        {"count": 7, "site_name": "UC PK", "items": []},
        chain_id=None, chain_depth=0,
    )
    base.update(kw)
    return Event(**base)


def test_overdue_backlog_rule_and_card():
    rule = RULES["todo.overdue_backlog"]
    assert hub.match(_backlog()) is rule
    assert (rule.rule_id, rule.purpose, rule.late_after_days) == (
        "todo.overdue_backlog", "overdue_backlog", 1)
    assert rule.bucket(_backlog(), date(2026, 11, 10)) == "2026-11-10"
    assert hub.card_key(_backlog(), rule) == "c-1|s-1|todo.overdue_backlog|s-1|overdue_backlog"
    card = rule.build_card(_backlog())
    assert card.kind == "fact"
    assert card.question == "7 to-dos on UC PK are more than 3 days overdue"
    assert card.consequence == "They stay on the open to-do list until someone acts."
    assert card.options == [{"id": "open_list", "label": "Open the list", "recommended": True}]
    assert card.evidence == [] and card.urgency == "normal"
    assert card.deadline_at is None
    assert card.timeout_default == "nothing changes"
    assert card.recipient_roles == ["pm", "site_manager"]


def test_overdue_backlog_card_singular_and_unnamed_site():
    rule = RULES["todo.overdue_backlog"]
    one = rule.build_card(_backlog(payload={"count": 1, "site_name": None, "items": []}))
    assert one.question == "1 to-do on this site is more than 3 days overdue"
    many = rule.build_card(_backlog(payload={"count": 60, "site_name": "  ", "items": []}))
    assert many.question == "60 to-dos on this site are more than 3 days overdue"


def test_unknown_type_matches_nothing():
    assert hub.match(_overdue(type="mystery.thing")) is None
    assert hub.match(_overdue()) is RULES["todo.overdue"]
    assert hub.match(_gap()) is RULES["checklist.gap"]


def test_too_deep_only_above_3():
    assert hub.too_deep(_overdue(chain_depth=3)) is False
    assert hub.too_deep(_overdue(chain_depth=4)) is True


def test_backoff_schedule():
    assert [hub.backoff_seconds(a) for a in range(1, 7)] == \
        [300, 600, 1200, 2400, 3600, 3600]
    assert hub.FAULT_AT == 3 and hub.STOP_AT == 5


# --- working day ------------------------------------------------------------

def test_next_working_day_skips_sunday_only():
    # Real calendar: Fri 6 Nov, Sat 7 Nov, Sun 8 Nov, Mon 9 Nov 2026.
    fri = hub.next_working_day_end_nz(date(2026, 11, 6))
    sat = hub.next_working_day_end_nz(date(2026, 11, 7))
    sun = hub.next_working_day_end_nz(date(2026, 11, 8))
    assert nz_time.to_nz(fri) == datetime(2026, 11, 7, 23, 59, 59)
    assert nz_time.to_nz(sat) == datetime(2026, 11, 9, 23, 59, 59)
    assert nz_time.to_nz(sun) == datetime(2026, 11, 9, 23, 59, 59)
    assert sat.tzinfo is not None
    # 23:59:59 NZDT is 10:59:59 UTC.
    assert sat == datetime(2026, 11, 9, 10, 59, 59, tzinfo=timezone.utc)


# --- card shapes ------------------------------------------------------------

def test_overdue_card_shape():
    card = RULES["todo.overdue"].build_card(_overdue(received_at=_utc(2026, 11, 7)))
    assert card.kind == "choose"
    assert "Order rebar" in card.question and "1 Nov 2026" in card.question
    assert [o["id"] for o in card.options] == ["remind", "done", "new_date", "not_needed"]
    assert [o["id"] for o in card.options if o["recommended"]] == ["remind"]
    assert "Tom" in card.options[0]["label"]
    assert card.timeout_default == "nothing changes"
    assert card.urgency == "normal"
    assert card.evidence == []
    assert card.recipient_roles == ["pm", "site_manager"]
    # Received Saturday 7 Nov NZ -> end of Monday 9 Nov NZ.
    assert nz_time.to_nz(card.deadline_at) == datetime(2026, 11, 9, 23, 59, 59)
    # Received Friday 6 Nov NZ -> end of Saturday 7 Nov NZ.
    fri = RULES["todo.overdue"].build_card(_overdue(received_at=_utc(2026, 11, 6)))
    assert nz_time.to_nz(fri.deadline_at) == datetime(2026, 11, 7, 23, 59, 59)


def test_overdue_card_without_valid_deadline_invents_no_date():
    card = RULES["todo.overdue"].build_card(
        _overdue(payload={"text": "Order rebar", "deadline": "soon", "responsible": None}))
    assert "Order rebar" in card.question
    assert "soon" not in card.question
    assert "due" not in card.question
    assert card.options[0]["label"] == "Remind the owner"


def test_gap_card_shape():
    card = RULES["checklist.gap"].build_card(_gap())
    assert card.kind == "choose"
    assert [o["id"] for o in card.options] == ["remind", "accept", "reassign"]
    assert [o["id"] for o in card.options if o["recommended"]] == ["remind"]
    accept = card.options[1]
    assert accept["reason_required"] is True
    assert card.options[0]["label"] == "Remind the inspector to recheck"
    assert "Cover to reinforcing" in card.question and "Pre-pour" in card.question
    assert "not covered" in card.question
    assert card.urgency == "normal"
    assert card.recipient_roles == ["pm", "site_manager"]
    assert card.timeout_default == "nothing changes"
    assert card.evidence == []          # quote is None: no evidence invented
    assert nz_time.to_nz(card.deadline_at) == datetime(2026, 11, 7, 23, 59, 59)


def test_gap_card_failed_item_with_quote_and_person():
    card = RULES["checklist.gap"].build_card(_gap(payload={
        "checklist": "Pre-pour", "item": "Cover to reinforcing", "kind": "failed",
        "session": "s1", "quote": "cover looks short here", "responsible": "Tom"}))
    assert "failed" in card.question
    assert card.evidence == [{"quote": "cover looks short here", "at": None}]
    assert card.options[0]["label"] == "Remind Tom to recheck"


def test_gap_on_safety_item_is_injury_urgency_and_copies_admin():
    card = RULES["checklist.gap"].build_card(_gap(payload={
        "checklist": "Pre-pour", "item": "Edge protection hazard",
        "kind": "not_covered", "session": "s1", "quote": None}))
    assert card.urgency == "injury"
    assert "company_admin" in card.recipient_roles
    assert card.timeout_default == "not accepted"


def test_safety_from_checklist_title_and_none_tolerated():
    card = RULES["checklist.gap"].build_card(_gap(payload={
        "checklist": "Site H&S walk", "item": None, "kind": "not_covered"}))
    assert card.urgency == "injury"
    assert hub.is_safety("Health and Safety induction") is True
    assert hub.is_safety("SAFETY rails") is True
    assert hub.is_safety("Cover to reinforcing") is False
    assert hub.is_safety(None) is False


# --- recipients -------------------------------------------------------------

SITE_MEMBERS = [
    {"id": 1, "global_role": "worker", "site_role": "pm"},
    {"id": 2, "global_role": "worker", "site_role": "site_manager"},
    {"id": 3, "global_role": "worker", "site_role": "worker"},
]
COMPANY_USERS = [
    {"id": 10, "global_role": "admin", "site_role": None},
    {"id": 11, "global_role": "worker", "site_role": None},
]
PLATFORM_ADMINS = [{"id": 99, "global_role": "platform_admin", "site_role": None}]


def test_recipients_v1_roles():
    normal = RULES["todo.overdue"].build_card(_overdue())
    assert hub.recipients(normal, SITE_MEMBERS, COMPANY_USERS, PLATFORM_ADMINS) == [
        {"user_id": "1", "role": "pm"}, {"user_id": "2", "role": "site_manager"}]
    fault = hub.fault_card(_overdue(), attempts=3, stopped=False)
    assert hub.recipients(fault, SITE_MEMBERS, COMPANY_USERS, PLATFORM_ADMINS) == [
        {"user_id": "10", "role": "company_admin"}, {"user_id": "99", "role": "ops"}]


def test_recipients_dedupe_first_role_wins():
    safety = RULES["checklist.gap"].build_card(_gap(payload={
        "checklist": "Hazard walk", "item": "x", "kind": "failed"}))
    members = [{"id": 10, "global_role": "admin", "site_role": "pm"}]
    company = [{"id": 10, "global_role": "admin", "site_role": None},
               {"id": 12, "global_role": "gm", "site_role": None},
               {"id": 13, "global_role": "regional_manager", "site_role": None}]
    assert hub.recipients(safety, members, company, PLATFORM_ADMINS) == [
        {"user_id": "10", "role": "pm"},
        {"user_id": "12", "role": "company_admin"},
        {"user_id": "13", "role": "company_admin"}]


# --- card keys --------------------------------------------------------------

def test_card_key_stable_across_versions():
    rule = RULES["todo.overdue"]
    a = _overdue(event_id="e-1", received_at=_utc(2026, 11, 6))
    b = _overdue(event_id="e-9", received_at=_utc(2026, 11, 9),
                 payload={"text": "Order rebar (edited)", "deadline": "2026-11-03"})
    assert hub.card_key(a, rule) == hub.card_key(b, rule) == \
        "c-1|s-1|todo.overdue|todo:42|overdue"
    g = RULES["checklist.gap"]
    assert hub.card_key(_gap(), g) == \
        "c-1|s-1|checklist.gap|rk:3:Cover to reinforcing|gap"
    assert RULES["checklist.gap"].rule_id == "checklist.gap"


def test_fault_card_and_keys():
    e = _overdue(event_id="e-77", chain_id="ch-5")
    running = hub.fault_card(e, attempts=3, stopped=False)
    stopped = hub.fault_card(e, attempts=5, stopped=True)
    assert running.kind == stopped.kind == "fault"
    assert running.recipient_roles == ["company_admin", "ops"]
    assert running.urgency == "normal"
    assert "stopped" not in running.consequence
    assert "stopped after 5 failures" in stopped.consequence
    assert "\n" not in running.question
    assert hub.fault_card_key("c-1", "s-1", "e-77", "fault") == "c-1|s-1|hub.fault|e-77|fault"
    deep = hub.too_deep_card(e)
    assert deep.kind == "fault" and deep.recipient_roles == ["company_admin", "ops"]
    assert hub.fault_card_key("c-1", "s-1", "ch-5", "too_deep") == \
        "c-1|s-1|hub.fault|ch-5|too_deep"


# --- today_nz at the NZ-midnight boundary -----------------------------------

@pytest.mark.parametrize("utc, nz_day", [
    # NZDT (UTC+13): 11:30Z on 6 Nov is 00:30 on 7 Nov in NZ
    (datetime(2026, 11, 6, 11, 30, tzinfo=timezone.utc), date(2026, 11, 7)),
    (datetime(2026, 11, 6, 10, 59, tzinfo=timezone.utc), date(2026, 11, 6)),
    # NZST (UTC+12): 12:30Z on 10 Jul is 00:30 on 11 Jul; 11:30Z is still 10 Jul
    (datetime(2026, 7, 10, 12, 30, tzinfo=timezone.utc), date(2026, 7, 11)),
    (datetime(2026, 7, 10, 11, 30, tzinfo=timezone.utc), date(2026, 7, 10)),
])
def test_today_nz_of_at_the_nz_midnight_boundary(utc, nz_day):
    e = _overdue(received_at=utc)
    assert hub.today_nz_of(e) == nz_day
    # the overdue dedupe bucket follows the event's NZ day
    assert RULES["todo.overdue"].bucket(e, hub.today_nz_of(e)) == nz_day.isoformat()
    assert hub.dedupe_key(e, hub.today_nz_of(e)).endswith("|" + nz_day.isoformat())


# --- run-rules (nightly plan Task 4) -----------------------------------------

def test_rule_requires_exactly_one_of_run_or_build_card():
    from decision_rules import Rule
    common = dict(rule_id="x.test", bucket=lambda e, d: "", late_after_days=None,
                  purpose="test")
    with pytest.raises(ValueError, match="exactly one"):
        Rule(**common)
    with pytest.raises(ValueError, match="exactly one"):
        Rule(**common, build_card=lambda e: None, run=lambda c, e, ctx: "done")
    assert Rule(**common, run=lambda c, e, ctx: "done").build_card is None
    assert Rule(**common, build_card=lambda e: None).run is None
    # the production rules are card rules, except the nightly handler (plan Task 6)
    assert {t for t, r in RULES.items() if r.run is not None} == {"nightly.due"}
    assert all(r.build_card is not None for t, r in RULES.items() if t != "nightly.due")


def test_run_context_is_frozen():
    ctx = hub.RunContext(run_id="r", company_id="c", site_id="s", now_utc=_utc(2026, 11, 6),
                         flag="shadow", s3_client=None, bucket=None)
    with pytest.raises(Exception):
        ctx.flag = "on"
    assert hub.RUN_OUTCOMES == ("done", "nothing")
