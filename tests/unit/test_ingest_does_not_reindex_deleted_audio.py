"""Unit: the nightly re-ingest must not rebuild a deleted recording's search index.

The delete endpoint archives the chunks that exist at the moment it runs. That is only
half. `_load_turns` lists `transcripts/{folder}/{date}/` and, until this shipped, applied
no deletion filter at all — so the next nightly ingest rebuilt `transcript_window` chunks
of exactly the audio the customer had deleted. The content vanished when they pressed the
button and was searchable again the next morning, which is worse than never hiding it.

Two halves, and they use DIFFERENT sources on purpose:

* `_load_turns` filters on the **S3 mirror**, because `lambda_embed_report` calls the same
  function and is not in the VPC — it has no database. If the two disagree about which
  transcripts to include, the surviving turns pack into different windows, so `chunk_text`
  differs, so its sha256 differs, so every vector lookup misses and the whole report fails
  to ingest. Here the requirement is AGREEMENT, not authority.
* `_archive_deleted_chunks` then applies **Aurora**, after the inserts, closing the gap the
  mirror leaves if its write failed or a delete landed in between.

The last test exists because deleting the sweep's call site broke nothing: the function was
written, and tested, and never invoked. That is a shape this repo has shipped before.
"""
import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(
    os.path.abspath(__file__)))), "src"))

ingest = pytest.importorskip("lambda_ingest")

FOLDER, DATE = "Ben", "2026-08-14"
KEPT = f"transcripts/{FOLDER}/{DATE}/Ben_{DATE}_10-00-00_sid-keep_c0001.json"
GONE = f"transcripts/{FOLDER}/{DATE}/Ben_{DATE}_11-00-00_sid-gone_c0001.json"


class _S3:
    def __init__(self, keys):
        self.keys = keys
        self.read = []

    def get_paginator(self, _n):
        keys = self.keys

        class P:
            def paginate(self, Bucket=None, Prefix=None, **kw):
                yield {"Contents": [{"Key": k} for k in keys if k.startswith(Prefix)]}
        return P()

    def get_object(self, Bucket=None, Key=None, **kw):
        self.read.append(Key)

        class B:
            def read(self_inner):
                return b'{"speaker_turns": []}'
        return {"Body": B()}


def test_a_deleted_sessions_transcript_is_not_chunked(monkeypatch):
    s3 = _S3([KEPT, GONE])
    monkeypatch.setattr(ingest, "_s3_client", s3)
    monkeypatch.setattr(ingest, "S3_BUCKET", "b")
    monkeypatch.setattr(ingest.deletion_mirror, "deleted_sessions",
                        lambda *a, **k: {"sid-gone"})
    monkeypatch.setattr(ingest, "normalize_transcript", lambda *a, **k: None)
    monkeypatch.setattr(ingest.agent_turn_filter, "apply_agent_filter",
                        lambda turns, *a, **k: (turns, {}))

    ingest._load_turns(FOLDER, DATE)
    assert GONE not in s3.read, "re-indexed a recording the customer deleted"
    assert KEPT in s3.read, "dropped a recording nobody deleted"


def test_the_filter_reads_the_mirror_not_the_database(monkeypatch):
    """`lambda_embed_report` calls this function with no database. Reading Aurora here
    would make the two chunk builds disagree, and a disagreement is not a partial failure:
    every transcript-window hash misses and the report does not ingest at all."""
    s3 = _S3([KEPT])
    monkeypatch.setattr(ingest, "_s3_client", s3)
    monkeypatch.setattr(ingest, "S3_BUCKET", "b")
    monkeypatch.setattr(ingest, "normalize_transcript", lambda *a, **k: None)
    monkeypatch.setattr(ingest.agent_turn_filter, "apply_agent_filter",
                        lambda turns, *a, **k: (turns, {}))
    called = []
    monkeypatch.setattr(ingest.redactions, "deleted_source_prefixes",
                        lambda *a, **k: called.append(a) or [])
    monkeypatch.setattr(ingest.deletion_mirror, "deleted_sessions", lambda *a, **k: set())

    ingest._load_turns(FOLDER, DATE)
    assert called == [], "_load_turns must not touch the database — embed-report has none"


def test_the_sweep_archives_what_the_mirror_let_through(monkeypatch):
    """Aurora's veto. The mirror is a cache: if its write failed, or a delete landed
    between embed-report and this run, deleted audio was re-chunked and only the database
    knows."""
    monkeypatch.setattr(ingest.redactions, "deleted_source_prefixes",
                        lambda *a, **k: [f"extractions/{FOLDER}/{DATE}/sid-gone"])
    monkeypatch.setattr(ingest.redactions, "list_deleted_batches_for_prefix",
                        lambda *a, **k: [{"batch_id": "b-1", "company_id": "c-1"}])
    seen = {}

    def _archive(conn, base, topic_ids, batch_id):
        seen["base"], seen["batch"] = base, batch_id
        return 3

    monkeypatch.setattr(ingest.chunks, "archive_chunks_for_session", _archive)
    ingest._archive_deleted_chunks(object(), FOLDER, DATE)
    assert seen["base"] == "sid-gone", "the prefix must be reduced to the session base"
    assert seen["batch"] == "b-1", \
        "rows must join the delete's batch or the undelete cannot restore them"


def test_an_unreadable_redactions_table_does_not_fail_the_nightly_run(monkeypatch):
    """A backstop that takes the nightly report down for every user to protect one
    deletion is the wrong trade. It still must not pass silently — but that is the log's
    job, not an exception's."""
    def _boom(*a, **k):
        raise RuntimeError("no db")

    monkeypatch.setattr(ingest.redactions, "deleted_source_prefixes", _boom)
    ingest._archive_deleted_chunks(object(), FOLDER, DATE)   # must not raise


def test_the_sweep_is_actually_called(monkeypatch):
    """Removing the call site broke no test: the function was written, tested, and never
    invoked. This repo has shipped that exact shape before, so the CALL is pinned here by
    reading the source rather than by trusting the suite to notice."""
    src = open(os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(
        os.path.abspath(__file__)))), "src", "lambda_ingest.py"), encoding="utf-8").read()
    body = src.split("def _archive_deleted_chunks", 1)[1]
    assert "_archive_deleted_chunks(conn, user_folder, date)" in body, \
        "the post-ingest deletion sweep is defined but never called"
