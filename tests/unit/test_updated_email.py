"""Unit: the updated email quotes the merged summary (Phase C, Task 9).

process_finalize_request PREFERS a freshly re-derived summary over the
artifact's, and that is right for a solo finalize: the enqueued rolling summary
can be stale, and by finalize time every transcript is in.

For an UPDATED email it is exactly wrong. _complete_summary re-gathers
`sid{sessionId}` — that member's OWN solo transcripts — so N members would each
receive a summary of what THEY heard, under a subject saying the meeting was
merged. N different bodies, N LLM calls, and the one thing the merge promised
(everyone gets the same record) quietly not delivered.
"""
import pytest

sf = pytest.importorskip("lambda_session_finalize", reason="requires the lambda deps")

GID = "a" * 32
SID = "b" * 32


def _run(artifact, **over):
    sent = {}
    kw = dict(
        complete_summary=lambda a: {"summary": "solo rewrite", "open_todos": []},
        send=lambda to, subject, text, html: sent.update(to=to, subject=subject, text=text, html=html),
        write_result=lambda *a, **k: None,
    )
    kw.update(over)
    res = sf.process_finalize_request(artifact, **kw)
    return res, sent


def test_an_updated_request_uses_the_carried_summary_verbatim(monkeypatch):
    called = []
    art = {"kind": "updated", "sessionId": SID, "groupId": GID,
           "summary": "The merged summary.", "openTodos": [{"text": "do it", "responsible": None, "due": None}],
           "recipient": "x@example.com", "date": "2026-08-07"}
    res, sent = _run(art, complete_summary=lambda a: called.append(a) or
                     {"summary": "solo rewrite", "open_todos": []})
    assert called == [], "an updated email must not re-summarise per member"
    body = (sent.get("text") or "") + (sent.get("html") or "")
    # The body no longer renders prose, so the carried-vs-re-derived choice is
    # observed through the to-dos: the artifact carries "do it", the (forbidden)
    # re-summarise would have replaced them with an empty list.
    assert "do it" in body


def test_a_normal_request_still_prefers_the_fresh_summary():
    called = []
    art = {"sessionId": SID, "summary": "stale rolling", "recipient": "x@example.com",
           "date": "2026-08-07"}
    _run(art, complete_summary=lambda a: called.append(a) or
         {"summary": "fresh", "open_todos": []})
    assert len(called) == 1, "the solo path must keep re-deriving"


def test_an_updated_email_says_it_is_an_update():
    art = {"kind": "updated", "sessionId": SID, "groupId": GID,
           "summary": "S", "recipient": "x@example.com", "date": "2026-08-07"}
    _, sent = _run(art)
    subject = sent.get("subject") or ""
    assert subject, "no subject built"
    assert "updated" in subject.lower() or "更新" in subject, \
        "a second email with a different body must not look like a duplicate"


def test_an_updated_result_is_written_to_its_own_key():
    # reconcile reads session_finalize_results/{sessionId}.json to settle a
    # CLAIMED session. A member can be counted settled by quietness while still
    # `finalizing`, so an updated result on the solo key could be read as that
    # member's solo outcome and move it to `sent` on the wrong evidence.
    written = []
    art = {"kind": "updated", "sessionId": SID, "groupId": GID,
           "summary": "S", "recipient": "x@example.com", "date": "2026-08-07"}
    _run(art, write_result=lambda sid, payload: written.append(sid))
    assert written and written[0].endswith("-updated"), \
        f"updated results must not collide with the solo key: {written}"
