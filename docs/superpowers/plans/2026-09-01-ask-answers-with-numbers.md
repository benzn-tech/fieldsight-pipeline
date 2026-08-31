# Ask answers with numbers — Implementation Plan (v1)

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** *"How long did I record yesterday"*, *"how many photos"*, *"how many safety issues"* get an answer that is a number — computed by SQL, never produced by a model.

**Architecture:** A second route beside RAG. Rules in `lambda_ask_agent` pick the metric; `lambda_rag_search` gains a `mode` and answers it from Aurora; the numbers come back down the invoke channel that already exists. No new function, no new endpoint, no migration.

**Tech Stack:** Python 3.12 Lambda, `pytest`, psycopg, existing `query_slots` / `scope.visible_scope` / `deletion_mirror`.

**Spec:** `docs/superpowers/specs/2026-08-31-ask-answers-with-numbers-design.md`

## Global Constraints

- **The number is computed. A model may never produce one.** No LLM call on this route at all.
- **Every count carries its denominator** — but only when the denominator is non-zero. "And 0 unclassified" on every answer is noise, and noise is how a caveat stops being read.
- **Three kinds of zero** (spec §7) must stay distinguishable: nothing recorded / nothing visible / **no rows for that day, which is not the same as no recording**.
- **No new table, no migration, no new IAM, no new endpoint.** If a task needs one, it has drifted — stop.
- **A guard that passes must still log.** "It ran and counted nothing" and "it never ran" are otherwise the same observation.
- **After each fix, put the defect back and watch the test go red.**
- `src/metric_slots.py` must stay **pure at import** — no boto3, no psycopg — because `lambda_ask_agent` imports it and the legacy hand-built prod bundle does not carry either.

### Numbers this plan is built on

All from `scripts/measure_metric_route_viability.py` against prod, 2026-09-01. Re-run it rather than trusting this list.

| | measured |
|---|---|
| days-with-topics having `recordings` rows | 33 / 39 = **84.6%** |
| chunk rows → sessions | 2823 → 287 = **9.8×** |
| duration from `duration_s` alone | 281 / 287 = **97.9%** |
| …with the span fallback | 286 / 287 = **99.7%** |
| `findings.domain` NULL | **0 / 189** |
| findings on NULL-author topics | 5 / 189 = **2.6%** |
| topic sources | extraction 242 (139 findings, **0** safety_obs) · report 25 (**0** findings, 4 safety_obs) |

---

## File Structure

| file | responsibility |
|---|---|
| `src/metric_slots.py` **(new)** | Pure. Which metric a question asks for, or None. |
| `src/repositories/recordings.py` **(modify)** | `range_stats` — `day_stats` widened to a date range, with the span fallback and the deletion filter. |
| `src/repositories/findings.py` **(modify)** | `count_by_domain` — findings-first, `safety_observations` fallback, tenant via `site_id`. |
| `src/lambda_rag_search.py` **(modify)** | `mode: "metric"` — ACL, dispatch, and the shape that comes back. |
| `src/lambda_ask_agent.py` **(modify)** | Detect, route, render. No model call. |
| `tests/unit/test_metric_slots.py` **(new)** | The rules, and that a miss returns nothing. |
| `tests/unit/test_metric_route.py` **(new)** | The wiring, the three zeros, and that no LLM is called. |
| `tests/integration/test_metric_queries.py` **(new)** | The SQL, against a real PostgreSQL. |

---

### Task 1: Which metric is being asked for

**Files:**
- Create: `src/metric_slots.py`
- Test: `tests/unit/test_metric_slots.py`

**Interfaces:**
- Produces: `metric_slots.detect(question: str) -> str | None` — one of `"duration"`, `"count_photos"`, `"count_sessions"`, `"count_findings_safety"`, `"count_findings_quality"`, or `None`.

- [ ] **Step 1: Write the failing test**

```python
"""Which metric a question asks for, or nothing.

Rules, not a classifier, for the reason `query_slots` is: a rule that MISSES
returns None and the question falls through to RAG — today's behaviour, so a miss
costs nothing new. A classifier that MISFIRES routes a retrieval question to a
counter and answers a different question confidently.

Not `pytest.importorskip`: this module is ours and pure.
"""
import pytest

import metric_slots as ms


@pytest.mark.parametrize("q,want", [
    ("昨天我录制了多长时间", "duration"),
    ("how long did I record yesterday", "duration"),
    ("我录了多久", "duration"),
    ("上周总共录了多长时间", "duration"),

    ("昨天拍了多少张照片", "count_photos"),
    ("how many photos did I take", "count_photos"),
    ("前天几张照片", "count_photos"),

    ("昨天录了几次", "count_sessions"),
    ("how many recordings yesterday", "count_sessions"),

    ("昨天有多少 safety 的问题", "count_findings_safety"),
    ("how many safety issues yesterday", "count_findings_safety"),
    ("多少安全问题", "count_findings_safety"),

    ("昨天有多少 QA 的问题", "count_findings_quality"),
    ("how many quality issues", "count_findings_quality"),
    ("多少质量问题", "count_findings_quality"),
])
def test_a_metric_question_is_recognised(q, want):
    assert ms.detect(q) == want


@pytest.mark.parametrize("q", [
    "昨天发生了什么",
    "what happened yesterday",
    "混凝土的问题怎么解决",
    "who is responsible for the door schedule",
    "上周的安全问题是什么",          # asks WHAT, not how many — retrieval
    "tell me about the safety issues",
    "",
    None,
])
def test_a_retrieval_question_falls_through(q):
    """None means "not mine" and the caller keeps doing what it does today.
    Every false positive here is a question answered with a count nobody asked
    for, which is worse than the miss."""
    assert ms.detect(q) is None


def test_a_question_asking_for_both_a_count_and_content_falls_through():
    """"How many safety issues and what were they" wants prose. A counter would
    answer half of it and look like it answered all of it."""
    assert ms.detect("昨天有多少安全问题，都是什么") is None
    assert ms.detect("how many safety issues were there and what were they") is None


def test_safety_and_quality_are_not_confused():
    assert ms.detect("多少 QA 问题") == "count_findings_quality"
    assert ms.detect("多少 safety 问题") == "count_findings_safety"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/unit/test_metric_slots.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'metric_slots'`

