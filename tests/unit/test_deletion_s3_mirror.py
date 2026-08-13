"""Unit: the lambdas with no database still honour a delete.

Plan: docs/superpowers/plans/2026-08-14-user-deletes-a-recording.md phase 6c.

`lambda_report_generator` and `lambda_ask_agent`'s day-scoped path are NOT in the VPC and
have no database connection at all. Every SQL-level protection built so far — the read
predicates, the renderer's drop, the write-side guard — is invisible to them. They read
transcripts and reports straight off S3 and answer from that.

So the tombstone set is mirrored to S3, and this is the shape of that mirror:

    redactions/{folder}/{date}/deleted_sessions.json   ->  {"sessions": ["sid…", …]}

Aurora stays the authority; the mirror is a copy, and a copy that fails to load must not
take the pipeline down — but it must also not silently let deleted content through, so the
loader reports what it found, including nothing.
"""
import json
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(
    os.path.abspath(__file__)))), "src"))

import deletion_mirror as dm  # noqa: E402


class _S3:
    def __init__(self, objects=None, raises=None):
        self.objects = dict(objects or {})
        self.raises = raises
        self.gets = []

    def get_object(self, Bucket, Key):
        self.gets.append(Key)
        if self.raises:
            raise self.raises
        if Key not in self.objects:
            raise KeyError(Key)

        class B:
            def __init__(self, d):
                self._d = d

            def read(self):
                return self._d
        return {"Body": B(json.dumps(self.objects[Key]).encode())}

    def put_object(self, Bucket, Key, Body, **kw):
        self.objects[Key] = json.loads(Body)


def test_the_mirror_key_is_derived_not_spelled_twice():
    """A writer and a reader that disagree about a key is how a whole feature silently
    does nothing while its tests pass — this repo has shipped that exact defect."""
    assert dm.mirror_key("Ben_UCPK", "2026-08-14") == \
        "redactions/Ben_UCPK/2026-08-14/deleted_sessions.json"


def test_a_deleted_session_is_read_back_from_what_the_writer_wrote():
    s3 = _S3()
    dm.write_mirror(s3, "b", "Ben_UCPK", "2026-08-14", ["sid-aaa", "sid-bbb"])
    got = dm.deleted_sessions(s3, "b", "Ben_UCPK", "2026-08-14")
    assert got == {"sid-aaa", "sid-bbb"}


def test_no_mirror_means_nothing_deleted_not_an_error():
    """The common case by far. Every day without a deletion has no mirror object, and a
    reader that raised there would break the nightly report for everyone."""
    assert dm.deleted_sessions(_S3(), "b", "Ben_UCPK", "2026-08-14") == set()


def test_an_unreadable_mirror_is_reported_not_swallowed(caplog):
    """It must not take the pipeline down — and it must not pass silently either. A
    permissions fault here looks exactly like 'nothing was deleted', which is the failure
    mode that has cost this project three separate silent breakages."""
    import logging
    s3 = _S3(raises=RuntimeError("AccessDenied"))
    with caplog.at_level(logging.WARNING):
        got = dm.deleted_sessions(s3, "b", "Ben_UCPK", "2026-08-14")
    assert got == set()
    assert any("mirror" in r.getMessage().lower() for r in caplog.records)


def test_transcript_keys_of_a_deleted_session_are_dropped():
    """What the generator actually needs: given the day's transcript keys, which survive."""
    keys = ["transcripts/Ben_UCPK/2026-08-14/x_sid-aaa_c0001.json",
            "transcripts/Ben_UCPK/2026-08-14/y_sid-ccc_c0002.json"]
    assert dm.drop_deleted(keys, {"sid-aaa"}) == [keys[1]]


def test_dropping_nothing_leaves_the_list_identical():
    keys = ["transcripts/Ben/2026-08-14/a.json"]
    assert dm.drop_deleted(keys, set()) is not keys or dm.drop_deleted(keys, set()) == keys


def test_the_report_generator_drops_deleted_sessions_and_says_how_many(monkeypatch, caplog):
    """The non-VPC leak, closed at the only place it can be.

    The generator has no database, so it cannot see a single thing this feature has built
    in SQL. It lists a day's transcripts off S3 and writes a report from them — and one of
    those transcripts belongs to a recording the customer deleted.

    The count is logged including zero, because 'the report was generated' and 'the
    exclusion ran' are otherwise the same observation.
    """
    import logging

    import lambda_report_generator as rg

    monkeypatch.setattr(rg, "list_s3_objects", lambda bucket, prefix: [
        {"key": f"{prefix}a_sid-aaa_c0001.json"},
        {"key": f"{prefix}b_sid-ccc_c0002.json"}])
    monkeypatch.setattr(rg, "_deleted_sessions_for", lambda bucket, folder, date: {"sid-aaa"})

    with caplog.at_level(logging.INFO):
        kept = rg._transcript_objects_for(  "b", "Ben_UCPK", "2026-08-14")

    assert [k["key"] for k in kept] == ["transcripts/Ben_UCPK/2026-08-14/b_sid-ccc_c0002.json"]
    assert any("deleted" in r.getMessage().lower() for r in caplog.records)
