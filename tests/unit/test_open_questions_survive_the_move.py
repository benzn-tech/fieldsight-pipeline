"""Unit: taking open questions out of the prose must not take them out of the system.

`lambda_meeting_minutes` used to glue open questions onto the end of every topic
summary — `summary += ' Open questions: ' + '; '.join(open_qs)` — which is what
made a real report read as a wall of text. Moving them to their own key fixed
the reading and, on its own, deleted them from three places at once:

  * `chunking._topic_text` renders header, participants, summary, decisions,
    actions and safety flags. Not a new key. So the RAG chunk that used to
    contain "Open questions: What are the port three requirements?" stopped
    containing it, and on the next embed the vector was simply gone.
  * `format_report_for_prompt` builds Ask's context from the same fields. Ask
    could no longer answer "what was still unknown about port three".
  * The frontend renders `topic.summary`.

Nothing failed. Search returned nothing and that looks exactly like a day where
nobody asked anything. The report was meant to stop SHOWING the questions as
prose; it was never meant to stop CARRYING them — "追溯的时候再去 query" is the
path the whole change is justified by.
"""
import chunking
import lambda_ask_agent as ask


QUESTION = "What are the Ten Peaks port three requirements?"


def _report(**kw):
    base = {
        "report_date": "2026-08-27",
        "user_name": "Ben_UCPK2",
        "site": "UC PK",
        "executive_summary": "A meeting.",
        "topics": [{
            "topic_id": 0,
            "time_range": "09:35 – 09:40",
            "topic_title": "Opening priorities",
            "category": "progress",
            "participants": ["Ben"],
            "summary": "Ben outlined the day's plan.",
            "key_decisions": [],
            "action_items": [],
            "safety_flags": [],
            "open_questions": [QUESTION],
        }],
    }
    base.update(kw)
    return base


def test_the_rag_chunk_still_contains_the_question():
    """This is the searchable copy. Without it, asking about port three on that
    day returns nothing and looks like a day when nobody asked."""
    chunks = chunking.chunk_report(_report())
    assert any(QUESTION in c["chunk_text"] for c in chunks), (
        "the question is not in any chunk: " + repr([c["chunk_text"][:80] for c in chunks]))


def test_asks_context_still_contains_the_question():
    text = ask.format_report_for_prompt(_report(), "daily")
    assert QUESTION in text


def test_the_extraction_spelling_is_carried_too():
    """The meeting path writes `open_questions`; the extraction schema says
    `questions`, as a list of {question}. Both are the same thing to a reader."""
    report = _report()
    report["topics"][0]["open_questions"] = []
    report["topics"][0]["questions"] = [{"question": QUESTION}]
    assert any(QUESTION in c["chunk_text"] for c in chunking.chunk_report(report))
    assert QUESTION in ask.format_report_for_prompt(report, "daily")


def test_a_topic_with_no_questions_is_unchanged():
    """No stray 'Open question:' label on the ordinary case."""
    report = _report()
    del report["topics"][0]["open_questions"]
    for c in chunking.chunk_report(report):
        assert "Open question" not in c["chunk_text"]
