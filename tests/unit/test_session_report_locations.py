"""The generated (template) report gains a Locations section (Track L Task 6).

Spec: docs/superpowers/specs/2026-11-09-track-l-task6-locations-in-generated-report.md
(its "Review changes" section overrides the text above it).

One section, `Locations`, after the template's own sections and before the
Actions table: one line per place in visit order, the place in bold, then the
titles of the report's topics recorded there -- the day report's own entries
(report_sections.locations_section). Behind REPORT_LOCATIONS_SECTION, read at
call time. Never in a walk report; never holds a photograph; never moves one.

Read out of a RENDERED document by position. The docx bytes are never compared
(core properties carry a timestamp): the flow of headings, paragraphs and
per-heading photograph counts is.
"""
import base64
import datetime as dt
import io
import logging

import pytest

import lambda_session_report as sr

lmm = pytest.importorskip("lambda_meeting_minutes")
needs_docx = pytest.mark.skipif(not getattr(lmm, "DOCX_AVAILABLE", False),
                                reason="python-docx is not installed here")

BLIP = "{http://schemas.openxmlformats.org/drawingml/2006/main}blip"
PNG = base64.b64decode(
    b"iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mP8"
    b"z8BQDwAEhQGAhKmMIQAAAABJRU5ErkJggg==")

BODY = {
    "sections": [{"key": "sum", "title": "Daily Summary", "purpose": "The day."},
                 {"key": "q", "title": "Quality", "purpose": "Checks."}],
    "catch_all": {"key": "other", "title": "Anything else", "purpose": "The rest."},
    "excluded_subjects": [], "style": [],
}
TEMPLATE_HEADINGS = ["Daily Summary", "Quality", "Anything else"]

# t0 at Level 1 with a photograph its Quality line claims (tier 1); t1 at
# Level 2 with a photograph only the SUMMARY names, so it falls to the last
# heading (tier 3) -- the photograph a Locations section appended too early
# would catch; t2 at Level 2, no photograph. Every topic is named, so there
# is no coverage note and the last heading is the template's catch-all.
TOPICS = [
    {"topic_title": "Formwork check", "time_range": "13:20 - 13:22",
     "related_photos": ["20260923_132100.jpg"],
     "action_items": [{"action": "Fix the brace", "responsible": "Ben", "deadline": None}]},
    {"topic_title": "Rebar spacing", "time_range": "13:38 - 13:39",
     "related_photos": ["20260923_133830.jpg"]},
    {"topic_title": "Scaffold tag", "time_range": "13:39 - 13:40"},
]
MARKERS = [{"at": "13:15", "location": "Level 1"}, {"at": "13:35", "location": "Level 2"}]

ANSWER = ("### Daily Summary\nRebar spacing was looked at. [t1]\n\n"
          "### Quality\nFormwork was checked. [t0]\n\n"
          "### Anything else\nThe scaffold tag was renewed. [t2]\n")


def _artifact(markers=MARKERS, topics=TOPICS, walk=None, template=BODY):
    a = {
        "requestId": "r1", "folder": "Ben_UCPK2", "date": "2026-09-23",
        "sessionId": "sid" + "a" * 32, "resultKey": "session_report_results/x.json",
        "title": "Day report", "deliver": "download",
        "generate": {"templateId": "u", "templateVersion": 1, "templateName": "Daily",
                     "templateBody": template, "templateSource": "library"},
        "window": {"from": "00:00", "to": "23:59"},
        "excludedTopics": [],
        "content": {"date": "2026-09-23", "participants": ["Ben"],
                    "topics": [dict(t) for t in topics]},
    }
    if markers is not None:
        a["reportFacts"] = {"locations": markers}
    if walk is not None:
        a["walk"] = walk
    return a


