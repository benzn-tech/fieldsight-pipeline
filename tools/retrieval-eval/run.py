#!/usr/bin/env python3
"""Score FieldSight retrieval against a frozen, hand-judged question set.

WHAT THIS MEASURES
------------------
Whether the evidence for a question reaches the two places a person meets it:

  ask     the chunks Ask puts in front of the model (top 5)
  list    the rows the search box shows (production `_aggregate_topics`)

and, as the ceiling both are drawn from:

  raw     did the evidence come back anywhere in the k=30 search result
          (up to 60 rows: 30 vector + 30 keyword-arm, deduplicated)

for several SYSTEMS evaluated on the same question, so they differ only in what
they are meant to differ in:

  prod          today (2026-10 `origin/main`): the hybrid search SQL (vector arm
                + keyword arm `lex`, visibility predicate). Ask = k=5 search ->
                `_admit_one_lexical_hit(rows, 5)`; list = k=30 search ->
                `_aggregate_topics` (distance gate 0.55 for non-keyword rows).
  link-rollup   list only, a PROPOSAL: transcript windows with no topic are
                linked to the Aurora topic of the same author and date whose
                time_range overlaps their window_span most; windows that still
                overlap nothing are kept under a one-hour row.
  rerank@32     32 candidates through the same SQL (up to 64 rows), at most 4 per
                recording (`_diversify`), reranked by qwen3-rerank. Ask = top 5 of
                the reranked order. List = the same aggregation, with rows ordered
                by the best reranked position of any chunk grouped under them.
  rerank@50     the same with k=50. Production's rag-search clamps k to 32 per arm
                (`max(1, min(k, 32))`), so this needs that clamp raised.

Everything that exists in production is IMPORTED from `origin/main`'s `src/`
(exported with `git archive` into a temp dir, because prod runs main, and
develop's visibility predicate references `topics.superseded_at`, a column the
prod database does not have): the SQL is `repositories.search_sql.
build_search_sql()`, the keyword-arm text is `lexical_terms.or_query(query_terms
(q))`, Ask's context is `_admit_one_lexical_hit`, the list is `_aggregate_topics`,
the rerank is `_diversify` + `dashscope_utils.rerank`. Set EVAL_SRC_REF to another
ref (or "worktree") to measure something else. The SQL is re-spelled for the RDS
Data API and every rewrite is asserted, so if production SQL changes shape this
fails loudly instead of quietly measuring another search.

NOT replicated: rag-search's alias normalisation of chunk text (changes wording,
not which chunks are returned) and the "widen to nearest day when empty" retry.

WHY EVIDENCE IS ANCHORED TO WORDS, NOT CHUNKS
---------------------------------------------
A gold item names the sentence the answer lives in (`match_any` patterns), never a
chunk id, topic id or report field. Chunk ids change on every re-index, topics
change on every re-extraction, and the report format has changed three times this
month. The words someone said on site do not change -- which is what lets this
question set keep measuring across embedding swaps, re-chunking and prompt rewrites.

WHAT IT DOES NOT MEASURE
------------------------
- Answer quality. That needs the answer text judged, and is v2 of this tool.
- Access control. Scope is pinned to one author on one site.
- In-region latency. Timings are from wherever this runs. Compare runs made from
  the same machine; do not compare them with CloudWatch. Production gives the
  reranker 3 s and falls back to distance order past that; `rerank_over_budget`
  counts how often that would have happened from here (the tool lets the call run
  to 20 s so the reranker's real answer is scored).

USAGE
-----
    export AWS_PROFILE=fieldsight-deployer AWS_DEFAULT_REGION=ap-southeast-2
    export DASHSCOPE_API_KEY=...
    python tools/retrieval-eval/run.py --gold tools/retrieval-eval/gold/v1.jsonl \
        --label baseline --repeat 2
"""
import argparse
import hashlib
import inspect
import json
import os
import re
import statistics
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

HERE = Path(__file__).resolve().parent
REPO = HERE.parents[1]


def _export_src(ref):
    """Directory holding `src/` as it is at `ref` (default origin/main = prod),
    or this worktree's own src for ref "worktree"."""
    if ref == "worktree":
        return REPO / "src", None
    import io
    import tarfile
    import tempfile
    sha = subprocess.run(["git", "-C", str(REPO), "rev-parse", ref], capture_output=True,
                         text=True, check=True).stdout.strip()
    blob = subprocess.run(["git", "-C", str(REPO), "archive", sha, "src"], capture_output=True,
                          check=True).stdout
    dest = Path(tempfile.mkdtemp(prefix="retrieval-eval-src-"))
    with tarfile.open(fileobj=io.BytesIO(blob)) as tf:
        tf.extractall(dest)
    return dest / "src", sha


