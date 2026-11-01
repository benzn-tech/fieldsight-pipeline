"""The action-pack agent's web lane (spec 2026-11-01 background action agent §6, §14 S5,
D6, S16; Task 9).

* `WebQueries` -- built ONLY from the Plan stage's output, before any record evidence is
  shown to a model (S5): at most MAX_QUERIES, each 1..MAX_QUERY_CHARS after strip, keyed by
  a short id. Later stages name a query by that id; nothing else reaches the search.
* `web_search` -- wraps web_answer.general_answer. A verified answer becomes Ask-shaped
  items (web_answer + up to 4 web_source, refs W from the run's RefAssigner, provenance
  'web'); anything else is dropped with its sources (D6: never citable, not even weak) and
  counted in the run trace.
* `snapshot_source` / `put_snapshot` -- fetch a cited web_source page once through
  web_fetch (SSRF-guarded) and store its text on the lake bucket at
  action_packs/<company_id>/<pack_id>/web/<ref>.txt. A failed fetch or put leaves
  snapshot_s3_key null (the verifier then caps the claims at weak). The lake ownership
  index row is written by org-api's store task (the agent has no database).

Logs: ids and counts only -- never query text, answer text, URLs or page text.
"""
from __future__ import annotations

import logging
import re

import action_pack_view
import question_admission

logger = logging.getLogger(__name__)

MAX_QUERIES = 3
MAX_QUERY_CHARS = question_admission.MAX_QUESTION_CHARS
QUERY_ID_RE = re.compile(r"^[A-Za-z0-9_-]{1,16}$")
_QUERY_KEYS = {"id", "text"}
# Ask's web envelope caps and cleanup (ask_orchestrator.web_tool), so a pack's web items
# are the same shape as Ask's (§6).
WEB_ANSWER_MAX_CHARS = 1200
WEB_MAX_SOURCES = 4
_NUMERIC_GROUP = re.compile(r"[ \t]*\[\s*\d+(?:\s*[-–,;]\s*\d+)*\s*\]")
_MULTI_BLANK = re.compile(r"[ \t]{2,}")
SNAPSHOT_CONTENT_TYPE = "text/plain; charset=utf-8"


class WebQueryError(ValueError):
    """The Plan stage's web queries are malformed. `code` names how."""

    def __init__(self, code):
        super().__init__(code)
        self.code = code


_FROM_PLAN = object()       # only from_plan may construct WebQueries


class WebQueries:
    """The run's fixed web queries. Each id may be searched at most once. Built ONLY by
    `from_plan` (the constructor refuses any other caller), so no stage can mint a query
    that skipped the plan's checks."""

    def __init__(self, queries, *, _token=None):
        if _token is not _FROM_PLAN:
            raise TypeError("WebQueries is built only by WebQueries.from_plan")
        self._texts = dict(queries)
        self._searched = set()

    @classmethod
    def from_plan(cls, planned) -> "WebQueries":
        if planned is None:
            return cls([], _token=_FROM_PLAN)
        if not isinstance(planned, list):
            raise WebQueryError("plan_not_list")
        if len(planned) > MAX_QUERIES:
            raise WebQueryError("too_many_queries")
        out = []
        for q in planned:
            if not isinstance(q, dict) or not set(q) <= _QUERY_KEYS:
                raise WebQueryError("query_shape")
            qid = q.get("id")
            if not isinstance(qid, str) or not QUERY_ID_RE.match(qid):
                raise WebQueryError("query_id")
            text = q.get("text")
            text = text.strip() if isinstance(text, str) else ""
            if not text or len(text) > MAX_QUERY_CHARS:
                raise WebQueryError("query_text")
            if any(qid == seen for seen, _ in out):
                raise WebQueryError("duplicate_query_id")
            out.append((qid, text))
        return cls(out, _token=_FROM_PLAN)

    @property
    def ids(self) -> tuple:
        return tuple(self._texts)

    def text_for(self, query_id) -> str:
        return self._texts[query_id]

    def take(self, query_id):
        """(text, None) and mark searched, or (None, error code)."""
        if not isinstance(query_id, str):
            return None, "bad_query_id"
        if query_id not in self._texts:
            return None, "unknown_query_id"
        if query_id in self._searched:
            return None, "already_searched"
        self._searched.add(query_id)
        return self._texts[query_id], None


def _bump(trace, key, n=1):
    if trace is not None:
        trace[key] = trace.get(key, 0) + n


def _envelope(query_id, items, meta):
    return {"tool": "web_search", "id": "web", "items": items, "meta": meta}


