"""The sections a report's code writes: its details and the day's weather.

Owner, 2026-09-29: the code decides, the model words. A report's project,
client, date and author are facts the system already holds, and the weather
is decided by site_weather from measured values. Asking the model to write
them hands it the chance to get them wrong -- a date misread off a filename,
a dry day called wet -- on a document a customer may file as a compliance
record. So a template section of kind `header` or `weather` is taken OUT of
the plan the model sees and written here instead, in the place the template
put it.

The facts arrive with the request (`reportFacts`, resolved by org-api in the
VPC). The weather comes from the record the nightly run keeps
(weather/<site uuid>/<date>/actual.json) so a template report and the daily
report say the same thing about the same day; with no record, it is worked
out here with the same code. With neither, the section says it was not
recorded -- on a compliance record a missing weather section reads as a fine
day.
"""
import datetime as dt
import json
import logging

import report_template
import site_weather

logger = logging.getLogger(__name__)

NOT_RECORDED = "Not recorded for this date."


def kind_of(section):
    return str((section or {}).get("kind") or "").strip().lower()


def split_plan(template):
    """(the template the model is given, the code sections and where they go).

    Each placement records the title of the model-written section it came
    before, so it can be put back in front of that section's answer. One at
    the end of the plan goes before the catch-all."""
    kept, pending, placements = [], [], []
    for s in (template or {}).get("sections") or []:
        if kind_of(s) in report_template.CODE_FILLED_KINDS:
            pending.append(s)
            continue
        before = (s.get("title") or "").strip().lower()
        placements.extend({"section": p, "before": before} for p in pending)
        pending = []
        kept.append(s)
    placements.extend({"section": p, "before": None} for p in pending)
    return dict(template or {}, sections=kept), placements


def drop_model_copies(prose, placements):
    """A model that writes one of our headings anyway -- it was not asked, but
    the house style or a customer's note can suggest it -- does not get it
    printed beside ours. Done before anything counts the answer, so a topic
    cited only there is reported as not covered rather than vanishing."""
    ours = {(p["section"].get("title") or "").strip().lower() for p in placements}
    dropped = [s.get("title") for s in prose if (s.get("title") or "").strip().lower() in ours]
    prose[:] = [s for s in prose if (s.get("title") or "").strip().lower() not in ours]
    return dropped


def insert(prose, placements, built, catch_all_title):
    """Put each built section in front of the answer's section it preceded in
    the plan; failing that, before the catch-all; failing that, at the end."""
    catch_all = (catch_all_title or "").strip().lower()
    drop_model_copies(prose, placements)
    titles = lambda: [(s.get("title") or "").strip().lower() for s in prose]  # noqa: E731
    for placement, section in zip(placements, built):
        now = titles()
        if placement["before"] and placement["before"] in now:
            at = now.index(placement["before"])
        elif catch_all and catch_all in now:
            at = now.index(catch_all)
        else:
            at = len(prose)
        prose.insert(at, section)


def _cell(value):
    return str(value).replace("|", "/").strip()


def _long_date(iso):
    try:
        d = dt.date.fromisoformat(iso)
    except (TypeError, ValueError):
        return iso or ""
    return "%s %d %s" % (d.strftime("%A"), d.day, d.strftime("%B %Y"))


def _table(rows):
    """Pipe rows the renderer turns into a table: the first row, a rule, the rest."""
    lines = [" | ".join(_cell(c) for c in r) for r in rows]
    width = len(rows[0])
    return lines[:1] + ["|".join(["---"] * width)] + lines[1:]


def _section(title, paragraphs):
    return {"title": title, "paragraphs": paragraphs, "line_refs": [[] for _ in paragraphs],
            "level": 1, "covers": []}


def header_section(title, artifact, facts):
    content = artifact.get("content") or {}
    window = artifact.get("window") or {}
    sites = facts.get("sites") or []
    names = [s["name"] for s in sites if s.get("name")] or list(content.get("siteNames") or []) \
        or ([content["siteName"]] if content.get("siteName") else [])
    clients = sorted({s["client"].strip() for s in sites if (s.get("client") or "").strip()})
    rows = [["Project", ", ".join(names) or "Not recorded"]]
    if clients:
        rows.append(["Client", ", ".join(clients)])
    rows.append(["Date", _long_date(artifact.get("date") or content.get("date"))])
    rows.append(["Recorded by", facts.get("recordedBy") or artifact.get("folder") or ""])
    rows.append(["Recording window", "%s - %s" % (window.get("from") or "00:00",
                                                 window.get("to") or "23:59")])
    return _section(title, _table(rows))


