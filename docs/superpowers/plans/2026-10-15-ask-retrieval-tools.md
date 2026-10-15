# Ask Retrieval Tools Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Give the Ask orchestrator five read-only, ACL-enforced retrieval tools (`day_overview`, `search_records`, `latest_activity`, `find_people`, `count_metric`) behind one function, `ask_tools.execute(calls, ctx)`, returning one evidence envelope.

**Architecture:** Tool definitions, argument validation, date resolution, ref assignment and output budget live in a new pure module `src/ask_tools.py` (runs in ask-agent / the voice worker, outside the VPC: it embeds queries and reranks). All SQL runs in the in-VPC `lambda_rag_search` through a new `mode: "tools"` batch dispatcher that resolves the caller's ACL once and runs each call in isolation. New SQL lives in the existing repositories.

**Tech Stack:** Python 3.12, psycopg 3, Postgres 16 + pgvector (Aurora), AWS Lambda, DashScope embeddings/rerank, pytest (unit + `integration` marker on local pgserver).

**Spec:** `docs/superpowers/specs/2026-10-15-ask-retrieval-tools-design.md` (revised after Fable review, §8).

## Global Constraints

- Read-only. No INSERT/UPDATE/DELETE anywhere in this plan's production code.
- Identity, company, ACL, timezone and "today" are NEVER tool arguments; the executor injects them.
- Out-of-ACL site/person/date resolves to the same shape as "no match" — never an error that confirms existence.
- Topic reads: `visible_topics_predicate` AND `work_class IS DISTINCT FROM 'non_work'`; NULL `user_id` hidden under a graded filter; per (site,user,day) extraction-sourced topics if any else report-sourced.
- Relative-date vocabulary: `today|yesterday|day_before_yesterday|this_week|last_week|this_month|last_month`. Unresolvable today → `meta.error="bad_args"`, `meta.field="tz"`.
- Clamps: `day_overview` ≤ 7 days; other dated tools ≤ 92 days; report in `meta.clamped`.
- Batch: ≤ 4 calls; `deadline_s = min(ctx.deadline - clock(), 20)` where `ctx.deadline` is an ABSOLUTE `time.monotonic()` timestamp (agreed with the orchestration line); RagSearch Timeout is 23 s.
- Refs: one `ask_tools.RefAssigner` per Ask turn, shared with the orchestrator's web tool (`R#` records, `W#` web) — never renumbered.
- Per tool result ≤ 2,500 tokens (budget as ≤ 10,000 chars of serialized items); item `text` ≤ 600 chars.
- Refs `R1..Rn` unique per turn; dedupe key `(kind, id)`.
- `ASK_TOOLS_MATCH_GATE` default `0.55`.
- Logging: tool name, arg shape, counts, empty_reason, ms, caller hash — never query text or item text.
- Never `git stash`; never `git add -A`; commit messages English, ending with the two attribution lines:
  `Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>` and `Claude-Session: https://claude.ai/code/session_014QUZA3uk6P8T7qYJoLRfQe`.
- Integration tests: `bash fswork/run-tests.sh tests/integration/<file>` (local pgserver; one pytest process at a time). Unit: `python -m pytest tests/unit/<file> -q`.

## Review Focus

1. A NZ caller asking about "yesterday" just after midnight / across the DST switch → the date is the caller's local yesterday (`query_slots.resolve_today` with `Pacific/Auckland`), not UTC's. Pinned in Task 1.
2. A personal (`non_work`) conversation recorded yesterday → absent from `day_overview` and from `latest_before`. Pinned in Task 2.
3. A worker (SELF tier) asking "what did Mike do yesterday" where Mike is on the same site → `person_unresolved`-shape no rows, never Mike's topics. Pinned in Task 3/5.
4. A day where the same session has both an extraction topic and a nightly-report topic → listed once (extraction), not twice. Pinned in Task 2.
5. One tool in a batch hitting a SQL error → the other tools' results still arrive. Pinned in Task 5.

---

### Task 1: `query_slots.span` + `ask_tools` definitions, validation, date resolution

**Files:**
- Modify: `src/query_slots.py` (export `span`)
- Create: `src/ask_tools.py`
- Test: `tests/unit/test_ask_tools_args.py`

**Interfaces:**
- Produces: `query_slots.span(today: date, name: str) -> tuple[date, date]` (`_span` stays as alias).
- Produces: `ask_tools.TOOLS: list[dict]` (provider-neutral JSON-Schema function defs), `ask_tools.RELATIVE_DATES: tuple[str, ...]`, `ask_tools.validate_call(call: dict, today: date | None) -> tuple[dict | None, dict | None]` returning `(normalized_args, error_meta)` where normalized args carry ISO `date_from`/`date_to` and optional `clamped`.

- [ ] **Step 1: Write the failing tests**

```python
# tests/unit/test_ask_tools_args.py
from datetime import date
import pytest
import ask_tools
import query_slots

TODAY = date(2026, 10, 15)  # a Thursday


def test_span_is_public_and_matches_private():
    assert query_slots.span(TODAY, "yesterday") == (date(2026, 10, 14),) * 2
    assert query_slots._span is query_slots.span


def test_tools_have_exactly_the_five_v1_names_and_no_identity_args():
    names = [t["name"] for t in ask_tools.TOOLS]
    assert names == ["day_overview", "search_records", "latest_activity",
                     "find_people", "count_metric"]
    for t in ask_tools.TOOLS:
        props = t["parameters"]["properties"]
        for forbidden in ("sub", "caller_sub", "company", "company_id", "tz", "today"):
            assert forbidden not in props, (t["name"], forbidden)


def test_relative_date_resolves_against_caller_today():
    args, err = ask_tools.validate_call(
        {"name": "day_overview", "args": {"date_from": "yesterday"}}, TODAY)
    assert err is None
    assert (args["date_from"], args["date_to"]) == ("2026-10-14", "2026-10-14")


def test_relative_date_without_today_is_bad_args_tz_never_unfiltered():
    args, err = ask_tools.validate_call(
        {"name": "day_overview", "args": {"date_from": "yesterday"}}, None)
    assert args is None
    assert err == {"error": "bad_args", "field": "tz"}


def test_iso_dates_need_no_today():
    args, err = ask_tools.validate_call(
        {"name": "search_records", "args": {"query": "pour", "date_from": "2026-10-01",
                                            "date_to": "2026-10-03"}}, None)
    assert err is None and args["date_from"] == "2026-10-01"


def test_day_overview_clamped_to_7_days():
    args, err = ask_tools.validate_call(
        {"name": "day_overview", "args": {"date_from": "2026-10-01", "date_to": "2026-10-20"}}, TODAY)
    assert err is None
    assert args["date_to"] == "2026-10-07"
    assert args["clamped"] == {"requested_to": "2026-10-20", "max_days": 7}


def test_other_tools_clamped_to_92_days():
    args, _ = ask_tools.validate_call(
        {"name": "search_records", "args": {"query": "x", "date_from": "2026-01-01",
                                            "date_to": "2026-10-15"}}, TODAY)
    assert args["date_to"] == "2026-04-02"


@pytest.mark.parametrize("call,field", [
    ({"name": "nope", "args": {}}, "name"),
    ({"name": "search_records", "args": {}}, "query"),
    ({"name": "search_records", "args": {"query": "x", "k": 13}}, "k"),
    ({"name": "day_overview", "args": {"date_from": "15/10/2026"}}, "date_from"),
    ({"name": "count_metric", "args": {"metric": "duration", "date_from": "2026-10-01",
                                       "site": "S1"}}, "site"),
    ({"name": "count_metric", "args": {"metric": "bogus", "date_from": "2026-10-01"}}, "metric"),
    ({"name": "day_overview", "args": {"date_from": "2026-10-01", "sub": "x"}}, "sub"),
])
def test_bad_args_name_the_field(call, field):
    args, err = ask_tools.validate_call(call, TODAY)
    assert args is None and err == {"error": "bad_args", "field": field}


def test_date_to_before_date_from_is_bad_args():
    _, err = ask_tools.validate_call(
        {"name": "day_overview", "args": {"date_from": "2026-10-10", "date_to": "2026-10-09"}}, TODAY)
    assert err == {"error": "bad_args", "field": "date_to"}
```

- [ ] **Step 2: Run to verify failure**

Run: `python -m pytest tests/unit/test_ask_tools_args.py -q`
Expected: FAIL — `ModuleNotFoundError: No module named 'ask_tools'`.

- [ ] **Step 3: Export `span` in `src/query_slots.py`**

Rename `def _span(today, name):` to `def span(today, name):`, add directly below it `_span = span  # old private name, kept for existing callers`, and change `__all__` to `["resolve_today", "time_range", "span"]`. `time_range` keeps calling `_span` (the alias) — no other change.

- [ ] **Step 4: Create `src/ask_tools.py` (definitions + validation)**

