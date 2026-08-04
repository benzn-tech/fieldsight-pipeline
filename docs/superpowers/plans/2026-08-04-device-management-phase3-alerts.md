# Device Management Phase 3 — Alerts and Notion Sync

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Turn the ledger into something a person reads without asking anyone: a Notion table that is always current, and a message that arrives only when a device needs attention.

**Architecture:** `lambda_device_report` — already deployed and inert — gains three pure modules it orchestrates: a Notion client, an alert deriver, and a pusher. All decision logic is pure functions over plain dicts, so the rules are unit-tested without Notion or AWS. The Lambda stays the only place that talks to the outside.

**Tech Stack:** Python 3.12 Lambda (non-VPC), `urllib3` (already bundled — no `requests`), the Notion REST API, SES via the existing `email_sender.py`, a Teams incoming webhook.

**Spec:** `docs/superpowers/specs/2026-08-03-device-management-design.md`

## Global Constraints

- **The Notion database already exists**, in the **company** workspace `Preformance`:
  - Database `https://app.notion.com/p/943da8c294734365b6c7294c2055c45d`
  - **Data source id `1c4d069f-3019-4210-8363-ce4c370aa433`** — this is what the API takes.
  - Twenty rows `FS-01`..`FS-20`, all `在库`.
- **Property names are exact and Chinese-free**: `Device` (title), `Dispatched`, `Due back`, `Returned`, `Client`, `Activated`, `Status`, `Last seen`, `App version`, `Actual site`, `Last synced`, `Notes`. `Status` is a select over `未激活 / 使用中 / 在库 / 失联 / 未认领` — those five option names **are** Chinese and must match byte-for-byte.
- **Fill-if-empty is the core safety rule.** `Client`, `Due back` and `Activated` are written **only when blank**. Once they hold any value — typed by a human or written by an earlier run — they are never touched again. Everything else in the system-owned half is overwritten freely.
- **`NOTION_TOKEN` empty ⇒ the function returns immediately.** That must stay true at every point in this plan, so a half-built Phase 3 is still safe to deploy.
- **All date arithmetic in `Pacific/Auckland`.** A bare `datetime.now()` is UTC and puts "today" a day out (BUG-37/19).
- **No new runtime dependencies.** `urllib3` is already available to Lambdas here; do not add `requests` or `notion-client`.
- **New resources must be named `fieldsight-*`**, and any new CFN resource needs matching IAM on `github-actions-fieldsight-deploy` — verify with `simulate-principal-policy`, never assume. This plan adds no new resources, but the rule holds if that changes.
- Test harness:
  ```
  export UV_LINK_MODE=copy
  export AWS_ACCESS_KEY_ID=testing AWS_SECRET_ACCESS_KEY=testing AWS_DEFAULT_REGION=ap-southeast-2
  uv run --with pytest --with boto3 --with "psycopg[binary]" --with urllib3 --with numpy pytest tests/unit -q
  ```
- `export MSYS_NO_PATHCONV=1` before any AWS CLI call carrying a leading-slash argument (BUG-28/42).

---

## Task 0: Prove the token works before building anything on it

The invoke hop was measured on 2026-08-04 (5 s, `{"status": "ok", "devices": 0}`). What has **not** been proven is that a real token reaches Notion and that the integration can see the database. Getting that wrong is a 404 that reads like a bad id, so it is worth ten minutes before writing code.

**Files:** none — this is a live check.

- [ ] **Step 1: Confirm the integration is connected to the database**

In Notion, open the database → `•••` → **Connections** → the integration must be listed. Creating an integration is not enough; without this the API returns `404 object_not_found` and it looks like the id is wrong.

- [ ] **Step 2: Query the data source directly**

```bash
export MSYS_NO_PATHCONV=1
curl -s -X POST 'https://api.notion.com/v1/data_sources/1c4d069f-3019-4210-8363-ce4c370aa433/query' \
  -H "Authorization: Bearer $NOTION_TOKEN" \
  -H 'Notion-Version: 2025-09-03' \
  -H 'Content-Type: application/json' -d '{"page_size":3}' | head -40
```
Expected: JSON with `"results"` holding rows whose `Device` titles are `FS-…`.
A `404` means Step 1 was skipped. A `401` means the token is wrong. **Do not proceed until this returns rows** — every later task assumes it.

If the `Notion-Version` header is rejected, fetch the current version from Notion's API changelog and use that; record whichever value worked in the module docstring in Task 1.

- [ ] **Step 3: Store the token**