@pytest.fixture
def run(monkeypatch):
    def go(flag, artifact=None, answer=ANSWER, capture=False, llm=None):
        if flag is None:
            monkeypatch.delenv("REPORT_LOCATIONS_SECTION", raising=False)
        else:
            monkeypatch.setenv("REPORT_LOCATIONS_SECTION", flag)
        monkeypatch.setattr(sr.transcript_window, "select_keys",
                            lambda *a, **k: [(dt.datetime(2026, 9, 23, 13, 20), "t/x.json")])
        monkeypatch.setattr(sr.transcript_window, "assemble",
                            lambda *a, **k: [{"at": dt.datetime(2026, 9, 23, 13, 20),
                                              "line": "[13:20:00] Ben: hello"}])
        monkeypatch.setattr(sr.llm_utils, "call_llm",
                            llm or (lambda prompt, **kw: (answer, None)))
        monkeypatch.setattr(sr.llm_utils, "active_model", lambda **k: "m")
        monkeypatch.setattr(sr.report_photos, "read_excluded", lambda *a, **k: set())

        def fetch(folder, date, names, budget, names_out=None, edge=None):
            if names_out is not None:
                names_out.extend(names)
            return [io.BytesIO(PNG) for _ in names]
        monkeypatch.setattr(sr, "_fetch_photos", fetch)
        got = {}
        if capture:
            def fake_doc(title, subtitle, sections, actions, **kw):
                got["sections"] = sections
                return io.BytesIO(b"docx")
            monkeypatch.setattr(sr.lambda_meeting_minutes, "generate_prose_document", fake_doc)
        buf, meta = sr._generate_document(artifact or _artifact())
        got.update(buf=buf, meta=meta)
        return got
    return go


def _flow(buf):
    """The document in order: ("h", title) | ("p", text) | ("table", first header),
    with ("img", n) after any paragraph or table holding n photographs."""
    from docx import Document
    doc = Document(io.BytesIO(buf.getvalue()))
    paras = {p._p: p for p in doc.paragraphs}
    tables = {t._tbl: t for t in doc.tables}
    out = []
    for el in doc.element.body.iterchildren():
        if el in tables:
            out.append(("table", tables[el].rows[0].cells[0].text))
        elif el in paras:
            p = paras[el]
            if p.text.strip():
                out.append(("h" if p.style.name.startswith("Heading") else "p", p.text.strip()))
        n = len(el.findall(".//" + BLIP))
        if n:
            out.append(("img", n))
    return out


def _headings(flow):
    return [x[1] for x in flow if x[0] == "h"]


def _blips_by_heading(flow):
    """Photographs under each heading (before the first heading: key None)."""
    out, cur = {}, None
    for kind, val in flow:
        if kind == "h":
            cur = val
            out.setdefault(cur, 0)
        elif kind == "img":
            out[cur] = out.get(cur, 0) + val
    return out


def _under(flow, heading):
    i = flow.index(("h", heading))
    rest = flow[i + 1:]
    end = next((k for k, x in enumerate(rest) if x[0] in ("h", "table")), len(rest))
    return rest[:end]


# ---- THE test -----------------------------------------------------------------

@needs_docx
def test_THE_two_places_make_one_locations_section_in_visit_order(run):
    flow = _flow(run("true")["buf"])
    assert _headings(flow).count("Locations") == 1
    assert _headings(flow)[:4] == TEMPLATE_HEADINGS + ["Locations"], \
        "after the template's sections"
    assert flow.index(("h", "Locations")) < flow.index(("table", "Action")), \
        "before the Actions table"
    assert _under(flow, "Locations") == [
        ("p", "Level 1 — Formwork check"),
        ("p", "Level 2 — Rebar spacing · Scaffold tag"),
    ]


@needs_docx
def test_the_place_is_bold_and_the_titles_are_not(run):
    from docx import Document
    doc = Document(io.BytesIO(run("true")["buf"].getvalue()))
    line = next(p for p in doc.paragraphs if p.text.startswith("Level 1"))
    bold = "".join(r.text for r in line.runs if r.bold)
    assert bold == "Level 1"


# ---- photographs and the template are untouched ----------------------------------

