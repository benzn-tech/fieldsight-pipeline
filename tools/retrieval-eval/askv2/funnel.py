"""Pure scoring over what one Ask v2 turn recorded (spec §1, review S1/S2/S10)."""


def classify_question(question, today):
    """The round-1 shape the real orchestrator will pick for `question` (no model,
    no SQL), computed with the production modules themselves (review S6). The
    metric fast path fires only for a dated question (`ask_orchestrator._answer`)."""
    import ask_orchestrator
    import lexical_terms
    import metric_slots
    import query_slots
    q_from, _ = query_slots.time_range(question, today)
    latest = bool(ask_orchestrator._LATEST.search(question))
    requirements = bool(ask_orchestrator._REQUIREMENTS.search(question))
    if q_from:
        shape = "day+search"
    elif latest:
        shape = "latest"
    else:
        shape = "search"
    return {"dated": bool(q_from), "latest": latest, "requirements": requirements,
            "metric_fast_path": bool(q_from) and bool(metric_slots.detect(question)),
            "subject_terms": list(lexical_terms.subject_terms(question)),
            "round1": shape,
            "round1_with_documents": shape + "+documents" if requirements else shape}
