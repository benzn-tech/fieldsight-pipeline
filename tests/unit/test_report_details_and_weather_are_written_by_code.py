"""A template's details and weather sections are written by code, not the model.

Owner, 2026-09-29: the code decides, the model words. The report's project,
client, date and author are facts the system holds; the weather is decided by
site_weather from measured values. A section of kind `header` or `weather`
never reaches the prompt -- it is written from the facts, in the place the
template put it.

THE test is `the model is never asked to write the weather, and the weather
it would have written is not what the document says`.
"""
import datetime as dt
import io
import json

import pytest

import lambda_session_report as sr
import report_facts
import report_template as rt

SITE = "d104ebbe-e294-4eb5-95bf-b153b623aa14"
FACTS = {"sites": [{"id": SITE, "name": "UC PK", "client": "Naylor Love",
                    "latitude": -43.52, "longitude": 172.58}],
         "recordedBy": "Ben Lin"}
RECORD = {"daily": {"condition_label": "Light rain", "temp_min_c": 7, "temp_max_c": 14,
                    "precip_mm": 6.2, "windspeed_kmh": 31},
          "lines": ["Rain likely this afternoon (from 13:00). Impact: roofing."]}

TEMPLATE = {"sections": [
    {"title": "Report details", "purpose": "Filled by FieldSight.", "kind": "header"},
    {"title": "Summary", "purpose": "The day."},
    {"title": "Weather", "purpose": "Filled by FieldSight.", "kind": "weather"},
    {"title": "Safety", "purpose": "Safety matters."}],
    "catch_all": {"title": "Anything else", "purpose": "Rest."}}


class NoSuchKey(Exception):
    pass


class FakeS3:
    exceptions = type("E", (), {"NoSuchKey": NoSuchKey})

    def __init__(self, objects=None):
        self.objects = objects or {}
        self.asked = []

    def get_object(self, Bucket, Key):
        self.asked.append(Key)
        if Key not in self.objects:
            raise NoSuchKey(Key)
        return {"Body": io.BytesIO(json.dumps(self.objects[Key]).encode())}


def artifact(**over):
    a = {"date": "2026-09-23", "folder": "Ben_UCPK2", "window": {"from": "07:00", "to": "16:30"},
         "content": {"siteNames": ["UC PK"]}, "reportFacts": FACTS}
    a.update(over)
    return a


# ---- the plan -----------------------------------------------------------------

def test_code_sections_leave_the_plan_and_remember_where_they_were():
    model, placements = report_facts.split_plan(TEMPLATE)
    assert [s["title"] for s in model["sections"]] == ["Summary", "Safety"]
    assert [(p["section"]["title"], p["before"]) for p in placements] == [
        ("Report details", "summary"), ("Weather", "safety")]


def test_they_go_back_in_front_of_the_section_they_preceded():
    model, placements = report_facts.split_plan(TEMPLATE)
    prose = sr._prose_sections("### Summary\nA day.\n\n### Safety\nNone.\n\n### Anything else\nNothing here.\n")
    built = [{"title": "Report details"}, {"title": "Weather"}]
    report_facts.insert(prose, placements, built, "Anything else")
    assert [s["title"] for s in prose] == ["Report details", "Summary", "Weather", "Safety",
                                           "Anything else"]


def test_a_code_section_whose_neighbour_the_model_dropped_goes_before_the_catch_all():
    model, placements = report_facts.split_plan(TEMPLATE)
    prose = sr._prose_sections("### Summary\nA day.\n\n### Anything else\nNothing here.\n")
    report_facts.insert(prose, placements, [{"title": "Report details"}, {"title": "Weather"}],
                        "Anything else")
    assert [s["title"] for s in prose] == ["Report details", "Summary", "Weather", "Anything else"]


# ---- the details ----------------------------------------------------------------

def test_the_details_come_from_the_facts():
    sec = report_facts.header_section("Report details", artifact(), FACTS)
    assert sec["paragraphs"] == [
        "Project | UC PK", "---|---", "Client | Naylor Love",
        "Date | Wednesday 23 September 2026", "Recorded by | Ben Lin",
        "Recording window | 07:00 - 16:30"]


def test_an_old_request_without_facts_still_gets_its_details():
    sec = report_facts.header_section("Report details", artifact(reportFacts=None), {})
    assert sec["paragraphs"][0] == "Project | UC PK"
    assert "Recorded by | Ben_UCPK2" in sec["paragraphs"]
    assert not any(p.startswith("Client") for p in sec["paragraphs"])


# ---- the weather ------------------------------------------------------------------

def test_the_nightly_record_is_what_the_weather_section_says():
    s3 = FakeS3({"weather/%s/2026-09-23/actual.json" % SITE: RECORD})
    sec, sources = report_facts.weather_section("Weather", artifact(), FACTS, s3, "b", "2026-10-01")
    assert sec["paragraphs"] == ["Sky | Rain | Temperature | Wind", "---|---|---|---",
                                 "Light rain | 6.2 mm | 7-14°C | up to 31 km/h",
                                 "- " + RECORD["lines"][0]]
    assert sources == [{"site": SITE, "source": "record"}]