```python
"""Retrieval tools for the Ask orchestrator (spec 2026-10-15-ask-retrieval-tools).

The tool layer only: definitions the model sees, argument validation, date
resolution, and (Task 6) `execute`. WHEN a tool is called is the orchestrator's
business (companion spec, Ask orchestration v2).

Identity, company, ACL, timezone and "today" are never arguments: `execute`
injects them from the authenticated caller. A schema that let the model name a
company would let the model widen its own visibility.
"""
from __future__ import annotations

import re
from datetime import date, timedelta

import query_slots

RELATIVE_DATES = ("today", "yesterday", "day_before_yesterday", "this_week",
                  "last_week", "this_month", "last_month")
METRICS = ("duration", "count_sessions", "count_photos",
           "count_findings_safety", "count_findings_quality")
MAX_DAYS = {"day_overview": 7}
DEFAULT_MAX_DAYS = 92
MAX_K = 12

_DATE_DESC = ("ISO date YYYY-MM-DD in the user's local time, or one of: "
              + ", ".join(RELATIVE_DATES) + ".")
_SITE = {"type": "string", "description": "Optional project/site name or id."}
_PERSON = {"type": "string",
           "description": "Optional person: a person_ref from find_people, or a name."}


def _fn(name, description, properties, required):
    return {"name": name, "description": description,
            "parameters": {"type": "object", "properties": properties,
                           "required": required, "additionalProperties": False}}


TOOLS = [
    _fn("day_overview",
        "List what was recorded on a day or a few days (up to 7): every topic with "
        "time, site, who, and a one-line summary. Use for 'what did we do yesterday'. "
        "When nothing is recorded, meta.latest_before gives the latest earlier day.",
        {"date_from": {"type": "string", "description": _DATE_DESC},
         "date_to": {"type": "string", "description": _DATE_DESC + " Defaults to date_from."},
         "site": _SITE, "person": _PERSON},
        ["date_from"]),
    _fn("search_records",
        "Search what was said or recorded about a subject, optionally within dates, "
        "a site or a person.",
        {"query": {"type": "string"},
         "date_from": {"type": "string", "description": _DATE_DESC},
         "date_to": {"type": "string", "description": _DATE_DESC},
         "site": _SITE, "person": _PERSON,
         "k": {"type": "integer", "minimum": 1, "maximum": MAX_K}},
        ["query"]),
    _fn("latest_activity",
        "Find the most recent day with records (optionally: the most recent day a "
        "subject came up) and what was recorded that day.",
        {"query": {"type": "string"},
         "on_or_before": {"type": "string", "description": _DATE_DESC},
         "site": _SITE, "person": _PERSON},
        []),
    _fn("find_people",
        "Resolve a name to the people this user can see. Returns every match; "
        "ask the user when more than one comes back.",
        {"name": {"type": "string"}},
        ["name"]),
    _fn("count_metric",
        "A number from the database, never estimated: recording duration, sessions, "
        "photos, safety or quality findings, over a date range.",
        {"metric": {"type": "string", "enum": list(METRICS)},
         "date_from": {"type": "string", "description": _DATE_DESC},
         "date_to": {"type": "string", "description": _DATE_DESC}},
        ["metric", "date_from"]),
]
_BY_NAME = {t["name"]: t for t in TOOLS}
_ISO = re.compile(r"^\d{4}-\d{2}-\d{2}$")


class _Bad(Exception):
    def __init__(self, field):
        super().__init__(field)
        self.field = field


def _one_date(value, today, field, end):
    if not isinstance(value, str) or not value.strip():
        raise _Bad(field)
    v = value.strip()
    if v in RELATIVE_DATES:
        if today is None:
            raise _Bad("tz")
        lo, hi = query_slots.span(today, v)
        return hi if end else lo
    if not _ISO.match(v):
        raise _Bad(field)
    try:
        return date.fromisoformat(v)
    except ValueError:
        raise _Bad(field)


def validate_call(call, today):
    """(normalized_args, None) or (None, {"error": "bad_args", "field": f})."""
    try:
        name = call.get("name") if isinstance(call, dict) else None
        if name not in _BY_NAME:
            raise _Bad("name")
        raw = call.get("args") or {}
        if not isinstance(raw, dict):
            raise _Bad("args")
        schema = _BY_NAME[name]["parameters"]
        for key in raw:
            if key not in schema["properties"]:
                raise _Bad(key)
        for key in schema["required"]:
            if raw.get(key) in (None, ""):
                raise _Bad(key)
        out = {}
        for key in ("query", "name", "site", "person"):
            if key in raw:
                if not isinstance(raw[key], str) or not raw[key].strip():
                    raise _Bad(key)
                out[key] = raw[key].strip()
        if "k" in raw:
            if not isinstance(raw["k"], int) or not 1 <= raw["k"] <= MAX_K:
                raise _Bad("k")
            out["k"] = raw["k"]
        if "metric" in raw:
            if raw["metric"] not in METRICS:
                raise _Bad("metric")
            out["metric"] = raw["metric"]
        if "on_or_before" in raw:
            out["on_or_before"] = _one_date(raw["on_or_before"], today, "on_or_before",
                                            end=True).isoformat()
        if "date_from" in raw:
            lo = _one_date(raw["date_from"], today, "date_from", end=False)
            hi = (_one_date(raw["date_to"], today, "date_to", end=True)
                  if raw.get("date_to") else
                  _one_date(raw["date_from"], today, "date_from", end=True))
            if hi < lo:
                raise _Bad("date_to")
            max_days = MAX_DAYS.get(name, DEFAULT_MAX_DAYS)
            if (hi - lo).days + 1 > max_days:
                out["clamped"] = {"requested_to": hi.isoformat(), "max_days": max_days}
                hi = lo + timedelta(days=max_days - 1)
            out["date_from"], out["date_to"] = lo.isoformat(), hi.isoformat()
        elif "date_to" in raw:
            raise _Bad("date_from")
        return out, None
    except _Bad as bad:
        return None, {"error": "bad_args", "field": bad.field}
```

Note: `count_metric` rejecting `site`/`person` falls out of `additionalProperties` (its schema lists neither), which the `test_bad_args_name_the_field` case pins.

- [ ] **Step 5: Run tests**

Run: `python -m pytest tests/unit/test_ask_tools_args.py tests/unit/test_query_slots.py -q`
Expected: PASS (all).

- [ ] **Step 6: Commit**

```bash
git add src/query_slots.py src/ask_tools.py tests/unit/test_ask_tools_args.py
git commit -m "Ask tools: definitions, argument validation, relative dates

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>
Claude-Session: https://claude.ai/code/session_014QUZA3uk6P8T7qYJoLRfQe"
```

---

### Task 2: `day_overview_rows` + `latest_visible_topic_date` (topics SQL)

**Files:**
- Modify: `src/repositories/topics.py` (append two functions)
- Test: `tests/integration/test_ask_tools_topics_sql.py`

**Interfaces:**
- Produces: `topics.day_overview_rows(conn, site_ids, date_from, date_to, *, author_ids=None, site_id=None, user_id=None, limit=60) -> list[dict]` — keys: `id, site_id, site_name, user_id, user_name, report_date, time_range, title, summary, source_s3_key, n_tasks, n_findings`, ordered `report_date, occurred_at NULLS LAST, time_range NULLS LAST, created_at`.
- Produces: `topics.latest_visible_topic_date(conn, site_ids, before, *, author_ids=None, site_id=None, user_id=None) -> str | None` — latest `report_date` strictly before `before` under the same rules.

- [ ] **Step 1: Write the failing integration tests**

```python
# tests/integration/test_ask_tools_topics_sql.py
import pytest
from repositories import companies, sites, topics, users

pytestmark = pytest.mark.integration


def _user(db, co, folder):
    u = users.upsert_user(db, f"sub-{folder}", f"{folder}@x.nz", company_id=co["id"],
                          first_name=folder)
    db.execute("UPDATE users SET folder_name=%s WHERE id=%s", (folder, u["id"]))
    return u


@pytest.fixture
def world(db):
    co = companies.create_company(db, "Acme")
    s1 = sites.create_site(db, co["id"], "S1")
    s2 = sites.create_site(db, co["id"], "S2")
    ben = _user(db, co, "Ben")
    mike = _user(db, co, "Mike")
    return {"co": co, "s1": s1, "s2": s2, "ben": ben, "mike": mike}


def _t(db, w, site, user, day, title, src, **kw):
    return topics.upsert_topic(db, w[site]["id"], day, title, user_id=w[user]["id"] if user else None,
                               source_s3_key=src, **kw)


def test_lists_visible_work_topics_once_per_family(db, world):
    w = world
    _t(db, w, "s1", "ben", "2026-10-14", "Pour", "extractions/2026-10-14/Ben/a.json",
       summary="B2 slab", time_range="09:00 - 09:40")
    _t(db, w, "s1", "ben", "2026-10-14", "Pour (report)", "reports/2026-10-14/Ben/daily_report.json")
    _t(db, w, "s1", "ben", "2026-10-14", "Lunch chat", "extractions/2026-10-14/Ben/b.json",
       work_class="non_work")
    _t(db, w, "s1", "mike", "2026-10-14", "Scaffold", "reports/2026-10-14/Mike/daily_report.json")
    _t(db, w, "s2", "ben", "2026-10-14", "Other site", "extractions/2026-10-14/Ben/c.json")
    rows = topics.day_overview_rows(db, [w["s1"]["id"]], "2026-10-14", "2026-10-14")
    titles = sorted(r["title"] for r in rows)
    # extraction family wins for Ben; report family stands for Mike (no extraction rows);
    # non_work and the out-of-ACL site are absent
    assert titles == ["Pour", "Scaffold"]
    pour = next(r for r in rows if r["title"] == "Pour")
    assert pour["time_range"] == "09:00 - 09:40" and pour["site_name"] == "S1"


def test_graded_filter_hides_other_authors_and_null_user(db, world):
    w = world
    _t(db, w, "s1", "mike", "2026-10-14", "Mike's", "extractions/2026-10-14/Mike/a.json")
    _t(db, w, "s1", None, "2026-10-14", "Unattributed", "reports/2026-10-14/x/daily_report.json")
    _t(db, w, "s1", "ben", "2026-10-14", "Ben's", "extractions/2026-10-14/Ben/a.json")
    rows = topics.day_overview_rows(db, [w["s1"]["id"]], "2026-10-14", "2026-10-14",
                                    author_ids={str(w["ben"]["id"])})
    assert [r["title"] for r in rows] == ["Ben's"]


def test_deleted_topic_absent(db, world):
    w = world
    t = _t(db, w, "s1", "ben", "2026-10-14", "Gone", "extractions/2026-10-14/Ben/a.json")
    db.execute("INSERT INTO redactions (target_type, target_id, scope) VALUES ('topic', %s, 'deleted')",
               (t["id"],))
    assert topics.day_overview_rows(db, [w["s1"]["id"]], "2026-10-14", "2026-10-14") == []


def test_latest_visible_topic_date_skips_non_work_and_deleted(db, world):
    w = world
    _t(db, w, "s1", "ben", "2026-10-10", "Real", "extractions/2026-10-10/Ben/a.json")
    _t(db, w, "s1", "ben", "2026-10-12", "Chat", "extractions/2026-10-12/Ben/a.json",
       work_class="non_work")
    gone = _t(db, w, "s1", "ben", "2026-10-13", "Gone", "extractions/2026-10-13/Ben/a.json")
    db.execute("INSERT INTO redactions (target_type, target_id, scope) VALUES ('topic', %s, 'deleted')",
               (gone["id"],))
    assert topics.latest_visible_topic_date(db, [w["s1"]["id"]], "2026-10-14") == "2026-10-10"


def test_latest_visible_topic_date_respects_graded_filter(db, world):
    w = world
    _t(db, w, "s1", "mike", "2026-10-13", "Mike's", "extractions/2026-10-13/Mike/a.json")
    _t(db, w, "s1", "ben", "2026-10-09", "Ben's", "extractions/2026-10-09/Ben/a.json")
    assert topics.latest_visible_topic_date(
        db, [w["s1"]["id"]], "2026-10-14", author_ids={str(w["ben"]["id"])}) == "2026-10-09"
```