def web_search(queries: WebQueries, query_id, *, refs, budget, trace=None,
               general_answer=None, clock=None) -> dict:
    """One web search by planned query id. Never raises. Returns Ask's envelope shape
    {tool, id, items, meta}; meta.status is verified | dropped_unverified | failed, or
    meta.error names a refusal (bad_query_id | unknown_query_id | already_searched) --
    a refusal never reaches general_answer. `trace` (the run's counts dict) gains
    web_searches / web_refused / web_unverified_dropped / web_sources_dropped / web_failed."""
    text, err = queries.take(query_id)
    if err is not None:
        _bump(trace, "web_refused")
        logger.info("action pack web refused code=%s", err)
        return _envelope(None, [], {"error": err})
    if general_answer is None:
        import web_answer
        general_answer = web_answer.general_answer
    _bump(trace, "web_searches")
    meta = {"query_id": query_id}
    kw = {"kind": "general", "voice": False}
    if clock is not None:
        kw["clock"] = clock
    try:
        block = general_answer(text, budget, **kw) or {}
    except Exception as exc:                              # noqa: BLE001
        logger.warning("action pack web failed query=%s type=%s", query_id,
                       type(exc).__name__)
        _bump(trace, "web_failed")
        return _envelope(query_id, [], dict(meta, status="failed"))
    answer = _MULTI_BLANK.sub(" ", _NUMERIC_GROUP.sub("", block.get("answer") or "")).strip()
    sources = [s for s in (block.get("sources") or []) if isinstance(s, dict)]
    if block.get("status") != "verified" or not answer:
        # D6: not verified -> not citable at all. Its sources came from the failed verify.
        _bump(trace, "web_unverified_dropped")
        _bump(trace, "web_sources_dropped", len(sources))
        logger.info("action pack web dropped query=%s status=%s sources=%d", query_id,
                    block.get("status") if block.get("status") in (
                        "verified", "unverified", "too_long") else "other", len(sources))
        return _envelope(query_id, [], dict(meta, status="dropped_unverified"))
    items = [{"ref": refs.assign("web_answer", query_id, "W"), "kind": "web_answer",
              "provenance": "web", "text": answer[:WEB_ANSWER_MAX_CHARS]}]
    for src in sources[:WEB_MAX_SOURCES]:
        url = src.get("url")
        if not url:
            continue
        item = {"ref": refs.assign("web", url, "W"), "kind": "web_source",
                "provenance": "web", "title": src.get("title"),
                "text": src.get("domain") or "", "source": url}
        items.append({k: v for k, v in item.items() if v is not None})
    logger.info("action pack web verified query=%s sources=%d", query_id, len(items) - 1)
    return _envelope(query_id, items, dict(meta, status="verified"))


def put_snapshot(s3, bucket, company_id, pack_id, ref, text) -> str:
    """Write the page text to the lake bucket; returns the key. Raises what S3 raises."""
    key = action_pack_view.snapshot_key(company_id, pack_id, ref)
    s3.put_object(Bucket=bucket, Key=key, Body=text.encode("utf-8"),
                  ContentType=SNAPSHOT_CONTENT_TYPE)
    return key


def snapshot_source(item, *, fetcher, s3, bucket, company_id, pack_id) -> dict:
    """A cited web_source item -> the store task's source row (ref, kind, provenance,
    title, url, fetched_at, snapshot_s3_key). Fetches the page once; a failed fetch or put
    keeps the url with snapshot_s3_key and fetched_at null. Never raises for a fetch/put
    failure; ValueError for an item that is not a web_source."""
    if not isinstance(item, dict) or item.get("kind") != "web_source" \
            or item.get("provenance") != "web":
        raise ValueError("not a web_source item")
    ref, url = item["ref"], item.get("source")
    out = {"ref": ref, "kind": "web_source", "provenance": "web", "title": item.get("title"),
           "url": url, "fetched_at": None, "snapshot_s3_key": None}
    page = fetcher.fetch(url)
    if not page.get("ok") or page.get("text") is None:
        logger.info("action pack snapshot none ref=%s reason=%s", ref, page.get("reason"))
        return out
    try:
        key = put_snapshot(s3, bucket, company_id, pack_id, ref, page["text"])
    except Exception as exc:                              # noqa: BLE001
        logger.warning("action pack snapshot put failed ref=%s type=%s", ref,
                       type(exc).__name__)
        return out
    out.update(fetched_at=page.get("fetched_at"), snapshot_s3_key=key)
    logger.info("action pack snapshot stored ref=%s truncated=%s", ref,
                bool(page.get("truncated")))
    return out