@needs_docx
def test_photographs_are_where_they_were_with_the_flag_on_and_off(run):
    on = _flow(run("true")["buf"])
    off = _flow(run("false")["buf"])
    assert sum(n for k, n in on if k == "img") == sum(n for k, n in off if k == "img") == 2
    by_on, by_off = _blips_by_heading(on), _blips_by_heading(off)
    assert by_on.pop("Locations") == 0, "Locations never holds a photograph"
    assert by_on == by_off
    # The tier-3 photograph is under the template's last heading, captioned there.
    assert by_off["Anything else"] == 1
    assert ("p", "Photos: Rebar spacing (13:38 - 13:39)") in _under(on, "Anything else")
    assert not any(x[1].startswith("Photos:") for x in _under(on, "Locations"))


@needs_docx
def test_the_template_headings_in_order_are_the_same_with_the_flag_on_and_off(run):
    on = [h for h in _headings(_flow(run("true")["buf"])) if h != "Locations"]
    off = _headings(_flow(run("false")["buf"]))
    assert on == off
    assert off[:3] == TEMPLATE_HEADINGS


@needs_docx
def test_flag_off_is_the_report_without_markers(run):
    with_markers = _flow(run("false")["buf"])
    without = _flow(run("false", artifact=_artifact(markers=None))["buf"])
    unset = _flow(run(None)["buf"])
    assert with_markers == without == unset
    assert ("h", "Locations") not in with_markers


# ---- absent, never empty --------------------------------------------------------------

@needs_docx
def test_no_markers_no_section(run, caplog):
    caplog.set_level(logging.INFO)
    flow = _flow(run("true", artifact=_artifact(markers=None))["buf"])
    assert ("h", "Locations") not in flow
    assert "locations: absent (no markers)" in caplog.text


def _sections(got):
    return [s["title"] for s in got["sections"]]


def test_only_topics_before_the_first_announcement_is_absent(run, caplog):
    """Every topic is before the day's only marker: the day report would show
    one "Before any location was announced" row. That says nothing here."""
    caplog.set_level(logging.INFO)
    got = run("true", artifact=_artifact(markers=[{"at": "15:00", "location": "Level 3"}]),
              capture=True)
    assert "Locations" not in _sections(got)
    assert "locations: absent (no topic at a named place)" in caplog.text


def test_only_topics_after_the_last_announcement_expired_is_absent(run):
    got = run("true", artifact=_artifact(markers=[{"at": "12:00", "location": "Gate"}]),
              capture=True)
    assert "Locations" not in _sections(got)


def test_unplaced_rows_stay_when_one_topic_is_placed(run, caplog):
    """Rule 3: groups without a place keep the day report's headings."""
    caplog.set_level(logging.INFO)
    got = run("true", artifact=_artifact(markers=[{"at": "13:30", "location": "Level 2"}]),
              capture=True)
    loc = next(s for s in got["sections"] if s["title"] == "Locations")
    assert loc["paragraphs"] == [
        "**Before any location was announced** — Formwork check",
        "**Level 2** — Rebar spacing · Scaffold tag",
    ]
    assert loc["line_refs"] == [[], []]
    assert not loc.get("photos_after") and not loc.get("photo_streams")
    assert "locations: 1 places, 2 topics placed, 1 unplaced" in caplog.text


def test_the_info_line_counts_places_and_topics(run, caplog):
    caplog.set_level(logging.INFO)
    run("true", capture=True)
    assert "locations: 2 places, 3 topics placed, 0 unplaced" in caplog.text


def test_the_section_is_last_before_rendering(run):
    got = run("true", capture=True)
    assert _sections(got)[-1] == "Locations"


# ---- walks -------------------------------------------------------------------------------