- [ ] **Step 3: Write minimal implementation**

```python
"""metric_slots.py — which metric a question asks for, or nothing.

Ask retrieves text and answers from it. "How long did I record yesterday" has no
textual answer anywhere: nobody says the duration in the meeting, it is a number
in a column. So it needs a different route, and this module is the switch.

RULES, NOT A CLASSIFIER, and the asymmetry is the whole argument: a rule that
misses returns None and the question falls through to RAG, which is what happens
today, so a miss costs nothing new. A classifier that misfires answers a
different question than the one asked, with a number, confidently.

PURE: no boto3, no psycopg, no network. `lambda_ask_agent` imports it, and the
legacy hand-built prod bundle carries neither.

Spec: docs/superpowers/specs/2026-08-31-ask-answers-with-numbers-design.md
"""
import re

__all__ = ["detect"]

# A quantity interrogative. Required for EVERY metric -- without it "the safety
# issues from yesterday" is a retrieval question wearing a countable noun.
_QUANTITY = re.compile(
    r"\bhow (long|much|many)\b|\btotal (time|number)\b"
    r"|多少|多长|多久|几张|几次|几段|几个",
    re.IGNORECASE,
)

# Asking for the items as well as the number. A counter answers half of that and
# looks like it answered all of it, so the whole question goes to RAG.
_ALSO_WANTS_CONTENT = re.compile(
    r"\b(and )?what (were|was|are|is) (they|them|it)\b|\blist them\b"
    r"|都是什么|分别是|具体是什么|有哪些",
    re.IGNORECASE,
)

_NOUNS = (
    ("count_findings_safety",  re.compile(r"\bsafety\b|安全", re.IGNORECASE)),
    ("count_findings_quality", re.compile(r"\bqa\b|\bquality\b|质量", re.IGNORECASE)),
    ("count_photos",           re.compile(r"\bphotos?\b|\bpictures?\b|照片|图片", re.IGNORECASE)),
    ("duration",               re.compile(r"\blong\b|\btime\b|\bduration\b|时间|多久|时长", re.IGNORECASE)),
    ("count_sessions",         re.compile(r"\brecordings?\b|\bsessions?\b|录音|录了几|录制了几", re.IGNORECASE)),
)


def detect(question):
    """The metric this question asks for, or None.

    None is the common answer and the safe one: it means "not mine", and the
    caller keeps doing exactly what it does today.
    """
    text = question if isinstance(question, str) else ""
    if not text.strip():
        return None
    if not _QUANTITY.search(text):
        return None
    if _ALSO_WANTS_CONTENT.search(text):
        return None
    for name, pattern in _NOUNS:
        if pattern.search(text):
            return name
    return None
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python -m pytest tests/unit/test_metric_slots.py -v`
Expected: PASS

- [ ] **Step 5: Put the gate back**

Comment out the `_QUANTITY` check, re-run, confirm `test_a_retrieval_question_falls_through` goes red on *"上周的安全问题是什么"*. Restore.

- [ ] **Step 6: Commit**

```bash
git add src/metric_slots.py tests/unit/test_metric_slots.py
git commit -m "Which metric a question asks for, and nothing when it asks for prose"
```

---

### Task 2: `range_stats` — sessions and duration over a date range

**Files:**
- Modify: `src/repositories/recordings.py` (beside `day_stats` at :78)
- Test: `tests/integration/test_metric_queries.py`

**Interfaces:**
- Consumes: nothing.
- Produces: `recordings.range_stats(conn, company_id, folders, date_from, date_to, deleted_bases=()) -> {"sessions": int, "duration_s": int, "unmeasured": int, "photos": int}`

- [ ] **Step 1: Write the failing test**

