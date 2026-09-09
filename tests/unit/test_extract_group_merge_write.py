"""Unit: extract_group — the function that actually produces the merged record.

The failure bias here is the opposite of the rest of the pipeline. Everywhere
else, raising is right: the S3 event retries and nothing is lost. Here the
members' own reports and emails have ALREADY gone out, so a merge that raises
buys a retry of something that will fail the same way, while a merge that
returns None degrades to exactly today's behaviour — N separate reports. So
every data-shaped failure logs and returns None.
"""
import json

import pytest

ex = pytest.importorskip("lambda_extract_session", reason="requires the lambda deps")

GID = "a" * 32
JOINER = "b" * 32


def _artifact(n=2):
    members = [{"userFolder": "Ben_UCPK", "date": "2026-08-07", "sessionBase": "sid" + GID}]
    if n > 1:
        members.append({"userFolder": "Sam_UCPK", "date": "2026-08-07",
                        "sessionBase": "sid" + JOINER})
    return {"groupId": GID, "leadSessionId": GID, "members": members,
            "mergedKey": f"extractions/Ben_UCPK/2026-08-07/grp{GID}.json"}


class _S3:
    def __init__(self):
        self.puts = []

    def put_object(self, Bucket=None, Key=None, Body=None, ContentType=None):
        self.puts.append((Key, json.loads(Body)))


@pytest.fixture
def wired(monkeypatch):
    s3 = _S3()
    monkeypatch.setattr(ex, "s3", lambda: s3)
    monkeypatch.setattr(ex, "gather_session_segments",
                        lambda b, f, d, sb: [f"transcripts/{f}/{d}/{sb}_c0.json"])
    monkeypatch.setattr(ex, "assemble_group_turns", lambda b, kbs: (
        [{"session_id": sb, "turns": [{"speaker": "spk_0", "text": "hello",
                                       "abs_start_str": "10:00:00"}]}
         for sb in sorted(kbs)],
        ["f1.json", "f2.json"]))
    monkeypatch.setattr(ex.llm_utils, "call_llm",
                        lambda *a, **k: (json.dumps({"topics": [{"topic_title": "T"}]}), None))
    monkeypatch.setattr(ex.llm_utils, "extract_json", lambda r: json.loads(r))
    return s3


def test_a_merge_writes_one_artifact_naming_every_member(wired):
    out = ex.extract_group("bkt", _artifact())
    assert out == f"extractions/Ben_UCPK/2026-08-07/grp{GID}.json"
    key, body = wired.puts[0]
    assert key == out
    assert body["tier"] == "group"
    assert body["groupId"] == GID
    assert body["session_base"] == "grp" + GID
    # Exactly what item-writer deletes — named here so the two cannot drift.
    assert set(body["mergedMembers"]) == {
        f"extractions/Ben_UCPK/2026-08-07/sid{GID}.json",
        f"extractions/Sam_UCPK/2026-08-07/sid{JOINER}.json"}
    # Everyone who was in the meeting gets the email, whether or not their audio
    # made the merge.
    assert set(body["memberSessions"]) == {GID, JOINER}


def test_no_usable_turns_writes_no_record_and_does_not_raise(monkeypatch, wired):
    monkeypatch.setattr(ex, "assemble_group_turns", lambda b, kbs: ([], []))
    assert ex.extract_group("bkt", _artifact()) is None
    assert [k for k, _ in wired.puts if k.endswith(".json")] == []


def test_a_meeting_with_nothing_in_it_records_that_it_was_passed_over(monkeypatch, wired):
    """A meeting cannot be re-driven -- extract_group returns None on failure
    rather than raising, and the claim has already set merged_at -- so the
    backlog probe is the only thing that would ever notice a lost merge. Left
    unmarked, every silent meeting would sit in that report forever and the
    alarm would stop being read, which is the failure the probe exists inside.
    """
    monkeypatch.setattr(ex, "assemble_group_turns", lambda b, kbs: ([], []))
    ex.extract_group("bkt", _artifact())
    assert len(wired.puts) == 1
    key, body = wired.puts[0]
    assert key == f"extractions/Ben_UCPK/2026-08-07/grp{GID}.skipped"
    assert body["reason"] == "no-usable-turns"
    assert body["groupId"] == GID


def test_the_meeting_marker_never_ends_in_dot_json(monkeypatch, wired):
    """item-writer is wired to `extractions/` + `.json`; a marker ending there
    would invoke it on every silent meeting with a file it cannot parse."""
    monkeypatch.setattr(ex, "assemble_group_turns", lambda b, kbs: ([], []))
    ex.extract_group("bkt", _artifact())
    assert not wired.puts[0][0].endswith(".json")


def test_an_llm_failure_writes_nothing_and_does_not_raise(monkeypatch, wired):
    # Raising would retry a call that fails the same way, at full price. The
    # members' own reports already went out; degrading to those is the correct
    # outcome.
    monkeypatch.setattr(ex.llm_utils, "call_llm", lambda *a, **k: (None, "boom"))
    assert ex.extract_group("bkt", _artifact()) is None
    assert wired.puts == []


def test_malformed_topics_are_not_written(monkeypatch, wired):
    monkeypatch.setattr(ex.llm_utils, "extract_json", lambda r: {"topics": "not a list"})
    assert ex.extract_group("bkt", _artifact()) is None
    assert wired.puts == []


def test_beyond_the_cap_the_omission_is_recorded_not_silent(monkeypatch, wired):
    monkeypatch.setattr(ex, "GROUP_MAX_MEMBERS", 1)
    body = None
    ex.extract_group("bkt", _artifact())
    key, body = wired.puts[0]
    assert body["omittedMembers"] == ["sid" + JOINER], \
        "a dropped device must be stated — silence here is the defect the feature removes"
    # ...but the omitted member is still emailed.
    assert set(body["memberSessions"]) == {GID, JOINER}
    assert len(body["mergedMembers"]) == 1


def test_the_group_prompt_tells_the_model_there_is_no_shared_clock():
    sources = [{"session_id": "sid" + GID,
                "turns": [{"speaker": "spk_0", "text": "hi", "abs_start_str": "10:00:00"}]}]
    p = ex.build_group_prompt(_artifact(1), sources)
    assert "do NOT share a clock" in p or "not share a clock" in p.lower()
    # and it must carry the same extraction contract as the solo prompt
    assert "## Instructions" in p and "topic_title" in p
