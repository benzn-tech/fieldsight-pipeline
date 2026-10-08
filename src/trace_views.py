"""Reading the recordings' traces back (pipeline_trace writes them).

Two views for the platform admin's Activity page (owner, 2026-10-05):

  day(...)     one day: which folders had activity, and for one folder every
               step of every recording in order -- the "tool call list";
  funnel(...)  a date range: per folder, how far recordings got through the
               pipeline (final extraction -> topics -> photos placed by
               location -> template reports -> checklists answered), and what
               the model calls cost.

The funnel is computed from the events, never stored: one source of truth,
and a new count is a code change rather than a backfill. It counts only what
is traced -- transcription and downloads are not (yet), so they are not in it
rather than shown as zero.

Pure over a `read_day(date) -> [event]` callable, so the counting is tested
without S3; `s3_day_reader` is the real one.
"""
import datetime as dt
import json

from pipeline_trace import PREFIX

MAX_FUNNEL_DAYS = 31


def s3_day_reader(s3, bucket, folder=None):
    """read_day(date) -> every event under traces/<date>/[<folder>/]."""
    def read_day(date):
        prefix = "%s%s/%s" % (PREFIX, date, (folder + "/") if folder else "")
        events = []
        for page in s3.get_paginator("list_objects_v2").paginate(Bucket=bucket, Prefix=prefix):
            for obj in page.get("Contents", []):
                body = s3.get_object(Bucket=bucket, Key=obj["Key"])["Body"].read()
                for line in body.decode("utf-8").splitlines():
                    if line.strip():
                        try:
                            events.append(json.loads(line))
                        except ValueError:
                            continue
        return events
    return read_day


def _llm(events):
    calls = [e for e in events if e.get("step") == "llm_call"]
    return {"llm_calls": len(calls),
            "prompt_tokens": sum(e.get("prompt_tokens") or 0 for e in calls),
            "completion_tokens": sum(e.get("completion_tokens") or 0 for e in calls),
            "llm_seconds": round(sum(e.get("seconds") or 0 for e in calls), 1)}


def day(read_day, date, folder=None):
    """Without `folder`: the folders active that day with their totals. With
    it: that folder's recordings, each with its events in time order."""
    events = sorted(read_day(date), key=lambda e: e.get("at") or "")
    if folder is None:
        by_folder = {}
        for e in events:
            by_folder.setdefault(e.get("user_folder") or "_unknown", []).append(e)
        return {"date": date, "folders": [
            dict({"folder": f, "company_id": next((e["company_id"] for e in evs
                                                    if e.get("company_id")), None),
                  "events": len(evs),
                  "recordings": len({e.get("session") for e in evs if e.get("session")})},
                 **_llm(evs))
            for f, evs in sorted(by_folder.items())]}
    mine = [e for e in events if (e.get("user_folder") or "_unknown") == folder]
    by_trace = {}
    for e in mine:
        by_trace.setdefault(e.get("trace_id"), []).append(e)
    return {"date": date, "folder": folder, "recordings": [
        dict({"trace_id": t, "session": evs[0].get("session"),
              "first": evs[0].get("at"), "last": evs[-1].get("at"), "events": evs},
             **_llm(evs))
        for t, evs in sorted(by_trace.items(), key=lambda kv: kv[1][0].get("at") or "")]}


def _count(events):
    """The funnel for one folder's events (any span of days)."""
    final = {}
    for e in events:
        if e.get("step") == "extraction" and e.get("result") == "ok" \
                and (e.get("detail") or {}).get("pass") == "final":
            final[e.get("session")] = e      # the latest final per recording wins
    placed = [e for e in events if e.get("step") == "photo_placed"]
    checklists = [e for e in events if e.get("step") == "checklist"]
    return dict({
        "recordings_extracted": len(final),
        "recordings_with_topics": sum(1 for e in final.values()
                                      if (e.get("detail") or {}).get("topics")),
        "location_markers": sum((e.get("detail") or {}).get("location_markers") or 0
                                for e in final.values()),
        "markers_timed": sum((e.get("detail") or {}).get("markers_timed") or 0
                             for e in final.values()),
        "photos": len({(e.get("detail") or {}).get("photo") for e in placed}),
        "photos_by_location": len({(e.get("detail") or {}).get("photo") for e in placed
                                   if e.get("result") == "location"}),
        "photos_unbound": len({(e.get("detail") or {}).get("photo") for e in placed
                               if e.get("result") == "unbound"}),
        "template_reports": sum(1 for e in events
                                if e.get("step") == "template_report" and e.get("result") == "ok"),
        "template_report_errors": sum(1 for e in events if e.get("step") == "template_report"
                                      and e.get("result") == "error"),
        "checklists": len(checklists),
        "checklists_answered": sum(1 for e in checklists if e.get("result") == "answered"),
        "glossary_applied": sum(1 for e in events
                                if e.get("step") == "glossary" and e.get("result") == "applied"),
        "glossary_learned": sum(1 for e in events if e.get("step") == "glossary_learned"),
    }, **_llm(events))


def _dates(date_from, date_to):
    start = dt.date.fromisoformat(date_from)
    end = dt.date.fromisoformat(date_to)
    if end < start:
        raise ValueError("to is before from")
    if (end - start).days + 1 > MAX_FUNNEL_DAYS:
        raise ValueError("at most %d days" % MAX_FUNNEL_DAYS)
    return [(start + dt.timedelta(days=i)).isoformat() for i in range((end - start).days + 1)]


def funnel(read_day, date_from, date_to):
    """Per folder: the funnel per day and over the whole range. Photos are
    counted once per day however many times a day was re-bound."""
    days = _dates(date_from, date_to)
    per = {}
    for d in days:
        by_folder = {}
        for e in read_day(d):
            by_folder.setdefault(e.get("user_folder") or "_unknown", []).append(e)
        for f, evs in by_folder.items():
            per.setdefault(f, {})[d] = evs
    rows = []
    for f, by_day in sorted(per.items()):
        daily = {d: _count(evs) for d, evs in sorted(by_day.items())}
        total = {}
        for c in daily.values():
            for k, v in c.items():
                total[k] = round(total.get(k, 0) + v, 1) if isinstance(v, float) \
                    else total.get(k, 0) + v
        company = next((e["company_id"] for evs in by_day.values() for e in evs
                        if e.get("company_id")), None)
        rows.append({"folder": f, "company_id": company, "total": total, "daily": daily})
    return {"from": date_from, "to": date_to, "folders": rows}
