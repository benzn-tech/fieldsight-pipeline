# Unified Semantic Topic Search (Search list + Ask synthesis) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make the mid-panel Search box return relevance-ranked topics via the existing semantic engine (embed + pgvector), grouped by date, and make "Ask the Agent" a zero-result fallback — reusing the RAG retrieval for both, differing only in whether Claude synthesizes an answer.

**Architecture:** One retrieval path, two heads. Search (`mode=search`) embeds the query → invokes rag-search (ACL by org sites, optional date range) → aggregates chunks into distinct topic rows ranked by best cosine distance → returns a list, **no Claude call**. Ask (`mode=ask`, unchanged) feeds the same retrieved chunks to Claude for a cited answer. Backend spans 3 pipeline lambdas (rag-search, ask-agent, report-api) that already share a SAM code bundle and gateway (`/api/{proxy+}` on `wdsgobb7b0`); no new APIGW routes or IAM. Frontend replaces the broken client topic fan-out with an async debounced call to `/api/search`.

**Tech Stack:** Python 3.12 lambdas + psycopg/pgvector (Aurora), DashScope v4 embeddings, SAM; browser React (`React.createElement`, no build) for the UI.

## Global Constraints

- AWS account **509194952652**, region **ap-southeast-2** only. Never touch company account 164088480050.
- Deploys are **serial** (concurrent CloudFormation on the same stack collides). Prefix SAM/CFN commands with `export AWS_CLI_FILE_ENCODING=UTF-8 PYTHONUTF8=1` (BUG-35). AWS calls use `AWS_PROFILE=fieldsight-deployer`.
- `mode=search` **must NOT call Claude** — assert zero `call_claude` invocations in tests.
- Search ACL is **rag-search's existing org-site ACL** (`get_user_by_sub` → accessible sites). Do NOT add a second ACL; do NOT read `user_mapping`.
- Date filter params are **optional**; absent → `None` → no filtering (Ask path behavior must stay byte-identical when no date is supplied).
- `q` shorter than **2 chars** → return `{"results": [], "count": 0}` without invoking the backend.
- A result's `route` must match the existing client topic route EXACTLY: `/timeline?date=<d>&user=<folder>&topic=<String(topic_id)>`, every value `encodeURIComponent`/`quote(safe="")`-encoded; omit `&user` when folder is unknown, omit `&topic` when the chunk has no topic.
- **Route folder is parsed from the chunk's `source_s3_key`** (`reports/<date>/<folder>/daily_report.json` → folder is the 3rd path segment; ingest stamps every chunk's `source_s3_key` = the report key). Do NOT rely on a `users.folder_name` join — that column is only populated by the seed (not run), so it would be NULL. Seed-independent by design.
- Windows/Git: single-line `Edit` anchors (mixed CRLF/LF); never `git add -A` on pipeline develop — stage exact paths.
- Pipeline work on branch `feat/topic-search-api` off `develop`; UI work on branch `feat/topic-search-ui` off `dev`.
- Subagent-driven: fresh sonnet implementer per task + file-based brief/report + **Fable 5 final review**.

---

## Prerequisite Task 0: Coverage backfill (operational — controller runs, NOT a TDD subagent task)

Search is only useful if `report_chunks` covers the whole prod lake. Migration 0007 is already applied (`get_by_folder_name` works). Run + verify before shipping the UI (backend code below does not depend on it and can be built in parallel).

- [ ] **0.1** Confirm every lake report has an embedding sidecar; embed the ones missing:
  - List reports: `AWS_PROFILE=fieldsight-deployer aws s3 ls s3://fieldsight-data-509194952652/reports/ --region ap-southeast-2`
  - For any report date/folder lacking `s3://fieldsight-data-509194952652/embeddings/{date}/{folder}/vectors.json`, invoke embed-report (non-VPC, DashScope): `aws lambda invoke --function-name fieldsight-test-embed-report --region ap-southeast-2 --cli-binary-format raw-in-base64-out --payload '{"date":"<d>","user":"<folder>"}' /tmp/e.json`
