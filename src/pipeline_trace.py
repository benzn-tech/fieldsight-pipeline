"""What the pipeline did with a recording, step by step, kept for 180 days.

Owner, 2026-10-05: every recording should be traceable the way an agent's
tool calls are -- which steps ran, how many model calls each made, whether a
template or a keyword fired, and when something did NOT fire, that it was
looked for and not found. A step that leaves no record cannot be told apart
from a step that never ran (see lambda-info-logs-dropped: a decision log that
was silently swallowed made a working feature look dead for weeks).

SHAPE (version 1 -- an agent reads these, so fields are only ever added):

    {"v": 1, "trace_id": "<folder>/<date>/<session or ->",
     "at": ISO-8601 UTC, "lambda": "extract-session", "invocation": "<8 hex>",
     "company_id": str|null, "user_folder": str, "date": "YYYY-MM-DD",
     "session": str|null, "step": "extraction", "result": "ok" | "no_match" | ...,
     "detail": {...counts and names, never conversation text...},
     "evidence": "<= 200 chars of the words that triggered it" | null,
     "model": str|null, "prompt_tokens": int|null,
     "completion_tokens": int|null, "seconds": float|null}

WHERE: one JSONL object per invocation, at

    traces/<date>/<user_folder>/<HHMMSS>-<lambda>-<invocation>.jsonl

`date` is the RECORDING's day, so everything that happened to one day's
recordings is under one prefix however late a step ran. S3 cannot append, so a
day is many small objects; a reader lists the prefix. Kept 180 days by a
bucket lifecycle rule on `traces/`.

NEVER FAILS THE WORK. Recording an event and writing the file are both
wrapped: a trace bug that turned into an outage would be the worst possible
trade, and a trace that silently stopped is caught by its own absence (a day
with recordings and no traces/ prefix).
"""
import contextvars
import datetime as dt
import json
import logging
import os
import uuid

logger = logging.getLogger(__name__)

VERSION = 1
PREFIX = "traces/"
EVIDENCE_MAX = 200
_MAX_EVENTS = 500          # per invocation; a runaway loop must not write megabytes

_state = contextvars.ContextVar("fieldsight_trace", default=None)


def enabled():
    return os.environ.get("TRACE_EVENTS", "on").lower() not in ("off", "0", "false")


def begin(lambda_name, user_folder=None, date=None, session=None, company_id=None):
    """Start collecting for this invocation. Calling it again (a lambda that
    handles several records) flushes nothing -- call flush() per record."""
    _state.set({"lambda": lambda_name, "invocation": uuid.uuid4().hex[:8],
                "user_folder": user_folder, "date": date, "session": session,
                "company_id": str(company_id) if company_id else None, "events": []})


def identify(user_folder=None, date=None, session=None, company_id=None):
    """Fill in who/when once it is known (often only after the event is parsed)."""
    st = _state.get()
    if st is None:
        return
    for k, v in (("user_folder", user_folder), ("date", date), ("session", session)):
        if v:
            st[k] = v
    if company_id:
        st["company_id"] = str(company_id)


def event(step, result="ok", detail=None, evidence=None, model=None, prompt_tokens=None,
          completion_tokens=None, seconds=None):
    """Record one step. No-op outside begin()...flush(), and never raises."""
    try:
        st = _state.get()
        if st is None or not enabled() or len(st["events"]) >= _MAX_EVENTS:
            return
        st["events"].append({
            "at": dt.datetime.now(dt.timezone.utc).isoformat(timespec="milliseconds"),
            "step": step, "result": result, "detail": detail or {},
            "evidence": (str(evidence)[:EVIDENCE_MAX] if evidence else None),
            "model": model, "prompt_tokens": prompt_tokens,
            "completion_tokens": completion_tokens,
            "seconds": round(seconds, 2) if isinstance(seconds, (int, float)) else None,
        })
    except Exception:
        logger.debug("trace event dropped", exc_info=True)


def llm_call(caller, provider, model, elapsed, prompt_tokens=None, completion_tokens=None):
    """Every model call, from the one place every client already reports it
    (llm_usage.log_usage) -- so a step's model calls are counted without each
    caller remembering to."""
    event("llm_call", "ok", detail={"caller": caller, "provider": provider},
          model=model, prompt_tokens=prompt_tokens, completion_tokens=completion_tokens,
          seconds=elapsed)


def key_for(st, now=None):
    now = now or dt.datetime.now(dt.timezone.utc)
    return "%s%s/%s/%s-%s-%s.jsonl" % (
        PREFIX, st.get("date") or now.strftime("%Y-%m-%d"),
        st.get("user_folder") or "_unknown", now.strftime("%H%M%S"),
        st["lambda"], st["invocation"])


def lines(st):
    """The JSONL body: each event with the invocation's identity on it, so a
    line read on its own is complete."""
    trace_id = "%s/%s/%s" % (st.get("user_folder") or "_unknown", st.get("date") or "-",
                             st.get("session") or "-")
    out = []
    for e in st["events"]:
        out.append(json.dumps(dict({"v": VERSION, "trace_id": trace_id,
                                    "lambda": st["lambda"], "invocation": st["invocation"],
                                    "company_id": st.get("company_id"),
                                    "user_folder": st.get("user_folder"),
                                    "date": st.get("date"), "session": st.get("session")},
                                   **e), ensure_ascii=False, default=str))
    return "\n".join(out) + "\n" if out else ""


def flush(s3, bucket):
    """Write what was collected and stop collecting. Returns the key, or None
    when there was nothing (or writing failed -- logged, never raised).

    `s3` is a client or a zero-argument factory for one; a factory is only
    called when there is something to write, so a lambda whose work never
    needed S3 does not create a client just to say nothing."""
    st = _state.get()
    _state.set(None)
    if st is None or not st["events"] or not enabled() or not bucket:
        return None
    key = key_for(st)
    try:
        if not hasattr(s3, "put_object") and callable(s3):
            s3 = s3()
        s3.put_object(Bucket=bucket, Key=key, Body=lines(st).encode("utf-8"),
                      ContentType="application/x-ndjson")
        return key
    except Exception:
        logger.warning("trace: could not write %s (%d events)", key, len(st["events"]),
                       exc_info=True)
        return None
