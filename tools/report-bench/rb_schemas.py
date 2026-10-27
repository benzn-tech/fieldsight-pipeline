"""JSON Schemas for Claude's `output_config.format` (spec §3), hand-written.

X follows lambda_extract_session.EXTRACTION_SCHEMA at develop, R follows
lambda_report_generator.DAILY_REPORT_SCHEMA; T is free text (None). Older X
candidates ask for a subset of develop's keys (763b3082 has no
location_markers / inspections), so `for_task(task, prompt)` requires only the
top-level keys the candidate's own prompt names; the rest stay allowed.

Leaf strings are nullable (the prompts allow null for responsible, deadline,
end_at, ...); enumerations stay plain strings, so the schema constrains shape,
not vocabulary -- the analogue of `json_object`, no stricter. Every object has
`additionalProperties: false` (required by the API) and no numeric / length
constraints (unsupported).
"""
from __future__ import annotations

import copy


def _s():
    return {"anyOf": [{"type": "string"}, {"type": "null"}]}


def _num():
    return {"anyOf": [{"type": "number"}, {"type": "null"}]}


def _bool():
    return {"anyOf": [{"type": "boolean"}, {"type": "null"}]}


def _obj(props, required=None):
    return {"type": "object", "properties": props,
            "required": list(props) if required is None else list(required),
            "additionalProperties": False}


def _arr(items):
    return {"type": "array", "items": items}


def _strs():
    return _arr({"type": "string"})


_QUOTE = _obj({"at": _s(), "quote": _s()})

X_TOPIC = _obj({
    "topic_title": _s(), "category": _s(), "work_class": _s(), "work_confidence": _num(),
    "is_mixed": _bool(), "summary": _s(), "time_range": _s(), "participants": _strs(),
    "origin": _s(), "evidence": _arr(_QUOTE),
    "action_items": _arr(_obj({"action": _s(), "responsible": _s(), "deadline": _s(),
                               "priority": _s()})),
    "findings": _arr(_obj({"observation": _s(), "domain": _s(), "severity": _s(),
                           "entity": _obj({"name": _s(), "trade": _s()}),
                           "recommended_action": _s()})),
    "decisions": _arr(_obj({"decision": _s(), "rationale": _s(), "decided_by": _s()})),
    "questions": _arr(_obj({"question": _s()})),
})

X_SCHEMA = _obj({
    "location_markers": _arr(_obj({"at": _s(), "location": _s(), "quote": _s()})),
    "inspections": _arr(_obj({"name": _s(), "kind": _s(), "start_at": _s(),
                              "start_quote": _s(), "end_at": _s(), "end_quote": _s(),
                              "resumed": _arr(_QUOTE)})),
    "topics": _arr(X_TOPIC),
    "declared_site": {"anyOf": [_obj({"stated": _s(), "confidence": _num()}),
                                {"type": "null"}]},
})

R_TOPIC = _obj({
    "topic_id": {"anyOf": [{"type": "integer"}, {"type": "null"}]}, "time_range": _s(),
    "topic_title": _s(), "category": _s(), "participants": _strs(), "summary": _s(),
    "key_decisions": _strs(),
    "action_items": _arr(_obj({"action": _s(), "responsible": _s(), "deadline": _s(),
                               "priority": _s()})),
    "safety_flags": _arr(_obj({"observation": _s(), "risk_level": _s(),
                               "recommended_action": _s()})),
    "related_photos": _strs(),
})

R_SCHEMA = _obj({
    "executive_summary": _s(),
    "quality_and_compliance": _arr(_obj({"item": _s(), "status": _s(), "details": _s(),
                                         "follow_up_needed": _bool()})),
    "safety_observations": _arr(_obj({"observation": _s(), "risk_level": _s(),
                                      "location": _s(), "who_raised": _s(),
                                      "recommended_action": _s()})),
    "critical_dates_and_deadlines": _arr(_obj({"date_mentioned": _s(), "context": _s(),
                                               "who_mentioned": _s(), "urgency": _s(),
                                               "type": _s()})),
    "topics": _arr(R_TOPIC),
})


def for_task(task: str, prompt: str | None = None):
    if task == "T":
        return None
    schema = copy.deepcopy({"X": X_SCHEMA, "R": R_SCHEMA}[task])
    if prompt is not None:
        schema["required"] = [k for k in schema["required"] if f'"{k}"' in prompt]
    return schema
