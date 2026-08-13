"""Unit: the pipeline cannot resurrect content a customer deleted.

Plan: docs/superpowers/plans/2026-08-14-user-deletes-a-recording.md phase 6.

This is the finding that decided the design. `lambda_ingest` deletes a day's topics by
source prefix and re-inserts them with NEW uuids whenever the nightly report supersedes the
live extraction. A tombstone naming a topic uuid stops matching within a day, and the
content the customer deleted is back on their dashboard the next morning — with no error
anywhere, because from the pipeline's point of view nothing went wrong.

Two different answers, and the difference matters:

* **item_writer SKIPS.** A live extraction for a deleted source should not create rows at
  all.
* **ingest RE-STAMPS.** The nightly path must let the row EXIST and then hide it, because
  an absent Aurora row is what makes the timeline fall through to the pre-deletion S3
  document (phase 4). A hidden row beats an absent one.
"""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(
    os.path.abspath(__file__)))), "src"))


def test_item_writer_refuses_to_write_topics_for_a_deleted_source(monkeypatch):
    """A live extraction landing for a source the customer deleted must produce nothing."""
    import lambda_item_writer as iw
    import repositories.redactions as red

    monkeypatch.setattr(red, "is_source_deleted", lambda conn, key: True)
    assert hasattr(iw, "_source_is_deleted"), "no guard exists"
    assert iw._source_is_deleted(object(), "extractions/Ben/2026-08-13/x.json") is True


def test_the_skip_is_counted_not_silent(caplog):
    """Positive evidence. Three IAM omissions in this codebase each looked exactly like
    nothing at all, because the only trace of the path executing was its failure."""
    import logging

    import lambda_item_writer as iw
    import repositories.redactions as red

    class _R:
        @staticmethod
        def is_source_deleted(conn, key):
            return True

    orig = red.is_source_deleted
    red.is_source_deleted = _R.is_source_deleted
    try:
        with caplog.at_level(logging.INFO):
            iw._source_is_deleted(object(), "extractions/Ben/2026-08-13/x.json")
    finally:
        red.is_source_deleted = orig
    assert any("deleted" in r.getMessage().lower() for r in caplog.records), \
        "a skip nobody can count is indistinguishable from a guard that never ran"


def test_a_guard_that_cannot_read_its_input_does_not_block_the_pipeline(monkeypatch):
    """If the tombstone table is unreachable, writing the row is the safe direction: the
    read filters still hide it, whereas refusing to write loses the audio's only record."""
    import lambda_item_writer as iw
    import repositories.redactions as red

    def _boom(conn, key):
        raise RuntimeError("connection reset")

    monkeypatch.setattr(red, "is_source_deleted", _boom)
    assert iw._source_is_deleted(object(), "extractions/Ben/2026-08-13/x.json") is False