Before running: open `src/migrations/*` for `CREATE TABLE redactions` and adjust the INSERT column list to its NOT NULL columns if it has more than `target_type, target_id, scope` (e.g. a `company_id` or `created_by`). Keep the assertion unchanged.

- [ ] **Step 2: Run to verify failure**

Run: `bash fswork/run-tests.sh tests/integration/test_ask_tools_topics_sql.py`
Expected: FAIL — `AttributeError: module 'repositories.topics' has no attribute 'day_overview_rows'`.

- [ ] **Step 3: Implement (append to `src/repositories/topics.py`)**

```python
# ---- Ask tools (spec 2026-10-15-ask-retrieval-tools §3.1) -------------------
# One WHERE for both functions so day_overview and latest_before can never
# disagree about what is visible. Rules: deletion/supersession arms, non_work
# out (NULL counts as work, as the timeline does), graded author filter with
# NULL user_id hidden, and the timeline's family rule -- per (site,user,day)
# extraction-sourced topics if any visible one exists, else report-sourced.
def _ask_tools_where(site_ids, *, author_ids, site_id, user_id):
    from deleted_predicates import visible_topics_predicate
    clauses = ["t.site_id = ANY(%s::uuid[])",
               visible_topics_predicate("t"),
               "t.work_class IS DISTINCT FROM 'non_work'",
               "(t.source_s3_key LIKE 'extractions/%%' OR NOT EXISTS ("
               " SELECT 1 FROM topics x WHERE x.site_id = t.site_id"
               " AND x.user_id IS NOT DISTINCT FROM t.user_id"
               " AND x.report_date = t.report_date"
               " AND x.source_s3_key LIKE 'extractions/%%'"
               " AND x.work_class IS DISTINCT FROM 'non_work'"
               " AND " + visible_topics_predicate("x") + "))"]
    params = [[str(s) for s in site_ids]]
    if author_ids is not None:
        clauses.append("t.user_id = ANY(%s::uuid[])")
        params.append([str(a) for a in author_ids])
    if site_id is not None:
        clauses.append("t.site_id = %s")
        params.append(str(site_id))
    if user_id is not None:
        clauses.append("t.user_id = %s")
        params.append(str(user_id))
    return " AND ".join(clauses), params


def day_overview_rows(conn, site_ids, date_from, date_to, *, author_ids=None,
                      site_id=None, user_id=None, limit=60) -> list[dict]:
    if not site_ids or (author_ids is not None and not author_ids):
        return []
    where, params = _ask_tools_where(site_ids, author_ids=author_ids,
                                     site_id=site_id, user_id=user_id)
    sql = (
        "SELECT t.id, t.site_id, s.name AS site_name, t.user_id, "
        "NULLIF(trim(concat_ws(' ', u.first_name, u.last_name)), '') AS user_name, "
        "t.report_date, t.time_range, t.title, t.summary, t.source_s3_key, "
        "(SELECT count(*) FROM action_items a WHERE a.topic_id = t.id) AS n_tasks, "
        "(SELECT count(*) FROM findings f WHERE f.topic_id = t.id) AS n_findings "
        "FROM topics t JOIN sites s ON s.id = t.site_id "
        "LEFT JOIN users u ON u.id = t.user_id "
        f"WHERE {where} AND t.report_date BETWEEN %s AND %s "
        "ORDER BY t.report_date, t.occurred_at NULLS LAST, t.time_range NULLS LAST, t.created_at "
        "LIMIT %s")
    return conn.cursor(row_factory=dict_row).execute(
        sql, params + [date_from, date_to, limit]).fetchall()


def latest_visible_topic_date(conn, site_ids, before, *, author_ids=None,
                              site_id=None, user_id=None):
    if not site_ids or (author_ids is not None and not author_ids):
        return None
    where, params = _ask_tools_where(site_ids, author_ids=author_ids,
                                     site_id=site_id, user_id=user_id)
    row = conn.execute(
        f"SELECT max(t.report_date) FROM topics t WHERE {where} AND t.report_date < %s",
        params + [before]).fetchone()
    return row[0].isoformat() if row and row[0] else None
```

If `dict_row` is not already imported in `topics.py`, it is (`_TOPIC_COLS` readers use it) — confirm with `grep -n "dict_row" src/repositories/topics.py`. If `action_items`/`findings` lack a `topic_id` column, check the migrations and use the actual FK column.

- [ ] **Step 4: Run tests**

Run: `bash fswork/run-tests.sh tests/integration/test_ask_tools_topics_sql.py`
Expected: PASS (5).

- [ ] **Step 5: Commit**

```bash
git add src/repositories/topics.py tests/integration/test_ask_tools_topics_sql.py
git commit -m "Ask tools: day overview and latest visible topic date (topics SQL)

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>
Claude-Session: https://claude.ai/code/session_014QUZA3uk6P8T7qYJoLRfQe"
```

---

### Task 3: people — candidate set, `find_visible_people`, re-validation

**Files:**
- Modify: `src/repositories/recordings.py` (append `author_candidates_in_sites`)
- Modify: `src/repositories/users.py` (append `find_visible_people`, `person_in_reach`)
- Test: `tests/integration/test_ask_tools_people_sql.py`

**Interfaces:**
- Produces: `recordings.author_candidates_in_sites(conn, site_ids) -> set[str]` — user ids (str) with a topic OR recording on `site_ids` OR a live membership there.
- Produces: `users.find_visible_people(conn, company_id, site_ids, author_ids, name) -> list[dict]` — `[{"person_ref": str(user_id), "name": "First Last", "folder_name": str}]`, sorted by name; matches case-insensitive on first name, full name, folder name, and active `name_aliases` (`kind='person'`) whose `wrong_term` equals the query and whose `right_term` matches a candidate's full/first name.
- Produces: `users.person_in_reach(conn, site_ids, author_ids, user_id) -> bool` — the per-call re-validation.

- [ ] **Step 1: Write the failing integration tests**

```python
# tests/integration/test_ask_tools_people_sql.py
import pytest
from repositories import companies, sites, topics, users, recordings

pytestmark = pytest.mark.integration


def _user(db, co, folder, first, last=None):
    u = users.upsert_user(db, f"sub-{folder}", f"{folder}@x.nz", company_id=co["id"],
                          first_name=first, last_name=last)
    db.execute("UPDATE users SET folder_name=%s WHERE id=%s", (folder, u["id"]))
    return u


@pytest.fixture
def w(db):
    a = companies.create_company(db, "A")
    b = companies.create_company(db, "B")
    sa = sites.create_site(db, a["id"], "SA")
    sb = sites.create_site(db, b["id"], "SB")
    mike1 = _user(db, a, "Mike_Smith", "Mike", "Smith")
    mike2 = _user(db, a, "Mike_Tane", "Mike", "Tane")
    bmike = _user(db, b, "Mike_B", "Mike", "Brown")
    for u, s in ((mike1, sa), (mike2, sa), (bmike, sb)):
        topics.upsert_topic(db, s["id"], "2026-10-14", "x", user_id=u["id"],
                            source_s3_key="extractions/2026-10-14/x/a.json")
    return {"a": a, "b": b, "sa": sa, "sb": sb, "mike1": mike1, "mike2": mike2, "bmike": bmike}


def test_two_mikes_both_returned_other_company_never(db, w):
    got = users.find_visible_people(db, w["a"]["id"], [w["sa"]["id"]], None, "mike")
    assert sorted(p["name"] for p in got) == ["Mike Smith", "Mike Tane"]


def test_graded_author_set_narrows(db, w):
    got = users.find_visible_people(db, w["a"]["id"], [w["sa"]["id"]],
                                    {str(w["mike1"]["id"])}, "mike")
    assert [p["name"] for p in got] == ["Mike Smith"]


def test_person_alias_resolves(db, w):
    db.execute("INSERT INTO name_aliases (company_id, wrong_term, right_term, kind) "
               "VALUES (%s, 'Mikey', 'Mike Tane', 'person')", (w["a"]["id"],))
    got = users.find_visible_people(db, w["a"]["id"], [w["sa"]["id"]], None, "mikey")
    assert [p["name"] for p in got] == ["Mike Tane"]


def test_person_in_reach_rejects_other_company_ref(db, w):
    assert users.person_in_reach(db, [w["sa"]["id"]], None, str(w["mike1"]["id"]))
    assert not users.person_in_reach(db, [w["sa"]["id"]], None, str(w["bmike"]["id"]))
    assert not users.person_in_reach(db, [w["sa"]["id"]], {str(w["mike2"]["id"])},
                                     str(w["mike1"]["id"]))


def test_candidates_include_live_membership_only_people(db, w):
    lone = _user(db, w["a"], "New_Hire", "New", "Hire")
    db.execute("INSERT INTO memberships (user_id, site_id, role) VALUES (%s, %s, 'worker')",
               (lone["id"], w["sa"]["id"]))
    assert str(lone["id"]) in recordings.author_candidates_in_sites(db, [w["sa"]["id"]])
```

