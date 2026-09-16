"""Contract test across the Ask Agent -> rag-search seam (spec 2026-09-15).

Every other scoped-Ask test drives ONE side with the other stubbed:
tests/unit/test_ask_scoped.py hand-types rag-search's return shape, and
tests/unit/test_rag_search_scoped_ask.py calls rag-search directly with an
event dict typed by hand. Both can be green while the two sides disagree
about a key's name -- neither test would notice.

This test builds a fully scoped Ask request through
`lambda_ask_agent._rag_answer`, captures the EXACT payload the Ask Agent's
lambda client sends, feeds that same payload into the REAL
`lambda_rag_search._search` (fake DB connection / monkeypatched
`scope.visible_scope`, `users.get_by_folder_name`, `topics.get_topic_visible`,
`chunks.search_chunks` -- mirrors the `wired` fixture in
test_rag_search_scoped_ask.py), and feeds the real `_search` result back
through the Ask Agent path so `_applied_scope` and `build_rag_prompt` consume
rag-search's REAL return shape. A key renamed on either side of the seam --
Ask sending `author` under a different name, or rag-search returning
`pinned_topic` under a different name -- must fail here even though both
sides' own unit tests stay green (see the module docstring's mutation note in
each of those two files).
"""
import io
import json
import os

import pytest

os.environ.setdefault("RAG_SEARCH_FUNCTION", "fieldsight-test-rag-search")

import lambda_ask_agent as laa   # noqa: E402
import lambda_rag_search as rag  # noqa: E402
import llm_utils                 # noqa: E402
import dashscope_utils           # noqa: E402
import web_answer                # noqa: E402

TOPIC_ID = "df023596-1111-4222-8333-444455556666"
SITE_ID = "5c0e8d7a-1111-4222-8333-444455556666"
OTHER_SITE_ID = "0a0b0c0d-1111-4222-8333-444455556666"
NOW = "2026-09-15T09:00:00+00:00"

CALLER = {"id": "u-1", "cognito_sub": "sub-1", "company_id": "c-1",
          "email": "a@x.nz", "first_name": "A", "last_name": "B",
          "global_role": "site_manager"}

ROW = {"id": "c-1", "site_id": SITE_ID, "topic_id": None, "report_date": "2026-09-03",
       "chunk_text": "Scaffold tagged.", "chunk_type": "topic", "distance": 0.1,
       "site_name": "UC PK", "site_slug": "uc-pk", "source_s3_key": "reports/x.json",
       "metadata": {}, "topic_title": "Scaffold", "topic_summary": ""}

PINNED = {"id": TOPIC_ID, "title": "Scaffold handover", "summary": "Signed off.",
          "report_date": "2026-09-03", "site_id": SITE_ID, "site_name": "UC PK",
          "user_id": "u-7", "time_range": "09:10-09:40",
          "action_items": [{"text": "Send tag photos", "responsible": "Ben",
                            "deadline": None, "status": "open"}]}

HEADER = "Pinned topic · UC PK · 2026-09-03 · Scaffold handover"


class FakeConn:
    pass


def wire_rag(mp, *, sites, authors, topic_result=None, rows=(ROW,)):
    """Mirrors the `wired` fixture in test_rag_search_scoped_ask.py -- the
    real lambda_rag_search._search runs against these doubles, not a stub of
    _search itself."""
    mp.setattr(rag, "get_cached_connection", lambda *a, **k: FakeConn())
    mp.setattr(rag, "close_cached_connection", lambda *a, **k: None)
    mp.setattr(rag.users, "get_user_by_sub", lambda conn, sub: dict(CALLER))
    mp.setattr(rag.aliases, "list_active", lambda conn, cid, site_ids=None: [])
    mp.setattr(rag.sites, "get_company_site_by_slug", lambda conn, cid, slug: None)
    mp.setattr(rag.scope, "visible_scope", lambda conn, caller: {
        "site_ids": set(sites), "author_ids": set(authors) if authors is not None else None,
        "cross_company": False})
    mp.setattr(rag.users, "get_by_folder_name",
              lambda conn, cid, folder: {"id": "u-7"} if folder == "Ben_UCPK2" else None)
    mp.setattr(rag.topics, "get_topic_visible",
              lambda conn, topic_id, site_ids, author_ids:
                  dict(topic_result) if topic_result else None)
    mp.setattr(rag.chunks, "search_chunks",
              lambda conn, qv, site_ids, k=5, author_ids=None, date_from=None, date_to=None:
                  [dict(r) for r in rows])