- [ ] **0.2** Run the idempotent backfill: `aws lambda invoke --function-name fieldsight-test-ingest --region ap-southeast-2 --cli-binary-format raw-in-base64-out --payload '{"backfill":true}' /tmp/ingest.json` → read `processed/skipped/failed`.
- [ ] **0.3** Verify coverage with the rag-search probe (from this session's method): invoke `fieldsight-test-rag-search` with the admin sub `f99e04e8-a0c1-7091-3da6-ed96fd63eb08` + a real "door" embedding via `fieldsight-test-ask-agent` (`{"question":"door","caller_sub":"<sub>","k":30}`) and confirm citations span **multiple folders/sites** (not just SB1108) and include Jarley 2–3 月 door topics + David 2026-03-09. Record which folders/dates are covered; if gaps remain, re-run 0.1 for the missing folder.

---

## Task 1: rag-search date filter (pipeline)

**Files:**
- Modify: `src/repositories/search_sql.py` (whole `build_search_sql` body)
- Modify: `src/repositories/chunks.py:24-28` (`search_chunks` signature + params)
- Modify: `src/lambda_rag_search.py:42-79` (read `date_from`/`date_to`, pass through)
- Test: `tests/unit/test_search_sql.py`, `tests/unit/test_lambda_rag_search.py`

**Interfaces:**
- Produces: `build_search_sql()` SQL now binds `%(date_from)s`, `%(date_to)s` (optional inclusive `report_date` range). `chunks.search_chunks(conn, query_embedding, accessible_site_ids, k=5, date_from=None, date_to=None)`. `lambda_rag_search.lambda_handler` reads `event["date_from"]`/`["date_to"]` (ISO strings or absent → `None`).
- NOTE: no `users` join / `user_folder` column — the search route derives the folder from each chunk's `source_s3_key` (Task 2), which is seed-independent. `search_sql` gains ONLY the date filter.

- [ ] **Step 1: Write the failing SQL test** — append to `tests/unit/test_search_sql.py`:

```python
def test_search_sql_has_date_filter():
    sql = build_search_sql()
    # optional inclusive date range (None => no filter)
    assert "%(date_from)s IS NULL OR c.report_date >= %(date_from)s" in sql
    assert "%(date_to)s IS NULL OR c.report_date <= %(date_to)s" in sql
    # unchanged essentials still present
    assert "site_id = ANY(%(site_ids)s)" in sql
    assert "LIMIT %(k)s" in sql
```

- [ ] **Step 2: Run it, verify it fails** — `cd fieldsight-pipeline && python -m pytest tests/unit/test_search_sql.py::test_search_sql_has_date_filter -v` → FAIL (`assert ... in sql`).

- [ ] **Step 3: Implement `build_search_sql`** — replace the whole return in `src/repositories/search_sql.py` (add ONLY the two date-range clauses; everything else byte-identical to the current SQL):

```python
def build_search_sql() -> str:
    # Deny-by-default: ALWAYS filter by the caller's accessible site ids.
    # small-to-big: parent topic title/summary via LEFT JOIN. Citations need
    # report_date/site_id/site_name. Optional inclusive report_date range
    # (both NULL => no date filtering, so the Ask path stays byte-identical
    # when it passes no dates).
    return (
        "SELECT c.id, c.chunk_text, c.chunk_type, c.topic_id, c.source_s3_key, "
        "       c.metadata, c.report_date, c.site_id, s.name AS site_name, "
        "       t.title AS topic_title, t.summary AS topic_summary, "
        "       c.embedding <=> %(q)s::vector AS distance "
        "FROM report_chunks c "
        "LEFT JOIN topics t ON t.id = c.topic_id "
        "LEFT JOIN sites s ON s.id = c.site_id "
        "WHERE c.site_id = ANY(%(site_ids)s) "
        "AND (%(date_from)s IS NULL OR c.report_date >= %(date_from)s) "
        "AND (%(date_to)s IS NULL OR c.report_date <= %(date_to)s) "
        "ORDER BY c.embedding <=> %(q)s::vector "
        "LIMIT %(k)s"
    )
```

- [ ] **Step 4: Run SQL tests** — `python -m pytest tests/unit/test_search_sql.py tests/unit/test_chunk_search_sql.py -v` → the new test PASSES; confirm the pre-existing `test_search_sql_contains_citation_columns` still PASSES (all its asserts remain true).

- [ ] **Step 5: Write the failing rag-search date test** — append to `tests/unit/test_lambda_rag_search.py`:

```python
def test_date_bounds_forwarded_to_search_chunks(wired):
    wired.setattr(rag.sites, "list_company_sites", lambda conn, cid: [{"id": "s-1"}])
    captured = {}

    def fake_search(conn, qv, site_ids, k=5, date_from=None, date_to=None):
        captured.update(date_from=date_from, date_to=date_to)
        return []

    wired.setattr(rag.chunks, "search_chunks", fake_search)
    ev = make_event()
    ev["date_from"] = "2026-02-01"
    ev["date_to"] = "2026-03-31"

    rag.lambda_handler(ev, None)

    assert captured["date_from"] == "2026-02-01"
    assert captured["date_to"] == "2026-03-31"


def test_date_bounds_default_none(wired):
    wired.setattr(rag.sites, "list_company_sites", lambda conn, cid: [{"id": "s-1"}])
    captured = {}

    def fake_search(conn, qv, site_ids, k=5, date_from=None, date_to=None):
        captured.update(date_from=date_from, date_to=date_to)
        return []

    wired.setattr(rag.chunks, "search_chunks", fake_search)

    rag.lambda_handler(make_event(), None)  # no date keys

    assert captured["date_from"] is None
    assert captured["date_to"] is None
```

- [ ] **Step 6: Run, verify fail** — `python -m pytest tests/unit/test_lambda_rag_search.py -k date_bounds -v` → FAIL (`search_chunks() got unexpected keyword 'date_from'`).

- [ ] **Step 7: Implement pass-through** — in `src/repositories/chunks.py` replace `search_chunks`:

```python
def search_chunks(conn, query_embedding, accessible_site_ids, k=5,
                  date_from=None, date_to=None) -> list[dict]:
    return conn.cursor(row_factory=dict_row).execute(
        build_search_sql(),
        {"q": query_embedding, "site_ids": list(accessible_site_ids), "k": k,
         "date_from": date_from, "date_to": date_to},
    ).fetchall()
```

  In `src/lambda_rag_search.py`, inside `lambda_handler` after `qv = event.get("query_embedding")` add:

```python
    date_from = event.get("date_from") or None
    date_to = event.get("date_to") or None
```

  and change the `search_chunks` call to:

```python
        rows = chunks.search_chunks(conn, qv, site_ids, k=k,
                                    date_from=date_from, date_to=date_to)
```

- [ ] **Step 8: Run rag-search suite** — `python -m pytest tests/unit/test_lambda_rag_search.py -v` → all PASS (new date tests + all existing, incl. `test_json_safe_return_coerces_uuid_and_date`).

- [ ] **Step 9: Commit**

```bash
git add src/repositories/search_sql.py src/repositories/chunks.py src/lambda_rag_search.py tests/unit/test_search_sql.py tests/unit/test_lambda_rag_search.py
git commit -m "feat(rag-search): optional report_date range filter + user_folder column for search list"
```

---

## Task 2: ask-agent `mode=search` — retrieve-only topic aggregation (pipeline)

**Files:**
- Modify: `src/lambda_ask_agent.py` (add `_aggregate_topics`, `_rag_search_list`; branch in `lambda_handler`)
- Test: `tests/unit/test_lambda_ask_agent_search.py` (new)

**Interfaces:**
- Consumes: rag-search chunk rows include `source_s3_key`, `report_date`, `site_name`, `topic_id`, `topic_title`, `chunk_text`, `chunk_type`, `distance`. The route folder is parsed from `source_s3_key` (seed-independent), NOT a `user_folder` column.
- Produces: when `body["mode"] == "search"`, `lambda_handler` returns `ok(_rag_search_list(body))` where the body is `{"results": [ {report_date, site_name, topic_id|None, title, snippet, chunk_type, route, score} ], "count": N, "grounded": True}`. Never calls `claude_utils.call_claude`. Forwards optional `date_from`/`date_to` + `k` (default 30) to rag-search.

- [ ] **Step 1: Write the failing search-mode tests** — create `tests/unit/test_lambda_ask_agent_search.py`:

```python
"""Tests for lambda_ask_agent mode=search (retrieve-only topic list).
Mirrors test_lambda_ask_agent_rag.py's wiring (FakeLambdaClient stand-in for
rag-search; dashscope_utils.embed / claude_utils.call_claude monkeypatched as
shared-module attributes)."""
import io
import json
import os

import pytest

os.environ.setdefault("AWS_ACCESS_KEY_ID", "testing")
os.environ.setdefault("AWS_SECRET_ACCESS_KEY", "testing")
os.environ.setdefault("AWS_DEFAULT_REGION", "ap-southeast-2")
os.environ.setdefault("ANTHROPIC_API_KEY", "sk-ant-test-dummy-key")
os.environ.setdefault("RAG_SEARCH_FUNCTION", "fieldsight-test-rag-search")

laa = pytest.importorskip("lambda_ask_agent", reason="requires boto3/urllib3")
import claude_utils  # noqa: E402
import dashscope_utils  # noqa: E402


def chunk(topic_id, date, dist, title, folder="Jarley_Trainor", site="Ellesmere",
          chunk_type="topic", text="door hardware defect", src=None):
    # ingest stamps every chunk's source_s3_key with the report key
    # (reports/<date>/<folder>/daily_report.json); the search route parses the
    # folder out of it (seed-independent). folder=None => no folder segment.
    if src is None:
        src = ("reports/" + date + "/" + folder + "/daily_report.json") if folder \
            else "reports/" + date + "/daily_report.json"
    return {"id": "c-" + str(dist), "chunk_text": text, "chunk_type": chunk_type,
            "topic_id": topic_id, "source_s3_key": src, "metadata": {},
            "report_date": date, "site_id": "s-1", "site_name": site,
            "topic_title": title, "topic_summary": "", "distance": dist}


class FakeLambdaClient:
    def __init__(self, chunks):
        self.payload = {"chunks": chunks}
        self.calls = []

    def invoke(self, FunctionName, InvocationType, Payload):
        self.calls.append({"FunctionName": FunctionName, "Payload": json.loads(Payload)})
        return {"Payload": io.BytesIO(json.dumps(self.payload).encode("utf-8"))}


def wire(monkeypatch, chunks):
    monkeypatch.setattr(dashscope_utils, "embed", lambda texts, dim=None: [[0.1] * 1024])
    fc = FakeLambdaClient(chunks)
    monkeypatch.setattr(laa, "_get_lambda_client", lambda: fc)

    def no_claude(*a, **k):
        raise AssertionError("call_claude must not run in search mode")
    monkeypatch.setattr(claude_utils, "call_claude", no_claude)
    return fc


def run(event):
    resp = laa.lambda_handler(event, None)
    return json.loads(resp["body"])


def ev(**kw):
    base = {"question": "door damage", "caller_sub": "sub-1", "mode": "search"}
    base.update(kw)
    return base


def test_search_mode_returns_topics_no_claude(monkeypatch):
    wire(monkeypatch, [chunk("t-1", "2026-02-09", 0.1, "Door Hardware Issues")])
    out = run(ev())
    assert out["count"] == 1
    r = out["results"][0]
    assert r["title"] == "Door Hardware Issues"
    assert r["report_date"] == "2026-02-09"
    assert r["route"] == "/timeline?date=2026-02-09&user=Jarley_Trainor&topic=t-1"


def test_search_mode_dedupes_topic_keeps_best_distance(monkeypatch):
    wire(monkeypatch, [
        chunk("t-1", "2026-02-09", 0.4, "Door Hardware Issues"),
        chunk("t-1", "2026-02-09", 0.1, "Door Hardware Issues"),  # same topic, closer
    ])
    out = run(ev())
    assert out["count"] == 1
    assert out["results"][0]["score"] == 0.1


def test_search_mode_orders_by_distance(monkeypatch):
    wire(monkeypatch, [
        chunk("t-2", "2026-03-02", 0.5, "Far"),
        chunk("t-1", "2026-02-09", 0.1, "Near"),
    ])
    out = run(ev())
    assert [r["title"] for r in out["results"]] == ["Near", "Far"]


def test_search_mode_topicless_chunk_grouped_no_topic_in_route(monkeypatch):
    wire(monkeypatch, [chunk(None, "2026-02-09", 0.2, "", chunk_type="transcript_window",
                             text="sliding door came off runner")])
    out = run(ev())
    r = out["results"][0]
    assert r["topic_id"] is None
    assert "&topic=" not in r["route"]
    assert r["route"].startswith("/timeline?date=2026-02-09&user=Jarley_Trainor")
    assert r["title"] == "sliding door came off runner"[:60]


def test_search_mode_route_omits_user_when_folder_missing(monkeypatch):
    wire(monkeypatch, [chunk("t-9", "2026-02-09", 0.2, "T", folder=None)])
    out = run(ev())
    assert out["results"][0]["route"] == "/timeline?date=2026-02-09&topic=t-9"


def test_search_mode_forwards_date_range_and_k(monkeypatch):
    fc = wire(monkeypatch, [])
    run(ev(date_from="2026-02-01", date_to="2026-03-31", k=25))
    p = fc.calls[0]["Payload"]
    assert p["date_from"] == "2026-02-01"
    assert p["date_to"] == "2026-03-31"
    assert p["k"] == 25


def test_search_mode_default_k_is_30(monkeypatch):
    fc = wire(monkeypatch, [])
    run(ev())
    assert fc.calls[0]["Payload"]["k"] == 30


def test_ask_mode_unaffected_still_calls_claude(monkeypatch):
    # mode absent => Ask path; call_claude IS used (no AssertionError wiring here)
    monkeypatch.setattr(dashscope_utils, "embed", lambda texts, dim=None: [[0.1] * 1024])
    fc = FakeLambdaClient([chunk("t-1", "2026-02-09", 0.1, "Door")])
    monkeypatch.setattr(laa, "_get_lambda_client", lambda: fc)
    seen = {}
    monkeypatch.setattr(claude_utils, "call_claude",
                        lambda prompt, max_tokens=4096: (seen.setdefault("called", True), "ans [1]")[1] if False else ("ans [1]", None))
    resp = laa.lambda_handler({"question": "q", "caller_sub": "sub-1"}, None)
    body = json.loads(resp["body"])
    assert "citations" in body  # Ask envelope, not the search {results} envelope
```

- [ ] **Step 2: Run, verify fail** — `python -m pytest tests/unit/test_lambda_ask_agent_search.py -v` → FAIL (search mode not implemented; `lambda_handler` runs the Ask path and calls the `no_claude` stub → AssertionError, or returns citations envelope).

- [ ] **Step 3: Implement aggregation + search-list + dispatch** — in `src/lambda_ask_agent.py`:

  (a) add near the top, after the existing `import urllib3`:

```python
import urllib.parse as _urlparse


def _q(s):
    return _urlparse.quote(str(s), safe="")


def _folder_from_source(src):
    """Report folder from the chunk's source_s3_key. Ingest stamps every chunk
    with the report key `reports/<date>/<folder>/daily_report.json` (folder =
    3rd segment). Tolerate the transcript key shape `transcripts/<folder>/
    <date>/...` (folder = 2nd segment) too. Miss => '' (route omits &user)."""
    parts = (src or "").split("/")
    if len(parts) >= 4 and parts[0] == "reports":
        return parts[2]
    if len(parts) >= 3 and parts[0] == "transcripts":
        return parts[1]
    return ""
```

  (b) add these two functions just above `_rag_answer`:

```python
def _aggregate_topics(chunks):
    """Collapse retrieved chunks into distinct rows for the Search list.
    Group key: (report_date, site_id, topic_id) when a topic is present,
    else (report_date, site_id, "src:"+source_s3_key) for a topic-less
    transcript window (shown as a report excerpt). Keep the smallest cosine
    distance per group as the relevance score. Route mirrors the client's
    existing topic deep-link EXACTLY (String(topic_id), encodeURIComponent);
    &user omitted when folder is unknown, &topic omitted when no topic."""
    groups = {}
    for c in chunks:
        topic_id = c.get("topic_id")
        date = str(c.get("report_date", "") or "")
        site_id = str(c.get("site_id", "") or "")
        key = (date, site_id, topic_id) if topic_id \
            else (date, site_id, "src:" + str(c.get("source_s3_key") or ""))
        dist = c.get("distance")
        dist = float(dist) if dist is not None else 1.0
        cur = groups.get(key)
        if cur is not None and dist >= cur["score"]:
            continue
        folder = _folder_from_source(c.get("source_s3_key"))
        title = c.get("topic_title") or (c.get("chunk_text") or "")[:60]
        route = "/timeline?date=" + _q(date)
        if folder:
            route += "&user=" + _q(folder)
        if topic_id:
            route += "&topic=" + _q(str(topic_id))
        groups[key] = {
            "report_date": date,
            "site_name": c.get("site_name"),
            "topic_id": str(topic_id) if topic_id else None,
            "title": title,
            "snippet": (c.get("chunk_text") or "")[:200],
            "chunk_type": c.get("chunk_type"),
            "route": route,
            "score": dist,
        }
    rows = list(groups.values())
    rows.sort(key=lambda r: r["report_date"], reverse=True)  # date desc tiebreak
    rows.sort(key=lambda r: r["score"])                      # stable: distance asc primary
    return rows


def _rag_search_list(body):
    """mode=search: embed question -> rag-search (ACL + optional date range)
    -> aggregate to a ranked topic list. NO Claude synthesis."""
    import dashscope_utils

    question = (body.get("question") or "").strip()
    caller_sub = body.get("caller_sub")
    try:
        k = int(body.get("k", 30))
    except (TypeError, ValueError):
        k = 30
    date_from = body.get("date_from") or None
    date_to = body.get("date_to") or None

    try:
        query_vec = dashscope_utils.embed([question])[0]
        payload = {"sub": caller_sub, "query_embedding": query_vec, "k": k}
        if date_from:
            payload["date_from"] = date_from
        if date_to:
            payload["date_to"] = date_to
        resp = _get_lambda_client().invoke(
            FunctionName=RAG_SEARCH_FUNCTION,
            InvocationType="RequestResponse",
            Payload=json.dumps(payload),
        )
        result = json.loads(resp["Payload"].read().decode("utf-8"))
        if result.get("error"):
            logger.warning(f"  search rag-search error: {result['error']}")
        rows = _aggregate_topics(result.get("chunks") or [])
        return {"results": rows, "count": len(rows), "grounded": True}
    except Exception as e:
        logger.error(f"  RAG search-list failed: {e}")
        return {"results": [], "error": str(e), "count": 0}
```

  (c) in `lambda_handler`, change the RAG dispatch guard (currently `if body.get('caller_sub') and os.environ.get('RAG_SEARCH_FUNCTION'): return ok(_rag_answer(body))`) to:

```python
    if body.get('caller_sub') and os.environ.get('RAG_SEARCH_FUNCTION'):
        if body.get('mode') == 'search':
            return ok(_rag_search_list(body))
        return ok(_rag_answer(body))
```

- [ ] **Step 4: Run search tests** — `python -m pytest tests/unit/test_lambda_ask_agent_search.py -v` → all PASS.

- [ ] **Step 5: Run the full ask-agent suite (no regressions)** — `python -m pytest tests/unit/test_lambda_ask_agent_rag.py tests/unit/test_lambda_ask_agent_search.py -v` → all PASS (Ask path untouched; `test_claude_and_dashscope_are_not_top_level_imports` still holds — `import dashscope_utils` stays inside `_rag_search_list`, NOT module top).

- [ ] **Step 6: Commit**

```bash
git add src/lambda_ask_agent.py tests/unit/test_lambda_ask_agent_search.py
git commit -m "feat(ask-agent): mode=search retrieve-only topic list (no LLM synthesis)"
```

---

## Task 3: report API `/api/search` route (pipeline)

**Files:**
- Modify: `src/lambda_fieldsight_api.py` (add `search_topics`; add router line near `:990`)
- Test: `tests/unit/test_lambda_fieldsight_api_search.py` (new)

**Interfaces:**
- Consumes: `ASK_AGENT_FUNCTION` env + `lambda_client` (both already present, used by `ask_question`); `resolve_user_display_name(caller)`.
- Produces: `POST /api/search` → forwards `{mode:"search", question, user, caller_sub, k, date_from?, date_to?}` to the Ask Agent and passes the result through. `q<2` → `{"results":[],"count":0}` without invoking.

- [ ] **Step 1: Write the failing tests** — create `tests/unit/test_lambda_fieldsight_api_search.py`:

```python
"""Tests for lambda_fieldsight_api.search_topics — /api/search forwarder.
Mirrors test_lambda_fieldsight_api_ask.py (FakeLambdaClient double, dummy env)."""
import io
import json
import os

import pytest

os.environ.setdefault("AWS_ACCESS_KEY_ID", "testing")
os.environ.setdefault("AWS_SECRET_ACCESS_KEY", "testing")
os.environ.setdefault("AWS_DEFAULT_REGION", "ap-southeast-2")

fapi = pytest.importorskip("lambda_fieldsight_api", reason="requires boto3")

ADMIN_CALLER = {"sub": "sub-admin-1", "email": "a@x.nz", "name": "Ada Admin",
                "role": "admin", "display_name": "Ada_Admin", "device_id": "",
                "sites": [], "managed_sites": [], "company_id": "c-1"}


class FakeLambdaClient:
    def __init__(self, response_payload=None, function_error=None):
        self.response_payload = response_payload if response_payload is not None else {
            "results": [], "count": 0}
        self.function_error = function_error
        self.calls = []

    def invoke(self, FunctionName, InvocationType, Payload):
        self.calls.append({"FunctionName": FunctionName, "Payload": json.loads(Payload)})
        resp = {"Payload": io.BytesIO(json.dumps(self.response_payload).encode("utf-8"))}
        if self.function_error:
            resp["FunctionError"] = self.function_error
        return resp


def wire(monkeypatch, **kw):
    fc = FakeLambdaClient(**kw)
    monkeypatch.setattr(fapi, "lambda_client", fc)
    return fc


def body_of(res):
    return json.loads(res["body"])


def test_forwards_mode_search_and_sub(monkeypatch):
    fc = wire(monkeypatch)
    res = fapi.search_topics({"question": "door damage"}, ADMIN_CALLER)
    assert res["statusCode"] == 200
    p = fc.calls[0]["Payload"]
    assert p["mode"] == "search"
    assert p["caller_sub"] == "sub-admin-1"
    assert p["question"] == "door damage"


def test_short_query_returns_empty_without_invoke(monkeypatch):
    fc = wire(monkeypatch)
    res = fapi.search_topics({"question": "d"}, ADMIN_CALLER)
    assert res["statusCode"] == 200
    assert body_of(res) == {"results": [], "count": 0}
    assert fc.calls == []


def test_forwards_date_range(monkeypatch):
    fc = wire(monkeypatch)
    fapi.search_topics({"question": "door", "date_from": "2026-02-01", "date_to": "2026-03-31"},
                       ADMIN_CALLER)
    p = fc.calls[0]["Payload"]
    assert p["date_from"] == "2026-02-01"
    assert p["date_to"] == "2026-03-31"


def test_function_error_returns_500_no_leak(monkeypatch):
    wire(monkeypatch,
         response_payload={"errorMessage": "boom", "errorType": "RuntimeError",
                           "stackTrace": ["  File \"lambda_ask_agent.py\", line 1"]},
         function_error="Unhandled")
    res = fapi.search_topics({"question": "door damage"}, ADMIN_CALLER)
    assert res["statusCode"] == 500
    assert "stackTrace" not in res["body"]
    assert "lambda_ask_agent.py" not in res["body"]


def test_router_dispatches_search(monkeypatch):
    wire(monkeypatch)
    event = {"httpMethod": "POST", "path": "/api/search",
             "body": json.dumps({"question": "door damage"}),
             "requestContext": {"authorizer": {"claims": {
                 "sub": "sub-admin-1", "email": "a@x.nz", "name": "Ada Admin"}}}}
    # user_mapping lookup during caller-build may miss; admin role isn't required
    # for search (ACL is downstream in rag-search) — a viewer caller still routes.
    res = fapi.lambda_handler(event, None)
    assert res["statusCode"] in (200, 500)  # routed (not 404); 200 with stub payload
    assert res["statusCode"] == 200
```

- [ ] **Step 2: Run, verify fail** — `python -m pytest tests/unit/test_lambda_fieldsight_api_search.py -v` → FAIL (`AttributeError: module ... has no attribute 'search_topics'`).

- [ ] **Step 3: Implement `search_topics` + router** — in `src/lambda_fieldsight_api.py`, add after `ask_question` (ends ~line 902):

```python
# ── POST /api/search ─────────────────────────────────────────

def search_topics(body, caller):
    """Retrieve-only topic search: forward to the Ask Agent with mode=search.
    Returns a ranked topic list (no LLM synthesis). ACL is enforced downstream
    in rag-search (org accessible sites via caller_sub), so no per-user gate is
    needed here. date_from/date_to are an optional inclusive range."""
    question = body.get('question', '').strip()
    if len(question) < 2:
        return ok({'results': [], 'count': 0})

    payload = {
        'mode': 'search',
        'question': question,
        'user': resolve_user_display_name(caller),  # soft context only
        'caller_sub': caller.get('sub', ''),
        'k': int(body.get('k', 30)) if str(body.get('k', 30)).isdigit() else 30,
    }
    if body.get('date_from'):
        payload['date_from'] = body['date_from']
    if body.get('date_to'):
        payload['date_to'] = body['date_to']

    try:
        resp = lambda_client.invoke(
            FunctionName=ASK_AGENT_FUNCTION,
            InvocationType='RequestResponse',
            Payload=json.dumps(payload),
        )
        if resp.get('FunctionError'):
            logger.error(f"Search agent returned FunctionError: {resp.get('FunctionError')}")
            return error('Search error', 500)
        result = json.loads(resp['Payload'].read().decode('utf-8'))
        if 'body' in result:
            return result
        return ok(result)
    except Exception as e:
        logger.error(f"Search invocation failed: {e}")
        return error(f'Search error: {e}', 500)
```

  Then add the router line right after the `/api/ask` line (~`:990`):

```python
        elif path == '/api/search' and method == 'POST': return search_topics(body, caller)
```

- [ ] **Step 4: Run search-api tests** — `python -m pytest tests/unit/test_lambda_fieldsight_api_search.py -v` → all PASS.

- [ ] **Step 5: Run the ask-api suite (no regressions)** — `python -m pytest tests/unit/test_lambda_fieldsight_api_ask.py tests/unit/test_lambda_fieldsight_api_search.py -v` → all PASS.

- [ ] **Step 6: Commit**

```bash
git add src/lambda_fieldsight_api.py tests/unit/test_lambda_fieldsight_api_search.py
git commit -m "feat(report-api): POST /api/search route forwarding to ask-agent mode=search"
```

---

## Task 4: Deploy backend + live smoke (pipeline — controller runs after Tasks 1-3 reviewed)

No `template.yaml` change: `/api/search` is served by the existing `/api/{proxy+}` → `fieldsight-test-api`; ask-agent→rag-search invoke perms and Aurora access already exist for `/ask`.

- [ ] **Step 1: Merge `feat/topic-search-api` → `develop`** (PR), then serial SAM deploy: `export AWS_CLI_FILE_ENCODING=UTF-8 PYTHONUTF8=1 AWS_PROFILE=fieldsight-deployer` and run the repo's deploy (or `gh workflow run "Deploy FieldSight" --ref develop`). Wait for stack UPDATE_COMPLETE (no concurrent deploy).
- [ ] **Step 2: Verify deployed code** — `aws lambda get-function-configuration --function-name fieldsight-test-rag-search --query CodeSha256` changed from the backup anchor `OZ1msUB2+JTM60GLNh5rUUnKOuR2HxJJqbKmdj71qK4=`.
- [ ] **Step 3: Live smoke** — invoke `fieldsight-test-ask-agent` with `{"question":"door damage","caller_sub":"f99e04e8-a0c1-7091-3da6-ed96fd63eb08","mode":"search","k":30}` → assert a `results` array of distinct topics with `route` strings, `count>0`, and **no** `citations`/`answer` field. Then `{"...","mode":"search","date_from":"2026-02-01","date_to":"2026-02-28"}` → only Feb dates returned. Also invoke plain `{"question":"door damage","caller_sub":"...","k":5}` (no mode) → still the Ask `{answer,citations}` envelope (regression check).

---

## Task 5: frontend search API module (fieldsight-ui)

**Files:**
- Create: `scripts/api/search.js`
- Modify: `app-shell-preview.html` (register script + cache-buster)
- Test: `node --check` + wired into palette in Task 6 (UI repo has no test framework; verification is `node --check` + manual browser).

**Interfaces:**
- Produces: `window.FS.api.search.topics({q, from, to})` → resolves to `{results:[{report_date, site_name, topic_id, title, snippet, chunk_type, route, score}], count}`. `q<2` chars → `{results:[],count:0}` locally. Mocks mode → `{results:[], _mock:true}` (palette keeps its client cache path).

- [ ] **Step 1: Create `scripts/api/search.js`**

```javascript
/* ==========================================================================
   FieldSight API · Topic Search (mechanism 1)
   --------------------------------------------------------------------------
   POST /api/search { question, date_from?, date_to? }
     → { results:[{report_date, site_name, topic_id, title, snippet,
                   chunk_type, route, score}], count }

   Semantic retrieve-only (no LLM). Routed to the org gateway's report base
   (orgBaseUrl → /api/{proxy+} → fieldsight-test-api), same as /ask. When the
   query is shorter than 2 chars we short-circuit locally (no round-trip).
   ========================================================================== */
(function () {
  'use strict';

  async function topics(opts) {
    opts = opts || {};
    var q = (opts.q || '').trim();
    if (q.length < 2) return { results: [], count: 0 };

    if (window.FS.api.useMocks) {
      /* Mock mode keeps the palette's existing client-cache topic search. */
      return { results: [], count: 0, _mock: true };
    }

    var body = { question: q };
    if (opts.from) body.date_from = opts.from;
    if (opts.to)   body.date_to   = opts.to;

    return window.FS.api.request('/search', {
      method:  'POST',
      baseUrl: window.FS.api.orgBaseUrl || undefined,
      body:    body,
    });
  }

  window.FS.api.search = { topics: topics };
})();
```

- [ ] **Step 2: `node --check`** — `node --check scripts/api/search.js` → no output (valid).

- [ ] **Step 3: Register in `app-shell-preview.html`** — find the `<script ... src="scripts/api/ask.js?v=..."></script>` line and add immediately after it (match the existing versioned pattern; single-line anchor):

```html
    <script src="scripts/api/search.js?v=1"></script>
```

- [ ] **Step 4: Commit**

```bash
git add scripts/api/search.js app-shell-preview.html
git commit -m "feat(ui): FS.api.search.topics — POST /api/search client"
```

---

## Task 6: search-palette async topics + Ask fallback (fieldsight-ui)

**Files:**
- Modify: `scripts/composites/search-palette.js` (remove client topic fan-out; add async debounced `/search`; group topics by date; gate Ask on empty)
- Modify: `app-shell-preview.html` (bump `search-palette.js` cache-buster)
- Test: `node --check` + manual browser verification checklist.

**Interfaces:**
- Consumes: `window.FS.api.search.topics` (Task 5).

- [ ] **Step 1: Remove the broken client topic fan-out** — in `scripts/composites/search-palette.js` delete `TOPIC_FANOUT_CAP` (`:45`), the functions `_topicFanoutPairs` (`:66-93`) and `_loadTopics` (`:99-123`), the `topics: []` field in `_cache` (`:52`), and in `_loadCache` remove the `_loadTopics(topicPairs)` array entry + its `reportDates`/`topicPairs` setup + the `_cache.topics = results[3] || [];` line, and drop the now-unused `span.dates` topic logic. Keep sites/tasks/safety preload. (The `_search` topic block at `:245-261` is removed in Step 3.)

- [ ] **Step 2: Add async topic-search state + debounce** — inside `SearchPalette`, after the `askMode` state (`:312-313`), add:

```javascript
    /* Mechanism 1 — server-side semantic topic search (replaces the old
       client fan-out). Async + debounced; results merged with the client-side
       task/safety/site/people matches below. */
    var refTopics = React.useState([]);
    var topicRows = refTopics[0]; var setTopicRows = refTopics[1];
    var refTLoad  = React.useState(false);
    var topicLoading = refTLoad[0]; var setTopicLoading = refTLoad[1];
```

  Replace the query-change effect (`:325-330`) with a debounced fetch that also drives client search:

```javascript
    /* Re-run client search + kick a debounced server topic search on query
       change. A stale-guard token drops out-of-order responses. */
    var reqSeq = React.useRef(0);
    React.useEffect(function () {
      var q = query.trim();
      setSelIdx(0);
      if (!q) { setResults([]); setTopicRows([]); setTopicLoading(false); return; }
      setResults(_search(q));                 /* tasks/safety/sites/people, sync */
      if (q.length < 2) { setTopicRows([]); setTopicLoading(false); return; }
      var mine = ++reqSeq.current;
      setTopicLoading(true);
      var t = setTimeout(function () {
        window.FS.api.search.topics({ q: q })
          .then(function (r) {
            if (mine !== reqSeq.current) return;       /* superseded */
            setTopicRows((r && r.results) || []);
            setTopicLoading(false);
          })
          .catch(function () {
            if (mine !== reqSeq.current) return;
            setTopicRows([]);
            setTopicLoading(false);
          });
      }, 250);
      return function () { clearTimeout(t); };
    }, [query]);
```

- [ ] **Step 2b: Strip the topic branch out of `_search`** — remove the `_cache.topics.forEach(...)` block (`:245-261`) so `_search` returns only task/safety/site/user rows; topics now come from `topicRows`.

- [ ] **Step 3: Build topic result rows from `topicRows` + gate Ask on empty** — replace the `groups`/`flatItems`/`askItem` computation (`:360-370`) with:

```javascript
    /* Client-side groups (task/safety/site/user) */
    var groups = [];
    TYPE_ORDER.forEach(function (type) {
      if (type === 'topic') return;  /* topics come from the server now */
      var items = results.filter(function (r) { return r.type === type; });
      if (items.length) groups.push({ type: type, items: items });
    });

    /* Server topic rows → result items, grouped by report_date */
    var topicItems = topicRows.map(function (row) {
      return {
        type:     'topic',
        id:       row.route,
        title:    row.title,
        subtitle: (row.site_name ? row.site_name + ' · ' : '') + row.report_date,
        route:    row.route,
        _date:    row.report_date,
      };
    });

    var flatItems = groups.reduce(function (acc, g) { return acc.concat(g.items); }, [])
                          .concat(topicItems);

    /* Mechanism 2 — Ask is a FALLBACK: only when nothing matched and we're
       not still waiting on the server topic search. */
    var nothingFound = flatItems.length === 0;
    var askItem = (trimmedQuery && nothingFound && !topicLoading)
      ? { type: 'ask', id: '__ask__', title: 'Ask FieldSight: “' + trimmedQuery + '”' }
      : null;
    if (askItem) flatItems = flatItems.concat([askItem]);
```

- [ ] **Step 4: Render server topics grouped by date + a loading row** — in the results body, the existing `query.trim() && results.length > 0 ? groups.map(...)` branch must also render `topicItems` (grouped by `_date`) and a "Searching…" hint while `topicLoading`. Replace the results-body conditional (`:543-621`) so the order is: (a) if `askMode` → ask panel (unchanged); (b) build the render list from client `groups` **and** a `Topics` section that sub-groups `topicItems` by `_date`; (c) show `topicLoading` as a muted "Searching topics…" row under the Topics header; (d) the "No results" empty state shows only when `!topicLoading && flatItems.length === 0`. Concretely, add this Topics block right after the client `groups.map(...)` render and before the recent/hint fallbacks:

```javascript
          /* Server topic results, sub-grouped by report date */
          (query.trim() && (topicItems.length || topicLoading))
            ? React.createElement('div', { className: 'fs-search-palette__group', key: '__topics__' },
                React.createElement('div', {
                  className: 'fs-search-palette__group-label', role: 'presentation',
                }, 'Topics'),
                topicLoading && !topicItems.length
                  ? React.createElement('div', { className: 'fs-search-palette__hint' }, 'Searching topics…')
                  : _groupTopicsByDate(topicItems).map(function (bucket) {
                      return React.createElement('div', { key: bucket.date },
                        React.createElement('div', {
                          className: 'fs-search-palette__group-label',
                          role: 'presentation',
                          style: { opacity: 0.7, fontSize: '11px', paddingLeft: '4px' },
                        }, bucket.date),
                        bucket.items.map(function (item) {
                          var idx = flatItems.indexOf(item);
                          var isSel = idx === selIdx;
                          return React.createElement('button', {
                            key: item.id, type: 'button', role: 'option',
                            'aria-selected': isSel, 'data-selected': String(isSel),
                            className: 'fs-search-palette__result'
                              + (isSel ? ' fs-search-palette__result--active' : ''),
                            onClick: function () { doSelect(item); },
                            onMouseEnter: function () { setSelIdx(idx); },
                          },
                            NavIcon && React.createElement(NavIcon, {
                              name: 'file-text', size: 15, color: 'var(--text-tertiary)' }),
                            React.createElement('div', { className: 'fs-search-palette__result-body' },
                              React.createElement('span', { className: 'fs-search-palette__result-title' }, item.title),
                              item.subtitle ? React.createElement('span', {
                                className: 'fs-search-palette__result-sub' }, item.subtitle) : null));
                        }));
                    }))
            : null,
```

  and add this helper near the top of the IIFE (after `TYPE_ICONS`):

```javascript
  /* Bucket topic result items by report date, most-recent first. */
  function _groupTopicsByDate(items) {
    var by = {};
    items.forEach(function (it) { (by[it._date] = by[it._date] || []).push(it); });
    return Object.keys(by).sort().reverse().map(function (d) {
      return { date: d, items: by[d] };
    });
  }
```

  Also update the "No match" empty branch (`:543-546`) to not fire while loading:

```javascript
          query.trim() && results.length === 0 && !topicItems.length && !topicLoading
            ? React.createElement('div', { className: 'fs-search-palette__empty' },
                'No results for “' + query.trim() + '”')
```

- [ ] **Step 5: `node --check`** — `node --check scripts/composites/search-palette.js` → valid.

- [ ] **Step 6: Bump cache-buster** — in `app-shell-preview.html` bump the `scripts/composites/search-palette.js?v=N` to `N+1` (single-line anchor).

- [ ] **Step 7: Commit**

```bash
git add scripts/composites/search-palette.js app-shell-preview.html
git commit -m "feat(ui): semantic topic search via /api/search + Ask as zero-result fallback"
```

- [ ] **Step 8: Manual browser verification** (after Amplify deploys `dev`): as admin (benl.tech), open the palette, type `door` → topic rows grouped by date appear (incl. Feb/Mar Jarley door topics); type `door damage` → semantically-related topics rank in; type gibberish (`zzzzz`) → after "Searching…" clears, only then the "Ask FieldSight" fallback row shows; click a topic → routes to the correct `/timeline?date=&user=&topic=`. Confirm tasks/safety/sites/people still match client-side.

---

## Self-Review

**Spec coverage:** A (Task 0), B1 date filter + folder (Task 1), B2 mode=search aggregation no-LLM (Task 2), B3 /api/search route (Task 3), deploy+smoke (Task 4), frontend client + rewire + Ask fallback (Tasks 5-6). Date query = explicit `date_from`/`date_to` end-to-end (Task 1/2/3 payloads; UI passes them when present — NL date parsing deferred per spec). ACL = rag-search org sites (Task 2/3 rely on it; no user_mapping). Latency absorbed by debounce + loading row (Task 6). ✔ all spec sections mapped.

**Placeholder scan:** none — every code step has full code; commands have expected outcomes.

**Type/name consistency:** `search_chunks(..., date_from, date_to)` defined in Task 1, consumed by rag-search Task 1; `mode=="search"` + `date_from/date_to/k` payload keys consistent Task 2↔3; result row shape `{report_date, site_name, topic_id, title, snippet, chunk_type, route, score}` identical in Task 2 (backend) and Tasks 5-6 (frontend consumers); `window.FS.api.search.topics({q,from,to})` defined Task 5, called Task 6; `route` format matches the existing client topic route (`String(topic_id)`, `encodeURIComponent`/`quote(safe="")`) per Global Constraints.
