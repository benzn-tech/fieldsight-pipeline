#!/usr/bin/env python3
"""Score FieldSight retrieval against a frozen, hand-judged question set.

WHAT THIS MEASURES
------------------
Whether the evidence for a question reaches the two places a person meets it:

  ask     the chunks Ask puts in front of the model (top 5)
  list    the rows the search box shows (production `_aggregate_topics`)

and, as the ceiling both are drawn from:

  raw     did the evidence come back from the top-30 vector search at all

for several SYSTEMS evaluated on the same retrieved rows, so they differ only in
what they are meant to differ in:

  prod          today: Ask = top 5 by distance; list = aggregator on top 30.
  rerank        Ask = production `_rerank_chunks` path (32 candidates, at most
                4 per recording, qwen3-rerank) -> top 5. List unchanged: the
                reranker is not on the list path.
  link          list only: transcript windows with no topic are linked to the
                Aurora topic of the same author and date whose time_range
                overlaps their window_span most, before aggregation.
  link-rollup   `link`, and windows that still overlap nothing are kept under a
                one-hour time-block row instead of being dropped.
  link-rollup+rerank   both changes at once.

Everything that exists in production is imported, not re-implemented: the search
SQL is `repositories.search_sql.build_search_sql()` (including the deleted-
recording predicates), the list is `lambda_ask_agent._aggregate_topics`, and the
rerank is `lambda_ask_agent._diversify` + `dashscope_utils.rerank`. The SQL is
re-spelled for the RDS Data API and every rewrite is asserted, so if production
SQL changes shape this fails loudly instead of quietly measuring another search.
`link` and `link-rollup` are proposals, so they are the only code written here.

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
  counts how often that would have happened from here.

USAGE
-----
    export AWS_PROFILE=fieldsight-deployer AWS_DEFAULT_REGION=ap-southeast-2
    export DASHSCOPE_API_KEY=...
    python tools/retrieval-eval/run.py --gold tools/retrieval-eval/gold/v1.draft.jsonl \
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
SRC = REPO / "src"

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
sys.path.insert(0, str(SRC))

import boto3  # noqa: E402
import dashscope_utils  # noqa: E402
import lambda_ask_agent  # noqa: E402
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
LIST_K = 30          # lambda_ask_agent._rag_search_list: body.get("k", 30)
PROD_RERANK_BUDGET_MS = 3000
SYSTEMS = ["prod", "rerank", "link", "link-rollup", "link-rollup+rerank", "link-rollup@0.65"]

REWRITES = [
    ("%(q)s::vector", "CAST(:q AS vector)"),
    ("ANY(%(site_ids)s)", "ANY(CAST(:site_ids AS uuid[]))"),
    ("%(author_ids)s::uuid[]", "CAST(:author_ids AS uuid[])"),
    ("%(date_from)s::date", "CAST(:date_from AS date)"),
    ("%(date_to)s::date", "CAST(:date_to AS date)"),
    ("%(k)s", ":k"),
    ("%%", "%"),
]


def data_api_sql():
    sql = search_sql.build_search_sql()
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

    def run(self, question, k, today):
        date_from, date_to = query_slots.time_range(question, today)
        t0 = time.perf_counter()
        vec = dashscope_utils.embed([question])[0]
        t_embed = time.perf_counter() - t0
        params = [
            {"name": "q", "value": {"stringValue": "[" + ",".join("%.7f" % x for x in vec) + "]"}},
            {"name": "site_ids", "value": {"stringValue": "{%s}" % self.site}},
            {"name": "author_ids", "value": {"stringValue": "{%s}" % self.author}},
            {"name": "date_from", "value": {"stringValue": date_from} if date_from else {"isNull": True}},
            {"name": "date_to", "value": {"stringValue": date_to} if date_to else {"isNull": True}},
            {"name": "k", "value": {"longValue": k}},
        ]
        t0 = time.perf_counter()
        rows = self._query(self.sql, params)
        t_sql = time.perf_counter() - t0
        for row in rows:
            if isinstance(row.get("metadata"), str):
                row["metadata"] = json.loads(row["metadata"])
            row["distance"] = float(row["distance"])
        return rows, {"embed_ms": t_embed * 1000, "sql_ms": t_sql * 1000}


# --- proposed systems ---------------------------------------------------------

def link_windows(rows, topics_by_date, rollup):
    """Copies of `rows` where topic-less transcript windows carry the Aurora topic
    they overlap most in time. With `rollup`, windows that overlap nothing become
    a one-hour time-block row rather than being dropped by the aggregator."""
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
                hour = span[0] // 3600
                r["chunk_type"] = "topic"
                r["metadata"] = dict(md, title="Recordings %02d:00–%02d:00" % (hour, hour + 1))
        out.append(r)
    return out


def rerank_top(question, rows):
    candidates = lambda_ask_agent._diversify(rows, lambda_ask_agent.RERANK_PER_SESSION_CAP)
    t0 = time.perf_counter()
    order = dashscope_utils.rerank(question, [c.get("chunk_text") or "" for c in candidates], ASK_K)
    ms = (time.perf_counter() - t0) * 1000
    if not order:
        return candidates[:ASK_K], ms, False
    return [candidates[i] for i in order[:ASK_K]], ms, True


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


def list_rank(rows, question, item, max_dist=None):
    """A list row stands for a topic, and clicking it opens that topic, so a row
    carries the evidence when any retrieved chunk grouped under it does -- not
    only the 200-character snippet printed on the row.

    `max_dist` overrides the aggregator's non-lexical distance cut-off for the
    duration of the call; None measures the production value."""
    saved = lambda_ask_agent._NO_LEX_MAX_DIST
    if max_dist is not None:
        lambda_ask_agent._NO_LEX_MAX_DIST = max_dist
    try:
        listed = lambda_ask_agent._aggregate_topics(rows, question)
    finally:
        lambda_ask_agent._NO_LEX_MAX_DIST = saved
    shown = []
    for row in listed:
        texts = []
        for r in rows:
            if str(r.get("report_date") or "") != row["report_date"]:
                continue
            if row["topic_id"]:
                if str(r.get("topic_id") or "") == row["topic_id"]:
                    texts.append(r.get("chunk_text") or "")
            elif not r.get("topic_id") and r.get("chunk_type") == "topic":
                md = r.get("metadata") if isinstance(r.get("metadata"), dict) else {}
                if (md.get("title") or (r.get("chunk_text") or "")[:60]) == row["title"]:
                    texts.append(r.get("chunk_text") or "")
        shown.append(("\n".join(texts), row["report_date"]))
    return first_hit(shown, item), len(listed)


def score_question(searcher, item, today):
    rows, timing = searcher.run(item["question"], max(LIST_K, lambda_ask_agent.RERANK_CANDIDATES), today)
    top30 = rows[:LIST_K]
    as_text = lambda rs: [(r.get("chunk_text"), str(r.get("report_date") or "")) for r in rs]
    raw = first_hit(as_text(top30), item)

    reranked, rerank_ms, rerank_ok = rerank_top(item["question"], rows)
    linked = link_windows(top30, searcher.topics, rollup=False)
    rolled = link_windows(top30, searcher.topics, rollup=True)

    ask_prod = first_hit(as_text(rows[:ASK_K]), item)
    ask_rr = first_hit(as_text(reranked), item)
    list_prod, n_prod = list_rank(top30, item["question"], item)
    list_link, n_link = list_rank(linked, item["question"], item)
    list_roll, n_roll = list_rank(rolled, item["question"], item)
    list_roll65, n_roll65 = list_rank(rolled, item["question"], item, max_dist=0.65)

    systems = {
        "prod": {"ask": ask_prod, "list": list_prod, "list_rows": n_prod},
        "rerank": {"ask": ask_rr, "list": list_prod, "list_rows": n_prod},
        "link": {"ask": ask_prod, "list": list_link, "list_rows": n_link},
        "link-rollup": {"ask": ask_prod, "list": list_roll, "list_rows": n_roll},
        "link-rollup+rerank": {"ask": ask_rr, "list": list_roll, "list_rows": n_roll},
        "link-rollup@0.65": {"ask": ask_prod, "list": list_roll65, "list_rows": n_roll65},
    }
    return {"id": item["id"], "lang": item.get("lang"), "expect": item.get("expect"),
            "category": item.get("category"), "style": item.get("style"),
            "raw": raw, "systems": systems, "rerank_ms": rerank_ms, "rerank_ok": rerank_ok,
            "best_distance": rows[0]["distance"] if rows else None, **timing}


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
    out["sql_ms_p50"] = round(statistics.median(r["sql_ms"] for r in results))
    rr = sorted(r["rerank_ms"] for r in results)
    out["rerank_ms_p50"] = round(statistics.median(rr))
    out["rerank_ms_max"] = round(rr[-1])
    out["rerank_over_budget"] = sum(ms > PROD_RERANK_BUDGET_MS for ms in rr)
    out["rerank_failed"] = sum(not r["rerank_ok"] for r in results)
    return out


def signature(run):
    return [(r["raw"], tuple((s, r["systems"][s]["ask"], r["systems"][s]["list"]) for s in SYSTEMS))
            for r in run["results"]]


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--gold", required=True)
    ap.add_argument("--label", required=True, help="short name for this configuration")
    ap.add_argument("--repeat", type=int, default=2)
    ap.add_argument("--site", default="4326dbc7-14a2-4251-8381-9d0067595a85")
    ap.add_argument("--author", default="f531eb99-d79c-4e6c-bac1-ff944b802349")
    ap.add_argument("--today", default=datetime.now(timezone.utc).date().isoformat())
    ap.add_argument("--out", default=str(HERE / "results"))
    args = ap.parse_args()

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
                  + "  rr=%dms" % r["rerank_ms"])
        runs.append({"results": results, "summary": summarise(results)})

    stable = all(signature(run) == signature(runs[0]) for run in runs[1:])
    fingerprint = {
        "git_commit": git_commit(),
        "embed_model": dashscope_utils.DASHSCOPE_EMBED_MODEL,
        "embed_dim": os.environ.get("DASHSCOPE_EMBED_DIM"),
        "rerank_model": dashscope_utils.DASHSCOPE_RERANK_MODEL,
        "ask_k": ASK_K, "list_k": LIST_K,
        "rerank_candidates": lambda_ask_agent.RERANK_CANDIDATES,
        "rerank_per_session_cap": lambda_ask_agent.RERANK_PER_SESSION_CAP,
        "search_sql_sha256": sha256_text(search_sql.build_search_sql()),
        "aggregator_sha256": sha256_text(inspect.getsource(lambda_ask_agent._aggregate_topics)),
        "link_sha256": sha256_text(inspect.getsource(link_windows)),
        "gold_file": os.path.relpath(args.gold, REPO),
        "gold_sha256": sha256_text(gold_text),
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