```bash
gh secret set NOTION_TOKEN --repo benzn-tech/fieldsight-pipeline
```
Then add it to `deploy.yml`'s TEST `--parameter-overrides` as `"NotionToken=${{ secrets.NOTION_TOKEN }}"`, and to `deploy-prod.yml` the same way **only when Task 6 says so** — prod stays inert until the whole path is proven on TEST.

---

## Task 1: A Notion client that speaks only the shapes we need

**Files:**
- Create: `src/notion_client.py`
- Test: `tests/unit/test_notion_client.py`

**Interfaces:**
- Produces:
  - `list_rows(token, data_source_id) -> list[dict]` — each `{"page_id": str, "device": str, "dispatched": date|None, "due_back": date|None, "returned": bool, "client": str|None, "activated": date|None, "notes": str|None}`.
  - `update_row(token, page_id, props: dict) -> None` — `props` uses plain Python values keyed by our own names (`last_seen`, `app_version`, `actual_site`, `status`, `last_synced`, `client`, `due_back`, `activated`); the module translates to Notion's property shapes.
- Task 2 consumes `list_rows`. Task 4 consumes `update_row`.

- [ ] **Step 1: Write the failing tests**

```python
# tests/unit/test_notion_client.py
"""Translation between Notion's property envelopes and plain Python.

These tests exist because Notion's shapes are deeply nested and easy to get
subtly wrong — a date written as a bare string silently no-ops rather than
erroring, which would leave a table that looks merely stale.
"""
import datetime as dt

import src.notion_client as nc


def test_parses_a_full_row():
    raw = {
        "id": "page-1",
        "properties": {
            "Device": {"title": [{"plain_text": "FS-07"}]},
            "Dispatched": {"date": {"start": "2026-08-01"}},
            "Due back": {"date": {"start": "2026-08-31"}},
            "Returned": {"checkbox": False},
            "Client": {"rich_text": [{"plain_text": "UC Property"}]},
            "Activated": {"date": None},
            "Notes": {"rich_text": []},
        },
    }
    row = nc.parse_row(raw)
    assert row["page_id"] == "page-1"
    assert row["device"] == "FS-07"
    assert row["dispatched"] == dt.date(2026, 8, 1)
    assert row["due_back"] == dt.date(2026, 8, 31)
    assert row["returned"] is False
    assert row["client"] == "UC Property"
    assert row["activated"] is None
    assert row["notes"] is None


def test_a_row_with_everything_blank_parses_to_nones_not_errors():
    raw = {"id": "p", "properties": {"Device": {"title": []}}}
    row = nc.parse_row(raw)
    assert row["device"] == ""
    assert row["dispatched"] is None
    assert row["returned"] is False


def test_a_datetime_valued_date_is_still_a_date():
    raw = {"id": "p", "properties": {
        "Device": {"title": [{"plain_text": "FS-01"}]},
        "Dispatched": {"date": {"start": "2026-08-01T09:30:00.000+12:00"}},
    }}
    assert nc.parse_row(raw)["dispatched"] == dt.date(2026, 8, 1)


def test_builds_notion_shapes_for_a_write():
    props = nc.build_props({
        "last_seen": dt.date(2026, 8, 4),
        "app_version": "1.4.2",
        "actual_site": "UC PK",
        "status": "使用中",
        "last_synced": dt.date(2026, 8, 4),
    })
    assert props["Last seen"] == {"date": {"start": "2026-08-04"}}
    assert props["App version"] == {"rich_text": [{"text": {"content": "1.4.2"}}]}
    assert props["Status"] == {"select": {"name": "使用中"}}


def test_a_none_clears_rather_than_writing_the_string_none():
    props = nc.build_props({"actual_site": None, "last_seen": None})
    assert props["Actual site"] == {"rich_text": []}
    assert props["Last seen"] == {"date": None}


def test_an_unknown_key_is_refused_loudly():
    """A typo'd key must not be silently dropped — that is a table that looks
    updated and is not."""
    try:
        nc.build_props({"lastseen": dt.date(2026, 8, 4)})
    except KeyError:
        return
    raise AssertionError("an unknown property key must raise")
```

- [ ] **Step 2: Run to verify failure**

```bash
uv run --with pytest --with urllib3 pytest tests/unit/test_notion_client.py -q
```
Expected: FAIL — `ModuleNotFoundError`.

- [ ] **Step 3: Write the module**

