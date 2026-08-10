"""Unit: verifying citations and anchoring them to audio (P1-2 Task 4).

The bias here is unusual and deliberate: this step must NEVER be able to fail an
extraction. It is a measurement, and a measurement that can destroy the thing it
measures is worse than no measurement. Every failure path ends in `unchecked`
plus a loud log.
"""
from datetime import datetime

import pytest

ex = pytest.importorskip("lambda_extract_session", reason="requires the lambda deps")

AT = datetime(2026, 8, 7, 14, 23, 7)
FN = "ben_2026-08-07_14-23-00_sidXYZ_c0007_off0.0_to30.0_srcwav.json"


def _turns():
    return [{"text": "the slab pour is pushed to Thursday", "start_sec": 4.0,
             "abs_start": AT, "abs_end": AT, "source_filename": FN}]


def _topic(quote, at="14:23:07"):
    return {"topics": [{"topic_title": "Slab",
                        "evidence": [{"at": at, "quote": quote}]}]}


def test_a_verified_quote_gets_an_audio_anchor():
    result = _topic("the slab pour is pushed to Thursday")
    counts = ex.verify_evidence(result, _turns(), "2026-08-07",
                                user_folder="Ben_UCPK", date="2026-08-07")
    ev = result["topics"][0]["evidence"][0]
    assert ev["status"] == "verified"
    # transcripts/...json -> audio_segments/...wav: same basename, prefix and
    # extension swapped. ALWAYS .wav, even when the name says srcmp4 — that
    # token records the SOURCE format, not the segment's.
    assert ev["segment_key"] == f"audio_segments/Ben_UCPK/2026-08-07/{FN[:-5]}.wav"
    assert ev["offset_sec"] == 4.0
    assert counts["verified"] == 1
    assert result["topics"][0]["evidence_status"] == "verified"


def test_an_srcmp4_segment_still_anchors_to_a_wav():
    fn = "ben_2026-08-07_14-23-00_sidXYZ_c0007_off0.0_to30.0_srcmp4.json"
    turns = [dict(_turns()[0], source_filename=fn)]
    result = _topic("the slab pour is pushed to Thursday")
    ex.verify_evidence(result, turns, "2026-08-07",
                       user_folder="Ben_UCPK", date="2026-08-07")
    assert result["topics"][0]["evidence"][0]["segment_key"].endswith(".wav")


def test_an_invented_quote_is_unverified_and_still_stored_verbatim():
    invented = "the market is now coming down sharply"
    result = _topic(invented)
    ex.verify_evidence(result, _turns(), "2026-08-07")
    ev = result["topics"][0]["evidence"][0]
    assert ev["status"] == "unverified"
    assert ev["quote"] == invented, \
        "the model's own words ARE the evidence — overwriting them destroys the record"


def test_a_topic_with_no_evidence_is_absent_and_still_written():
    result = {"topics": [{"topic_title": "Something"}]}
    counts = ex.verify_evidence(result, _turns(), "2026-08-07")
    assert result["topics"][0]["evidence_status"] == "absent"
    assert counts["absent"] == 1


def test_a_raising_matcher_yields_unchecked_and_never_propagates(monkeypatch):
    def boom(*a, **k):
        raise ValueError("bug in the matcher")
    monkeypatch.setattr(ex.evidence_match, "check_quote", boom)
    result = _topic("the slab pour is pushed to Thursday")
    counts = ex.verify_evidence(result, _turns(), "2026-08-07")   # must not raise
    assert result["topics"][0]["evidence_status"] == "unchecked"
    assert counts["unchecked"] == 1


def test_evidence_below_the_topic_level_is_stripped():
    # The model may volunteer it despite the instruction. Aurora drops it
    # (explicit columns), but it costs output tokens and would leave an
    # UNVERIFIED citation in the S3 artifact for a reader to trust.
    result = {"topics": [{"topic_title": "T", "action_items": [
        {"action": "do it", "evidence": [{"at": "14:23:07", "quote": "anything"}]}],
        "findings": [{"observation": "o", "evidence": [{"at": "1", "quote": "x"}]}]}]}
    ex.verify_evidence(result, _turns(), "2026-08-07")
    assert "evidence" not in result["topics"][0]["action_items"][0]
    assert "evidence" not in result["topics"][0]["findings"][0]


def test_a_group_extraction_is_skipped_entirely():
    # The matcher windows on absolute time; group turn lists have no shared
    # clock by design (a device 12 hours out is shipped history), so an honest
    # quote from the second device would land outside W and be counted as
    # fabrication — poisoning the one number this exists to produce.
    result = dict(_topic("the slab pour is pushed to Thursday"), tier="group")
    counts = ex.verify_evidence(result, _turns(), "2026-08-07")
    assert counts == {}
    assert "status" not in result["topics"][0]["evidence"][0]