Before running: check `CREATE TABLE memberships` in migrations for its NOT NULL columns and adjust the INSERT column list only.

- [ ] **Step 2: Run to verify failure**

Run: `bash fswork/run-tests.sh tests/integration/test_ask_tools_people_sql.py`
Expected: FAIL — `AttributeError: ... 'find_visible_people'`.

- [ ] **Step 3: Implement**

Append to `src/repositories/recordings.py`:

```python
def author_candidates_in_sites(conn, site_ids) -> set:
    """Set-returning sibling of `author_footprint_in_sites`: every user with a
    topic or a recording on `site_ids`, or a live membership there. The
    candidate set for Ask's find_people (spec 2026-10-15 §3.1.4)."""
    if not site_ids:
        return set()
    ids = [str(s) for s in site_ids]
    rows = conn.execute(
        "SELECT user_id FROM topics WHERE site_id = ANY(%s::uuid[]) AND user_id IS NOT NULL "
        "UNION SELECT user_id FROM recordings WHERE site_id = ANY(%s::uuid[]) AND user_id IS NOT NULL "
        "UNION SELECT user_id FROM memberships WHERE site_id = ANY(%s::uuid[]) "
        "AND archived_at IS NULL",
        (ids, ids, ids)).fetchall()
    return {str(r[0]) for r in rows}
```

Append to `src/repositories/users.py`:

```python
def _candidates(conn, site_ids, author_ids):
    from repositories import recordings
    cands = recordings.author_candidates_in_sites(conn, site_ids)
    if author_ids is not None:
        cands &= {str(a) for a in author_ids}
    return cands


def person_in_reach(conn, site_ids, author_ids, user_id) -> bool:
    """Re-validated on EVERY tool call (spec §3.1.4): a person_ref is never
    trusted because find_people minted it."""
    return bool(user_id) and str(user_id) in _candidates(conn, site_ids, author_ids)


def find_visible_people(conn, company_id, site_ids, author_ids, name) -> list[dict]:
    cands = _candidates(conn, site_ids, author_ids)
    q = (name or "").strip().lower()
    if not cands or not q:
        return []
    alias_targets = [r[0].lower() for r in conn.execute(
        "SELECT right_term FROM name_aliases WHERE company_id=%s AND status='active' "
        "AND kind='person' AND lower(wrong_term)=%s", (company_id, q)).fetchall()]
    rows = conn.cursor(row_factory=dict_row).execute(
        "SELECT id, first_name, last_name, folder_name FROM users "
        "WHERE id = ANY(%s::uuid[])", (sorted(cands),)).fetchall()
    out = []
    for r in rows:
        full = " ".join(p for p in (r["first_name"], r["last_name"]) if p).strip()
        keys = {(r["first_name"] or "").lower(), full.lower(), (r["folder_name"] or "").lower(),
                (r["folder_name"] or "").replace("_", " ").lower()}
        if q in keys or full.lower() in alias_targets or (r["first_name"] or "").lower() in alias_targets:
            out.append({"person_ref": str(r["id"]), "name": full or r["folder_name"],
                        "folder_name": r["folder_name"]})
    return sorted(out, key=lambda p: p["name"] or "")
```

The lookup is by user id across companies (no company pin), so external authors with a footprint on the caller's sites are found — the "global lookup" the spec requires — while company B's people never enter `cands`. Confirm `dict_row` is imported in `users.py`; add `from psycopg.rows import dict_row` if not.

- [ ] **Step 4: Run tests**

Run: `bash fswork/run-tests.sh tests/integration/test_ask_tools_people_sql.py`
Expected: PASS (5).

- [ ] **Step 5: Commit**

```bash
git add src/repositories/recordings.py src/repositories/users.py tests/integration/test_ask_tools_people_sql.py
git commit -m "Ask tools: people candidate set, find_visible_people, per-call reach check

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>
Claude-Session: https://claude.ai/code/session_014QUZA3uk6P8T7qYJoLRfQe"
```

---

### Task 4: `chunks.latest_matching_date`

**Files:**
- Modify: `src/repositories/chunks.py`
- Test: `tests/integration/test_ask_tools_latest_match_sql.py`

**Interfaces:**
- Consumes: `search_sql.family_predicate` / `visible_chunks_predicate` exactly as `latest_visible_date` uses them (read that function first and copy its WHERE construction).
- Produces: `chunks.latest_matching_date(conn, query_embedding, site_ids, *, on_or_before, window_days=92, gate=0.55, query_text="", author_ids=None, source_family=None) -> str | None` — latest `report_date` in `[on_or_before - window_days + 1, on_or_before]` with a visible chunk whose cosine distance ≤ `gate`, or a keyword-arm hit on `query_text`.

- [ ] **Step 1: Write the failing test**

```python
# tests/integration/test_ask_tools_latest_match_sql.py
import pytest
from repositories import companies, sites, chunks

pytestmark = pytest.mark.integration


def _v(hot):
    v = [0.0] * 1024
    v[hot] = 1.0
    return v


def test_latest_day_within_gate_and_window(db):
    co = companies.create_company(db, "A")
    s = sites.create_site(db, co["id"], "S")
    chunks.insert_chunk(db, s["id"], "2026-10-01", "topic", "concrete pour", _v(0))
    chunks.insert_chunk(db, s["id"], "2026-10-12", "topic", "scaffold", _v(5))      # far
    chunks.insert_chunk(db, s["id"], "2026-06-01", "topic", "concrete old", _v(0))  # out of window
    got = chunks.latest_matching_date(db, _v(0), [s["id"]], on_or_before="2026-10-15")
    assert got == "2026-10-01"


def test_keyword_hit_passes_gate(db):
    co = companies.create_company(db, "A")
    s = sites.create_site(db, co["id"], "S")
    chunks.insert_chunk(db, s["id"], "2026-10-12", "topic", "the GIB delivery", _v(5))
    got = chunks.latest_matching_date(db, _v(0), [s["id"]], on_or_before="2026-10-15",
                                      query_text="GIB")
    assert got == "2026-10-12"


def test_other_site_never_counts(db):
    co = companies.create_company(db, "A")
    s1 = sites.create_site(db, co["id"], "S1")
    s2 = sites.create_site(db, co["id"], "S2")
    chunks.insert_chunk(db, s2["id"], "2026-10-12", "topic", "concrete", _v(0))
    assert chunks.latest_matching_date(db, _v(0), [s1["id"]], on_or_before="2026-10-15") is None
```

- [ ] **Step 2: Run to verify failure**

Run: `bash fswork/run-tests.sh tests/integration/test_ask_tools_latest_match_sql.py`
Expected: FAIL — `AttributeError: ... 'latest_matching_date'`.

- [ ] **Step 3: Implement (append to `src/repositories/chunks.py`)**

Read `latest_visible_date` and `search_sql.build_latest_date_sql` first; reuse the same visibility/family clause builder they use (call it, do not copy its text). Shape:

```python
def latest_matching_date(conn, query_embedding, accessible_site_ids, *, on_or_before,
                         window_days=92, gate=0.55, query_text="", author_ids=None,
                         source_family=None):
    """Latest report_date with a visible chunk near the query (Ask tools,
    spec 2026-10-15 §3.1.3). Exact scan over a bounded window: the gate makes
    HNSW useless here, so the window is what bounds the cost."""
    if not accessible_site_ids:
        return None
    from datetime import date, timedelta
    hi = date.fromisoformat(str(on_or_before))
    lo = hi - timedelta(days=window_days - 1)
    vis_sql, vis_params = search_sql.visibility_clause(          # name per search_sql
        accessible_site_ids, author_ids=author_ids, source_family=source_family)
    lex = query_text.strip()
    sql = (f"SELECT max(c.report_date) FROM report_chunks c WHERE {vis_sql} "
           "AND c.report_date BETWEEN %s AND %s "
           "AND ((c.embedding <=> %s::vector) <= %s"
           + (" OR c.chunk_text ILIKE %s" if lex else "") + ")")
    params = vis_params + [lo, hi, query_embedding, gate] + ([f"%{lex}%"] if lex else [])
    row = conn.execute(sql, params).fetchone()
    return row[0].isoformat() if row and row[0] else None
```

`search_sql.visibility_clause` is a placeholder name for whatever helper `build_latest_date_sql` uses to emit the site/author/family/visible-chunk WHERE; if it is inline rather than a helper, extract it into `search_sql.visibility_clause(site_ids, *, author_ids, source_family) -> (sql, params)`, make `build_latest_date_sql` call it, and keep `tests/unit/test_search_sql*.py` green (run them). Use the same keyword match `build_search_sql` uses (its tsquery/`or_query`) if it is not ILIKE — the test only needs "a keyword-arm hit counts".

- [ ] **Step 4: Run tests**