```python
# src/notion_client.py
"""Minimal Notion access for the device ledger.

Only two operations are needed — read every row, patch some properties on one
row — so this is a hand-rolled client over urllib3 rather than a dependency.

Notion's property envelopes are nested and forgiving in the worst way: a value
in the wrong shape is often accepted and ignored, leaving a table that looks
merely stale. Hence build_props() raises on an unknown key rather than dropping
it, and every shape is unit-tested.

API version 2025-09-03 (data_sources endpoints). If Notion rejects it, check
their changelog and update BOTH this constant and the docstring.
"""

import datetime as dt
import json
import logging

import urllib3

logger = logging.getLogger()

NOTION_VERSION = "2025-09-03"
_API = "https://api.notion.com/v1"
_http = urllib3.PoolManager()

# our name -> (Notion property name, kind)
_PROPS = {
    "client":      ("Client", "rich_text"),
    "due_back":    ("Due back", "date"),
    "activated":   ("Activated", "date"),
    "status":      ("Status", "select"),
    "last_seen":   ("Last seen", "date"),
    "app_version": ("App version", "rich_text"),
    "actual_site": ("Actual site", "rich_text"),
    "last_synced": ("Last synced", "date"),
}


def _headers(token):
    return {
        "Authorization": f"Bearer {token}",
        "Notion-Version": NOTION_VERSION,
        "Content-Type": "application/json",
    }


def _text_of(prop):
    if not prop:
        return None
    parts = prop.get("rich_text") or prop.get("title") or []
    joined = "".join(p.get("plain_text", "") for p in parts)
    return joined or None


def _date_of(prop):
    if not prop:
        return None
    d = prop.get("date")
    if not d or not d.get("start"):
        return None
    # Notion returns either 2026-08-01 or a full ISO datetime; a date is what we mean.
    return dt.date.fromisoformat(d["start"][:10])


def parse_row(raw):
    props = raw.get("properties", {})
    return {
        "page_id": raw.get("id"),
        "device": _text_of(props.get("Device")) or "",
        "dispatched": _date_of(props.get("Dispatched")),
        "due_back": _date_of(props.get("Due back")),
        "returned": bool((props.get("Returned") or {}).get("checkbox", False)),
        "client": _text_of(props.get("Client")),
        "activated": _date_of(props.get("Activated")),
        "notes": _text_of(props.get("Notes")),
    }


def build_props(values):
    """Translate our plain values into Notion envelopes. Raises on unknown keys."""
    out = {}
    for key, value in values.items():
        if key not in _PROPS:
            raise KeyError(f"unknown device-ledger property {key!r}")
        name, kind = _PROPS[key]
        if kind == "date":
            out[name] = {"date": {"start": value.isoformat()} if value else None}
        elif kind == "select":
            out[name] = {"select": {"name": value} if value else None}
        else:
            out[name] = {"rich_text": [{"text": {"content": str(value)}}] if value else []}
    return out


def list_rows(token, data_source_id):
    rows, cursor = [], None
    while True:
        body = {"page_size": 100}
        if cursor:
            body["start_cursor"] = cursor
        resp = _http.request(
            "POST", f"{_API}/data_sources/{data_source_id}/query",
            headers=_headers(token), body=json.dumps(body).encode(),
        )
        if resp.status != 200:
            raise RuntimeError(f"notion query failed {resp.status}: {resp.data[:300]!r}")
        payload = json.loads(resp.data)
        rows.extend(parse_row(r) for r in payload.get("results", []))
        if not payload.get("has_more"):
            return rows
        cursor = payload.get("next_cursor")


def update_row(token, page_id, props):
    body = json.dumps({"properties": build_props(props)}).encode()
    resp = _http.request("PATCH", f"{_API}/pages/{page_id}", headers=_headers(token), body=body)
    if resp.status != 200:
        raise RuntimeError(f"notion update failed {resp.status}: {resp.data[:300]!r}")
```

- [ ] **Step 4: Run to verify pass**

```bash
uv run --with pytest --with urllib3 pytest tests/unit/test_notion_client.py -q
```
Expected: PASS, 6 tests.

- [ ] **Step 5: Commit**

```bash
git add src/notion_client.py tests/unit/test_notion_client.py
git commit -m "feat(devices): a Notion client that refuses unknown properties"
```

---

## Task 2: Derive the alerts — pure, so the rules are the tests

**Files:**
- Create: `src/device_alerts.py`
- Test: `tests/unit/test_device_alerts.py`

**Interfaces:**
- Consumes: ledger rows from `lambda_device_ledger` (Phase 1) and Notion rows from Task 1.
- Produces:
  - `derive(ledger: list[dict], notion: list[dict], today: date, quiet_working_days: int, grace_days: int) -> list[dict]` — one entry per Notion row: `{"page_id", "device", "status", "alerts": [str], "updates": {...}}`.
  - `working_days_between(a: date, b: date) -> int`.

- [ ] **Step 1: Write the failing tests**

