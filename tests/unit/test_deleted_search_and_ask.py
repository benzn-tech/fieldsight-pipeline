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
    # Task 3 (2026-09-20 plan) unioned a keyword arm into build_search_sql,
    # built from the SAME _scope_predicate as the vector arm -- so each of the
    # two tombstone sub-predicates (topic, source) now appears once per SQL
    # arm: 2 arms x 2 sub-predicates = 4, not 2. The thing this test protects
    # (both tombstone arms present, on EVERY read arm) is unchanged; only the
    # count of arms in the query grew.
    assert sql.count("scope = 'deleted'") == 4
    assert sql.count("reverted_at IS NULL") == 4


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


# ---- Ask: the stored-report path is gone ----------------------------------
# (Ask used to read stored reports/transcripts from S3 behind the mirror; that
# path was removed with the legacy gateway, so only retrieval -- filtered by the
# SQL arms above -- remains. See test_legacy_gateway_closed.py.)


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