Run: `bash fswork/run-tests.sh tests/integration/test_ask_tools_latest_match_sql.py` then `python -m pytest tests/unit -q -k "search_sql"`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add src/repositories/chunks.py src/repositories/search_sql.py tests/integration/test_ask_tools_latest_match_sql.py
git commit -m "Ask tools: latest matching date over a bounded window

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>
Claude-Session: https://claude.ai/code/session_014QUZA3uk6P8T7qYJoLRfQe"
```

---

### Task 5: rag-search `mode: "tools"` batch dispatcher

**Files:**
- Create: `src/rag_tools_dispatch.py` (keeps `lambda_rag_search.py` small)
- Modify: `src/lambda_rag_search.py` (route `mode == "tools"`)
- Test: `tests/unit/test_rag_tools_dispatch.py`, `tests/integration/test_rag_tools_dispatch_sql.py`

**Interfaces:**
- Consumes: Tasks 2–4 repository functions; existing `_search(event, context)` and `_metric(event)` in `lambda_rag_search`.
- Event in: `{"mode": "tools", "sub": str, "deadline_s": float, "calls": [{"id": str, "name": str, "args": {...normalized by Task 1...}, "query_embedding": [..] (search_records / latest_activity with query)}]}`.
- Produces (out): `{"results": [{"id": str, "name": str, "rows": [...], "meta": {...}}]}` — raw rows; refs/budget are Task 6's job. `meta` keys: `empty_reason`, `latest_before` (`{"date","from"}`), `error`, `person_ambiguous`, `person_unresolved`, `ms`.
- Produces: `rag_tools_dispatch.run(conn, caller, sc, event, *, search_fn, metric_fn, clock=time.monotonic) -> dict`.

Rules the dispatcher implements (each one a test):
1. ACL resolved once by the handler (`scope.visible_scope`) and passed in as `sc`; `caller` from `users.get_user_by_sub`; unprovisioned → every call `meta.error="not_provisioned"`.
2. At most 4 calls; extra calls → `meta.error="bad_args"`, `field="calls"`.
3. Before each call: if `clock() - start >= deadline_s` → `meta.error="timeout"`, not run.
4. Each call in `try/except Exception` → `meta.error="upstream"` for that call only; log `type(e).__name__` only.
5. `site` arg → resolved against `sc["site_ids"]` by UUID equality or case-insensitive `sites.name`; unresolved → rows `[]`, `empty_reason="no_match"`.
6. `person` arg → UUID that passes `users.person_in_reach`, else a name through `users.find_visible_people`: exactly 1 → that id; 0 → `person_unresolved`; >1 → `person_ambiguous` with the candidates listed in `meta.person_ambiguous`; both return rows `[]`.
7. `day_overview` → `topics.day_overview_rows`; empty → `empty_reason="nothing_visible"` if `sc["site_ids"]` is empty else `"nothing_in_range"`, plus `latest_before={"date": topics.latest_visible_topic_date(...before=date_from...), "from": "topics"}`.
8. `search_records` → builds a `_search` event (`sub`, `query_embedding`, `question=query`, `k=k*2 or 16`, dates, site id, author folder resolved from person) and calls `search_fn(event, None)`; returns its `chunks` as rows; empty with dates → `latest_before={"date": chunks.latest_visible_date(...on_or_before=date_from - 1 day...), "from": "chunks"}`; empty without dates → `no_match`.
9. `latest_activity` → no query: `topics.latest_visible_topic_date(before=on_or_before+1 day)` then `day_overview_rows` on that day; with query: `chunks.latest_matching_date(gate=float(os.environ.get("ASK_TOOLS_MATCH_GATE", "0.55")))` then `search_fn` on that day.
10. `find_people` → rows from `users.find_visible_people`; empty → `no_match`.
11. `count_metric` → `metric_fn({"mode": "metric", "sub", "metric", "date_from", "date_to"})`; its zero kind (whatever key `_metric` uses for it — read `_metric`) → `meta.empty_reason`; the result dict is the single row.
12. All rows JSON-safe (`json.loads(json.dumps(rows, default=str))`), and every text field passed through the alias normalization `_search` applies (`aliases.list_active` + `text_normalize.normalize`) — for `day_overview` rows apply it to `title` and `summary`.

- [ ] **Step 1: Write the failing unit tests (fakes for repos, real control flow)**

```python
# tests/unit/test_rag_tools_dispatch.py
import rag_tools_dispatch as d


class Clock:
    def __init__(self, ticks): self.ticks = list(ticks)
    def __call__(self): return self.ticks.pop(0) if len(self.ticks) > 1 else self.ticks[0]


SC = {"site_ids": {"s1"}, "author_ids": None, "company_id": "co"}
CALLER = {"id": "u1", "company_id": "co"}


def _run(calls, monkeypatch, *, clock=None, search=None, metric=None, sc=SC):
    monkeypatch.setattr(d, "_normalize_rows", lambda conn, caller, sc, rows, fields: rows)
    return d.run(object(), CALLER, sc, {"calls": calls, "deadline_s": 20},
                 search_fn=search or (lambda e, c: {"chunks": []}),
                 metric_fn=metric or (lambda e: {"value": 0}),
                 clock=clock or Clock([0.0]))["results"]


def test_one_failing_call_does_not_lose_the_others(monkeypatch):
    monkeypatch.setattr(d.topics, "day_overview_rows",
                        lambda *a, **k: [{"id": "t1", "title": "Pour"}])
    def boom(*a, **k): raise RuntimeError("sql")
    monkeypatch.setattr(d.users, "find_visible_people", boom)
    res = _run([{"id": "a", "name": "day_overview", "args": {"date_from": "2026-10-14", "date_to": "2026-10-14"}},
                {"id": "b", "name": "find_people", "args": {"name": "Mike"}},
                {"id": "c", "name": "day_overview", "args": {"date_from": "2026-10-13", "date_to": "2026-10-13"}}],
               monkeypatch)
    assert [r["meta"].get("error") for r in res] == [None, "upstream", None]
    assert res[0]["rows"][0]["title"] == "Pour"


def test_past_deadline_calls_do_not_run(monkeypatch):
    ran = []
    monkeypatch.setattr(d.topics, "day_overview_rows", lambda *a, **k: ran.append(1) or [])
    monkeypatch.setattr(d.topics, "latest_visible_topic_date", lambda *a, **k: None)
    res = _run([{"id": "a", "name": "day_overview", "args": {"date_from": "2026-10-14", "date_to": "2026-10-14"}},
                {"id": "b", "name": "day_overview", "args": {"date_from": "2026-10-13", "date_to": "2026-10-13"}}],
               monkeypatch, clock=Clock([0.0, 0.0, 25.0]))
    assert len(ran) == 1 and res[1]["meta"]["error"] == "timeout"


def test_more_than_four_calls_rejected(monkeypatch):
    res = _run([{"id": str(i), "name": "find_people", "args": {"name": "x"}} for i in range(5)],
               monkeypatch)
    assert all(r["meta"] == {"error": "bad_args", "field": "calls"} for r in res)


def test_empty_day_carries_latest_before_from_topics(monkeypatch):
    monkeypatch.setattr(d.topics, "day_overview_rows", lambda *a, **k: [])
    monkeypatch.setattr(d.topics, "latest_visible_topic_date", lambda *a, **k: "2026-10-12")
    res = _run([{"id": "a", "name": "day_overview", "args": {"date_from": "2026-10-14", "date_to": "2026-10-14"}}],
               monkeypatch)
    assert res[0]["meta"]["empty_reason"] == "nothing_in_range"
    assert res[0]["meta"]["latest_before"] == {"date": "2026-10-12", "from": "topics"}


def test_ambiguous_person_returns_no_rows(monkeypatch):
    monkeypatch.setattr(d.users, "find_visible_people", lambda *a, **k: [
        {"person_ref": "m1", "name": "Mike Smith"}, {"person_ref": "m2", "name": "Mike Tane"}])
    called = []
    monkeypatch.setattr(d.topics, "day_overview_rows", lambda *a, **k: called.append(1) or [{"id": "x"}])
    res = _run([{"id": "a", "name": "day_overview",
                 "args": {"date_from": "2026-10-14", "date_to": "2026-10-14", "person": "Mike"}}],
               monkeypatch)
    assert res[0]["rows"] == [] and not called
    assert [p["name"] for p in res[0]["meta"]["person_ambiguous"]] == ["Mike Smith", "Mike Tane"]


def test_person_ref_out_of_reach_is_unresolved_not_error(monkeypatch):
    monkeypatch.setattr(d.users, "person_in_reach", lambda *a, **k: False)
    monkeypatch.setattr(d.users, "find_visible_people", lambda *a, **k: [])
    res = _run([{"id": "a", "name": "day_overview",
                 "args": {"date_from": "2026-10-14", "date_to": "2026-10-14",
                          "person": "7d3c1a52-0000-0000-0000-000000000000"}}], monkeypatch)
    assert res[0]["rows"] == [] and res[0]["meta"].get("person_unresolved") is True
    assert "error" not in res[0]["meta"]


def test_no_sites_is_nothing_visible(monkeypatch):
    monkeypatch.setattr(d.topics, "day_overview_rows", lambda *a, **k: [])
    monkeypatch.setattr(d.topics, "latest_visible_topic_date", lambda *a, **k: None)
    res = _run([{"id": "a", "name": "day_overview", "args": {"date_from": "2026-10-14", "date_to": "2026-10-14"}}],
               monkeypatch, sc={"site_ids": set(), "author_ids": None, "company_id": "co"})
    assert res[0]["meta"]["empty_reason"] == "nothing_visible"


def test_count_metric_passes_zero_kind_through(monkeypatch):
    res = _run([{"id": "a", "name": "count_metric",
                 "args": {"metric": "duration", "date_from": "2026-10-14", "date_to": "2026-10-14"}}],
               monkeypatch, metric=lambda e: {"value": 0, "zero": "no_rows_for_that_day"})
    assert res[0]["meta"]["empty_reason"] == "no_rows_for_that_day"
```

Adjust the `"zero"` key in the last test to whatever key `_metric` actually uses for its zero kind (read `lambda_rag_search._metric` first); keep the assertion on `empty_reason`.

- [ ] **Step 2: Run to verify failure**

Run: `python -m pytest tests/unit/test_rag_tools_dispatch.py -q`
Expected: FAIL — `ModuleNotFoundError: No module named 'rag_tools_dispatch'`.

- [ ] **Step 3: Implement `src/rag_tools_dispatch.py`**

```python
"""rag-search `mode: "tools"`: run a batch of Ask tool calls (spec
2026-10-15-ask-retrieval-tools §3.4). In-VPC, one connection, ACL resolved
once by the caller. Every call is isolated: one failure never loses the rest.
Logs carry tool names, counts and timings -- never query or row text."""
from __future__ import annotations

import json
import logging
import os
import re
import time
from datetime import date, timedelta

from repositories import aliases, chunks, sites, topics, users
import text_normalize

logger = logging.getLogger()
MAX_CALLS = 4
_UUID = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$", re.I)