```python
# tests/unit/test_device_alerts.py
"""The four alerts, and the one rule that makes them safe.

Every alert is evaluated ONLY inside the window where it can be true — that is
what lets a quiet-device alert exist at all without drowning in false positives
from devices legitimately dark in stock or in transit.
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


# --- never activated ---

def test_handed_over_and_never_seen_past_the_grace_period_alerts():
    r = only(run([notion(dispatched=dt.date(2026, 7, 28))], [ledger()]))
    assert "never_activated" in r["alerts"]


def test_inside_the_grace_period_it_stays_quiet():
    r = only(run([notion(dispatched=dt.date(2026, 8, 3))], [ledger()]))
    assert "never_activated" not in r["alerts"]


def test_a_device_still_in_stock_is_never_an_alert():
    r = only(run([notion(dispatched=None)], [ledger()]))
    assert r["alerts"] == []
    assert r["status"] == "在库"


# --- quiet ---

def test_an_activated_device_gone_quiet_alerts():
    seen = dt.datetime(2026, 7, 20, tzinfo=dt.timezone.utc)
    r = only(run([notion(dispatched=dt.date(2026, 7, 15))], [ledger(last_seen_at=seen)]))
    assert "quiet" in r["alerts"]


def test_a_recently_seen_device_is_quiet_about_being_quiet():
    seen = dt.datetime(2026, 8, 3, tzinfo=dt.timezone.utc)
    r = only(run([notion(dispatched=dt.date(2026, 7, 15))], [ledger(last_seen_at=seen)]))
    assert "quiet" not in r["alerts"]


def test_a_returned_device_never_raises_a_quiet_alert():
    """This is the whole reason the quiet alert is safe: outside the in-use
    window, silence is normal and must not fire."""
    seen = dt.datetime(2026, 6, 1, tzinfo=dt.timezone.utc)
    r = only(run([notion(dispatched=dt.date(2026, 5, 1), returned=True)],
                 [ledger(last_seen_at=seen)]))
    assert "quiet" not in r["alerts"]
    assert r["status"] == "在库"


def test_a_not_yet_activated_device_raises_never_activated_not_quiet():
    r = only(run([notion(dispatched=dt.date(2026, 7, 1))], [ledger(last_seen_at=None)]))
    assert "never_activated" in r["alerts"]
    assert "quiet" not in r["alerts"]


# --- due back ---

def test_overdue_alerts_and_the_due_date_itself_does_not():
    over = only(run([notion(due_back=dt.date(2026, 8, 3))], [ledger()]))
    assert "due_back" in over["alerts"]
    on_day = only(run([notion(due_back=TODAY)], [ledger()]))
    assert "due_back" not in on_day["alerts"]


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


def test_a_device_that_never_reported_a_version_is_not_called_outdated():
    rows = [notion("FS-01"), notion("FS-02")]
    led = [ledger("FS-01", app_version=None), ledger("FS-02", app_version="1.4.2")]
    assert "outdated_version" not in only(run(rows, led), "FS-01")["alerts"]


# --- fill-if-empty ---

def test_client_is_filled_from_the_first_sighting_only_when_blank():
    row = notion(dispatched=dt.date(2026, 8, 1))
    led = [ledger(last_seen_at=dt.datetime(2026, 8, 2, tzinfo=dt.timezone.utc),
                  actual_company="UC Property")]
    assert only(run([row], led))["updates"].get("client") == "UC Property"


def test_a_hand_typed_client_is_never_overwritten():
    row = notion(dispatched=dt.date(2026, 8, 1), client="Southbase")
    led = [ledger(last_seen_at=dt.datetime(2026, 8, 2, tzinfo=dt.timezone.utc),
                  actual_company="UC Property")]
    assert "client" not in only(run([row], led))["updates"]


def test_due_back_defaults_to_thirty_days_after_dispatch_when_blank():
    r = only(run([notion(dispatched=dt.date(2026, 8, 1))], [ledger()]))
    assert r["updates"]["due_back"] == dt.date(2026, 8, 31)


def test_a_hand_typed_due_back_is_never_overwritten():
    r = only(run([notion(dispatched=dt.date(2026, 8, 1), due_back=dt.date(2026, 8, 10))],
                 [ledger()]))
    assert "due_back" not in r["updates"]


def test_activated_is_written_once_and_then_left_alone():
    led = [ledger(last_seen_at=dt.datetime(2026, 8, 2, tzinfo=dt.timezone.utc))]
    first = only(run([notion(dispatched=dt.date(2026, 8, 1))], led))
    assert first["updates"]["activated"] == dt.date(2026, 8, 2)
    again = only(run([notion(dispatched=dt.date(2026, 8, 1),
                             activated=dt.date(2026, 8, 2))], led))
    assert "activated" not in again["updates"]


# --- rows with no counterpart ---

def test_a_notion_row_with_no_ledger_row_still_reports():
    r = only(run([notion(dispatched=dt.date(2026, 7, 1))], []))
    assert r["updates"]["last_seen"] is None
    assert "never_activated" in r["alerts"]


def test_a_ledger_device_absent_from_notion_is_surfaced_as_unclaimed():
    res = run([], [ledger("unclaimed:a3f9c2d1")])
    assert res == [] or all(r["device"] != "FS-07" for r in res)
```