```python
"""The metric queries, against a real PostgreSQL.

A fake connection records SQL without parsing it, and everything that matters
here is something only the database can answer: that the session fold is a fold,
that a date range built on the s3_key segment is the same clock the rest of the
timeline uses, and that a deleted recording stops being counted.

pytestmark = integration: skipped without TEST_DATABASE_URL, run in CI.
"""
import pytest

from repositories import companies, recordings, sites, users

pytestmark = pytest.mark.integration


def _seed(db, folder="Ben_UCPK2"):
    """`upsert_field_only_user` is the helper that takes a folder_name — there is
    no `create_user`, and `upsert_user` keys on a cognito_sub these rows do not
    have. Folder names are globally unique (migration 0012), so each test uses
    its own."""
    co = companies.create_company(db, "Acme-" + folder)
    site = sites.create_site(db, co["id"], "S1")
    u = users.upsert_field_only_user(db, co["id"], folder, folder, "")
    return co["id"], site["id"], u["id"]


def _chunk(db, co, site, uid, folder, date, sid, idx, dur=30, kind="audio"):
    """`insert_pending` is the writer; there is no `recordings.create`. Positional
    through `started_at`, keyword after."""
    recordings.insert_pending(
        db, co, uid, site, kind,
        f"users/{folder}/{kind}/{date}/dev_{date}_10-00-00_sid{sid}_c{idx:04d}.wav",
        f"{sid}-{idx}", f"{date}T10:00:00Z", duration_s=dur)


def test_a_session_is_one_session_however_many_chunks_it_arrived_in(db):
    """The whole reason this is not COUNT(*). Measured on prod: 2823 rows are
    287 sessions, a fold of 9.8x. Counting rows tells a person they made nearly
    ten times the recordings they made."""
    co, site, uid = _seed(db, "fold")
    for i in range(21):
        _chunk(db, co, site, uid, "fold", "2026-08-27", "a" * 32, i)

    out = recordings.range_stats(db, co, ["fold"], "2026-08-27", "2026-08-27")

    assert out["sessions"] == 1
    assert out["duration_s"] == 21 * 30


def test_the_range_is_matched_on_the_key_segment_not_started_at(db):
    """`started_at` is UTC and "yesterday" is the device's local day, so an
    evening recording moves to the next day (BUG-37's family). The key segment is
    the clock the topics and the timeline are on."""
    co, site, uid = _seed(db, "clock")
    recordings.insert_pending(
        db, co, uid, site, "audio",
        "users/clock/audio/2026-08-27/dev_x_sid" + "b" * 32 + "_c0001.wav",
        "c-1", "2026-08-27T23:30:00Z",   # 11:30am the NEXT day in UTC+12
        duration_s=60)

    assert recordings.range_stats(db, co, ["clock"], "2026-08-27", "2026-08-27")["sessions"] == 1
    assert recordings.range_stats(db, co, ["clock"], "2026-08-28", "2026-08-28")["sessions"] == 0


def test_a_session_with_no_duration_is_counted_and_named(db):
    """Measured: 1 of 287 sessions on prod can produce no duration at all.
    Dropping it from the total silently understates the answer."""
    co, site, uid = _seed(db, "nodur")
    recordings.insert_pending(
        db, co, uid, site, "audio",
        "users/nodur/audio/2026-08-27/dev_sid" + "c" * 32 + "_c0001.wav",
        "n-1", "2026-08-27T10:00:00Z", ended_at=None, duration_s=None)

    out = recordings.range_stats(db, co, ["nodur"], "2026-08-27", "2026-08-27")
    assert out["sessions"] == 1
    assert out["duration_s"] == 0
    assert out["unmeasured"] == 1


def test_the_span_is_the_fallback_when_duration_s_is_absent(db):
    """`day_stats` sums `duration_s` only, which is 97.9% of sessions on prod.
    The span fallback lives in `duration_for_media` and is what takes it to
    99.7% — so `range_stats` has to carry it, or 5 sessions in 287 contribute
    zero and the total is quietly short."""
    co, site, uid = _seed(db, "span")
    recordings.insert_pending(
        db, co, uid, site, "audio",
        "users/span/audio/2026-08-27/dev_sid" + "d" * 32 + "_c0001.wav",
        "s-1", "2026-08-27T10:00:00Z",
        ended_at="2026-08-27T10:02:00Z", duration_s=None)

    out = recordings.range_stats(db, co, ["span"], "2026-08-27", "2026-08-27")
    assert out["duration_s"] == 120
    assert out["unmeasured"] == 0


def test_photos_are_counted_and_do_not_join_the_session_fold(db):
    """Photos are rows in the same table with `kind='photo'`. They are their own
    number and must not inflate the recording count — on prod they are 304 of
    the 3127 rows, and mixing them in is how the fold ratio was first
    miscomputed."""
    co, site, uid = _seed(db, "pix")
    _chunk(db, co, site, uid, "pix", "2026-08-27", "e" * 32, 1)
    for i in range(3):
        recordings.insert_pending(
            db, co, uid, site, "photo",
            f"users/pix/pictures/2026-08-27/IMG_{i}.jpg",
            f"p-{i}", "2026-08-27T10:00:00Z")

    out = recordings.range_stats(db, co, ["pix"], "2026-08-27", "2026-08-27")
    assert out["sessions"] == 1
    assert out["photos"] == 3


def test_a_deleted_session_stops_being_counted(db):
    """The assertion that proves the key-space translation rather than the
    intention. Tombstones are in the `extractions/` space and `recordings` has
    no `source_s3_key` at all, so a predicate written the obvious way matches
    nothing and the count silently includes deleted recordings — which is a way
    to observe what was deleted."""
    co, site, uid = _seed(db, "del")
    _chunk(db, co, site, uid, "del", "2026-08-27", "f" * 32, 1)
    _chunk(db, co, site, uid, "del", "2026-08-27", "0" * 32, 1)

    before = recordings.range_stats(db, co, ["del"], "2026-08-27", "2026-08-27")
    after = recordings.range_stats(db, co, ["del"], "2026-08-27", "2026-08-27",
                                   deleted_bases={"sid" + "f" * 32})

    assert before["sessions"] == 2
    assert after["sessions"] == 1, "a deleted recording is still being counted"


def test_another_companys_recordings_are_never_counted(db):
    co_a, site_a, uid_a = _seed(db, "tenant_a")
    co_b, site_b, uid_b = _seed(db, "tenant_b")
    _chunk(db, co_a, site_a, uid_a, "tenant_a", "2026-08-27", "1" * 32, 1)
    _chunk(db, co_b, site_b, uid_b, "tenant_b", "2026-08-27", "2" * 32, 1)

    out = recordings.range_stats(db, co_a, ["tenant_a", "tenant_b"],
                                 "2026-08-27", "2026-08-27")
    assert out["sessions"] == 1
```

- [ ] **Step 2: Run to verify it fails**

Run: `TEST_DATABASE_URL=... python -m pytest tests/integration/test_metric_queries.py -v`
Expected: FAIL — `module 'repositories.recordings' has no attribute 'range_stats'`

If `TEST_DATABASE_URL` is unset the file **skips**, which is not a pass. Set it or read the CI run.

- [ ] **Step 3: Write minimal implementation**

Add beside `day_stats` in `src/repositories/recordings.py`:

```python
def range_stats(conn, company_id, folders, date_from, date_to, deleted_bases=()) -> dict:
    """Sessions, seconds and photos for a set of folders over a date RANGE.

    `day_stats`'s two rules, widened, and one it does not have:

    1. **Sessions, not rows.** Folded on the sid parsed from the key, with the
       key itself as the fold value when there is no sid. Measured on prod:
       2823 rows are 287 sessions.
    2. **The key segment, not `started_at`.** That column is UTC and the range
       here is the caller's local calendar day (`query_slots.time_range`), the
       same clock the topics and the s3_key are on. Filtering by UTC moves an
       evening recording to the next day -- BUG-37's family.
    3. **The span fallback, which `day_stats` does not have.** It sums
       `duration_s` only; `duration_for_media` is where `ended_at - started_at`
       lives. Without it, 5 sessions in 287 contribute zero and the total is
       quietly short. `unmeasured` counts the sessions that could produce
       neither, so a short total is visible rather than assumed.

    `deleted_bases` are session bases (either spelling) the caller has already
    resolved from the deletion mirror. They are excluded. The mirror is keyed on
    (folder, date, sessionBase) and `recordings` has no `source_s3_key`, so the
    tombstone predicate used elsewhere cannot be applied here -- the translation
    happens in the caller and the exclusion happens here.
    """
    bases = {b for b in (deleted_bases or ()) if b}
    bases |= {b[3:] for b in list(bases) if b.startswith("sid")}
    bases |= {"sid" + b for b in list(bases) if not b.startswith("sid")}

    row = conn.cursor(row_factory=dict_row).execute(
        "WITH matched AS ("
        "  SELECT s3_key, kind, duration_s, started_at, ended_at,"
        "    COALESCE(substring(s3_key from '_(sid[0-9a-f]{32})_c[0-9]+\\.'), s3_key) AS fold,"
        "    substring(s3_key from '/([0-9]{4}-[0-9]{2}-[0-9]{2})/') AS keydate"
        "  FROM recordings"
        "  WHERE company_id = %(company)s"
        "    AND substring(s3_key from 'users/([^/]+)/') = ANY(%(folders)s)"
        "), windowed AS ("
        "  SELECT * FROM matched"
        "  WHERE keydate BETWEEN %(from)s AND %(to)s"
        "    AND NOT (fold = ANY(%(deleted)s))"
        "), sess AS ("
        "  SELECT fold,"
        "    COALESCE(SUM(duration_s), 0) AS dur,"
        "    MAX(EXTRACT(EPOCH FROM (ended_at - started_at))) AS span"
        "  FROM windowed WHERE kind IN ('audio','video') GROUP BY fold"
        ")"
        "SELECT"
        "  (SELECT count(*) FROM sess) AS sessions,"
        "  (SELECT COALESCE(SUM(CASE WHEN dur > 0 THEN dur"
        "                            WHEN span > 0 THEN span ELSE 0 END), 0) FROM sess) AS duration_s,"
        "  (SELECT count(*) FROM sess WHERE NOT (dur > 0) AND NOT (span > 0)) AS unmeasured,"
        "  (SELECT count(*) FROM windowed WHERE kind = 'photo') AS photos",
        {"company": company_id, "folders": list(folders),
         "from": date_from, "to": date_to, "deleted": list(bases)},
    ).fetchone()
    return {"sessions": int(row["sessions"] or 0),
            "duration_s": int(row["duration_s"] or 0),
            "unmeasured": int(row["unmeasured"] or 0),
            "photos": int(row["photos"] or 0)}
```

- [ ] **Step 4: Run to verify it passes**

Run: `TEST_DATABASE_URL=... python -m pytest tests/integration/test_metric_queries.py -v`
Expected: PASS (7 tests)

- [ ] **Step 5: Put the fold back**

Replace the `GROUP BY fold` with `GROUP BY s3_key`, re-run, confirm the 21-chunk test reports 21. Restore.

- [ ] **Step 6: Commit**

```bash
git add src/repositories/recordings.py tests/integration/test_metric_queries.py
git commit -m "Sessions and seconds over a range, folded, on the clock the timeline uses"
```

---

### Task 3: `count_by_domain` — findings first, `safety_observations` second

**Files:**
- Modify: `src/repositories/findings.py`
- Test: `tests/integration/test_metric_queries.py`

**Interfaces:**
- Produces: `findings.count_by_domain(conn, company_id, domain, date_from, date_to, site_ids=None, author_ids=None) -> {"count": int, "unlabelled": int, "null_author": int, "from_fallback": int}`

- [ ] **Step 1: Write the failing test**