def test_with_no_record_it_is_worked_out_by_the_same_code(monkeypatch):
    calls = {}
    monkeypatch.setattr(report_facts.site_weather, "build_weather_block_for_site",
                        lambda info, d, today: calls.setdefault("block", (info, d)) and RECORD["daily"])
    monkeypatch.setattr(report_facts.site_weather, "build_weather_findings",
                        lambda info, d, today, programme=None: {"lines": ["Dry day."]})
    sec, sources = report_facts.weather_section("Weather", artifact(), FACTS, FakeS3(), "b",
                                                "2026-10-01")
    assert calls["block"][0] == {"latitude": -43.52, "longitude": 172.58}
    assert sec["paragraphs"][-1] == "- Dry day."
    assert sources == [{"site": SITE, "source": "computed"}]


def test_a_past_day_never_reads_the_forecast():
    s3 = FakeS3({"weather/%s/2026-09-23/forecast.json" % SITE: RECORD})
    report_facts._stored(s3, "b", SITE, "2026-09-23", "2026-10-01")
    assert s3.asked == ["weather/%s/2026-09-23/actual.json" % SITE]


def test_no_weather_at_all_says_so_rather_than_reading_as_a_fine_day():
    facts = {"sites": [{"id": SITE, "name": "UC PK", "latitude": None, "longitude": None}]}
    sec, sources = report_facts.weather_section("Weather", artifact(), facts, FakeS3(), "b",
                                                "2026-10-01")
    assert sec["paragraphs"] == [report_facts.NOT_RECORDED]
    assert sources == [{"site": SITE, "source": "none"}]


def test_two_sites_are_told_apart():
    other = "e2e2e2e2-e2e2-e2e2-e2e2-e2e2e2e2e2e2"
    facts = {"sites": FACTS["sites"] + [{"id": other, "name": "Riccarton"}]}
    s3 = FakeS3({"weather/%s/2026-09-23/actual.json" % SITE: RECORD,
                 "weather/%s/2026-09-23/actual.json" % other: RECORD})
    sec, _ = report_facts.weather_section("Weather", artifact(), facts, s3, "b", "2026-10-01")
    assert sec["paragraphs"][0] == "Site | Sky | Rain | Temperature | Wind"
    assert "- UC PK: " + RECORD["lines"][0] in sec["paragraphs"]


# ---- the door --------------------------------------------------------------------

def _body(sections):
    return {"sections": sections, "catch_all": {"title": "Anything else", "purpose": "Rest."},
            "excluded_subjects": [], "style": []}


def test_the_door_accepts_the_code_kinds_at_the_top_only():
    assert rt.validate_body(_body([{"title": "Weather", "purpose": "p", "kind": "weather"}])) is None
    err = rt.validate_body(_body([{"title": "Day", "purpose": "p", "children": [
        {"title": "Weather", "purpose": "p", "kind": "weather"}]}]))
    assert "top level" in (err or "")


# ---- end to end through the worker ---------------------------------------------------

def test_THE_the_model_is_never_asked_to_write_the_weather(monkeypatch):
    prompts = []
    s3 = FakeS3({"weather/%s/2026-09-23/actual.json" % SITE: RECORD})
    monkeypatch.setattr(sr, "s3", lambda: s3)
    monkeypatch.setattr(sr.transcript_window, "select_keys",
                        lambda *a, **k: [(dt.datetime(2026, 9, 23, 9, 0), "t/x.json")])
    monkeypatch.setattr(sr.transcript_window, "assemble", lambda *a, **k: [
        {"at": dt.datetime(2026, 9, 23, 9, 0), "line": "[09:00:00] ben (spk_0): Sunny and dry all day."}])
    # What the model would say about the weather if it were asked: the
    # opposite of the record.
    answer = ("### Report details\nProject: somewhere else\n\n### Summary\nA day.\n\n"
              "### Weather\nSunny and dry all day.\n\n### Safety\nNothing here.\n\n"
              "### Anything else\nNothing here.\n")
    monkeypatch.setattr(sr.llm_utils, "call_llm",
                        lambda prompt, **kw: (prompts.append(prompt) or answer, None))
    monkeypatch.setattr(sr.llm_utils, "active_model", lambda **k: "m")
    a = artifact(requestId="r", sessionId="sid" + "a" * 32, resultKey="x", title="Daily",
                 generate={"templateId": "u", "templateVersion": 1, "templateName": "Daily",
                           "templateBody": TEMPLATE, "templateSource": "library"},
                 excludedTopics=[], content={"date": "2026-09-23", "topics": [],
                                             "siteNames": ["UC PK"]})
    buf, meta = sr._generate_document(a)
    plan = prompts[0][prompts[0].index("## Sections"):prompts[0].index("## The transcript")
                      if "## The transcript" in prompts[0] else None]
    assert "Weather" not in plan and "Report details" not in plan
    assert meta["codeFilled"]["Weather"] == {"kind": "weather",
                                             "sites": [{"site": SITE, "source": "record"}]}
    assert meta["modelCopiesDropped"] == ["Report details", "Weather"]
    from docx import Document
    d = Document(io.BytesIO(buf.getvalue()))
    body = []
    for el in d.element.body.iterchildren():
        tag = el.tag.split("}")[1]
        text = "".join(x.text or "" for x in el.iter() if x.tag.endswith("}t")).strip()
        body.append(("TABLE:" if tag == "tbl" else "") + text)
    texts = " ".join(body)
    assert "Sunny and dry" not in texts, "the model's weather is not what the document says"
    assert "somewhere else" not in texts
    assert "Light rain" in texts and "Naylor Love" in texts
    heads = [b for b in body if b in ("Report details", "Summary", "Weather", "Safety")]
    assert heads == ["Report details", "Summary", "Weather", "Safety"]