- [ ] **Step 2: Run to verify failure**

```bash
uv run --with pytest pytest tests/unit/test_device_alerts.py -q
```
Expected: FAIL — `ModuleNotFoundError`.

- [ ] **Step 3: Write the deriver**

```python
# src/device_alerts.py
"""Derive every device's state and alerts. Pure — no I/O, no clock.

The design decision that makes this safe: each alert is evaluated only inside
the window where it CAN be true. A device sitting in stock, or already
collected, is legitimately dark, so the quiet alert simply does not apply to
it. That is what lets a quiet alert exist at all without becoming noise that
gets muted — and a muted alert is worse than none, because you believe you are
covered.
"""

import datetime as dt

STATUS_UNACTIVATED = "未激活"
STATUS_IN_USE = "使用中"
STATUS_IN_STOCK = "在库"
STATUS_QUIET = "失联"
STATUS_UNCLAIMED = "未认领"


def working_days_between(a, b):
    """NZ working days from a to b, weekends excluded. Public holidays are not
    modelled: the threshold is generous and configurable, so a holiday shifts
    the count by one at most."""
    if b <= a:
        return 0
    days = 0
    cur = a
    while cur < b:
        cur += dt.timedelta(days=1)
        if cur.weekday() < 5:
            days += 1
    return days


def _version_key(v):
    parts = []
    for chunk in str(v).replace("-", ".").split("."):
        parts.append(int(chunk) if chunk.isdigit() else 0)
    return tuple(parts)


def derive(ledger, notion, today, quiet_working_days, grace_days):
    by_tag = {r["asset_tag"]: r for r in ledger}

    versions = [r.get("app_version") for r in ledger if r.get("app_version")]
    newest = max(versions, key=_version_key) if versions else None

    out = []
    for row in notion:
        led = by_tag.get(row["device"], {})
        seen_at = led.get("last_seen_at")
        seen_date = seen_at.date() if isinstance(seen_at, dt.datetime) else None

        dispatched = row["dispatched"]
        returned = row["returned"]
        activated = dispatched is not None and seen_date is not None and seen_date >= dispatched

        alerts = []
        updates = {
            "last_seen": seen_date,
            "app_version": led.get("app_version"),
            "actual_site": led.get("actual_site"),
            "last_synced": today,
        }

        # --- status ---
        if returned or dispatched is None:
            status = STATUS_IN_STOCK
        elif not activated:
            status = STATUS_UNACTIVATED
        else:
            status = STATUS_IN_USE
        if row["device"].startswith("unclaimed:"):
            status = STATUS_UNCLAIMED

        # --- handed over, never activated ---
        if dispatched is not None and not returned and not activated:
            if (today - dispatched).days > grace_days:
                alerts.append("never_activated")

        # --- activated then gone quiet: ONLY inside the in-use window ---
        if activated and not returned and seen_date is not None:
            if working_days_between(seen_date, today) > quiet_working_days:
                alerts.append("quiet")
                status = STATUS_QUIET

        # --- due back ---
        if not returned and row["due_back"] is not None and row["due_back"] < today:
            alerts.append("due_back")

        # --- outdated version ---
        mine = led.get("app_version")
        if mine and newest and _version_key(mine) < _version_key(newest):
            alerts.append("outdated_version")

        # --- site mismatch: a flag on the row, never a push ---
        company = led.get("actual_company")
        if row["client"] and company and company != row["client"]:
            alerts.append("site_mismatch_flag")

        # --- fill-if-empty: write ONLY into blanks, never over a value ---
        if row["client"] is None and company:
            updates["client"] = company
        if row["due_back"] is None and dispatched is not None:
            updates["due_back"] = dispatched + dt.timedelta(days=30)
        if row["activated"] is None and activated:
            updates["activated"] = seen_date

        updates["status"] = status
        out.append({
            "page_id": row["page_id"],
            "device": row["device"],
            "status": status,
            "alerts": alerts,
            "updates": updates,
        })
    return out
```

- [ ] **Step 4: Run to verify pass**

