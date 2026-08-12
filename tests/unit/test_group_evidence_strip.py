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