def _day_before(iso):
    return (date.fromisoformat(iso) - timedelta(days=1)).isoformat()


def _day_after(iso):
    return (date.fromisoformat(iso) + timedelta(days=1)).isoformat()


def _normalize_rows(conn, caller, sc, rows, fields):
    active = aliases.list_active(conn, caller["company_id"],
                                 site_ids=[str(s) for s in sc["site_ids"]])
    pairs = [{"wrong_term": a["wrong_term"], "right_term": a["right_term"]} for a in active]
    if pairs:
        for r in rows:
            for f in fields:
                if r.get(f):
                    r[f] = text_normalize.normalize(r[f], pairs)
    return rows


def _resolve_site(conn, sc, value):
    if value is None:
        return None, True
    ids = {str(s) for s in sc["site_ids"]}
    if _UUID.match(value) and value.lower() in ids:
        return value.lower(), True
    for s in sites.list_sites_by_ids(conn, list(ids)):
        if (s.get("name") or "").strip().lower() == value.strip().lower():
            return str(s["id"]), True
    return None, False


def _resolve_person(conn, caller, sc, value, meta):
    """(user_id or None, ok). ok False => meta already says why; return no rows."""
    if value is None:
        return None, True
    if _UUID.match(value):
        if users.person_in_reach(conn, sc["site_ids"], sc["author_ids"], value):
            return value.lower(), True
        meta["person_unresolved"] = True
        return None, False
    found = users.find_visible_people(conn, caller["company_id"], sc["site_ids"],
                                      sc["author_ids"], value)
    if len(found) == 1:
        return found[0]["person_ref"], True
    if found:
        meta["person_ambiguous"] = [{"person_ref": p["person_ref"], "name": p["name"]}
                                    for p in found]
    else:
        meta["person_unresolved"] = True
    return None, False


def _empty_reason(sc):
    return "nothing_visible" if not sc["site_ids"] else "nothing_in_range"


def _day_overview(conn, caller, sc, a, meta, **_):
    site_id, ok_s = _resolve_site(conn, sc, a.get("site"))
    user_id, ok_p = _resolve_person(conn, caller, sc, a.get("person"), meta)
    if not (ok_s and ok_p):
        if not ok_s:
            meta["empty_reason"] = "no_match"
        return []
    kw = dict(author_ids=sc["author_ids"], site_id=site_id, user_id=user_id)
    rows = topics.day_overview_rows(conn, sc["site_ids"], a["date_from"], a["date_to"], **kw)
    if not rows:
        meta["empty_reason"] = _empty_reason(sc)
        meta["latest_before"] = {"date": topics.latest_visible_topic_date(
            conn, sc["site_ids"], a["date_from"], **kw), "from": "topics"}
    return _normalize_rows(conn, caller, sc, rows, ("title", "summary"))


def _search_event(caller, sc, a, call, site_id, user_id, conn):
    ev = {"sub": caller["sub"], "query_embedding": call["query_embedding"],
          "question": a["query"], "k": a.get("k", 8) * 2}
    if a.get("date_from"):
        ev["date_from"], ev["date_to"] = a["date_from"], a["date_to"]
    if site_id:
        ev["site"] = site_id
    if user_id:
        u = users.get_by_id(conn, user_id)
        ev["author"] = u and u.get("folder_name")
    return ev


def _search_records(conn, caller, sc, a, meta, call, search_fn, **_):
    site_id, ok_s = _resolve_site(conn, sc, a.get("site"))
    user_id, ok_p = _resolve_person(conn, caller, sc, a.get("person"), meta)
    if not (ok_s and ok_p):
        if not ok_s:
            meta["empty_reason"] = "no_match"
        return []
    res = search_fn(_search_event(caller, sc, a, call, site_id, user_id, conn), None) or {}
    if res.get("error"):
        meta["error"] = "not_provisioned" if "provision" in res["error"] else "upstream"
        return []
    rows = res.get("chunks") or []
    if not rows:
        if a.get("date_from"):
            meta["empty_reason"] = _empty_reason(sc)
            meta["latest_before"] = {"date": chunks.latest_visible_date(
                conn, sc["site_ids"], author_ids=sc["author_ids"],
                on_or_before=_day_before(a["date_from"])), "from": "chunks"}
        else:
            meta["empty_reason"] = "no_match" if sc["site_ids"] else "nothing_visible"
    return rows


def _latest_activity(conn, caller, sc, a, meta, call, search_fn, **_):
    site_id, ok_s = _resolve_site(conn, sc, a.get("site"))
    user_id, ok_p = _resolve_person(conn, caller, sc, a.get("person"), meta)
    if not (ok_s and ok_p):
        if not ok_s:
            meta["empty_reason"] = "no_match"
        return []
    upto = a.get("on_or_before") or date.today().isoformat()
    kw = dict(author_ids=sc["author_ids"], site_id=site_id, user_id=user_id)
    if not a.get("query"):
        day = topics.latest_visible_topic_date(conn, sc["site_ids"], _day_after(upto), **kw)
        if not day:
            meta["empty_reason"] = _empty_reason(sc)
            return []
        rows = topics.day_overview_rows(conn, sc["site_ids"], day, day, **kw)
        return _normalize_rows(conn, caller, sc, rows, ("title", "summary"))
    gate = float(os.environ.get("ASK_TOOLS_MATCH_GATE", "0.55"))
    day = chunks.latest_matching_date(conn, call["query_embedding"], sc["site_ids"],
                                      on_or_before=upto, gate=gate, query_text=a["query"],
                                      author_ids=sc["author_ids"])
    if not day:
        meta["empty_reason"] = "no_match" if sc["site_ids"] else "nothing_visible"
        return []
    a2 = dict(a, date_from=day, date_to=day)
    res = search_fn(_search_event(caller, sc, a2, call, site_id, user_id, conn), None) or {}
    return res.get("chunks") or []


def _find_people(conn, caller, sc, a, meta, **_):
    rows = users.find_visible_people(conn, caller["company_id"], sc["site_ids"],
                                     sc["author_ids"], a["name"])
    if not rows:
        meta["empty_reason"] = "no_match" if sc["site_ids"] else "nothing_visible"
    return rows


def _count_metric(conn, caller, sc, a, meta, metric_fn, **_):
    res = metric_fn({"mode": "metric", "sub": caller["sub"], "metric": a["metric"],
                     "date_from": a["date_from"], "date_to": a["date_to"]}) or {}
    if res.get("error"):
        meta["error"] = "upstream"
        return []
    zero = res.get("zero")          # the key _metric uses for its zero kind
    if zero:
        meta["empty_reason"] = zero
    return [res]


_TOOLS = {"day_overview": _day_overview, "search_records": _search_records,
          "latest_activity": _latest_activity, "find_people": _find_people,
          "count_metric": _count_metric}


def run(conn, caller, sc, event, *, search_fn, metric_fn, clock=time.monotonic):
    calls = event.get("calls") or []
    if len(calls) > MAX_CALLS:
        return {"results": [{"id": c.get("id"), "name": c.get("name"), "rows": [],
                             "meta": {"error": "bad_args", "field": "calls"}} for c in calls]}
    start = clock()
    deadline = float(event.get("deadline_s") or 20)
    out = []
    for call in calls:
        meta, rows = {}, []
        t0 = clock()
        if t0 - start >= deadline:
            meta["error"] = "timeout"
        else:
            fn = _TOOLS.get(call.get("name"))
            try:
                if fn is None:
                    meta.update({"error": "bad_args", "field": "name"})
                else:
                    rows = fn(conn, caller, sc, call.get("args") or {}, meta, call=call,
                              search_fn=search_fn, metric_fn=metric_fn)
            except Exception as exc:                       # noqa: BLE001
                logger.warning("ask tool %s failed: %s", call.get("name"), type(exc).__name__)
                meta = {"error": "upstream"}
                rows = []
        meta["ms"] = int((clock() - t0) * 1000)
        logger.info("ask tool %s rows=%d empty=%s err=%s ms=%d", call.get("name"), len(rows),
                    meta.get("empty_reason"), meta.get("error"), meta["ms"])
        out.append({"id": call.get("id"), "name": call.get("name"),
                    "rows": json.loads(json.dumps(rows, default=str)), "meta": meta})
    return {"results": out}
```

Fix the test helper to match: `_run` monkeypatches `_normalize_rows` (already does). In the timeout test the `Clock` returns `0.0` for `start`, `0.0` for call 1's `t0` and ms, and `25.0` thereafter — so call 2 is skipped. If `sites.list_sites_by_ids` has a different name, use the existing function that lists sites by id (grep `def list_sites` in `src/repositories/sites.py`). `caller["sub"]` must be present: the handler passes `dict(caller, sub=event["sub"])`.

- [ ] **Step 4: Route the mode in `src/lambda_rag_search.py`**

In `_search`, directly after the existing `if event.get("mode") == "metric":` branch, add:

```python
    if event.get("mode") == "tools":
        return _tools(event)
```

and add the function:

```python
def _tools(event):
    """Ask tools batch (spec 2026-10-15-ask-retrieval-tools). ACL resolved once."""
    import rag_tools_dispatch
    sub = event.get("sub")
    calls = event.get("calls") or []
    conn = get_cached_connection()
    caller = users.get_user_by_sub(conn, sub) if sub else None
    if caller is None:
        return {"results": [{"id": c.get("id"), "name": c.get("name"), "rows": [],
                             "meta": {"error": "not_provisioned"}} for c in calls]}
    sc = scope.visible_scope(conn, caller)
    sc = {"site_ids": {str(s) for s in sc["site_ids"]},
          "author_ids": ({str(a) for a in sc["author_ids"]}
                         if sc["author_ids"] is not None else None),
          "company_id": caller["company_id"]}
    return rag_tools_dispatch.run(conn, dict(caller, sub=sub), sc, event,
                                  search_fn=_search, metric_fn=_metric)