```bash
uv run --with pytest pytest tests/unit/test_device_alerts.py -q
```
Expected: PASS, 19 tests. If `test_a_ledger_device_absent_from_notion_is_surfaced_as_unclaimed` reads as vacuous, replace it with the behaviour you actually want in Task 5 — do not leave an assertion that cannot fail.

- [ ] **Step 5: Commit**

```bash
git add src/device_alerts.py tests/unit/test_device_alerts.py
git commit -m "feat(devices): derive status and the four alerts, purely"
```

---

## Task 3: Push — Teams and email, only when something fires

**Files:**
- Create: `src/device_notify.py`
- Test: `tests/unit/test_device_notify.py`

**Interfaces:**
- Consumes: `derive()`'s output from Task 2.
- Produces:
  - `format_message(results, database_url) -> str | None` — `None` when nothing needs attention.
  - `push(text, teams_webhook, email_to, ses_sender) -> None`.

- [ ] **Step 1: Write the failing tests**

```python
# tests/unit/test_device_notify.py
import src.device_notify as dn


def result(device, alerts):
    return {"page_id": "p", "device": device, "status": "使用中",
            "alerts": alerts, "updates": {}}


URL = "https://app.notion.com/p/943da8c294734365b6c7294c2055c45d"


def test_silence_when_nothing_needs_attention():
    assert dn.format_message([result("FS-01", [])], URL) is None


def test_a_row_flag_alone_is_not_worth_a_message():
    """site_mismatch is shown in the table, never pushed — it is context, not a
    call to action, and pushing it would train people to ignore the channel."""
    assert dn.format_message([result("FS-05", ["site_mismatch_flag"])], URL) is None


def test_names_the_devices_and_links_the_table():
    msg = dn.format_message(
        [result("FS-02", ["due_back"]), result("FS-07", ["never_activated"])], URL)
    assert "FS-02" in msg and "FS-07" in msg
    assert URL in msg


def test_groups_by_alert_rather_than_listing_one_line_per_device():
    msg = dn.format_message(
        [result("FS-01", ["outdated_version"]), result("FS-02", ["outdated_version"])], URL)
    assert msg.count("FS-0") == 2
    assert msg.lower().count("version") == 1, "one heading, not one per device"


def test_push_is_a_no_op_without_a_destination():
    dn.push("anything", teams_webhook="", email_to=[], ses_sender="")  # must not raise
```

- [ ] **Step 2: Run to verify failure**

```bash
uv run --with pytest --with boto3 --with urllib3 pytest tests/unit/test_device_notify.py -q
```
Expected: FAIL — `ModuleNotFoundError`.

- [ ] **Step 3: Write the module**

```python
# src/device_notify.py
"""Tell someone, but only when there is something to tell.

The table is always current, so a push is not "here is the state" — it is
"something needs a decision". Anything that does not need a decision stays in
the table: pushing context trains people to ignore the channel, and then the
alerts that matter are ignored too.
"""

import json
import logging

import urllib3

logger = logging.getLogger()
_http = urllib3.PoolManager()

# Ordered most-actionable first. site_mismatch_flag is deliberately absent.
_HEADINGS = [
    ("due_back", "该回收"),
    ("never_activated", "发出未上线"),
    ("quiet", "使用中失联"),
    ("outdated_version", "版本落后"),
]


def format_message(results, database_url):
    lines = []
    for key, heading in _HEADINGS:
        hit = [r["device"] for r in results if key in r["alerts"]]
        if hit:
            lines.append(f"{heading} ({len(hit)}): " + " · ".join(sorted(hit)))
    if not lines:
        return None
    lines.append("")
    lines.append(database_url)
    return "\n".join(lines)


def push(text, teams_webhook, email_to, ses_sender):
    if not text:
        return
    if teams_webhook:
        try:
            _http.request(
                "POST", teams_webhook,
                headers={"Content-Type": "application/json"},
                body=json.dumps({"text": text}).encode(),
            )
        except Exception:
            logger.exception("teams push failed")
    if email_to and ses_sender:
        try:
            import email_sender
            email_sender.send(
                sender=ses_sender, to=email_to,
                subject="FieldSight 设备台账 — 有设备需要处理",
                body_text=text,
            )
        except Exception:
            logger.exception("email push failed")
```

Read `src/email_sender.py` first and match its real function name and signature — the call above assumes `send(sender, to, subject, body_text)`. If it differs, adapt the call, not the mailer.

- [ ] **Step 4: Run to verify pass, then commit**

```bash
uv run --with pytest --with boto3 --with urllib3 pytest tests/unit/test_device_notify.py -q
git add src/device_notify.py tests/unit/test_device_notify.py
git commit -m "feat(devices): push only what needs a decision"
```

---

## Task 4: Wire it into `lambda_device_report`