SRC_REF = os.environ.get("EVAL_SRC_REF", "origin/main")
SRC, SRC_SHA = _export_src(SRC_REF)

# lambda_ask_agent reads these at import. Placeholders only: this tool never calls
# a chat model and never touches S3 through that module.
os.environ.setdefault("S3_BUCKET", "retrieval-eval-unused")
os.environ.setdefault("ANTHROPIC_API_KEY", "retrieval-eval-unused")
os.environ.setdefault("AWS_DEFAULT_REGION", "ap-southeast-2")
os.environ.setdefault("DASHSCOPE_EMBED_MODEL", "text-embedding-v4")
os.environ.setdefault("DASHSCOPE_EMBED_DIM", "1024")
# Measure the reranker's real answer and time, rather than the production 3 s
# fallback firing from a slower network; over-budget calls are counted instead.
os.environ.setdefault("DASHSCOPE_RERANK_TIMEOUT_SECONDS", "20")
os.environ.pop("ENABLE_RERANK", None)   # prod value: off; rerank is applied explicitly below
sys.path.insert(0, str(SRC))

import boto3  # noqa: E402
import dashscope_utils  # noqa: E402
import lambda_ask_agent  # noqa: E402
import lexical_terms  # noqa: E402
import query_slots  # noqa: E402
from repositories import search_sql  # noqa: E402

CLUSTER = os.environ.get("EVAL_DB_CLUSTER_ARN",
                         "arn:aws:rds:ap-southeast-2:509194952652:cluster:"
                         "fieldsight-db-test-dbcluster-hywiixu8ihi9")
SECRET = os.environ.get("EVAL_DB_SECRET_ARN",
                        "arn:aws:secretsmanager:ap-southeast-2:509194952652:secret:"
                        "rds!cluster-1757a281-ee31-460d-b56e-950817921010-Ansbey")
DATABASE = os.environ.get("EVAL_DB_NAME", "fieldsight")

ASK_K = 5            # lambda_ask_agent: body.get("k", 5)
# lambda_ask_agent._rag_search_list: body.get("k", 30) before 2026-10-14, 64 after (rag-search clamp
# raised 32 -> 64 in the same change). EVAL_LIST_K lets the ruler mirror whichever source ref it imports.
LIST_K = int(os.environ.get("EVAL_LIST_K", "30"))
PROD_RERANK_BUDGET_MS = 3000
LIST_RERANK_SYSTEMS = {"rerank@32": 32, "rerank@50": 50}
SYSTEMS = ["prod", "link-rollup", "rerank@32", "rerank@50"]
# --family: the index family rag-search reads (SEARCH_INDEX_READ, "report" | "session").
# None keeps the pre-switch behaviour (no family filter) so older runs stay reproducible.
FAMILY = None

REWRITES = [
    ("%(q)s::vector", "CAST(:q AS vector)"),
    ("ANY(%(site_ids)s)", "ANY(CAST(:site_ids AS uuid[]))"),
    ("%(author_ids)s::uuid[]", "CAST(:author_ids AS uuid[])"),
    ("%(date_from)s::date", "CAST(:date_from AS date)"),
    ("%(date_to)s::date", "CAST(:date_to AS date)"),
    ("%(q_text)s", ":q_text"),
    ("%(k)s", ":k"),
    ("%%", "%"),
]


OPTIONAL_REWRITES = [
    ("EXISTS (SELECT 1 FROM topics t WHERE t.id = c.topic_id AND t.superseded_at IS NULL)",
     "EXISTS (SELECT 1 FROM topics t WHERE t.id = c.topic_id)"),
]