def _stored(s3, bucket, site_id, date, today_iso):
    """The nightly record for this site and day: the measured one, or -- for
    today or later -- the morning forecast. None when neither was written."""
    names = ["actual.json"] + (["forecast.json"] if date >= today_iso else [])
    for name in names:
        try:
            obj = s3.get_object(Bucket=bucket, Key="weather/%s/%s/%s" % (site_id, date, name))
        except s3.exceptions.NoSuchKey:
            continue
        return json.loads(obj["Body"].read().decode("utf-8"))
    return None


def _programme(s3, bucket, site_id):
    try:
        obj = s3.get_object(Bucket=bucket, Key="programmes/%s/programme.json" % site_id)
    except s3.exceptions.NoSuchKey:
        return None
    return json.loads(obj["Body"].read().decode("utf-8"))


def site_day_weather(s3, bucket, site, date, today_iso):
    """(daily totals, finding lines, where they came from) for one site."""
    sid = site.get("id")
    if sid:
        try:
            rec = _stored(s3, bucket, sid, date, today_iso)
        except Exception:
            logger.warning("weather record for %s on %s could not be read", sid, date,
                           exc_info=True)
            rec = None
        if rec:
            return rec.get("daily"), list(rec.get("lines") or []), "record"
    info = {"latitude": site.get("latitude"), "longitude": site.get("longitude")}
    if info["latitude"] is None or info["longitude"] is None:
        return None, [], "none"
    programme = None
    if sid:
        try:
            programme = _programme(s3, bucket, sid)
        except Exception:
            logger.warning("programme for %s could not be read", sid, exc_info=True)
    daily = site_weather.build_weather_block_for_site(info, date, today_iso)
    found = site_weather.build_weather_findings(info, date, today_iso, programme=programme)
    lines = list((found or {}).get("lines") or [])
    return daily, lines, ("computed" if (daily or lines) else "none")


def _totals(daily):
    temp = ""
    if daily.get("temp_min_c") is not None and daily.get("temp_max_c") is not None:
        temp = "%s-%s°C" % (daily["temp_min_c"], daily["temp_max_c"])
    rain = "%s mm" % daily["precip_mm"] if daily.get("precip_mm") is not None else ""
    wind = ("up to %s km/h" % daily["windspeed_kmh"]
            if daily.get("windspeed_kmh") is not None else "")
    return [daily.get("condition_label") or "", rain, temp, wind]


def weather_section(title, artifact, facts, s3, bucket, today_iso):
    """Returns (section, provenance per site)."""
    date = artifact.get("date") or (artifact.get("content") or {}).get("date")
    sites = [s for s in facts.get("sites") or [] if s.get("id") or s.get("latitude") is not None]
    rows, lines, sources = [], [], []
    many = len(sites) > 1
    for site in sites:
        daily, found, source = site_day_weather(s3, bucket, site, date, today_iso)
        sources.append({"site": site.get("id"), "source": source})
        if daily:
            rows.append(([site.get("name") or ""] if many else []) + _totals(daily))
        prefix = ("%s: " % site["name"]) if many and site.get("name") else ""
        lines.extend("- " + prefix + ln for ln in found)
    paragraphs = []
    if rows:
        head = (["Site"] if many else []) + ["Sky", "Rain", "Temperature", "Wind"]
        paragraphs += _table([head] + rows)
    paragraphs += lines
    return _section(title, paragraphs or [NOT_RECORDED]), sources


def build(placements, artifact, s3, bucket, today_iso):
    """The code sections, in placement order, and what each stood on."""
    facts = artifact.get("reportFacts") or {}
    built, meta = [], {}
    for p in placements:
        s = p["section"]
        title = s.get("title") or ""
        if kind_of(s) == "header":
            built.append(header_section(title, artifact, facts))
            meta[title] = {"kind": "header", "facts": bool(facts)}
        else:
            section, sources = weather_section(title, artifact, facts, s3, bucket, today_iso)
            built.append(section)
            meta[title] = {"kind": "weather", "sites": sources}
    return built, meta
