"""Voice notes (M2 Task 7, plan decision 9): one note, one topic, no email.

item-writer merges a voice note's extracted topics into exactly one -- title of the
first, summaries joined with a space, action items / decisions / questions
concatenated, time range from the first start to the last end -- and the
confirmation email is never built for a voice note, on either arm.
"""
import pytest

iw = pytest.importorskip("lambda_item_writer", reason="requires psycopg (installed in CI)")

SID = "e" * 32
BASE = "sid" + SID
DATE = "2026-10-21"


def _topic(title, time_range, action, summary):
    return {"topic_title": title, "time_range": time_range, "summary": summary,
            "category": "progress",
            "action_items": [{"action": action}],
            "decisions": [{"decision": f"{title} decided"}],
            "questions": [{"question": f"{title}?"}]}


def test_merge_two_topics():
    merged = iw._merge_voice_note_topics([
        _topic("Crack on level 2", "14:32 - 14:33", "Fill the crack", "A crack on level 2."),
        _topic("Order more ties", "14:33 - 14:34", "Order ties", "Ties are running low."),
    ])
    assert len(merged) == 1
    t = merged[0]
    assert t["topic_title"] == "Crack on level 2"
    assert t["time_range"] == "14:32 - 14:34"
    assert [a["action"] for a in t["action_items"]] == ["Fill the crack", "Order ties"]
    assert "A crack on level 2." in t["summary"] and "Ties are running low." in t["summary"]
    assert len(t["decisions"]) == 2 and len(t["questions"]) == 2


def test_merge_handles_the_extractors_en_dash():
    merged = iw._merge_voice_note_topics([
        _topic("A", "09:00 – 09:01", "x", "a"), _topic("B", "09:01 – 09:03", "y", "b")])
    assert merged[0]["time_range"] == "09:00 - 09:03"


def test_merge_one_topic_is_identity():
    one = [_topic("Only", "10:00 - 10:01", "Do it", "One thing.")]
    assert iw._merge_voice_note_topics(one) == one


def test_merge_zero_is_empty():
    assert iw._merge_voice_note_topics([]) == []


def test_voice_note_gets_no_follow_up_email(monkeypatch):
    monkeypatch.setattr(iw.meeting_session, "get",
                        lambda conn, sid: {"session_id": sid, "status": "sent",
                                           "session_type": "voice_note", "user_id": "u"})
    extraction = {"tier": "final", "topics": []}
    assert iw._final_email_context("C", BASE, extraction, DATE, follow_up=True) is None


def test_voice_note_gets_no_final_email(monkeypatch):
    monkeypatch.setattr(iw.meeting_session, "get",
                        lambda conn, sid: {"session_id": sid, "status": "finalizing",
                                           "session_type": "voice_note", "user_id": "u"})
    assert iw._final_email_context("C", BASE, {"tier": "final", "topics": []}, DATE) is None