```

(`_search` is called recursively with a normal search event; that path resolves the ACL again — accepted cost, ~1 query, and it keeps a single implementation of search.)

- [ ] **Step 5: Run unit tests**

Run: `python -m pytest tests/unit/test_rag_tools_dispatch.py tests/unit/test_lambda_rag_search.py -q`
Expected: PASS.

- [ ] **Step 6: Write the end-to-end integration test (real SQL through the handler)**

```python
# tests/integration/test_rag_tools_dispatch_sql.py
import pytest
import lambda_rag_search
from repositories import companies, sites, topics, users, memberships

pytestmark = pytest.mark.integration


def _user(db, co, folder, role):
    u = users.upsert_user(db, f"sub-{folder}", f"{folder}@x.nz", company_id=co["id"],
                          first_name=folder)
    db.execute("UPDATE users SET folder_name=%s, global_role=%s WHERE id=%s",
               (folder, role, u["id"]))
    return u


def test_cross_company_person_ref_and_site_get_the_no_match_shape(db, monkeypatch):
    monkeypatch.setattr(lambda_rag_search, "get_cached_connection", lambda: db)
    a = companies.create_company(db, "A")
    b = companies.create_company(db, "B")
    sa = sites.create_site(db, a["id"], "Alpha")
    sb = sites.create_site(db, b["id"], "Bravo")
    ga = _user(db, a, "GmA", "gm")
    bw = _user(db, b, "WorkerB", "worker")
    topics.upsert_topic(db, sb["id"], "2026-10-14", "B secret", user_id=bw["id"],
                        source_s3_key="extractions/2026-10-14/WorkerB/a.json")
    ev = {"mode": "tools", "sub": "sub-GmA", "deadline_s": 20, "calls": [
        {"id": "1", "name": "day_overview",
         "args": {"date_from": "2026-10-14", "date_to": "2026-10-14", "person": str(bw["id"])}},
        {"id": "2", "name": "day_overview",
         "args": {"date_from": "2026-10-14", "date_to": "2026-10-14", "site": str(sb["id"])}},
        {"id": "3", "name": "day_overview",
         "args": {"date_from": "2026-10-14", "date_to": "2026-10-14", "site": "Bravo"}}]}
    res = lambda_rag_search._search(ev, None)["results"]
    assert all(r["rows"] == [] for r in res)
    assert res[0]["meta"].get("person_unresolved") is True
    assert res[1]["meta"]["empty_reason"] == "no_match" == res[2]["meta"]["empty_reason"]
    assert all("error" not in r["meta"] for r in res)
```

If `gm` requires a membership row to see `sa` under `visible_scope`, add the same membership seeding other `tests/integration/test_rag_search_*.py` files use (copy their fixture). The assertions stay.

Run: `bash fswork/run-tests.sh tests/integration/test_rag_tools_dispatch_sql.py`
Expected: PASS.

- [ ] **Step 7: Commit**

```bash
git add src/rag_tools_dispatch.py src/lambda_rag_search.py tests/unit/test_rag_tools_dispatch.py tests/integration/test_rag_tools_dispatch_sql.py
git commit -m "rag-search: tools batch mode with per-call isolation and deadline

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>
Claude-Session: https://claude.ai/code/session_014QUZA3uk6P8T7qYJoLRfQe"
```

---

### Task 6: `ask_tools.execute` — embed, invoke, post-process, refs, budget

**Files:**
- Modify: `src/ask_tools.py` (append)
- Test: `tests/unit/test_ask_tools_execute.py`

**Interfaces:**
- Consumes: `validate_call` (Task 1); the rag-search event/response of Task 5; `dashscope_utils.embed(list[str]) -> list[list[float]]`; `lambda_ask_agent._admit_one_lexical_hit(chunks, keep)` and `_rerank_chunks(question, chunks, keep)` (import lazily inside the function, as ask-agent imports its own optional modules).
- Produces: `ask_tools.RefAssigner` — `assign(kind: str, ident: str, prefix: str = "R") -> str`; same `(kind, ident)` always gets the same ref; `R` and `W` counters are independent. One instance per Ask turn, constructed by the orchestrator and passed in `ctx.refs`.
- Produces: `ask_tools.ToolContext` (dataclass: `caller_sub: str`, `today: date | None`, `deadline: float` (absolute `time.monotonic()` timestamp), `lambda_client`, `function_name: str`, `refs: RefAssigner` (default: a fresh one), `clock` (default `time.monotonic`)).
- Produces: `ask_tools.execute(calls: list[dict], ctx: ToolContext) -> list[dict]` — one envelope per call: `{"tool", "id", "items", "meta"}` per spec §3.2. Never raises.

- [ ] **Step 1: Write the failing tests**

```python
# tests/unit/test_ask_tools_execute.py
import io
import json
from datetime import date
import ask_tools


class FakeLambda:
    def __init__(self, results):
        self.results, self.events = results, []

    def invoke(self, FunctionName, InvocationType, Payload):
        ev = json.loads(Payload)
        self.events.append(ev)
        return {"Payload": io.BytesIO(json.dumps({"results": self.results(ev)}).encode())}


def _ctx(fake, today=date(2026, 10, 15)):
    return ask_tools.ToolContext(caller_sub="sub-1", today=today, deadline=1030.0,
                                 lambda_client=fake, function_name="rag",
                                 clock=lambda: 1000.0)


def test_ref_assigner_shares_counters_per_prefix():
    r = ask_tools.RefAssigner()
    assert r.assign("topic", "t1") == "R1"
    assert r.assign("web", "https://a", prefix="W") == "W1"
    assert r.assign("chunk", "c1") == "R2"
    assert r.assign("topic", "t1") == "R1"


def test_identity_injected_and_args_cannot_override(monkeypatch):
    fake = FakeLambda(lambda ev: [{"id": c["id"], "name": c["name"], "rows": [], "meta": {}}
                                  for c in ev["calls"]])
    env = ask_tools.execute([{"name": "day_overview",
                              "args": {"date_from": "yesterday", "sub": "evil"}}], _ctx(fake))
    assert env[0]["meta"] == {"error": "bad_args", "field": "sub"}
    ask_tools.execute([{"name": "day_overview", "args": {"date_from": "yesterday"}}], _ctx(fake))
    assert fake.events[-1]["sub"] == "sub-1"
    assert fake.events[-1]["calls"][0]["args"]["date_from"] == "2026-10-14"


def test_refs_unique_across_calls_and_deduped_by_kind_and_id(monkeypatch):
    rows = [{"id": "t1", "title": "Pour", "summary": "B2", "report_date": "2026-10-14",
             "site_name": "S1", "user_name": "Ben", "time_range": "09:00 - 09:40"}]
    fake = FakeLambda(lambda ev: [{"id": c["id"], "name": c["name"], "rows": rows, "meta": {}}
                                  for c in ev["calls"]])
    ctx = _ctx(fake)
    env = ask_tools.execute([{"name": "day_overview", "args": {"date_from": "yesterday"}},
                             {"name": "latest_activity", "args": {}}], ctx)
    assert env[0]["items"][0]["ref"] == "R1"
    assert env[1]["items"][0]["ref"] == "R1"            # same topic, same ref
    env2 = ask_tools.execute([{"name": "find_people", "args": {"name": "Ben"}}], ctx)
    assert env2[0]["meta"].get("empty_reason") or env2[0]["items"][0]["ref"] == "R2"


def test_topic_and_chunk_with_same_id_are_different_items():
    def results(ev):
        out = []
        for c in ev["calls"]:
            if c["name"] == "day_overview":
                out.append({"id": c["id"], "name": c["name"], "rows": [{"id": "x", "title": "T"}], "meta": {}})
            else:
                out.append({"id": c["id"], "name": c["name"],
                            "rows": [{"id": "x", "chunk_text": "c", "topic_id": "x"}], "meta": {}})
        return out
    fake = FakeLambda(results)
    import ask_tools as at
    at._embed = lambda texts: [[0.0] * 4 for _ in texts]
    at._post_search = lambda q, rows, k: rows
    env = at.execute([{"name": "day_overview", "args": {"date_from": "yesterday"}},
                      {"name": "search_records", "args": {"query": "pour"}}], _ctx(fake))
    assert env[0]["items"][0]["ref"] != env[1]["items"][0]["ref"]


def test_output_budget_truncates_and_flags():
    big = [{"id": f"t{i}", "title": "T", "summary": "x" * 600} for i in range(40)]
    fake = FakeLambda(lambda ev: [{"id": c["id"], "name": c["name"], "rows": big, "meta": {}}
                                  for c in ev["calls"]])
    env = ask_tools.execute([{"name": "day_overview", "args": {"date_from": "yesterday"}}], _ctx(fake))
    assert env[0]["meta"]["truncated"] is True
    assert len(json.dumps(env[0]["items"])) <= ask_tools.MAX_RESULT_CHARS


def test_invoke_failure_is_upstream_for_every_call_never_raises():
    class Broken:
        def invoke(self, **k): raise RuntimeError("down")
    env = ask_tools.execute([{"name": "day_overview", "args": {"date_from": "yesterday"}}],
                            _ctx(Broken()))
    assert env[0]["meta"]["error"] == "upstream"


def test_deadline_passed_down_and_capped_at_20():
    fake = FakeLambda(lambda ev: [{"id": c["id"], "name": c["name"], "rows": [], "meta": {}}
                                  for c in ev["calls"]])
    ask_tools.execute([{"name": "day_overview", "args": {"date_from": "yesterday"}}], _ctx(fake))
    assert fake.events[-1]["deadline_s"] == 20


def test_text_capped_at_600():
    fake = FakeLambda(lambda ev: [{"id": c["id"], "name": c["name"],
                                   "rows": [{"id": "t", "title": "T", "summary": "y" * 900}], "meta": {}}
                                  for c in ev["calls"]])
    env = ask_tools.execute([{"name": "day_overview", "args": {"date_from": "yesterday"}}], _ctx(fake))
    assert len(env[0]["items"][0]["text"]) <= 600