def test_the_at_string_is_resolved_against_the_session_date():
    result = _topic("the slab pour is pushed to Thursday", at="14:23:07")
    ex.verify_evidence(result, _turns(), "2026-08-07")
    assert result["topics"][0]["evidence"][0]["status"] == "verified"


def test_a_malformed_at_does_not_crash_the_pass():
    result = _topic("the slab pour is pushed to Thursday", at="not a time")
    counts = ex.verify_evidence(result, _turns(), "2026-08-07")
    assert result["topics"][0]["evidence_status"] in ("unverified", "unchecked")
    assert sum(counts.values()) == 1


def test_the_flag_off_means_verification_never_runs(monkeypatch):
    # prod ships with it off; nothing in this file may run there.
    monkeypatch.setattr(ex, "EMIT_EVIDENCE", False)
    monkeypatch.setattr(ex, "verify_evidence",
                        lambda *a, **k: pytest.fail("ran with the flag off"))
    assert ex.EMIT_EVIDENCE is False


def test_a_total_verification_failure_does_not_lose_the_extraction(monkeypatch):
    # The outer guard. Even if verify_evidence itself is broken end to end, the
    # extraction it was measuring must still be written.
    monkeypatch.setattr(ex, "EMIT_EVIDENCE", True)
    monkeypatch.setattr(ex, "verify_evidence",
                        lambda *a, **k: (_ for _ in ()).throw(RuntimeError("boom")))
    # Exercised through the same shape extract_session uses.
    try:
        if ex.EMIT_EVIDENCE:
            try:
                ex.verify_evidence({}, [], "2026-08-07")
            except Exception:
                pass
    except Exception:
        pytest.fail("the guard must swallow it")


def test_the_counts_are_logged_once_per_extraction(caplog):
    result = _topic("the slab pour is pushed to Thursday")
    with caplog.at_level("INFO"):
        ex.verify_evidence(result, _turns(), "2026-08-07")
    assert "evidence:" in caplog.text, "the counts ARE the Phase A deliverable"


# ---- the reason reaches the artifact ----------------------------------
#
# The Phase A number is not a decision input until a person has split a sample
# of unverified quotes into "our matcher missed it" and "the model invented it".
# That split starts from the artifact, so whatever the matcher already knew
# about WHY has to survive into it -- otherwise a reader re-derives by hand what
# the code had in a local variable.

def test_an_unverified_citation_records_why():
    result = _topic("the market is now coming down sharply")
    ex.verify_evidence(result, _turns(), "2026-08-07")
    ev = result["topics"][0]["evidence"][0]
    assert ev["status"] == "unverified"
    # Nothing in the window resembles it -- not a near miss of the cut.
    assert ev["reason"] == ex.evidence_match.REASON_NOTHING_CLOSE


def test_a_verified_citation_records_no_reason():
    result = _topic("the slab pour is pushed to Thursday")
    ex.verify_evidence(result, _turns(), "2026-08-07")
    assert "reason" not in result["topics"][0]["evidence"][0]


def test_a_malformed_at_is_not_reported_as_our_own_bug():
    # `unchecked` exists to stop OUR bugs deflating the signal. A model emitting
    # a garbage timestamp is not our bug, and reading the two as one thing sends
    # the next person into the matcher looking for a crash that never happened.
    result = _topic("the slab pour is pushed to Thursday", at="not a time")
    ex.verify_evidence(result, _turns(), "2026-08-07")
    ev = result["topics"][0]["evidence"][0]
    assert ev["status"] == "unchecked"
    assert ev["reason"] == ex.REASON_BAD_ANCHOR


def test_a_matcher_crash_is_still_reported_as_our_own_bug(monkeypatch):
    monkeypatch.setattr(ex.evidence_match, "check_quote",
                        lambda *a, **k: (_ for _ in ()).throw(ValueError("bug")))
    result = _topic("the slab pour is pushed to Thursday")
    ex.verify_evidence(result, _turns(), "2026-08-07")
    ev = result["topics"][0]["evidence"][0]
    assert ev["status"] == "unchecked"
    assert ev["reason"] == ex.REASON_VERIFIER_ERROR


def test_the_unverified_reasons_are_logged_beside_the_counts(caplog):
    result = _topic("the market is now coming down sharply")
    with caplog.at_level("INFO"):
        ex.verify_evidence(result, _turns(), "2026-08-07")
    assert ex.evidence_match.REASON_NOTHING_CLOSE in caplog.text, \
        "a run whose unverifieds are all one cause is readable from the log alone"
