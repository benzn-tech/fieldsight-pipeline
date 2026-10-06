"""Test seam for Ask's general-knowledge flow (spec 2026-10-06).

Tests of `_rag_answer` that are about retrieval, scope or citations -- not about
the classify/draft/verify prompts -- stub the TWO web_answer entry points the
lambda calls (`classify`, `general_answer`) rather than the vendor client. That
keeps them on the real routing code in `lambda_ask_agent` while keeping the
model out of it. The flow's own behaviour is tested against the stubbed vendor
client in test_ask_knowledge_then_verify.py.
"""
import web_answer


class Calls:
    def __init__(self):
        self.classify = []     # the chunk list each classify call received
        self.general = []      # the question each general_answer call received


def block(**over):
    base = {"answer": None, "sources": [], "status": "verified", "kind": "general",
            "conflicts": [], "searched": True, "timed_out": False, "failed": False,
            "refused": None, "_trace": {"retried": False, "ms": {}}}
    base.update(over)
    return base


def stub_web(mp, web=None, kind="general"):
    """Turn the flow on and stub it.

    `web=None`: the classifier says the records answer it (a project question),
    so the general flow is not entered for a retrieved set; for an EMPTY set the
    general flow still runs and comes back with no answer.
    `web={"answer": ...}`: the question is `kind` (default general) and the
    records, if any, are useful.
    """
    mp.setenv("ENABLE_WEB_ANSWER", "true")
    calls = Calls()

    def fake_classify(question, chunks, budget):
        calls.classify.append(list(chunks))
        if web is None:
            return {"kind": "project", "records_answer": bool(chunks), "source": "model"}
        return {"kind": kind, "records_answer": bool(chunks), "source": "model"}

    def fake_general(question, budget, **kw):
        calls.general.append(question)
        return block(**(web or {"failed": True, "status": "unverified"}))

    mp.setattr(web_answer, "classify", fake_classify)
    mp.setattr(web_answer, "general_answer", fake_general)
    return calls