**Files:**
- Modify: `src/lambda_device_report.py`
- Test: `tests/unit/test_lambda_device_report.py` (extend)

- [ ] **Step 1: Write the failing tests**

```python
def test_still_disabled_without_a_token(monkeypatch):
    monkeypatch.setattr(report, "NOTION_TOKEN", "")
    assert report.lambda_handler({}, None) == {"status": "disabled", "devices": 0}


def test_writes_every_row_and_reports_the_alert_count(monkeypatch):
    monkeypatch.setattr(report, "NOTION_TOKEN", "t")
    monkeypatch.setattr(report, "NOTION_DATA_SOURCE", "ds")
    monkeypatch.setattr(report, "_lambda", lambda: FakeLambdaClient(
        {"devices": [{"asset_tag": "FS-01", "app_version": "1.0.0",
                      "last_seen_at": None, "actual_site": None,
                      "actual_company": None, "uuid_trusted": True,
                      "device_uuid": "u", "last_account_sub": None}]}))
    written = []
    monkeypatch.setattr(report.notion_client, "list_rows", lambda t, d: [
        {"page_id": "p1", "device": "FS-01", "dispatched": None, "due_back": None,
         "returned": False, "client": None, "activated": None, "notes": None}])
    monkeypatch.setattr(report.notion_client, "update_row",
                        lambda t, pid, props: written.append(pid))
    monkeypatch.setattr(report.device_notify, "push", lambda *a, **k: None)

    out = report.lambda_handler({}, None)

    assert out["devices"] == 1
    assert written == ["p1"]


def test_one_failing_row_does_not_abort_the_rest(monkeypatch):
    """A single bad row must not leave the other nineteen stale — that is the
    silent-staleness failure this design exists to avoid."""
    monkeypatch.setattr(report, "NOTION_TOKEN", "t")
    monkeypatch.setattr(report, "_lambda", lambda: FakeLambdaClient({"devices": []}))
    monkeypatch.setattr(report.notion_client, "list_rows", lambda t, d: [
        {"page_id": "bad", "device": "FS-01", "dispatched": None, "due_back": None,
         "returned": False, "client": None, "activated": None, "notes": None},
        {"page_id": "ok", "device": "FS-02", "dispatched": None, "due_back": None,
         "returned": False, "client": None, "activated": None, "notes": None},
    ])
    done = []

    def flaky(t, pid, props):
        if pid == "bad":
            raise RuntimeError("notion 500")
        done.append(pid)

    monkeypatch.setattr(report.notion_client, "update_row", flaky)
    monkeypatch.setattr(report.device_notify, "push", lambda *a, **k: None)

    out = report.lambda_handler({}, None)

    assert done == ["ok"]
    assert out["failed"] == 1
```

- [ ] **Step 2: Run to verify failure, then implement**

First add the imports. The tests monkeypatch `report.notion_client`, `report.device_alerts` and
`report.device_notify`, so they must be **module attributes** — import the modules, not the
functions out of them:

```python
import datetime as dt
from zoneinfo import ZoneInfo

import device_alerts
import device_notify
import notion_client
```

(Bare module names, matching how the other Lambdas in `src/` import their siblings — the zip is
flat, so `from src.x import y` fails at runtime even though it works in tests.)

Extend `lambda_handler` after the existing ledger invoke:

```python
    rows = notion_client.list_rows(NOTION_TOKEN, NOTION_DATA_SOURCE)
    today = dt.datetime.now(ZoneInfo("Pacific/Auckland")).date()
    results = device_alerts.derive(devices, rows, today, QUIET_WORKING_DAYS, GRACE_DAYS)

    failed = 0
    for r in results:
        try:
            notion_client.update_row(NOTION_TOKEN, r["page_id"], r["updates"])
        except Exception:
            failed += 1
            logger.exception("notion update failed for %s", r["device"])

    device_notify.push(
        device_notify.format_message(results, DATABASE_URL),
        TEAMS_WEBHOOK, EMAIL_TO, SES_SENDER,
    )
    return {"status": "ok", "devices": len(results), "failed": failed}
```

New module-level env reads, all with safe defaults so an unset variable degrades rather than crashes:

```python
NOTION_DATA_SOURCE = os.environ.get("NOTION_DATA_SOURCE", "")
DATABASE_URL = os.environ.get("DEVICE_LEDGER_URL", "")
TEAMS_WEBHOOK = os.environ.get("TEAMS_WEBHOOK", "")
EMAIL_TO = [a for a in os.environ.get("DEVICE_ALERT_EMAILS", "").split(",") if a.strip()]
SES_SENDER = os.environ.get("EMAIL_SENDER", "")
QUIET_WORKING_DAYS = int(os.environ.get("QUIET_ALERT_WORKING_DAYS", "7"))
GRACE_DAYS = int(os.environ.get("DEVICE_GRACE_DAYS", "3"))
```

