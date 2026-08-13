"""Unit: the two surfaces a customer checks first, and the grant that makes them work.

Spec: docs/superpowers/specs/2026-08-14-user-deletes-a-recording.md

The request that started this feature named the fear directly: 不能再被别人搜出来. When the
delete endpoint was first written, every OTHER surface hid the content and these two still
returned it verbatim — search because `build_search_sql` had no deleted predicate at all,
Ask because it reads reports and transcripts straight off S3 in a lambda with no database.

The third test is about IAM rather than logic, and it is here because the failure it
prevents is invisible: the mirror write is wrapped in `except Exception: logger.exception`
on purpose (the SQL filters already hide the content, so failing the whole delete would be
worse), so a missing grant produces a successful delete, one WARNING nobody reads, and a
nightly report that still contains the deleted recording.
"""
import json
import os
import re
import sys

import pytest

SRC = os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(
    os.path.abspath(__file__)))), "src")
sys.path.insert(0, SRC)
sys.path.insert(0, os.path.join(SRC, "repositories"))


# ---- search ------------------------------------------------------------

def test_search_sql_carries_both_arms():
    """Both, or neither. The topic arm covers the rows that exist now; the source arm
    covers the ones `lambda_ingest` re-creates overnight with new uuids that no topic-keyed
    tombstone names. A search filter with only the first passes today and leaks tomorrow."""
    import search_sql
    sql = search_sql.build_search_sql()
    assert "r.target_id = c.topic_id" in sql, "the topic arm is missing or keyed on c.id"
    assert "c.source_s3_key LIKE r.target_key" in sql, "the source arm is missing"
    assert sql.count("scope = 'deleted'") == 2
    assert sql.count("reverted_at IS NULL") == 2


def test_the_chunk_predicate_is_keyed_on_topic_id_not_id():
    """`report_chunks` REFERENCES a topic, it is not one. Reusing the topics predicate here
    compares `redactions.target_id` to `report_chunks.id` — the subquery simply never
    matches, no error is raised, and every deleted row stays searchable."""
    import deleted_predicates as dp
    assert "{alias}.topic_id" in dp.DELETED_CHUNK_TOPIC_PREDICATE
    assert "{alias}.id" not in dp.DELETED_CHUNK_TOPIC_PREDICATE


def test_one_definition_of_the_predicate_not_two():
    """`search_sql` is forbidden from importing psycopg and `redactions` imports it, which
    is why the strings live in a third, dependency-free module. The alternative was a second
    copy — and this repo has already shipped a feature that did nothing for weeks because a
    writer and a reader spelled the same key twice and drifted apart."""
    import deleted_predicates as dp
    import redactions as red
    assert red.DELETED_TOPIC_PREDICATE is dp.DELETED_TOPIC_PREDICATE
    assert red.DELETED_SOURCE_PREDICATE is dp.DELETED_SOURCE_PREDICATE
    src = open(os.path.join(SRC, "repositories", "search_sql.py"), encoding="utf-8").read()
    assert "NOT EXISTS" not in src, "search_sql must USE the shared predicate, not copy it"


# ---- Ask ---------------------------------------------------------------

class _AskS3:
    def __init__(self, mirror=None, objects=None):
        self.mirror = mirror or {}
        self.objects = objects or {}

    def get_object(self, Bucket=None, Key=None, **kw):
        payload = self.mirror.get(Key, self.objects.get(Key))
        if payload is None:
            raise KeyError(Key)

        class B:
            def read(self_inner):
                return json.dumps(payload).encode()
        return {"Body": B()}

    def get_paginator(self, _name):
        objects = self.objects

        class P:
            def paginate(self_inner, Bucket=None, Prefix=None, **kw):
                yield {"Contents": [{"Key": k, "Size": 1} for k in objects
                                    if k.startswith(Prefix)]}
        return P()


def _ask(monkeypatch, s3):
    ask = pytest.importorskip("lambda_ask_agent")
    monkeypatch.setattr(ask, "s3_client", s3)
    monkeypatch.setattr(ask.deletion_mirror, "logger", ask.logger, raising=False)
    return ask


MIRROR = "redactions/Ben/2026-08-14/deleted_sessions.json"


def test_ask_serves_no_stored_report_for_a_day_with_a_deletion(monkeypatch):
    """The stored report was written BEFORE the delete and contains the session verbatim.
    Ask has no database, so the only thing standing between the model and that file is the
    mirror."""
    s3 = _AskS3(mirror={MIRROR: {"sessions": ["sid-a"]}},
                objects={"reports/2026-08-14/Ben/daily_report.json": {"topics": ["leak"]}})
    ask = _ask(monkeypatch, s3)
    doc, kind = ask.load_report("b", "2026-08-14", "Ben")
    assert (doc, kind) == (None, None)


def test_ask_still_serves_a_clean_day(monkeypatch):
    """The filter must not take the feature down for everyone who deleted nothing — which
    is nearly every day."""
    s3 = _AskS3(objects={"reports/2026-08-14/Ben/daily_report.json": {"topics": ["fine"]}})
    ask = _ask(monkeypatch, s3)
    doc, kind = ask.load_report("b", "2026-08-14", "Ben")
    assert kind == "daily" and doc["topics"] == ["fine"]


def test_ask_drops_the_deleted_sessions_transcripts(monkeypatch):
    """Ask answers verbatim from these files. A filter anywhere else in the stack does
    nothing here."""
    s3 = _AskS3(mirror={MIRROR: {"sessions": ["sid-a"]}}, objects={
        "transcripts/Ben/2026-08-14/x_sid-a_c0001.json": {"text": "deleted"},
        "transcripts/Ben/2026-08-14/x_sid-b_c0001.json": {"text": "kept"},
    })
    ask = _ask(monkeypatch, s3)
    seen = []
    monkeypatch.setattr(ask, "download_json_from_s3",
                        lambda bucket, key: seen.append(key) or {"text": key})
    monkeypatch.setattr(ask, "normalize_transcript",
                        lambda data, filename, user_mapping=None: None)
    ask.load_transcripts("b", "2026-08-14", "Ben")
    assert not any("sid-a" in k for k in seen), "a deleted session reached the model"
    assert any("sid-b" in k for k in seen), "the kept session was dropped too"


# ---- the grant that makes the mirror real ------------------------------

def test_the_org_api_may_write_the_mirror():
    """This function's S3 grants are all prefix-scoped, so `redactions/*` needs its own.
    Without it the mirror write 403s, the endpoint logs and continues by design, the delete
    reports success, and every reader with no database keeps serving the recording.

    GetObject as well as PutObject: the mirror is MERGED, so a second delete on the same
    day has to read the first one's sessions or it silently un-hides them."""
    tpl = open(os.path.join(SRC, "template.yaml"), encoding="utf-8").read()
    i = tpl.find("\n  OrgApiFunction:")
    assert i > 0
    m = re.search(r"\n  [A-Za-z0-9]+:\n", tpl[i + 5:])
    block = tpl[i:i + 5 + (m.start() if m else len(tpl))]
    stmt = re.search(r"Action:\s*\n\s*- s3:PutObject\s*\n\s*- s3:GetObject\s*\n\s*"
                     r"Resource: !Sub arn:aws:s3:::\$\{DataBucketName\}/redactions/\*",
                     block)
    assert stmt, "org-api cannot write redactions/* — the S3 mirror silently never lands"
