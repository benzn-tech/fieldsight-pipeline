"""A group extraction cannot verify citations, so it must not ship any.

`verify_evidence` returns early for a group and the reason is sound: the members' turn lists have
no shared clock, so an honest quote from a second device lands outside the anchor window and
would be manufactured into evidence of fabrication.

But the early return happens BEFORE the loop that strips citations out of the children, and
before anything sets `evidence_status`. So with EMIT_EVIDENCE on -- which is how test runs today
-- a group artifact carries the model's raw citations with no status, and `_evidence_payload`
writes `{"status": None, "quotes": [...]}` into Aurora. That is a fourth state the column was
never designed for: its docstring lists NULL for never-measured, `absent` for measured-and-uncited,
and a real status otherwise. A reader sees quotes and no reason to doubt them.

It is the exact defect the child strip exists to prevent -- "it would leave an UNVERIFIED
citation in the S3 artifact for a reader to trust" -- on the multi-device path, where meetings
and their findings matter most.
"""
import json

import lambda_extract_session as ex


def _group_result():
    return {
        "tier": ex.TIER_GROUP,
        "topics": [
            {
                "topic_title": "Concrete pour",
                "evidence": [{"at": "12:13:21", "quote": "the pour is Monday"}],
                "findings": [
                    {"observation": "Pour scheduled Monday",
                     "evidence": [{"at": "12:13:21", "quote": "the pour is Monday"}]},
                ],
                "action_items": [
                    {"action": "Confirm the pour",
                     "evidence": [{"at": "12:13:25", "quote": "confirm it"}]},
                ],
            }
        ],
    }


def test_a_group_ships_no_topic_citations():
    result = _group_result()
    ex.verify_evidence(result, turns=[], session_date="2026-08-12")
    assert not result["topics"][0].get("evidence")


def test_a_group_ships_no_finding_citations():
    """The child strip runs for solo extractions and is skipped for groups purely because the
    early return sits above it. Nothing about a group makes child citations more trustworthy."""
    result = _group_result()
    ex.verify_evidence(result, turns=[], session_date="2026-08-12")
    assert "evidence" not in result["topics"][0]["findings"][0]


def test_a_group_ships_no_action_item_citations():
    result = _group_result()
    ex.verify_evidence(result, turns=[], session_date="2026-08-12")
    assert "evidence" not in result["topics"][0]["action_items"][0]


def test_a_group_does_not_claim_a_status():
    """Stripped, not marked. `unchecked` means our own code failed to measure something -- it is
    there to stop our bugs deflating the signal -- and borrowing it for "we never try on groups"
    would make that number unreadable. With no quotes and no status, _evidence_payload returns
    None and the column stays NULL: never measured, which is the truth."""
    result = _group_result()
    ex.verify_evidence(result, turns=[], session_date="2026-08-12")
    topic = result["topics"][0]
    assert not topic.get("evidence")
    assert topic.get("evidence_status") is None


def test_the_return_value_is_still_empty_for_a_group():
    """Callers use the returned counts for logging; a group contributes none."""
    assert ex.verify_evidence(_group_result(), turns=[], session_date="2026-08-12") == {}


def test_a_group_with_no_citations_is_untouched():
    result = {"tier": ex.TIER_GROUP,
              "topics": [{"topic_title": "Quiet", "findings": [{"observation": "x"}]}]}
    ex.verify_evidence(result, turns=[], session_date="2026-08-12")
    assert result["topics"][0]["findings"][0] == {"observation": "x"}


def test_a_group_write_ships_no_continues_claim(monkeypatch):
    """The group prompt never asks for a `continues` claim (spec D7), but it shares the
    instructions block with the solo prompt, so a model could still volunteer one. extract_group
    -- the function that actually writes the merged artifact -- must strip it, the same way it
    already derives safety_flags rather than trusting the model's raw output."""
    GID = "c" * 32

    class _S3:
        def __init__(self):
            self.puts = []

        def put_object(self, Bucket=None, Key=None, Body=None, ContentType=None):
            self.puts.append((Key, json.loads(Body)))

    s3 = _S3()
    monkeypatch.setattr(ex, "s3", lambda: s3)
    monkeypatch.setattr(ex, "gather_session_segments",
                        lambda b, f, d, sb: [f"transcripts/{f}/{d}/{sb}_c0.json"])
    monkeypatch.setattr(ex, "assemble_group_turns", lambda b, kbs: (
        [{"session_id": sb, "turns": [{"speaker": "spk_0", "text": "hello",
                                       "abs_start_str": "10:00:00"}]}
         for sb in sorted(kbs)],
        ["f1.json"]))
    group_result = {
        "topics": [
            {
                "topic_title": "Concrete pour",
                "findings": [
                    {"observation": "Pour scheduled Monday",
                     "continues": {"id": "F1", "starts": "Pour scheduled"}},
                ],
                "action_items": [
                    {"action": "Confirm the pour",
                     "continues": {"id": "A1", "starts": "Confirm the"}},
                ],
            }
        ],
    }
    monkeypatch.setattr(ex.llm_utils, "call_llm",
                        lambda *a, **k: (json.dumps(group_result), None))
    monkeypatch.setattr(ex.llm_utils, "extract_json", lambda r: json.loads(r))

    artifact = {"groupId": GID, "leadSessionId": GID,
                "members": [{"userFolder": "Ben_UCPK", "date": "2026-08-12",
                            "sessionBase": "sid" + GID}],
                "mergedKey": f"extractions/Ben_UCPK/2026-08-12/grp{GID}.json"}
    out = ex.extract_group("bkt", artifact)
    assert out is not None

    _, body = s3.puts[0]
    topic = body["topics"][0]
    assert "continues" not in topic["findings"][0]
    assert "continues" not in topic["action_items"][0]