def data_api_sql():
    sql = search_sql.build_search_sql()
    # Schema compatibility for measuring newer source against a database that has
    # not migrated yet (the `fieldsight` database has no topics.superseded_at), and
    # for develop's source_family switch, which this ruler leaves off (None = no
    # filter, as in every call the ruler models). Optional: absent in older source.
    # Applied to every ref alike, so before/after comparisons stay like for like.
    for old, new in OPTIONAL_REWRITES:
        sql = sql.replace(old, new)
    if FAMILY is not None and "%(source_family)s" not in sql:
        raise SystemExit("--family given but the imported search SQL has no source_family switch")
    sql = sql.replace("%(source_family)s::text",
                      "CAST(NULL AS text)" if FAMILY is None else "CAST(:source_family AS text)")
    for old, new in REWRITES:
        if old not in sql:
            raise SystemExit("production search SQL no longer contains %r -- "
                             "update REWRITES before trusting any number" % old)
        sql = sql.replace(old, new)
    if "%(" in sql:
        raise SystemExit("unrewritten parameter left in search SQL")
    return sql


def sha256_text(text):
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def git_commit():
    try:
        return subprocess.run(["git", "-C", str(REPO), "rev-parse", "HEAD"],
                              capture_output=True, text=True, check=True).stdout.strip()
    except Exception:  # noqa: BLE001 -- a fingerprint field, not a precondition
        return None


# --- time ranges --------------------------------------------------------------

_HMS = re.compile(r"(\d{1,2}):(\d{2})(?::(\d{2}))?")


def span_seconds(text, minute_resolution):
    """'11:01 – 11:02' or '11:01:10–11:02:40' -> (start_s, end_s) or None.
    A minute-resolution end covers its whole minute."""
    parts = _HMS.findall(text or "")
    if len(parts) < 2:
        return None
    (h1, m1, s1), (h2, m2, s2) = parts[0], parts[-1]
    start = int(h1) * 3600 + int(m1) * 60 + int(s1 or 0)
    end = int(h2) * 3600 + int(m2) * 60 + int(s2 or 0)
    if minute_resolution and not s2:
        end += 59
    return (start, end) if end >= start else None


class Searcher:
    def __init__(self, site, author):
        self.site, self.author = site, author
        self.sql = data_api_sql()
        self.rds = boto3.client("rds-data")
        self.topics = self._load_topics()
        self.blocks = recording_blocks(HERE / "corpus" / "manifest.v1.json")

    def _query(self, sql, params):
        resp = self.rds.execute_statement(resourceArn=CLUSTER, secretArn=SECRET, database=DATABASE,
                                          sql=sql, parameters=params, formatRecordsAs="JSON")
        return json.loads(resp.get("formattedRecords") or "[]")

    def _load_topics(self):
        rows = self._query(
            "SELECT id::text AS id, report_date::text AS report_date, time_range, title "
            "FROM topics WHERE user_id = CAST(:u AS uuid) AND site_id = CAST(:s AS uuid)",
            [{"name": "u", "value": {"stringValue": self.author}},
             {"name": "s", "value": {"stringValue": self.site}}])
        by_date = {}
        for t in rows:
            span = span_seconds(t.get("time_range"), minute_resolution=True)
            if span:
                by_date.setdefault(t["report_date"], []).append((span, t["id"], t["title"]))
        return by_date

    def embed(self, question):
        t0 = time.perf_counter()
        vec = dashscope_utils.embed([question])[0]
        return vec, (time.perf_counter() - t0) * 1000

    def run(self, question, vec, k, today):
        """What rag-search returns for this question at this k: vector arm + keyword
        arm, deduplicated, at most 2*k rows."""
        date_from, date_to = query_slots.time_range(question, today)
        q_text = lexical_terms.or_query(lexical_terms.query_terms(question))
        params = [
            {"name": "q", "value": {"stringValue": "[" + ",".join("%.7f" % x for x in vec) + "]"}},
            {"name": "q_text", "value": {"stringValue": q_text}},
            {"name": "site_ids", "value": {"stringValue": "{%s}" % self.site}},
            {"name": "author_ids", "value": {"stringValue": "{%s}" % self.author}},
            {"name": "date_from", "value": {"stringValue": date_from} if date_from else {"isNull": True}},
            {"name": "date_to", "value": {"stringValue": date_to} if date_to else {"isNull": True}},
            {"name": "k", "value": {"longValue": k}},
        ]
        if ":source_family" in self.sql:
            params.append({"name": "source_family", "value": {"stringValue": FAMILY}})
        t0 = time.perf_counter()
        rows = self._query(self.sql, params)
        sql_ms = (time.perf_counter() - t0) * 1000
        for row in rows:
            if isinstance(row.get("metadata"), str):
                row["metadata"] = json.loads(row["metadata"])
            row["distance"] = float(row["distance"]) if row.get("distance") is not None else None
        return rows, sql_ms


