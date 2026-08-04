"""The four alerts, and the one rule that makes a quiet-device alert survivable.

Every alert is evaluated ONLY inside the window where it can be true. A device
in stock, or already collected, is legitimately dark — so silence there must
never fire. Without that, a quiet alert is noise, noise gets muted, and a muted
alert is worse than none because you believe you are covered.
"""

import datetime as dt

import src.device_alerts as da

TODAY = dt.date(2026, 8, 4)   # a Tuesday


def notion(device="FS-07", **over):
    row = {"page_id": "p-" + device, "device": device, "dispatched": None,
           "due_back": None, "returned": False, "client": None,
           "activated": None, "notes": None}
    row.update(over)
    return row


def ledger(device="FS-07", **over):
    row = {"asset_tag": device, "device_uuid": "u1", "uuid_trusted": True,
           "app_version": "1.4.2", "last_seen_at": None, "last_account_sub": None,
           "actual_site": None, "actual_company": None}
    row.update(over)
    return row


def seen(y, m, d):
    return dt.datetime(y, m, d, 9, 0, tzinfo=dt.timezone.utc)


def only(result, device="FS-07"):
    return next(r for r in result if r["device"] == device)


def run(notion_rows, ledger_rows, today=TODAY, quiet=7, grace=3):
    return da.derive(ledger_rows, notion_rows, today, quiet, grace)


# --- working days ---

def test_working_days_skips_the_weekend():
    # Fri 2026-07-31 -> Tue 2026-08-04 is 2 working days (Mon, Tue)
    assert da.working_days_between(dt.date(2026, 7, 31), TODAY) == 2


def test_working_days_is_zero_for_the_same_day():
    assert da.working_days_between(TODAY, TODAY) == 0


def test_working_days_is_zero_for_a_future_date():
    assert da.working_days_between(dt.date(2026, 8, 10), TODAY) == 0


# --- never activated ---

def test_handed_over_and_never_seen_past_the_grace_period_alerts():
    r = only(run([notion(dispatched=dt.date(2026, 7, 28))], [ledger()]))
    assert "never_activated" in r["alerts"]
    assert r["status"] == da.STATUS_UNACTIVATED


def test_inside_the_grace_period_it_stays_quiet():
    r = only(run([notion(dispatched=dt.date(2026, 8, 3))], [ledger()]))
    assert "never_activated" not in r["alerts"]


def test_a_device_still_in_stock_is_never_an_alert():
    r = only(run([notion(dispatched=None)], [ledger()]))
    assert r["alerts"] == []
    assert r["status"] == da.STATUS_IN_STOCK


def test_a_sighting_before_dispatch_does_not_count_as_activation():
    """Bench-tested before hand-over, then never switched on at the client."""
    r = only(run([notion(dispatched=dt.date(2026, 7, 28))],
                 [ledger(last_seen_at=seen(2026, 7, 20))]))
    assert "never_activated" in r["alerts"]


# --- quiet ---

def test_an_activated_device_gone_quiet_alerts():
    r = only(run([notion(dispatched=dt.date(2026, 7, 15))],
                 [ledger(last_seen_at=seen(2026, 7, 20))]))
    assert "quiet" in r["alerts"]
    assert r["status"] == da.STATUS_QUIET


def test_a_recently_seen_device_is_quiet_about_being_quiet():
    r = only(run([notion(dispatched=dt.date(2026, 7, 15))],
                 [ledger(last_seen_at=seen(2026, 8, 3))]))
    assert "quiet" not in r["alerts"]
    assert r["status"] == da.STATUS_IN_USE


def test_a_returned_device_never_raises_a_quiet_alert():
    r = only(run([notion(dispatched=dt.date(2026, 5, 1), returned=True)],
                 [ledger(last_seen_at=seen(2026, 6, 1))]))
    assert "quiet" not in r["alerts"]
    assert r["status"] == da.STATUS_IN_STOCK


def test_a_not_yet_activated_device_raises_never_activated_not_quiet():
    r = only(run([notion(dispatched=dt.date(2026, 7, 1))], [ledger(last_seen_at=None)]))
    assert "never_activated" in r["alerts"]
    assert "quiet" not in r["alerts"]


def test_the_quiet_threshold_is_configurable():
    rows, led = [notion(dispatched=dt.date(2026, 7, 15))], [ledger(last_seen_at=seen(2026, 7, 30))]
    assert "quiet" not in only(run(rows, led, quiet=7))["alerts"]
    assert "quiet" in only(run(rows, led, quiet=2))["alerts"]


# --- due back ---