class RealRagLambdaClient:
    """Stands in for boto3's Lambda client on the Ask Agent side: captures
    the exact payload the Ask Agent sends, then runs it through the REAL
    lambda_rag_search._search instead of a canned response."""

    def __init__(self):
        self.calls = []

    def invoke(self, FunctionName, InvocationType, Payload):
        payload = json.loads(Payload)
        self.calls.append(payload)
        result = rag._search(payload, None)
        return {"Payload": io.BytesIO(json.dumps(result).encode("utf-8"))}


def wire_ask(mp, answer=("Grounded answer [1].", None)):
    mp.setattr(dashscope_utils, "embed", lambda texts, dim=None: [[0.1] * 1024])
    client = RealRagLambdaClient()
    mp.setattr(laa, "_get_lambda_client", lambda: client)
    seen = {"llm_calls": 0}

    def fake_llm(prompt, max_tokens=4096, force_json=False):
        seen["prompt"] = prompt
        seen["llm_calls"] += 1
        return answer

    mp.setattr(llm_utils, "call_llm", fake_llm)
    mp.setattr(web_answer, "answer", lambda question, chunks, **k: None)
    return client, seen


def ask(**body):
    body.setdefault("caller_sub", "sub-1")
    body.setdefault("tz", "Pacific/Auckland")
    body.setdefault("now", NOW)
    return laa._rag_answer(body)


def drops(items):
    return {(d["field"], d["reason"]) for d in items}


def test_full_scope_with_a_visible_topic_crosses_the_real_seam(monkeypatch):
    """topic_row_id + site_id + author_folder + date, topic visible: rag-search
    overrides the requested site/author (spec §4.3.1) with the topic's own,
    and the pinned block reaches the prompt through the REAL wire format."""
    wire_rag(monkeypatch, sites={SITE_ID, OTHER_SITE_ID}, authors={"u-1", "u-7"},
            topic_result=PINNED)
    client, seen = wire_ask(monkeypatch)

    out = ask(question="Who is responsible for follow-ups?", topic_row_id=TOPIC_ID,
              site_id=OTHER_SITE_ID, author_folder="Someone_Else", date="2026-08-01",
              scoped=True)

    sent = client.calls[0]
    assert sent["topic_row_id"] == TOPIC_ID
    assert sent["site"] == OTHER_SITE_ID
    assert sent["author"] == "Someone_Else"
    assert (sent["date_from"], sent["date_to"]) == ("2026-08-01", "2026-08-01")

    applied = out["applied_scope"]
    assert applied["topic_row_id"] == TOPIC_ID
    assert applied["topic_title"] == "Scaffold handover"
    assert applied["site_id"] == SITE_ID          # the topic's site, not the requested one
    assert applied["date"] == "2026-09-03"         # the topic's day, not the requested one
    assert drops(applied["dropped"]) == {
        ("site_id", "overridden_by_topic"), ("author_folder", "overridden_by_topic"),
        ("date", "overridden_by_topic")}

    assert seen["llm_calls"] == 1
    assert HEADER in seen["prompt"]
    assert out["answer"] == "Grounded answer [1]."


def test_site_and_author_scope_with_no_topic_crosses_the_real_seam(monkeypatch):
    """site_id + author_folder + date, no topic: rag-search narrows retrieval
    to exactly what was requested, and applied_scope/the prompt reflect the
    REAL (un-pinned) return shape."""
    wire_rag(monkeypatch, sites={SITE_ID}, authors=None)
    client, seen = wire_ask(monkeypatch)

    out = ask(question="concrete issues", site_id=SITE_ID, author_folder="Ben_UCPK2",
              date="2026-09-03", scoped=True)

    sent = client.calls[0]
    assert sent["site"] == SITE_ID
    assert sent["author"] == "Ben_UCPK2"
    assert "topic_row_id" not in sent
    assert (sent["date_from"], sent["date_to"]) == ("2026-09-03", "2026-09-03")

    assert out["applied_scope"] == {"site_id": SITE_ID, "author_folder": "Ben_UCPK2",
                                    "date": "2026-09-03", "dropped": []}
    assert seen["llm_calls"] == 1
    assert "Pinned topic" not in seen["prompt"]
    assert out["answer"] == "Grounded answer [1]."