**Per-row try/except is deliberate**: one malformed row must not leave the other nineteen stale, because a stale table is indistinguishable from a correct one at a glance.

- [ ] **Step 3: Run the full suite and commit**

```bash
uv run --with pytest --with boto3 --with "psycopg[binary]" --with urllib3 --with numpy pytest tests/unit -q
git add src/lambda_device_report.py tests/unit/test_lambda_device_report.py
git commit -m "feat(devices): sync the ledger to Notion and push what needs a decision"
```

---

## Task 5: Template wiring, then TEST

**Files:**
- Modify: `src/template.yaml` (`DeviceReportFunction` environment + new parameters)
- Modify: `.github/workflows/deploy.yml` (TEST only for now)

- [ ] **Step 1: Add the parameters and environment**

New parameters beside `NotionToken`: `NotionDataSource`, `DeviceLedgerUrl`, `TeamsWebhook` (`NoEcho`), `DeviceAlertEmails`, `QuietAlertWorkingDays` (Number, default 7). Wire each into `DeviceReportFunction`'s `Environment.Variables` under the names Task 4 reads.

Append parameters near the existing ones and leave the resource block where it is — several branches edit this file.

- [ ] **Step 2: Validate and check IAM**

```bash
export MSYS_NO_PATHCONV=1 AWS_CLI_FILE_ENCODING=UTF-8 PYTHONUTF8=1
sam validate --template src/template.yaml --region ap-southeast-2 --lint
```
No new resources are added, so no new IAM is needed. If you do add one, run `simulate-principal-policy` against its concrete ARN before pushing — a denial rolls the whole stack back.

- [ ] **Step 3: Deploy to TEST and run it by hand**

```bash
export MSYS_NO_PATHCONV=1
aws lambda invoke --function-name fieldsight-test-device-report \
  --payload '{}' --cli-binary-format raw-in-base64-out \
  --region ap-southeast-2 out.json && cat out.json
```
Expected: `{"status": "ok", "devices": 20, "failed": 0}`.

- [ ] **Step 4: Look at the actual table**

Open the Notion database. Every row must now show a `Last synced` of today. `Status` should read `在库` for all twenty, because nothing has been dispatched yet. **`Dispatched`, `Returned` and `Notes` must be untouched.**

Then the real test of the safety rule: type `Southbase` into one row's `Client`, run the Lambda again, and confirm it is **still** `Southbase`.

- [ ] **Step 5: Rehearse an alert**

Set one row's `Dispatched` to ten days ago. Run the Lambda. Expect `never_activated` for that device, a Teams message naming it, and its `Due back` auto-filled to dispatch + 30 days. Clear the row afterwards.

- [ ] **Step 6: Commit**

```bash
git add src/template.yaml .github/workflows/deploy.yml
git commit -m "feat(devices): wire the Notion sync on TEST"
```

---

## Task 6: Enable the schedule, then prod

- [ ] **Step 1: Let it run on its own on TEST**

The schedule rule follows `EnableSchedules`, which is `false` on TEST. Enable the rule alone for a few days:

```bash
export MSYS_NO_PATHCONV=1
aws events enable-rule --name "$(aws events list-rules --name-prefix fieldsight-test-DeviceReport \
  --region ap-southeast-2 --query 'Rules[0].Name' --output text)" --region ap-southeast-2
```
Watch that `Last synced` advances daily without anyone invoking it.

- [ ] **Step 2: Only then, prod**

Add the same parameters to `deploy-prod.yml`, merge to `main`, approve the deploy. Prod is worth doing only once devices are actually reporting — i.e. after Phase 2 ships — because until then every prod row would read "never seen".

- [ ] **Step 3: Set the quiet threshold from data, not from this document**

`QUIET_ALERT_WORKING_DAYS` defaults to 7 and **that number is a placeholder**. After a month of real heartbeats, look at the actual distribution of gaps between sightings for devices in use:

```sql
select asset_tag, last_seen_at, now() - last_seen_at as gap
from devices order by last_seen_at asc nulls first;
```
Pick a threshold above the normal tail. Too tight and the alert becomes noise, gets muted, and is then worse than no alert at all — you would believe you were covered.

---

## Not in this plan

Phase 2 (mobile) is a separate plan and a separate repo. Until it ships, every ledger row shows an empty `Last seen`, which is correct rather than broken: the alerts will honestly report "never activated" for devices that genuinely cannot report yet. Do not tune thresholds or judge the alerts against that state.