```python
# append to tests/integration/test_metric_queries.py

from repositories import findings, topics


def test_a_report_sourced_topic_is_counted_through_the_fallback(db):
    """Measured on prod: extraction topics carry findings and ZERO
    safety_observations; report topics carry safety_observations and ZERO
    findings. The two paths are disjoint, so a findings-only count does not
    under-report by a margin — it reports nothing at all for 25 of 267 topics,
    four of which genuinely carry safety items."""
    co, site, uid = _seed(db, "fallback")
    t_live = topics.upsert_topic(db, site, "2026-08-27", "Live",
                                 user_id=uid, source_s3_key="extractions/f/2026-08-27/sidx.json")
    findings.insert_findings(db, t_live["id"], site,
                             [{"observation": "loose board", "domain": "safety"}])
    t_rep = topics.upsert_topic(db, site, "2026-08-27", "Report",
                                user_id=uid, source_s3_key="reports/2026-08-27/f/daily_report.json",
                                safety=[{"observation": "no handrail"}])

    out = findings.count_by_domain(db, co, "safety", "2026-08-27", "2026-08-27")

    assert out["count"] == 2, "the report-path topic was not counted"
    assert out["from_fallback"] == 1


def test_a_topic_with_findings_does_not_double_count_its_legacy_rows(db):
    """The shipped read path falls back ONLY when a topic has zero safety-domain
    findings (`topics.py`). A topic carrying both must not count twice."""
    co, site, uid = _seed(db, "both")
    t = topics.upsert_topic(db, site, "2026-08-27", "Both", user_id=uid,
                            source_s3_key="extractions/b/2026-08-27/sidy.json",
                            safety=[{"observation": "legacy row"}])
    findings.insert_findings(db, t["id"], site,
                             [{"observation": "current row", "domain": "safety"}])

    out = findings.count_by_domain(db, co, "safety", "2026-08-27", "2026-08-27")
    assert out["count"] == 1


def test_the_tenant_comes_through_site_id_not_through_users(db):
    """`findings.site_id` is NOT NULL and `sites.company_id` is NOT NULL — one
    hop, and it loses nothing (0/189 NULL on prod). Reaching the tenant through
    `topics.user_id -> users` instead drops every NULL-author row from EVERY
    caller's count, including an ALL-scoped admin."""
    co, site, uid = _seed(db, "tenant")
    t = topics.upsert_topic(db, site, "2026-08-27", "Unattributed",
                            user_id=None,
                            source_s3_key="extractions/t/2026-08-27/sidz.json")
    findings.insert_findings(db, t["id"], site,
                             [{"observation": "x", "domain": "safety"}])

    out = findings.count_by_domain(db, co, "safety", "2026-08-27", "2026-08-27")
    assert out["count"] == 1, "a NULL-author finding vanished from an unscoped count"
    assert out["null_author"] == 1, "…and it was not named"


def test_a_self_scoped_caller_is_told_what_they_cannot_see(db):
    """2.6% of findings on prod hang off NULL-author topics. A per-author scope
    cannot see them by construction; it must say so rather than quietly answer
    a smaller number."""
    co, site, uid = _seed(db, "selfscope")
    mine = topics.upsert_topic(db, site, "2026-08-27", "Mine", user_id=uid,
                               source_s3_key="extractions/s/2026-08-27/sid1.json")
    findings.insert_findings(db, mine["id"], site, [{"observation": "a", "domain": "safety"}])
    orphan = topics.upsert_topic(db, site, "2026-08-27", "Orphan", user_id=None,
                                 source_s3_key="extractions/s/2026-08-27/sid2.json")
    findings.insert_findings(db, orphan["id"], site, [{"observation": "b", "domain": "safety"}])

    out = findings.count_by_domain(db, co, "safety", "2026-08-27", "2026-08-27",
                                   author_ids=[uid])
    assert out["count"] == 1
    assert out["null_author"] == 1


def test_a_deleted_topics_findings_are_not_counted(db):
    co, site, uid = _seed(db, "deltopic")
    t = topics.upsert_topic(db, site, "2026-08-27", "Gone", user_id=uid,
                            source_s3_key="extractions/d/2026-08-27/sid9.json")
    findings.insert_findings(db, t["id"], site, [{"observation": "x", "domain": "safety"}])
    assert findings.count_by_domain(db, co, "safety", "2026-08-27", "2026-08-27")["count"] == 1

    db.execute("INSERT INTO redactions (target_type, target_id, scope) "
               "VALUES ('topic', %s, 'deleted')", (t["id"],))

    assert findings.count_by_domain(db, co, "safety", "2026-08-27", "2026-08-27")["count"] == 0
```

- [ ] **Step 2: Run to verify it fails**

Run: `TEST_DATABASE_URL=... python -m pytest tests/integration/test_metric_queries.py -k domain -v`
Expected: FAIL — `no attribute 'count_by_domain'`

- [ ] **Step 3: Write minimal implementation**

Add to `src/repositories/findings.py`:

```python
def count_by_domain(conn, company_id, domain, date_from, date_to,
                    site_ids=None, author_ids=None) -> dict:
    """How many safety- or quality-domain items in a date range.

    FINDINGS FIRST, `safety_observations` SECOND, per topic -- the semantics the
    shipped dashboard read already uses (`repositories/topics.py`), not a third
    opinion about which table is true. Measured on prod, the two paths are
    DISJOINT: live-extraction topics carry findings and no safety_observations;
    nightly-report topics carry safety_observations and no findings. A
    findings-only count reports nothing at all for the second kind.

    (Nightly report topics exist only for zero-extraction days on prod, because
    `AUTHORITY_FLIP=true` makes a day WITH extraction topics defer. The supersede
    that would delete the day's findings does not fire.)

    TENANT COMES THROUGH `site_id`, one hop, NOT NULL on both sides -- never
    through `topics.user_id -> users`, which is nullable and would drop every
    NULL-author row from every caller's count (5 of 189 on prod).

    `unlabelled` is findings whose `domain` is NULL. Measured 0/189 on prod, so
    it is almost always zero; the caller does not print a zero.
    """
    return conn.cursor(row_factory=dict_row).execute(
        "WITH scoped AS ("
        "  SELECT t.id AS topic_id, t.user_id"
        "  FROM topics t JOIN sites s ON s.id = t.site_id"
        "  WHERE s.company_id = %(company)s"
        "    AND t.report_date BETWEEN %(from)s AND %(to)s"
        "    AND (%(site_ids)s::uuid[] IS NULL OR t.site_id = ANY(%(site_ids)s::uuid[]))"
        "    AND " + visible_topics_predicate("t") +
        "), per_topic AS ("
        "  SELECT sc.topic_id, sc.user_id,"
        "    (SELECT count(*) FROM findings f"
        "      WHERE f.topic_id = sc.topic_id AND f.domain = %(domain)s) AS n_findings,"
        "    (SELECT count(*) FROM findings f"
        "      WHERE f.topic_id = sc.topic_id AND f.domain IS NULL) AS n_unlabelled,"
        "    (SELECT count(*) FROM safety_observations so"
        "      WHERE so.topic_id = sc.topic_id) AS n_legacy"
        "  FROM scoped sc"
        "), counted AS ("
        "  SELECT user_id,"
        "    CASE WHEN n_findings > 0 THEN n_findings"
        "         WHEN %(domain)s = 'safety' THEN n_legacy ELSE 0 END AS n,"
        "    CASE WHEN n_findings = 0 AND %(domain)s = 'safety' THEN n_legacy ELSE 0 END AS n_fb,"
        "    n_unlabelled"
        "  FROM per_topic"
        ")"
        "SELECT"
        "  COALESCE(SUM(n) FILTER (WHERE %(authors)s::uuid[] IS NULL"
        "                          OR user_id = ANY(%(authors)s::uuid[])), 0) AS count,"
        "  COALESCE(SUM(n_unlabelled), 0) AS unlabelled,"
        "  COALESCE(SUM(n) FILTER (WHERE user_id IS NULL), 0) AS null_author,"
        "  COALESCE(SUM(n_fb), 0) AS from_fallback"
        " FROM counted",
        {"company": company_id, "domain": domain, "from": date_from, "to": date_to,
         "site_ids": list(site_ids) if site_ids is not None else None,
         "authors": list(author_ids) if author_ids is not None else None},
    ).fetchone()
```

