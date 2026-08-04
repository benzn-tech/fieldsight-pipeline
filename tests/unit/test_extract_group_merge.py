"""
Unit: assembling a multi-device meeting for extraction (spec 2026-08-04 §5).

Each member is assembled with the existing per-session path and then kept
SEPARATE, as labelled parallel sources. They are deliberately not concatenated
and not time-merged: across devices there is no shared clock to merge on —
assemble_deduped_turns orders turns on "the single session clock", and BUG-37
is a shipped case of a device's wall clock being 12 hours out. Alignment has to
be content-based, which is what the extraction LLM does natively in a call the
pipeline was going to make anyway.

The failure behaviour matters as much as the happy path: losing one device must
never lose the meeting.
"""
import pytest

ex = pytest.importorskip("lambda_extract_session",
                         reason="requires the lambda deps (installed in CI)")

SID_A = "a" * 32
SID_B = "b" * 32


def test_single_member_group_matches_the_solo_path(monkeypatch):
    """A group of one must not take a different code path — that is how the
    common case silently regresses."""
    monkeypatch.setattr(ex, "assemble_deduped_turns",
                        lambda b, k: ([{"text": "hello"}], ["f1.json"]))

    sources, files = ex.assemble_group_turns("bkt", {SID_A: ["k1"]})

    assert len(sources) == 1
    assert sources[0]["session_id"] == SID_A
    assert sources[0]["turns"] == [{"text": "hello"}]
    assert files == ["f1.json"]


def test_each_member_stays_a_separate_labelled_source(monkeypatch):
    """The merge is performed by the LLM, which needs to see which device heard
    what. Concatenating the turns would destroy exactly that signal."""
    monkeypatch.setattr(ex, "assemble_deduped_turns",
                        lambda bucket, keys: ([{"text": keys[0]}], [keys[0] + ".json"]))

    sources, files = ex.assemble_group_turns("bkt", {SID_A: ["A"], SID_B: ["B"]})

    assert [s["session_id"] for s in sources] == [SID_A, SID_B]
    assert sources[0]["turns"] != sources[1]["turns"]
    assert set(files) == {"A.json", "B.json"}


def test_member_with_no_usable_transcript_is_dropped_not_fatal(monkeypatch):
    """One device's audio being corrupt must not lose the whole meeting."""
    def fake(bucket, keys):
        return ([], []) if keys == ["bad"] else ([{"text": "ok"}], ["good.json"])
    monkeypatch.setattr(ex, "assemble_deduped_turns", fake)

    sources, files = ex.assemble_group_turns("bkt", {SID_A: ["bad"], SID_B: ["good"]})

    assert [s["session_id"] for s in sources] == [SID_B]
    assert files == ["good.json"]


def test_member_that_raises_is_skipped(monkeypatch):
    """An S3 failure on one member is counted and stepped over — raising would
    abort the batch and lose the devices that did work."""
    def fake(bucket, keys):
        if keys == ["boom"]:
            raise RuntimeError("s3 down")
        return ([{"text": "ok"}], ["good.json"])
    monkeypatch.setattr(ex, "assemble_deduped_turns", fake)

    sources, _ = ex.assemble_group_turns("bkt", {SID_A: ["boom"], SID_B: ["good"]})

    assert [s["session_id"] for s in sources] == [SID_B]


def test_group_with_nothing_usable_returns_empty(monkeypatch):
    monkeypatch.setattr(ex, "assemble_deduped_turns", lambda b, k: ([], []))

    sources, files = ex.assemble_group_turns("bkt", {SID_A: ["x"]})

    assert sources == []
    assert files == []


def test_member_order_is_deterministic(monkeypatch):
    """Prompt input order must not vary run to run, or the same meeting yields
    different extractions on a retry."""
    monkeypatch.setattr(ex, "assemble_deduped_turns",
                        lambda bucket, keys: ([{"text": keys[0]}], []))

    first = ex.assemble_group_turns("bkt", {SID_B: ["B"], SID_A: ["A"]})[0]
    second = ex.assemble_group_turns("bkt", {SID_A: ["A"], SID_B: ["B"]})[0]

    assert [s["session_id"] for s in first] == [s["session_id"] for s in second]


def test_turns_are_never_merged_across_devices(monkeypatch):
    """Guards the core design decision: no cross-device time merge, because
    there is no shared clock (BUG-37)."""
    monkeypatch.setattr(ex, "assemble_deduped_turns", lambda bucket, keys: (
        [{"text": keys[0], "abs_start": 100}], []))

    sources, _ = ex.assemble_group_turns("bkt", {SID_A: ["A"], SID_B: ["B"]})

    assert len(sources) == 2
    for s in sources:
        assert len(s["turns"]) == 1, "turns must stay within their own device"