```

- [ ] **Step 2: Run to verify failure**

Run: `python -m pytest tests/unit/test_ask_tools_execute.py -q`
Expected: FAIL — `AttributeError: module 'ask_tools' has no attribute 'ToolContext'`.

- [ ] **Step 3: Implement (append to `src/ask_tools.py`)**

```python
import json
import logging
import time
from dataclasses import dataclass, field

logger = logging.getLogger()
MAX_RESULT_CHARS = 10_000
MAX_TEXT = 600
MAX_BATCH = 4
_KIND = {"day_overview": "topic", "latest_activity": None, "search_records": "chunk",
         "find_people": "person", "count_metric": "metric"}


class RefAssigner:
    """One per Ask turn, shared by records tools (R#) and the orchestrator's
    web tool (W#). The same (kind, ident) always gets the same ref."""

    def __init__(self):
        self._refs, self._next = {}, {}

    def assign(self, kind, ident, prefix="R"):
        key = (kind, str(ident))
        if key not in self._refs:
            n = self._next.get(prefix, 1)
            self._refs[key] = f"{prefix}{n}"
            self._next[prefix] = n + 1
        return self._refs[key]


@dataclass
class ToolContext:
    caller_sub: str
    today: object
    deadline: float                 # absolute time.monotonic() timestamp
    lambda_client: object
    function_name: str
    refs: "RefAssigner" = field(default_factory=lambda: RefAssigner())
    clock: object = time.monotonic


def _embed(texts):
    import dashscope_utils
    return dashscope_utils.embed(texts)


def _post_search(query, rows, k):
    import lambda_ask_agent as laa
    kept = laa._admit_one_lexical_hit(rows, max(len(rows), k))
    return laa._rerank_chunks(query, kept, k)


def _item(name, row):
    kind = _KIND[name] or ("chunk" if "chunk_text" in row else "topic")
    if kind == "person":
        return kind, row.get("person_ref"), {"kind": kind, "who": row.get("name"),
                                             "person_ref": row.get("person_ref"),
                                             "text": row.get("name") or ""}
    if kind == "metric":
        return kind, name, {"kind": kind, "text": json.dumps(row, default=str)[:MAX_TEXT]}
    meta = row.get("metadata") or {}
    text = row.get("chunk_text") if kind == "chunk" else row.get("summary")
    span = meta.get("window_span") if isinstance(meta, dict) else None
    return kind, row.get("id"), {
        "kind": kind, "provenance": "records",
        "date": str(row.get("report_date") or ""),
        "time": row.get("time_range") or (span if isinstance(span, str) else None),
        "site": row.get("site_name"), "site_slug": row.get("site_slug"),
        "who": row.get("user_name") or row.get("author_name"),
        "title": row.get("title") or row.get("topic_title"),
        "text": (text or "")[:MAX_TEXT],
        "topic_id": row.get("topic_id") if kind == "chunk" else row.get("id"),
        "chunk_id": row.get("id") if kind == "chunk" else None,
        "chunk_type": row.get("chunk_type"), "time_start": row.get("time_start"),
        "source": row.get("source_s3_key"),
    }


def _envelope(name, call_id, rows, meta, ctx, clamped):
    items, used = [], 0
    for row in rows:
        kind, key, item = _item(name, row)
        ident = (kind, str(key))
        item = {"ref": ctx.refs.assign(*ident),
                **{k: v for k, v in item.items() if v is not None}}
        size = len(json.dumps(item, default=str))
        if used + size > MAX_RESULT_CHARS:
            meta["truncated"] = True
            break
        items.append(item)
        used += size
    meta.setdefault("truncated", False)
    if clamped:
        meta["clamped"] = clamped
    return {"tool": name, "id": call_id, "items": items, "meta": meta}


def execute(calls, ctx):
    """Run up to MAX_BATCH tool calls in one rag-search invoke. Never raises."""
    envs, wire, pending = [None] * len(calls), [], {}
    for i, call in enumerate(calls[:MAX_BATCH]):
        args, err = validate_call(call, ctx.today)
        if err:
            envs[i] = {"tool": call.get("name"), "id": str(i), "items": [], "meta": dict(err)}
            continue
        clamped = args.pop("clamped", None)
        pending[str(i)] = (call["name"], clamped, args)
        wire.append({"id": str(i), "name": call["name"], "args": args})
    for i in range(MAX_BATCH, len(calls)):
        envs[i] = {"tool": calls[i].get("name"), "id": str(i), "items": [],
                   "meta": {"error": "bad_args", "field": "calls"}}
    if wire:
        try:
            need = [w for w in wire if w["args"].get("query")]
            for w, vec in zip(need, _embed([w["args"]["query"] for w in need]) if need else []):
                w["query_embedding"] = vec
            resp = ctx.lambda_client.invoke(
                FunctionName=ctx.function_name, InvocationType="RequestResponse",
                Payload=json.dumps({"mode": "tools", "sub": ctx.caller_sub,
                                    "deadline_s": max(0.0, min(ctx.deadline - ctx.clock(), 20)),
                                    "calls": wire}))
            if resp.get("FunctionError"):
                raise RuntimeError("rag-search FunctionError")
            results = {r["id"]: r for r in json.loads(resp["Payload"].read())["results"]}
        except Exception as exc:                               # noqa: BLE001
            logger.warning("ask tools batch failed: %s", type(exc).__name__)
            results = {w["id"]: {"rows": [], "meta": {"error": "upstream"}} for w in wire}
        for cid, (name, clamped, args) in pending.items():
            r = results.get(cid) or {"rows": [], "meta": {"error": "upstream"}}
            rows = r.get("rows") or []
            if name in ("search_records", "latest_activity") and args.get("query") and rows:
                try:
                    rows = _post_search(args["query"], rows, args.get("k", 8))
                except Exception as exc:                       # noqa: BLE001
                    logger.warning("ask tools post-search failed: %s", type(exc).__name__)
                    rows = rows[: args.get("k", 8)]
            envs[int(cid)] = _envelope(name, cid, rows, dict(r.get("meta") or {}), ctx, clamped)
    return envs
```

In `test_refs_unique_across_calls_and_deduped_by_kind_and_id`, `latest_activity` rows without `chunk_text` are topic-kind, so the same `t1` keeps `R1` — that is the intended dedupe.

- [ ] **Step 4: Run tests**

Run: `python -m pytest tests/unit/test_ask_tools_execute.py tests/unit/test_ask_tools_args.py -q`
Expected: PASS.

- [ ] **Step 5: Run the whole unit suite (CI parity)**

First compare the `pip install` line in `.github/workflows/*test*.yml` with the local env (memory: local has onnxruntime, CI does not). Then:
Run: `python -m pytest tests/unit -q -x -p no:cacheprovider`
Expected: all pass (baseline count from `git stash`-free comparison: run the same command on `origin/develop` in a separate worktree if any failure appears, and only call a failure pre-existing if it fails there too).

- [ ] **Step 6: Commit**

```bash
git add src/ask_tools.py tests/unit/test_ask_tools_execute.py
git commit -m "Ask tools: execute - embed, batch invoke, post-search, refs, budget

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>
Claude-Session: https://claude.ai/code/session_014QUZA3uk6P8T7qYJoLRfQe"
```

---

### Task 7: TEST deploy, tool-level eval and latency

**Files:**
- Create: `scripts/ask_tools_probe.py` (read-only probe: invokes `fieldsight-test-rag-search` with `mode: "tools"` using a TEST user sub; prints counts and ms only)
- Create: `docs/superpowers/specs/2026-10-15-ask-retrieval-tools-results.md` (measurements)

**Interfaces:**
- Consumes: deployed TEST stack after the PR merges to develop.

- [ ] **Step 1: Open the PR to develop**, wait for CI green, merge (develop merges are this line's), confirm the TEST deploy run's `headSha` equals the merge commit.

- [ ] **Step 2: Write `scripts/ask_tools_probe.py`** — embeds with DashScope (key exported in the shell only, never written), invokes rag-search `mode: "tools"` 30 times per tool for test2 (`398e3438-70e1-704c-b2cc-b28e33d7ffb1`), prints p50/p95 ms per tool and row counts. No row text printed.

- [ ] **Step 3: Acceptance runs on TEST** (spec §4.4–4.5):
  - 10 day_overview days on Aurora-topic days for test2: compare titles to `topics` rows the org-api `/timeline` Aurora branch returns for the same user-day (via `aws rds-data execute-statement` SELECT on `fieldsight_test`); recall must be 1.0.
  - 10 latest_activity questions with hand-checked dates; ≥ 9/10.
  - The 39-question Ask set through `search_records` with the existing scorer in `fs-eval`; hit rate ≥ 82.1%.
  - Latency p50/p95 per tool against §3.4 targets.

- [ ] **Step 4: Record results** in the results doc; commit; send the companion session (`ASR+LLM优化v3`) the contract path, the merged commit and the measured latencies.

```bash
git add scripts/ask_tools_probe.py docs/superpowers/specs/2026-10-15-ask-retrieval-tools-results.md
git commit -m "Ask tools: TEST probe and acceptance results

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>
Claude-Session: https://claude.ai/code/session_014QUZA3uk6P8T7qYJoLRfQe"
```

---

## Self-review notes

- Spec coverage: §3.1 tools → Tasks 1–6; shared visibility rules → Task 2 (`_ask_tools_where`), Task 3 (candidates), Task 5 (site/person resolution); §3.2 envelope/refs/dedupe/budget → Task 6; §3.3 empty/error semantics → Tasks 5–6; §3.4 batch/deadline/isolation/logging → Tasks 5–6; §3.5 files → all; §4 tests → Tasks 2–6 (unit + PG), §4.4–4.5 → Task 7; phase-2 `list_items` intentionally absent.
- Known soft spots for the implementer to verify against code, called out in-task: `redactions`/`memberships` NOT NULL columns (Tasks 2–3), `search_sql` visibility helper name (Task 4), `_metric`'s zero-kind key (Task 5), `sites.list_sites_by_ids` name (Task 5).