Add `from deleted_predicates import visible_topics_predicate` at the top of the file if it is not already imported.

- [ ] **Step 4: Run to verify it passes**

Run: `TEST_DATABASE_URL=... python -m pytest tests/integration/test_metric_queries.py -v`
Expected: PASS (12 tests)

- [ ] **Step 5: Put both halves back**

Remove the `n_legacy` fallback arm → the report-path test goes red. Restore. Then change the tenant join to go through `topics.user_id → users` → the NULL-author test goes red. Restore.

- [ ] **Step 6: Commit**

```bash
git add src/repositories/findings.py tests/integration/test_metric_queries.py
git commit -m "Count safety the way the dashboard reads it, and reach the tenant the way findings allows"
```

---

### Task 4: `mode: "metric"` in rag-search

**Files:**
- Modify: `src/lambda_rag_search.py`
- Test: `tests/unit/test_metric_route.py`

**Interfaces:**
- Consumes: `metric_slots` (name only, passed in), `recordings.range_stats`, `findings.count_by_domain`, `scope.visible_scope`, `deletion_mirror`.
- Produces: on `{"mode": "metric", "metric": …, "sub": …, "date_from": …, "date_to": …}`, returns `{"metric", "value", "unit", "from", "to", "scope", "n", "notes": {...}}`.

- [ ] **Step 1: Write the failing test**

```python
"""The metric mode: ACL, dispatch, and the three zeros.

What is asserted here is the ROUTING and the shape. The SQL is exercised against
a real database in tests/integration/test_metric_queries.py — a fake connection
records SQL without parsing it, and asserting on the string it recorded would
prove only that a string was passed.
"""
import pytest

rag = pytest.importorskip("lambda_rag_search", reason="requires psycopg (installed in CI)")


class FakeConn:
    def __enter__(self): return self
    def __exit__(self, *a): return False


CALLER = {"id": "u-1", "cognito_sub": "sub-1", "company_id": "c-1",
          "email": "a@x.nz", "first_name": "A", "last_name": "B",
          "avatar_s3_key": None, "global_role": "pm", "created_at": "2026-07-04"}


@pytest.fixture
def wired(monkeypatch):
    monkeypatch.setattr(rag, "get_cached_connection", lambda *a, **k: FakeConn())
    monkeypatch.setattr(rag.users, "get_user_by_sub", lambda conn, sub: dict(CALLER))
    monkeypatch.setattr(rag.scope, "visible_scope",
                        lambda conn, caller: {"site_ids": {"s-1"}, "author_ids": None})
    monkeypatch.setattr(rag.recordings, "range_stats",
                        lambda *a, **k: {"sessions": 3, "duration_s": 4620,
                                         "unmeasured": 0, "photos": 7})
    monkeypatch.setattr(rag.deletion_mirror, "deleted_sessions", lambda *a, **k: set())
    return monkeypatch


def ev(**kw):
    e = {"mode": "metric", "sub": "sub-1", "metric": "duration",
         "date_from": "2026-08-30", "date_to": "2026-08-30", "folders": ["Ben"]}
    e.update(kw)
    return e


def test_duration_comes_back_as_a_number_and_a_unit(wired):
    out = rag.lambda_handler(ev(), None)
    assert out["metric"] == "duration"
    assert out["value"] == 4620
    assert out["unit"] == "seconds"
    assert out["from"] == "2026-08-30"


def test_no_model_is_ever_called(wired, monkeypatch):
    """The rule the whole spec exists to enforce. rag-search has never had an
    LLM and must not gain one here — a model asked how long you recorded says
    'about two hours' with the fluency of a fact."""
    import sys
    monkeypatch.setitem(sys.modules, "llm_utils", None)
    monkeypatch.setitem(sys.modules, "dashscope_utils", None)
    out = rag.lambda_handler(ev(), None)
    assert out["value"] == 4620


def test_photos_are_a_different_metric_from_sessions(wired):
    assert rag.lambda_handler(ev(metric="count_photos"), None)["value"] == 7
    assert rag.lambda_handler(ev(metric="count_sessions"), None)["value"] == 3


def test_an_unknown_metric_is_refused_rather_than_guessed(wired):
    out = rag.lambda_handler(ev(metric="astrology"), None)
    assert out.get("error")
    assert "value" not in out


# ---- the three zeros ------------------------------------------------------

def test_zero_because_nothing_was_recorded(wired, monkeypatch):
    monkeypatch.setattr(rag.recordings, "range_stats",
                        lambda *a, **k: {"sessions": 0, "duration_s": 0,
                                         "unmeasured": 0, "photos": 0})
    out = rag.lambda_handler(ev(), None)
    assert out["value"] == 0
    assert out["notes"]["zero_kind"] == "nothing_recorded"


def test_zero_because_the_caller_can_see_nothing(wired, monkeypatch):
    monkeypatch.setattr(rag.scope, "visible_scope",
                        lambda conn, caller: {"site_ids": set(), "author_ids": None})
    out = rag.lambda_handler(ev(), None)
    assert out["notes"]["zero_kind"] == "nothing_visible"
    assert "value" not in out or out["value"] == 0


def test_zero_because_that_day_has_no_rows_at_all(wired, monkeypatch):
    """The third zero, and the one the shipped KPI deliberately refuses to
    report as zero: the RealPTT path never registers rows, days before migration
    0009 have none, lake-fed environments have none. Measured: 15.4% of days
    with topics. Reporting it as 'you recorded nothing' is the misleading zero
    `lambda_org_api` was changed to stop producing."""
    monkeypatch.setattr(rag.recordings, "range_stats",
                        lambda *a, **k: {"sessions": 0, "duration_s": 0,
                                         "unmeasured": 0, "photos": 0})
    monkeypatch.setattr(rag, "_day_has_topics", lambda *a, **k: True)
    out = rag.lambda_handler(ev(), None)
    assert out["notes"]["zero_kind"] == "no_rows_for_that_day"


def test_deleted_sessions_are_excluded_and_the_translation_is_the_callers(wired, monkeypatch):
    seen = {}
    monkeypatch.setattr(rag.deletion_mirror, "deleted_sessions",
                        lambda *a, **k: {"sid" + "a" * 32})
    monkeypatch.setattr(rag.recordings, "range_stats",
                        lambda *a, **k: seen.update(k) or {"sessions": 1, "duration_s": 10,
                                                           "unmeasured": 0, "photos": 0})
    rag.lambda_handler(ev(), None)
    assert "sid" + "a" * 32 in (seen.get("deleted_bases") or set())
```