WALK_BODY = {
    "sections": [{"title": "Inspection Details", "purpose": "Who walked which units."},
                 {"title": "Framing Checklist", "purpose": "The framing checks.",
                  "kind": "checklist", "items": ["Studs straight and plumb"]}],
    "catch_all": {"title": "Anything else", "purpose": "Everything else."},
    "excluded_subjects": [], "style": [],
}
WALK = {"key": "walk:abc", "units": {"names": ["Unit 1"], "count": 1},
        "entered": [{"unit": "Unit 1", "at_s": "13:15:00"}],
        "stretches": [{"unit": "Unit 1", "from": "13:15:00", "to": None}], "answers": []}
WALK_ANSWER = "\n".join([
    "Unit | Item no | Answer | Comment | Responsible | Due | Evidence",
    "---|---|---|---|---|---|---",
    "Unit 1 | 1 | Yes | Studs fine | | | hello",
])


def test_a_walk_report_has_no_locations_section(run, caplog):
    caplog.set_level(logging.INFO)

    def llm(prompt, **kw):
        if "One row per (unit, checklist item)" in prompt:
            return WALK_ANSWER, None
        return "### Inspection Details\nA walk. [t0]\n", None
    got = run("true", artifact=_artifact(walk=WALK, template=WALK_BODY), capture=True, llm=llm)
    assert any("Framing Checklist" in t for t in _sections(got)), "it was a walk report"
    assert "Locations" not in _sections(got)
    assert "locations: absent (walk report)" in caplog.text


def test_a_walk_demoted_to_an_ordinary_report_has_the_section(run):
    """A walk whose template has no checklist is written as an ordinary report;
    the local `walk` is None and it is treated as one."""
    got = run("true", artifact=_artifact(walk=WALK), capture=True)
    assert "Locations" in _sections(got)


# ---- fail open ---------------------------------------------------------------------------

@needs_docx
def test_a_grouping_that_raises_leaves_the_report_without_locations(run, monkeypatch):
    import location_grouping

    def boom(*a, **k):
        raise RuntimeError("boom")
    monkeypatch.setattr(location_grouping, "group_day", boom)
    got = run("true")
    flow = _flow(got["buf"])
    assert ("h", "Locations") not in flow
    assert _headings(flow) == _headings(_flow(run("false")["buf"]))


def test_a_builder_that_raises_leaves_the_report_without_locations(run, monkeypatch, caplog):
    import report_sections

    def boom(*a, **k):
        raise RuntimeError("boom")
    monkeypatch.setattr(report_sections, "locations_section", boom)
    got = run("true", capture=True)
    assert "Locations" not in _sections(got)
    assert "locations: could not be built" in caplog.text


# ---- the switch is wired (template parameter, function env, both workflows) -------------

def _repo_text(*parts):
    import os
    root = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    with open(os.path.join(root, *parts), encoding="utf-8") as f:
        return f.read()


def test_the_flag_is_a_parameter_defaulting_off_and_reaches_the_worker():
    import re
    text = _repo_text("src", "template.yaml").replace("\r\n", "\n")
    block = re.search(r"\n  ReportLocationsSection:\n(.*?)(?=\n  \w+:\n)", text, re.S)
    assert block, "no ReportLocationsSection parameter"
    assert "Default: 'false'" in block.group(1)
    assert "AllowedValues: ['true', 'false']" in block.group(1)
    start = text.index("\n  SessionReportFunction:\n")
    nxt = re.search(r"\n  [A-Za-z][A-Za-z0-9]*:\n", text[start + 1:])
    fn = text[start:start + 1 + nxt.start()]
    assert "REPORT_LOCATIONS_SECTION: !Ref ReportLocationsSection" in fn
    assert "CodeUri: src/" in fn, "the worker bundles src/, so location_locate.py ships"


def test_test_turns_it_on_and_prod_leaves_it_to_the_owner():
    assert ("\"ReportLocationsSection=${{ vars.TEST_REPORT_LOCATIONS_SECTION || 'true' }}\" \\"
            in _repo_text(".github", "workflows", "deploy.yml"))
    assert ("\"ReportLocationsSection=${{ vars.PROD_REPORT_LOCATIONS_SECTION || 'false' }}\" \\"
            in _repo_text(".github", "workflows", "deploy-prod.yml"))
