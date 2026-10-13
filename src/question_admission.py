"""May this question be sent to a search engine, whole?

Owner decision, 2026-10-06 (spec 2026-10-06-ask-knowledge-then-verify-design.md,
D4): admission keeps ONLY the length cap. The names-from-records screen, the
site-name screen and the commercial-term screen are gone. Commercial details in
a question may reach the search engine.

Why they went: the names screen dropped a sentence-opener only at position 0, so
`Which` counted as a name and `which` occurs in every transcript -- "search
online Which NZ standard governs ..." was refused, and the reader was told
nothing. The screens were guesses at "is this the customer's world" and refused
the questions the feature exists for.

What stays, and why: a question longer than `MAX_QUESTION_CHARS` is a paragraph
of the customer's situation rather than a question, and is not sent. The caller
reports it (`status: "too_long"`) instead of swallowing it: a refusal nobody can
see cannot be measured.

`corroboration_gate` (names from an answer, a different question) is untouched.
PURE PYTHON, NO MODEL.
"""
from __future__ import annotations

MAX_QUESTION_CHARS = 300

EMPTY = "empty question"
TOO_LONG = "longer than %d characters" % MAX_QUESTION_CHARS


def screen(question):
    """Return None when the question may be sent, or the reason when it may not."""
    q = (question or "").strip()
    if not q:
        return EMPTY
    if len(q) > MAX_QUESTION_CHARS:
        return TOO_LONG
    return None