- [ ] **Step 2: Run to verify it fails**

Run: `python -m pytest tests/unit/test_metric_route.py -v`
Expected: FAIL — the handler ignores `mode` and takes the chunk-search path.

- [ ] **Step 3: Write minimal implementation**

In `src/lambda_rag_search.py`, at the top of `_search`, before the embedding guard:

```python
    if event.get("mode") == "metric":
        return _metric(event)
```

and add `_metric(event)` implementing: resolve caller → `visible_scope` → if no sites, return `zero_kind: "nothing_visible"` → resolve `deleted_bases` from `deletion_mirror` per (folder, date) in the range → dispatch on `metric` to `recordings.range_stats` or `findings.count_by_domain` → build `notes` including `zero_kind`, `unmeasured`, `unlabelled`, `null_author`, `from_fallback`, dropping any that are zero.

`_day_has_topics(conn, …)` distinguishes the third zero: a day with topics but no `recordings` rows is `no_rows_for_that_day`, not `nothing_recorded`.

- [ ] **Step 4: Run to verify it passes**

Run: `python -m pytest tests/unit/test_metric_route.py -v`
Expected: PASS

- [ ] **Step 5: Put the third zero back**

Make `_day_has_topics` always return False, re-run, confirm `test_zero_because_that_day_has_no_rows_at_all` goes red. Restore.

- [ ] **Step 6: Commit**

```bash
git add src/lambda_rag_search.py tests/unit/test_metric_route.py
git commit -m "A metric mode that answers with a number and three distinguishable zeros"
```

---

### Task 5: Ask routes to it, and renders it without a model

**Files:**
- Modify: `src/lambda_ask_agent.py`
- Test: `tests/unit/test_metric_route.py`

**Interfaces:**
- Consumes: `metric_slots.detect`, `query_slots.time_range`, the metric mode from Task 4.
- Produces: the Ask response for a metric question — `{answer, metric, value, unit, basis, grounded: True}` with **no `citations`** and no model call.

- [ ] **Step 1: Write the failing test**