def test_overdue_alerts_and_the_due_date_itself_does_not():
    assert "due_back" in only(run([notion(due_back=dt.date(2026, 8, 3))], [ledger()]))["alerts"]
    assert "due_back" not in only(run([notion(due_back=TODAY)], [ledger()]))["alerts"]


def test_a_returned_device_is_not_overdue():
    r = only(run([notion(due_back=dt.date(2026, 7, 1), returned=True)], [ledger()]))
    assert "due_back" not in r["alerts"]


# --- version ---

def test_a_device_below_the_highest_seen_version_alerts():
    rows = [notion("FS-01"), notion("FS-02")]
    led = [ledger("FS-01", app_version="1.3.9"), ledger("FS-02", app_version="1.4.2")]
    res = run(rows, led)
    assert "outdated_version" in only(res, "FS-01")["alerts"]
    assert "outdated_version" not in only(res, "FS-02")["alerts"]


def test_version_comparison_is_numeric_not_lexical():
    """1.10.0 is newer than 1.9.0, which string comparison gets backwards."""
    rows = [notion("FS-01"), notion("FS-02")]
    led = [ledger("FS-01", app_version="1.9.0"), ledger("FS-02", app_version="1.10.0")]
    res = run(rows, led)
    assert "outdated_version" in only(res, "FS-01")["alerts"]
    assert "outdated_version" not in only(res, "FS-02")["alerts"]


def test_a_device_that_never_reported_a_version_is_not_called_outdated():
    rows = [notion("FS-01"), notion("FS-02")]
    led = [ledger("FS-01", app_version=None), ledger("FS-02", app_version="1.4.2")]
    assert "outdated_version" not in only(run(rows, led), "FS-01")["alerts"]


# --- fill-if-empty ---

def test_client_is_filled_from_the_first_sighting_only_when_blank():
    row = notion(dispatched=dt.date(2026, 8, 1))
    led = [ledger(last_seen_at=seen(2026, 8, 2), actual_company="UC Property")]
    assert only(run([row], led))["updates"]["client"] == "UC Property"


def test_a_hand_typed_client_is_never_overwritten():
    row = notion(dispatched=dt.date(2026, 8, 1), client="Southbase")
    led = [ledger(last_seen_at=seen(2026, 8, 2), actual_company="UC Property")]
    assert "client" not in only(run([row], led))["updates"]


def test_due_back_defaults_to_thirty_days_after_dispatch_when_blank():
    r = only(run([notion(dispatched=dt.date(2026, 8, 1))], [ledger()]))
    assert r["updates"]["due_back"] == dt.date(2026, 8, 31)


def test_a_hand_typed_due_back_is_never_overwritten():
    r = only(run([notion(dispatched=dt.date(2026, 8, 1), due_back=dt.date(2026, 8, 10))],
                 [ledger()]))
    assert "due_back" not in r["updates"]


def test_activated_is_written_once_and_then_left_alone():
    led = [ledger(last_seen_at=seen(2026, 8, 2))]
    first = only(run([notion(dispatched=dt.date(2026, 8, 1))], led))
    assert first["updates"]["activated"] == dt.date(2026, 8, 2)
    again = only(run([notion(dispatched=dt.date(2026, 8, 1),
                             activated=dt.date(2026, 8, 2))], led))
    assert "activated" not in again["updates"]


def test_every_row_records_when_it_was_synced():
    assert only(run([notion()], [ledger()]))["updates"]["last_synced"] == TODAY


# --- site mismatch ---

def test_a_device_working_for_another_company_is_flagged_not_pushed():
    row = notion(dispatched=dt.date(2026, 8, 1), client="Millwater")
    led = [ledger(last_seen_at=seen(2026, 8, 2), actual_company="UC Property",
                  actual_site="UC PK")]
    assert "site_mismatch_flag" in only(run([row], led))["alerts"]


def test_a_matching_company_is_not_flagged():
    row = notion(dispatched=dt.date(2026, 8, 1), client="UC Property")
    led = [ledger(last_seen_at=seen(2026, 8, 2), actual_company="UC Property")]
    assert "site_mismatch_flag" not in only(run([row], led))["alerts"]


# --- rows with no counterpart ---

def test_a_notion_row_with_no_ledger_row_still_reports():
    r = only(run([notion(dispatched=dt.date(2026, 7, 1))], []))
    assert r["updates"]["last_seen"] is None
    assert "never_activated" in r["alerts"]


def test_the_result_has_one_entry_per_notion_row_and_no_more():
    res = run([notion("FS-01"), notion("FS-02")], [ledger("FS-01"), ledger("FS-99")])
    assert sorted(r["device"] for r in res) == ["FS-01", "FS-02"]