# --- proposed systems ---------------------------------------------------------

_SEGMENT_KEY = re.compile(r"_\d{4}-\d{2}-\d{2}_(\d{2})-(\d{2})-(\d{2})_.*_off([\d.]+)_to([\d.]+)_src")


def recording_blocks(manifest_path, gap_seconds=600):
    """{date: [(start_s, end_s), ...]} -- the design's recording blocks (PR #838
    §5.4): transcript segments placed by filename base time + VAD offset, merged
    where the silence between them is at most `gap_seconds`."""
    manifest = json.loads(Path(manifest_path).read_text(encoding="utf-8"))
    out = {}
    for day in manifest["days"]:
        segs = []
        for f in day["files"]:
            m = _SEGMENT_KEY.search(f["key"])
            if m:
                base = int(m.group(1)) * 3600 + int(m.group(2)) * 60 + int(m.group(3))
                segs.append((base + float(m.group(4)), base + float(m.group(5))))
        blocks = []
        for s, e in sorted(segs):
            if blocks and s - blocks[-1][1] <= gap_seconds:
                blocks[-1][1] = max(blocks[-1][1], e)
            else:
                blocks.append([s, e])
        out[day["date"]] = [(int(s), int(e)) for s, e in blocks]
    return out


def _hhmm(sec):
    return "%02d:%02d" % (sec // 3600, (sec % 3600) // 60)


def rollup_title(start, date, mode, blocks_by_date, long_block_seconds=5400):
    """The parent row an unlinked window is shown under. `hour`: its clock hour.
    `block`: its recording block when that block is short enough to be one
    selectable unit, else the clock hour inside it -- the design's rule that
    long blocks are not a unit."""
    hour = start // 3600
    by_hour = "Recordings %02d:00–%02d:00" % (hour, hour + 1)
    if mode != "block":
        return by_hour
    for bs, be in blocks_by_date.get(date, []):
        if bs - 60 <= start <= be + 60:
            if be - bs <= long_block_seconds:
                return "Recording %s–%s" % (_hhmm(bs), _hhmm(be))
            return by_hour
    return by_hour


def link_windows(rows, topics_by_date, rollup, blocks_by_date=None):
    """Copies of `rows` where topic-less transcript windows carry the Aurora topic
    they overlap most in time. With `rollup` ("hour" or "block"; True means
    "hour"), windows that overlap nothing are shown under a parent row rather
    than being dropped by the aggregator."""
    out = []
    for r in rows:
        r = dict(r)
        md = r.get("metadata") if isinstance(r.get("metadata"), dict) else {}
        if not r.get("topic_id") and r.get("chunk_type") != "topic":
            span = span_seconds(md.get("window_span") or md.get("time_range"), minute_resolution=False)
            best = None
            if span:
                for (ts, te), tid, title in topics_by_date.get(str(r.get("report_date")), []):
                    overlap = min(span[1], te) - max(span[0], ts)
                    if overlap >= 0 and (best is None or overlap > best[0]):
                        best = (overlap, tid, title)
            if best:
                r["topic_id"], r["topic_title"] = best[1], best[2]
            elif rollup and span:
                mode = "block" if rollup == "block" else "hour"
                r["chunk_type"] = "topic"
                r["metadata"] = dict(md, title=rollup_title(
                    span[0], str(r.get("report_date")), mode, blocks_by_date or {}))
        out.append(r)
    return out


def rerank_all(question, rows):
    """Production's rerank step on `rows` (what rag-search returned): at most
    RERANK_PER_SESSION_CAP per recording, then qwen3-rerank. Returns
    (ordered rows -- reranked candidates first, diversify-dropped rows after in
    their original order --, latency ms, whether the reranker answered). On
    failure the candidates keep their arrival order, as production's fallback."""
    candidates = lambda_ask_agent._diversify(rows, lambda_ask_agent.RERANK_PER_SESSION_CAP)
    kept = {id(c) for c in candidates}
    dropped = [r for r in rows if id(r) not in kept]
    t0 = time.perf_counter()
    order = dashscope_utils.rerank(question, [c.get("chunk_text") or "" for c in candidates],
                                   len(candidates))
    ms = (time.perf_counter() - t0) * 1000
    if not order:
        return candidates + dropped, ms, False
    return [candidates[i] for i in order] + dropped, ms, True


# --- scoring ------------------------------------------------------------------

def patterns(item):
    return [re.compile(p, re.I) for ev in item.get("evidence", []) for p in ev["match_any"]]


def first_hit(texts_with_dates, item):
    """1-based rank of the first text carrying the item's evidence on its date."""
    pats = patterns(item)
    for rank, (text, date) in enumerate(texts_with_dates, 1):
        if date == item["date"] and any(p.search(text or "") for p in pats):
            return rank
    return None


def list_rank(rows, question, item, max_dist=None, order_by_chunk=False):
    """A list row stands for a topic, and clicking it opens that topic, so a row
    carries the evidence when any retrieved chunk grouped under it does -- not
    only the 200-character snippet printed on the row.

    `max_dist` overrides the aggregator's non-lexical distance cut-off for the
    duration of the call; None measures the production value. `order_by_chunk`
    re-orders the aggregator's rows by the earliest position, in `rows`, of any
    chunk grouped under them (used when `rows` is already in reranked order)."""
    saved = lambda_ask_agent._NO_LEX_MAX_DIST
    if max_dist is not None:
        lambda_ask_agent._NO_LEX_MAX_DIST = max_dist
    try:
        if FAMILY is None:
            listed = lambda_ask_agent._aggregate_topics(rows, question)
        else:
            listed = lambda_ask_agent._aggregate_topics(rows, question, family=FAMILY)
    finally:
        lambda_ask_agent._NO_LEX_MAX_DIST = saved
    members = []
    for row in listed:
        idx = []
        for i, r in enumerate(rows):
            if str(r.get("report_date") or "") != row["report_date"]:
                continue
            if row["topic_id"]:
                if str(r.get("topic_id") or "") == row["topic_id"]:
                    idx.append(i)
            elif not r.get("topic_id") and r.get("chunk_type") == "topic":
                md = r.get("metadata") if isinstance(r.get("metadata"), dict) else {}
                if (md.get("title") or (r.get("chunk_text") or "")[:60]) == row["title"]:
                    idx.append(i)
        members.append((row, idx))
    if order_by_chunk:
        members.sort(key=lambda m: min(m[1]) if m[1] else len(rows))
    shown = [("\n".join(rows[i].get("chunk_text") or "" for i in idx), row["report_date"])
             for row, idx in members]
    return first_hit(shown, item), len(listed)


def score_question(searcher, item, today):
    q = item["question"]
    vec, embed_ms = searcher.embed(q)
    sql_ms = 0.0
    fetched = {}
    for k in sorted({ASK_K, LIST_K} | set(LIST_RERANK_SYSTEMS.values())):
        fetched[k], ms = searcher.run(q, vec, k, today)
        sql_ms += ms
    as_text = lambda rs: [(r.get("chunk_text"), str(r.get("report_date") or "")) for r in rs]
    list_rows = fetched[LIST_K]
    raw = first_hit(as_text(list_rows), item)

    # What Ask feeds the model today (ENABLE_RERANK off): k=5 -> _admit_one_lexical_hit.
    ask_prod = first_hit(as_text(lambda_ask_agent._admit_one_lexical_hit(fetched[ASK_K], ASK_K)), item)
    list_prod, n_prod = list_rank(list_rows, q, item)
    rolled = link_windows(list_rows, searcher.topics, rollup=True)
    list_roll, n_roll = list_rank(rolled, q, item)

    systems = {"prod": {"ask": ask_prod, "list": list_prod, "list_rows": n_prod},
               "link-rollup": {"ask": ask_prod, "list": list_roll, "list_rows": n_roll}}
    # Diagnostics for the Ask drop. prod-no-admit: the k=5 search's own order, without
    # _admit_one_lexical_hit. vector-only@5: the vector arm alone (the first 5
    # non-keyword rows of the k=30 search, which is the vec CTE's own cosine order).
    vec30 = [r for r in list_rows if not r.get("lexical_hit")]
    vec5 = vec30[:ASK_K]
    no_admit = fetched[ASK_K][:ASK_K]
    systems["prod-no-admit"] = {"ask": first_hit(as_text(no_admit), item), "list": list_prod, "list_rows": n_prod}
    systems["vector-only@5"] = {"ask": first_hit(as_text(vec5), item), "list": list_prod, "list_rows": n_prod}
    pats = patterns(item)
    carries = lambda r: str(r.get("report_date") or "") == item["date"] and any(
        p.search(r.get("chunk_text") or "") for p in pats)
    brief = lambda r: {"chunk_type": r.get("chunk_type"), "lexical_hit": bool(r.get("lexical_hit")),
                       "distance": r.get("distance"), "text80": (r.get("chunk_text") or "")[:80],
                       "evidence": carries(r)}
    ev_vec_rank = next((i for i, r in enumerate(vec30, 1) if carries(r)), None)
    diag = {"ask_prod_rows": [brief(r) for r in lambda_ask_agent._admit_one_lexical_hit(fetched[ASK_K], ASK_K)],
            "no_admit_rows": [brief(r) for r in no_admit],
            "vector_arm_evidence_rank_in_30": ev_vec_rank,
            "k5_search_rows": len(fetched[ASK_K]),
            "k5_keyword_rows": sum(bool(r.get("lexical_hit")) for r in fetched[ASK_K])}
    rerank_ms, rerank_ok, rerank_cands = {}, {}, {}
    for name, k in LIST_RERANK_SYSTEMS.items():
        ordered, ms, ok = rerank_all(q, fetched[k])
        ask_rr = first_hit(as_text(ordered[:ASK_K]), item)
        # The list path (mode=search) is k=30 and has no reranker today; this is the
        # hypothetical list built from the same candidates in reranked order.
        list_rr, n_rr = list_rank(ordered, q, item, order_by_chunk=True)
        systems[name] = {"ask": ask_rr, "list": list_rr, "list_rows": n_rr}
        rerank_ms[name], rerank_ok[name], rerank_cands[name] = ms, ok, len(fetched[k])
    return {"id": item["id"], "lang": item.get("lang"), "expect": item.get("expect"),
            "category": item.get("category"), "style": item.get("style"),
            "raw": raw, "systems": systems, "rerank_ms": rerank_ms, "rerank_ok": rerank_ok,
            "rerank_rows": rerank_cands, "diag": diag,
            "keyword_rows_k30": sum(bool(r.get("lexical_hit")) for r in list_rows),
            "best_distance": min((r["distance"] for r in list_rows if r["distance"] is not None),
                                 default=None),
            "embed_ms": embed_ms, "sql_ms": sql_ms}


def summarise(results):
    ans = [r for r in results if r["expect"] == "answerable"]
    pct = lambda n: round(100.0 * n / (len(ans) or 1), 1)
    out = {"answerable": len(ans), "raw_hit_pct": pct(sum(r["raw"] is not None for r in ans)),
           "systems": {}}
    for s in SYSTEMS:
        out["systems"][s] = {
            "ask_hit_pct": pct(sum(r["systems"][s]["ask"] is not None for r in ans)),
            "list_hit_pct": pct(sum(r["systems"][s]["list"] is not None for r in ans)),
            "list_rows_median": statistics.median(r["systems"][s]["list_rows"] for r in results),
            "zh_list_hits": sum(r["systems"][s]["list"] is not None for r in ans if r["lang"] == "zh"),
        }
    # Slices: what the question is about, and whether it was typed cleanly. An
    # item without `style` is one of the original, well-formed questions.
    for field, default in (("category", "uncategorised"), ("style", "clean")):
        groups = {}
        for r in ans:
            key = r.get(field) or default
            if field == "style" and key != "clean":
                key = "messy"
            groups.setdefault(key, []).append(r)
        out["by_" + field] = {
            key: {"n": len(g), **{s: "%d/%d" % (sum(x["systems"][s]["ask"] is not None for x in g),
                                                 sum(x["systems"][s]["list"] is not None for x in g))
                                  for s in SYSTEMS}}
            for key, g in sorted(groups.items())}
    ctl = [r for r in results if r["expect"] == "unanswerable"]
    out["controls_with_list_rows"] = {s: sum(r["systems"][s]["list_rows"] > 0 for r in ctl) for s in SYSTEMS}
    out["embed_ms_p50"] = round(statistics.median(r["embed_ms"] for r in results))
    out["sql_ms_total_p50"] = round(statistics.median(r["sql_ms"] for r in results))
    out["rerank"] = {}
    for name in LIST_RERANK_SYSTEMS:
        ms = sorted(r["rerank_ms"][name] for r in results)
        out["rerank"][name] = {
            "p50_ms": round(statistics.median(ms)),
            "p90_ms": round(ms[min(len(ms) - 1, int(0.9 * len(ms)))]),
            "max_ms": round(ms[-1]),
            "over_3s": sum(m > PROD_RERANK_BUDGET_MS for m in ms),
            "failed": sum(not r["rerank_ok"][name] for r in results)}
    return out


def signature(run):
    return [(r["raw"], tuple((s, r["systems"][s]["ask"], r["systems"][s]["list"]) for s in SYSTEMS))
            for r in run["results"]]


def main():
    global DATABASE, SYSTEMS, FAMILY
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--gold", required=True)
    ap.add_argument("--label", required=True, help="short name for this configuration")
    ap.add_argument("--repeat", type=int, default=2)
    ap.add_argument("--site", default="4326dbc7-14a2-4251-8381-9d0067595a85")
    ap.add_argument("--author", default="f531eb99-d79c-4e6c-bac1-ff944b802349")
    ap.add_argument("--today", default=datetime.now(timezone.utc).date().isoformat())
    ap.add_argument("--out", default=str(HERE / "results"))
    ap.add_argument("--diagnose", action="store_true",
                    help="Ask-drop diagnosis: no reranker calls; systems prod, prod-no-admit, vector-only@5")
    ap.add_argument("--database", default=DATABASE, help="`fieldsight` = prod, `fieldsight_test` = TEST")
    ap.add_argument("--family", choices=["report", "session"], default=None,
                    help="index family to read, as SEARCH_INDEX_READ; omit for no family filter")
    args = ap.parse_args()
    DATABASE = args.database
    FAMILY = args.family
    if args.diagnose:
        LIST_RERANK_SYSTEMS.clear()
        SYSTEMS = ["prod", "prod-no-admit", "vector-only@5"]

    gold_text = Path(args.gold).read_text(encoding="utf-8")
    items = [json.loads(line) for line in gold_text.splitlines() if line.strip()]
    searcher = Searcher(args.site, args.author)
    today = datetime.fromisoformat(args.today).date()

    runs = []
    for i in range(args.repeat):
        results = []
        for item in items:
            r = score_question(searcher, item, today)
            results.append(r)
            print("run %d %-6s raw=%-4s " % (i + 1, item["id"], r["raw"])
                  + " ".join("%s=%s/%s" % (s, r["systems"][s]["ask"], r["systems"][s]["list"]) for s in SYSTEMS)
                  + "  rr=%s" % "/".join("%dms" % v for v in r["rerank_ms"].values()))
        runs.append({"results": results, "summary": summarise(results)})

    stable = all(signature(run) == signature(runs[0]) for run in runs[1:])
    fingerprint = {
        "git_commit": git_commit(),
        "src_ref": SRC_REF, "src_sha": SRC_SHA,
        "embed_model": dashscope_utils.DASHSCOPE_EMBED_MODEL,
        "embed_dim": os.environ.get("DASHSCOPE_EMBED_DIM"),
        "rerank_model": dashscope_utils.DASHSCOPE_RERANK_MODEL,
        "ask_k": ASK_K, "list_k": LIST_K,
        "rerank_candidates": dict(LIST_RERANK_SYSTEMS),
        "rerank_per_session_cap": lambda_ask_agent.RERANK_PER_SESSION_CAP,
        "search_sql_sha256": sha256_text(search_sql.build_search_sql()),
        "aggregator_sha256": sha256_text(inspect.getsource(lambda_ask_agent._aggregate_topics)),
        "no_lex_max_dist": lambda_ask_agent._NO_LEX_MAX_DIST,
        "admit_one_sha256": sha256_text(inspect.getsource(lambda_ask_agent._admit_one_lexical_hit)),
        "link_sha256": sha256_text(inspect.getsource(link_windows)),
        "gold_file": os.path.relpath(args.gold, REPO),
        "gold_sha256": sha256_text(gold_text),
        "database": args.database,
        "source_family": FAMILY,
        "scope": {"site": args.site, "author": args.author},
        "today": args.today,
    }
    record = {"label": args.label, "ran_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
              "fingerprint": fingerprint, "repeat_identical": stable, "runs": runs}

    Path(args.out).mkdir(parents=True, exist_ok=True)
    dest = Path(args.out) / ("%s-%s.json" % (datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ"), args.label))
    dest.write_text(json.dumps(record, ensure_ascii=False, indent=1), encoding="utf-8")

    print("\nsummary per run:")
    for i, run in enumerate(runs, 1):
        print("  run %d  %s" % (i, json.dumps(run["summary"], ensure_ascii=False)))
    print("repeat identical: %s" % stable)
    print("wrote %s" % dest)


if __name__ == "__main__":
    main()