```python
# append to tests/unit/test_metric_route.py

import json
import io as _io


def test_a_metric_question_never_reaches_the_model(monkeypatch):
    """The routing decision, and the rule. If this regresses, Ask answers 'how
    long did I record' with a summary of the day's topics — which is what it did
    before this route existed."""
    import lambda_ask_agent as laa
    import llm_utils

    called = {"llm": 0}
    monkeypatch.setattr(llm_utils, "call_llm",
                        lambda *a, **k: called.__setitem__("llm", called["llm"] + 1) or ("x", None))

    class C:
        def invoke(self, FunctionName, InvocationType, Payload):
            assert json.loads(Payload)["mode"] == "metric"
            body = {"metric": "duration", "value": 4620, "unit": "seconds",
                    "from": "2026-08-30", "to": "2026-08-30", "n": 3, "notes": {}}
            return {"Payload": _io.BytesIO(json.dumps(body).encode())}

    monkeypatch.setattr(laa, "_get_lambda_client", lambda: C())

    resp = laa.lambda_handler({"question": "昨天我录制了多长时间",
                               "caller_sub": "sub-1", "tz": "Pacific/Auckland"}, None)
    out = json.loads(resp["body"])

    assert called["llm"] == 0, "a model was asked to produce a number"
    assert "1 hour 17 minutes" in out["answer"]
    assert out["value"] == 4620


def test_a_retrieval_question_still_goes_to_rag(monkeypatch):
    """The fall-through, which is the safe direction and the reason the detector
    is rules."""
    import lambda_ask_agent as laa

    seen = {}

    class C:
        def invoke(self, FunctionName, InvocationType, Payload):
            seen["mode"] = json.loads(Payload).get("mode")
            return {"Payload": _io.BytesIO(json.dumps({"chunks": []}).encode())}

    monkeypatch.setattr(laa, "_get_lambda_client", lambda: C())
    import dashscope_utils
    monkeypatch.setattr(dashscope_utils, "embed", lambda t, dim=None: [[0.1] * 1024])

    laa.lambda_handler({"question": "昨天发生了什么", "caller_sub": "sub-1"}, None)
    assert seen.get("mode") != "metric"


def test_the_denominator_is_printed_only_when_it_is_not_zero(monkeypatch):
    """`unlabelled` is 0 on 189 of 189 findings on prod. "And 0 unclassified" on
    every answer is noise, and noise is how a caveat stops being read."""
    import lambda_ask_agent as laa

    def reply(notes):
        class C:
            def invoke(self, FunctionName, InvocationType, Payload):
                body = {"metric": "count_findings_safety", "value": 3, "unit": "items",
                        "from": "2026-08-30", "to": "2026-08-30", "n": 3, "notes": notes}
                return {"Payload": _io.BytesIO(json.dumps(body).encode())}
        return C()

    monkeypatch.setattr(laa, "_get_lambda_client", lambda: reply({}))
    quiet = json.loads(laa.lambda_handler(
        {"question": "昨天有多少安全问题", "caller_sub": "s", "tz": "Pacific/Auckland"},
        None)["body"])["answer"]
    assert "unclassified" not in quiet

    monkeypatch.setattr(laa, "_get_lambda_client", lambda: reply({"unlabelled": 7}))
    loud = json.loads(laa.lambda_handler(
        {"question": "昨天有多少安全问题", "caller_sub": "s", "tz": "Pacific/Auckland"},
        None)["body"])["answer"]
    assert "7" in loud and "unclassified" in loud


def test_the_third_zero_does_not_say_you_recorded_nothing(monkeypatch):
    import lambda_ask_agent as laa

    class C:
        def invoke(self, FunctionName, InvocationType, Payload):
            body = {"metric": "duration", "value": 0, "unit": "seconds",
                    "from": "2026-08-30", "to": "2026-08-30", "n": 0,
                    "notes": {"zero_kind": "no_rows_for_that_day"}}
            return {"Payload": _io.BytesIO(json.dumps(body).encode())}

    monkeypatch.setattr(laa, "_get_lambda_client", lambda: C())
    out = json.loads(laa.lambda_handler(
        {"question": "昨天录了多久", "caller_sub": "s", "tz": "Pacific/Auckland"},
        None)["body"])["answer"]

    assert "no recordings" not in out.lower()
    assert "no recording data" in out.lower() or "not registered" in out.lower()
```

- [ ] **Step 2: Run to verify it fails**

Run: `python -m pytest tests/unit/test_metric_route.py -v`
Expected: FAIL — the metric question takes the RAG path.

- [ ] **Step 3: Write minimal implementation**

In `_rag_answer`, after `today` / `date_from` / `date_to` are resolved and **before** the embedding call:

```python
    import metric_slots
    metric = metric_slots.detect(question)
    if metric:
        return _metric_answer(body, metric, date_from, date_to, today)
```

`_metric_answer` invokes rag-search with `{"mode": "metric", "metric", "sub", "date_from", "date_to"}` and renders from a template — `4620 → "1 hour 17 minutes"`, `7 → "7 photos"` — appending only the non-zero notes. It never calls `llm_utils`.

- [ ] **Step 4: Run to verify it passes**

Run: `python -m pytest tests/unit -q`
Expected: no new failures.

- [ ] **Step 5: Put the routing back**

Force `metric = None`, re-run, confirm `test_a_metric_question_never_reaches_the_model` goes red. Restore.

- [ ] **Step 6: Commit**

```bash
git add src/lambda_ask_agent.py tests/unit/test_metric_route.py
git commit -m "Ask answers a metric question with a number it did not ask a model for"
```

---

### Task 6: On TEST, against a real question

- [ ] **Step 1: Deploy and verify the package, not the run**

Merge to `develop`. Then confirm the deployed zips carry it — a green deploy is not evidence:

```bash
aws lambda get-function --function-name fieldsight-test-rag-search --query 'Code.Location' --output text
# download; python -c "import zipfile; z=zipfile.ZipFile('f.zip'); print('metric' in z.read('lambda_rag_search.py').decode())"
```

- [ ] **Step 2: Ask the real question**

```bash
AWS_PROFILE=fieldsight-deployer aws lambda invoke \
  --function-name fieldsight-test-ask-agent --region ap-southeast-2 \
  --cli-binary-format raw-in-base64-out \
  --payload '{"question":"how long did I record yesterday","caller_sub":"<sub>","tz":"Pacific/Auckland"}' out.json
```

Expected: a duration, or one of the three zeros **named**. Not a summary of the day's topics.

- [ ] **Step 3: Cross-check the number against the measurement**

Run `scripts/measure_metric_route_viability.py` and confirm the route's answer for the same window agrees with the query. **A number that disagrees with its own measurement script is the failure this whole plan is arranged to prevent.**

- [ ] **Step 4: Break it on purpose**

Locally revert the session fold, re-run the integration test, confirm the 21-chunk case reports 21. Restore.

- [ ] **Step 5: Commit anything the run exposed**

---

## Out of scope, and why

- **"At the XX meeting"** — needs a meeting-name → session-time-span resolver that does not exist. v1 answers the whole day **and says so** (spec §9).
- **Comparisons** — *"how much longer than last week"* is arithmetic over two windows, not a count.
- **`observations`** (human-filed) — a neighbouring question, and merging it silently is the error the spec's §3 is about.
- **Widening "我" to the team** — the ACL supports it; whether it ships is a product call (spec §10.1).
- **Any UI.** The API gains a route; presenting it is separate.
