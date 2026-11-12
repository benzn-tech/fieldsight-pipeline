"""One walk, one report (spec 2026-11-03, Task 8).

A framing inspector walks Unit 1 and Unit 2 in one recording against one
checklist. The worker writes ONE report: the template's other sections from
the main call, General, one checklist per unit, a Summary grid, then the
catch-all -- with the answers he gave on site laid over it. A request without
a walk is exactly today's report.

THE test is `a walk report has one header, General, the units and a summary`.
"""
import datetime as dt
import hashlib
import io
import json
import logging

import pytest

import checklist
import lambda_session_report as sr
import walk_report

ITEMS = ["Studs straight and plumb", "Nogs fitted at 800 centres", "Lintels as per plan"]
TEMPLATE = {
    "sections": [
        {"title": "Inspection Details", "purpose": "Who walked which units."},
        {"title": "Framing Checklist", "purpose": "The framing checks.", "kind": "checklist",
         "items": ITEMS},
    ],
    "catch_all": {"title": "Anything else", "purpose": "Everything else."},
    "excluded_subjects": [], "style": [],
}
TITLE = "Framing Checklist"
U1, U2, U3 = ("Unit 1 — " + TITLE, "Unit 2 — " + TITLE, "Unit 3 — " + TITLE)

LINES = [
    (9, 0, 10, "the scaffold tag has expired on the east side"),
    (9, 1, 5, "now in unit one"),
    (9, 1, 30, "the studs are all straight here"),
    (9, 2, 10, "two nogs are missing by the door"),
    (9, 3, 5, "now in unit two"),
    (9, 3, 40, "this one is all good"),
    (9, 4, 20, "unit three lintels look right as per plan"),
]
STRETCHES = [{"unit": None, "from": "09:00:00", "to": "09:01:05"},
             {"unit": "Unit 1", "from": "09:01:05", "to": "09:03:05"},
             {"unit": "Unit 2", "from": "09:03:05", "to": None}]
WALK = {"key": "walk:abc", "units": {"names": ["Unit 1", "Unit 2"], "count": 2},
        "entered": [{"unit": "Unit 1", "at_s": "09:01:05"}, {"unit": "Unit 2", "at_s": "09:03:05"}],
        "stretches": STRETCHES, "answers": []}

MAIN_ANSWER = ("### Inspection Details\nFraming walk of two units.\n\n"
               "### Anything else\nNothing here.\n")
WALK_ANSWER = "\n".join([
    "Unit | Item no | Answer | Comment | Responsible | Due | Evidence",
    "---|---|---|---|---|---|---",
    "Unit 1 | 1 | Yes | Studs fine | | | the studs are all straight here",
    "Unit 1 | 2 | No | Nogs missing | | | two nogs are missing by the door",
    "General | | | Scaffold tag expired | | | the scaffold tag has expired",
    "Unit 2 | ALL | Yes | | | | this one is all good",
    "Unit 3 | 3 | Yes | Lintels right | | | lintels look right as per plan",
])
IS_WALK = "One row per (unit, checklist item)"


def _turns(lines):
    return [{"at": dt.datetime(2026, 11, 3, h, m, s),
             "line": "[%02d:%02d:%02d] Ben (spk_0): %s" % (h, m, s, text)}
            for h, m, s, text in lines]


@pytest.fixture
def run(monkeypatch):
    """Runs the worker on a fake recording; returns (meta, sections, prompts)."""
    def go(walk=None, lines=LINES, walk_answer=WALK_ANSWER, template=TEMPLATE,
           offer=None, streams=None, main_answer=MAIN_ANSWER):
        got = {"prompts": [], "sections": None}
        monkeypatch.setattr(sr.transcript_window, "select_keys",
                            lambda *a, **k: [(dt.datetime(2026, 11, 3, 9, 0), "t/x.json")])
        monkeypatch.setattr(sr.transcript_window, "assemble", lambda *a, **k: _turns(lines))

        def fake_llm(prompt, **kw):
            got["prompts"].append(prompt)
            return (walk_answer if IS_WALK in prompt else main_answer), None
        monkeypatch.setattr(sr.llm_utils, "call_llm", fake_llm)
        monkeypatch.setattr(sr.llm_utils, "active_model", lambda **k: "m")
        monkeypatch.setattr(sr.report_photos, "read_excluded", lambda *a, **k: set())
        if offer is not None:
            monkeypatch.setattr(sr, "_offered_topics", lambda *a, **k: (offer, streams))

        def fake_doc(title, subtitle, sections, actions, **kw):
            got["sections"] = sections
            got["closing"] = kw.get("closing")
            return io.BytesIO(b"docx")
        monkeypatch.setattr(sr.lambda_meeting_minutes, "generate_prose_document", fake_doc)
        artifact = {"requestId": "r", "folder": "Ben", "date": "2026-11-03",
                    "sessionId": "sid" + "a" * 32, "resultKey": "x", "title": "Framing walk",
                    "generate": {"templateId": "u", "templateVersion": 1,
                                 "templateName": "Framing", "templateBody": template,
                                 "templateSource": "library"},
                    "window": {"from": "09:00:00", "to": "09:10:00"}, "excludedTopics": [],
                    "content": {"date": "2026-11-03", "participants": ["Ben"], "topics": []}}
        if walk is not None:
            artifact["walk"] = walk
        _, meta = sr._generate_document(artifact)
        go.closing = got["closing"]
        return meta, got["sections"], got["prompts"]
    return go


def _titles(sections):
    return [s["title"] for s in sections]


# ---- THE test -----------------------------------------------------------------

def test_THE_a_walk_report_has_one_header_general_units_and_summary(run):
    meta, sections, prompts = run(walk=WALK)
    assert _titles(sections) == ["Inspection Details", "General", U1, U2, U3,
                                 "Summary", "Anything else"]
    main = [p for p in prompts if IS_WALK not in p]
    walks = [p for p in prompts if IS_WALK in p]
    assert len(main) == 1 and len(walks) == 1
    assert "Checklist items:" not in main[0], "the main call's plan has no checklist"
    assert "Checklist items:\n1. Studs straight and plumb" in walks[0]
    by = {s["title"]: s for s in sections}
    assert by["General"]["paragraphs"] == ['Scaffold tag expired — "the scaffold tag has expired"']
    assert by[U1]["paragraphs"][2:] == [
        "Studs straight and plumb | Yes | Studs fine |  | ",
        "Nogs fitted at 800 centres | No | Nogs missing |  | ",
        "Lintels as per plan |  | %s |  | " % checklist.NOT_COVERED]
    assert by[U2]["paragraphs"][2].startswith("Studs straight and plumb | Yes | covered by 'this one is all good' (09:03)")
    assert by[U3]["paragraphs"][0] == walk_report.UNLISTED
    assert by["Summary"]["paragraphs"] == [
        "Unit | Studs straight and plumb | Nogs fitted at 800 centres | Lintels as per plan",
        "--- | --- | --- | ---",
        "Unit 1 | Yes | No | Not checked",
        "Unit 2 | Yes | Yes | Yes",
        "Unit 3 | Not checked | Not checked | Yes"]
    # provenance: one checklist report per unit section, which the gaps reader reads
    assert list(meta["checklists"]) == [U1, U2, U3]
    assert meta["checklists"][U1]["gaps"]["failed"][0]["text"] == ITEMS[1]
    w = meta["walk"]
    assert w["key"] == "walk:abc"
    assert [u["unit"] for u in w["units"]] == ["Unit 1", "Unit 2", "Unit 3"]
    assert w["units"][0]["report"]["gaps"]["not_covered"] == [{"n": 3, "text": ITEMS[2]}]
    assert w["units"][0]["report"]["conflicts"] == []
    assert w["summary"]["rows"][0] == ["Unit 1", "Yes", "No", "Not checked"]
    assert (w["conflicts"], w["unmatched_answers"]) == ([], [])
    json.dumps(w)


def test_the_gaps_reader_reads_a_walk_result():
    org = pytest.importorskip("lambda_org_api")
    result = {"checklists": {U1: {"gaps": {"not_covered": [{"n": 3, "text": ITEMS[2]}], "failed": []}}}}
    assert org._checklist_gaps(result)["notCovered"] == [{"n": 3, "text": ITEMS[2], "checklist": U1}]


# ---- a long walk ---------------------------------------------------------------

def test_a_long_walk_is_split_at_unit_boundaries(run):
    def words(tag, n):
        return " ".join("%s%d" % (tag, i) for i in range(n))
    # Three stretches of ~2,500 words each, ten lines a stretch.
    lines = []
    for h, m, tag in ((9, 0, "gen"), (9, 2, "one"), (9, 4, "two")):
        for k in range(10):
            lines.append((h, m, k * 5, words("%s%d_" % (tag, k), 250)))
    stretches = [{"unit": None, "from": "09:00:00", "to": "09:02:00"},
                 {"unit": "Unit 1", "from": "09:02:00", "to": "09:04:00"},
                 {"unit": "Unit 2", "from": "09:04:00", "to": None}]
    walk = dict(WALK, stretches=stretches)
    meta, sections, prompts = run(walk=walk, lines=lines)
    walks = [p for p in prompts if IS_WALK in p]
    assert len(walks) >= 2, "over 6000 words is split"
    for tag in ("gen", "one", "two"):
        holding = [p for p in walks if "%s0_0" % tag in p]
        assert len(holding) == 1
        assert all("%s%d_0" % (tag, k) in holding[0] for k in range(10)), \
            "a stretch is never split across parts"
    for p in walks:
        body = p[p.index("Transcript:\n") + len("Transcript:\n"):].strip()
        assert all(ln.startswith("[09:") for ln in body.splitlines()), "lines keep their heads"
        assert sum(len(ln.split()) for ln in body.splitlines()) <= 6000 + 10 * 3
    # in order, and every line is in exactly one part
    bodies = "\n".join(p[p.index("Transcript:\n") + 12:].strip() for p in walks)
    assert bodies == "\n".join(t["line"] for t in _turns(lines))


def test_parts_never_end_inside_a_stretch_even_when_one_stretch_is_over_size():
    lines = [("09:00:%02d" % i, "[09:00:%02d] " % i + "w " * 100) for i in range(5)] + \
            [("09:01:%02d" % i, "[09:01:%02d] " % i + "w " * 100) for i in range(5)]
    st = [{"unit": "Unit 1", "from": "09:00:00", "to": "09:01:00"},
          {"unit": "Unit 2", "from": "09:01:00", "to": None}]
    got = walk_report.parts(lines, st, max_words=300)
    assert [len(p.splitlines()) for p in got] == [5, 5]
    assert walk_report.parts(lines, st, max_words=5000) == ["\n".join(ln for _, ln in lines)]


def test_a_walk_part_is_split_when_units_times_items_would_exceed_the_rows_cap():
    # Short speech, but 4 units x 60 items = 240 rows > 150: parts of 2 units each.
    lines, st = [], []
    for k in range(4):
        at = "09:0%d:00" % k
        lines.append((at, "[%s] in unit %d now" % (at, k + 1)))
        st.append({"unit": "Unit %d" % (k + 1), "from": at,
                   "to": "09:0%d:00" % (k + 1) if k < 3 else None})
    assert walk_report.MAX_PART_ROWS == 150
    got = walk_report.parts(lines, st, items=60)
    assert [len(p.splitlines()) for p in got] == [2, 2]
    assert walk_report.parts(lines, st, items=30) == ["\n".join(ln for _, ln in lines)]
    # A stretch with no unit adds no rows; a unit seen twice in a part counts once.
    st2 = [{"unit": None, "from": "09:00:00", "to": "09:01:00"},
           {"unit": "Unit 1", "from": "09:01:00", "to": "09:02:00"},
           {"unit": "Unit 2", "from": "09:02:00", "to": "09:03:00"},
           {"unit": "Unit 1", "from": "09:03:00", "to": None}]
    back = [("09:0%d:00" % k, "[09:0%d:00] line %d" % (k, k)) for k in range(4)]
    assert walk_report.parts(back, st2, items=75) == ["\n".join(ln for _, ln in back)]
    assert [len(p.splitlines()) for p in walk_report.parts(back, st2, items=76)] == [2, 1, 1]


def test_the_worker_splits_a_long_checklist_walk_by_rows(run):
    items = ["Item %d" % i for i in range(80)]           # 2 units x 80 = 160 rows
    tpl = dict(TEMPLATE, sections=[TEMPLATE["sections"][0],
                                   dict(TEMPLATE["sections"][1], items=items)])
    _, _, prompts = run(walk=WALK, template=tpl)
    assert len([p for p in prompts if IS_WALK in p]) == 2


# ---- photographs -------------------------------------------------------------------

def test_a_photo_goes_to_the_unit_current_at_its_time(run):
    names = ["Ben_2026-11-03_09-00-30.jpg",      # before the first unit: General
             "Ben_2026-11-03_09-02-00.jpg",      # Unit 1
             "Ben_2026-11-03_09-04-00.jpg",      # Unit 2
             "IMG_no_time.jpg"]                  # no time: the ordinary placement
    offer = [{"ref": "t0", "title": "Framing walk", "time_range": "09:00 - 09:05",
              "category": "inspection", "content": [], "photos": 4, "photo_names": names,
              "photos_left_out": 0}]
    streams = {"t0": ["p-general", "p-unit1", "p-unit2", "p-untimed"]}
    meta, sections, _ = run(walk=WALK, offer=offer, streams=streams)
    by = {s["title"]: s for s in sections}
    assert by["General"].get("photo_streams") == ["p-general"]
    assert by[U1].get("photo_streams") == ["p-unit1"]
    assert by[U2].get("photo_streams") == ["p-unit2"]
    assert not by[U3].get("photo_streams")
    elsewhere = [st for s in sections + [run.closing] if s["title"] not in ("General", U1, U2)
                 for st in (s.get("photo_streams") or []) + [x for v in (s.get("photos_after") or {}).values() for x in v]]
    assert elsewhere == ["p-untimed"], "every photograph appears once"
    assert meta["photosUnderASection"] == 3


def test_a_general_photo_makes_a_general_section_when_there_was_none(run):
    answer = "\n".join(WALK_ANSWER.splitlines()[:4])      # no General row
    offer = [{"ref": "t0", "title": "Walk", "time_range": "09:00 - 09:05", "category": "x",
              "content": [], "photos": 1, "photo_names": ["Ben_2026-11-03_09-00-30.jpg"],
              "photos_left_out": 0}]
    meta, sections, _ = run(walk=WALK, walk_answer=answer, offer=offer, streams={"t0": ["p"]})
    assert _titles(sections) == ["Inspection Details", "General", U1, U2, "Summary", "Anything else"]
    assert sections[1]["photo_streams"] == ["p"] and sections[1]["paragraphs"] == []


# ---- on-site answers ------------------------------------------------------------------

ANSWER_ROW = {"unit": "Unit 1", "item_text": ITEMS[1], "answer": "Yes", "comment": "fixed",
              "move_to_unit": None, "source": "final_check", "quote": "nogs are fitted now",
              "voice_note_id": "vn-1", "answered_at": "09:09:00"}


def test_artifact_answers_are_applied_and_missing_or_malformed_answers_are_not_fatal(run, caplog):
    meta, sections, _ = run(walk=dict(WALK, answers=[ANSWER_ROW, dict(ANSWER_ROW, unit="Unit 9")]))
    w = meta["walk"]
    assert w["conflicts"] == [{"unit": "Unit 1", "item": ITEMS[1], "on_site": "Yes", "recording": "No"}]
    assert meta["checklists"][U1]["conflicts"] == w["conflicts"]
    assert [a["unit"] for a in w["unmatched_answers"]] == ["Unit 9"]
    assert w["summary"]["rows"][0] == ["Unit 1", "Yes", "Check", "Not checked"]
    u1 = next(s for s in sections if s["title"] == U1)
    assert u1["paragraphs"][3].startswith("Nogs fitted at 800 centres |  | On site: Yes (09:09)")

    caplog.set_level(logging.INFO)
    for bad in ("garbage", [1, 2], [{"unit": None, "item_text": None, "answer": None}]):
        caplog.clear()
        meta, sections, _ = run(walk=dict(WALK, answers=bad))
        assert _titles(sections)[2] == U1
        assert meta["walk"]["conflicts"] == []
    missing = {k: v for k, v in WALK.items() if k != "answers"}
    caplog.clear()
    meta, sections, _ = run(walk=missing)
    assert meta["walk"]["summary"]["rows"][0] == ["Unit 1", "Yes", "No", "Not checked"]
    assert any("walk answers" in r.getMessage() for r in caplog.records)


def test_a_malformed_answer_list_is_logged(run, caplog):
    caplog.set_level(logging.INFO)
    run(walk=dict(WALK, answers="garbage"))
    assert any("walk answers" in r.getMessage() for r in caplog.records)


def test_a_walk_part_that_fails_fails_the_report(monkeypatch):
    calls = []

    def fails(prompt, **kw):
        calls.append(kw)
        return "", "provider down"
    monkeypatch.setattr(sr.llm_utils, "call_llm", fails)
    with pytest.raises(RuntimeError, match="walk checklist: provider down"):
        sr._walk_call("prompt", 30)
    assert calls[0]["deadline"] == 30


TWO = dict(TEMPLATE, sections=TEMPLATE["sections"] + [
    {"title": "Second", "purpose": "p", "kind": "checklist", "items": ["Bracing fixed"]}])


def test_a_walk_template_with_two_checklists_walks_all_its_items_under_the_template_name(run):
    assert walk_report.the_checklist(TWO, "Framing") == ("Framing", ITEMS + ["Bracing fixed"])
    assert walk_report.the_checklist(TEMPLATE, "Framing") == (TITLE, ITEMS)
    meta, sections, prompts = run(walk=WALK, template=TWO)
    one = "Unit 1 — Framing"
    assert _titles(sections) == ["Inspection Details", "General", one, "Unit 2 — Framing",
                                 "Unit 3 — Framing", "Summary", "Anything else"]
    walk_prompt = next(p for p in prompts if IS_WALK in p)
    assert "4. Bracing fixed" in walk_prompt
    assert meta["walk"]["summary"]["columns"] == ITEMS + ["Bracing fixed"]
    assert list(meta["checklists"])[0] == one


def test_a_walk_with_no_checklist_is_the_ordinary_report_and_says_so(run, caplog):
    caplog.set_level(logging.INFO)
    plain = dict(TEMPLATE, sections=TEMPLATE["sections"][:1])
    meta, sections, prompts = run(walk=WALK, template=plain)
    assert "walk" not in meta and len(prompts) == 1
    assert _titles(sections) == ["Inspection Details", "Anything else"]
    assert any("no checklist" in r.getMessage() for r in caplog.records)
    assert walk_report.the_checklist(plain, "Framing") is None


# ---- the header stays at the top --------------------------------------------------

HEADER = {"title": "Report Details", "purpose": "Who, where, when.", "kind": "header"}


def test_a_header_placed_before_the_checklist_stays_at_the_top(run):
    tpl = dict(TEMPLATE, sections=[HEADER, TEMPLATE["sections"][1], TEMPLATE["sections"][0]])
    meta, sections, _ = run(walk=WALK, template=tpl)
    assert _titles(sections) == ["Report Details", "Inspection Details", "General", U1, U2, U3,
                                 "Summary", "Anything else"]


def test_a_header_right_before_the_checklist_goes_before_general(run):
    tpl = dict(TEMPLATE, sections=[TEMPLATE["sections"][0], HEADER, TEMPLATE["sections"][1]])
    meta, sections, _ = run(walk=WALK, template=tpl)
    assert _titles(sections) == ["Inspection Details", "Report Details", "General", U1, U2, U3,
                                 "Summary", "Anything else"]


def test_a_header_placed_before_the_checklist_stays_at_the_top_without_general(run):
    answer = "\n".join(WALK_ANSWER.splitlines()[:4])      # no General row
    tpl = dict(TEMPLATE, sections=[TEMPLATE["sections"][0], HEADER, TEMPLATE["sections"][1]])
    meta, sections, _ = run(walk=WALK, template=tpl, walk_answer=answer)
    assert _titles(sections) == ["Inspection Details", "Report Details", U1, U2,
                                 "Summary", "Anything else"]


# ---- the ordinary placement's place hints ---------------------------------------------

def test_an_untimed_photo_is_placed_by_its_own_place_not_a_timed_ones(run, monkeypatch):
    seen = {}
    real = sr._place_photos

    def spy(sections, streams, **kw):
        seen["streams"], seen["places"] = streams, kw.get("places")
        return real(sections, streams, **kw)
    monkeypatch.setattr(sr, "_place_photos", spy)
    monkeypatch.setattr(sr.photo_binding, "place_at",
                        lambda markers, when: "Kitchen" if when else None)
    names = ["Ben_2026-11-03_09-02-00.jpg", "IMG_no_time.jpg"]
    offer = [{"ref": "t0", "title": "Walk", "time_range": "09:00 - 09:05", "category": "x",
              "content": [], "photos": 2, "photo_names": names, "photos_left_out": 0}]
    run(walk=WALK, offer=offer, streams={"t0": ["p-unit1", "p-untimed"]})
    assert seen["streams"] == {"t0": ["p-untimed"]}
    assert seen["places"] == {"t0": [None]}, "the untimed photo's place, not the timed one's"


def test_place_photos_returns_the_names_left():
    secs = [{"title": U1, "unit": "Unit 1", "paragraphs": [], "line_refs": []}]
    offer = [{"ref": "t0", "photo_names": ["a_2026-11-03_09-02-00.jpg", "b.jpg"]}]
    placed, left, names = walk_report.place_photos(
        secs, {"t0": ["A", "B"]}, offer, STRETCHES, sr.photo_binding.photo_time)
    assert (placed, left, names) == (1, {"t0": ["B"]}, {"t0": ["b.jpg"]})


# ---- calls not left running -------------------------------------------------------------

class FakeFuture:
    def __init__(self, value=None, error=None):
        self.value, self.error, self.cancelled, self.waited = value, error, False, False

    def cancel(self):
        self.cancelled = True
        return True

    def result(self, timeout=None):
        self.waited = True
        if self.error:
            raise self.error
        return self.value


def test_a_failed_main_call_cancels_the_walk_calls(run, monkeypatch):
    futures = [FakeFuture("x"), FakeFuture("y")]
    monkeypatch.setattr(sr, "_start_walk_calls", lambda *a, **k: futures)
    with pytest.raises(RuntimeError, match="empty answer"):
        run(walk=WALK, main_answer="")
    assert all(f.cancelled and not f.waited for f in futures)


def test_a_failed_walk_part_cancels_the_rest_and_does_not_wait_on_them(run, monkeypatch):
    futures = [FakeFuture(error=RuntimeError("walk checklist: down")), FakeFuture("y")]
    monkeypatch.setattr(sr, "_start_walk_calls", lambda *a, **k: futures)
    with pytest.raises(RuntimeError, match="walk checklist: down"):
        run(walk=WALK)
    assert futures[1].cancelled and not futures[1].waited


# ---- assembly edges ---------------------------------------------------------------------

def test_a_catch_all_with_no_title_does_not_crash_the_assembly():
    tpl = dict(TEMPLATE, catch_all={"title": None, "purpose": "x"})
    secs = walk_report.sections(tpl, [{"title": "Inspection Details", "paragraphs": ["a"]}],
                                {"units": [], "general": [], "summary": {"columns": ITEMS, "rows": []}},
                                "Framing")
    assert _titles(secs) == ["Inspection Details"]


def test_no_unit_rows_means_no_summary():
    secs = walk_report.sections(TEMPLATE, [], {"units": [], "general": [{"comment": "c", "evidence": "e"}],
                                               "summary": {"columns": ITEMS, "rows": []}}, "Framing")
    assert _titles(secs) == ["General"]


# ---- R44: a unit he walked into is a section, and nothing on site is dropped ----

ENTERED_LINES = [
    (9, 0, 10, "doing two units today"),
    (9, 1, 5, "now in unit one"),
    (9, 1, 30, "the studs are all straight here"),
    (9, 2, 10, "two nogs are missing by the door"),
    (9, 3, 5, "now in unit two"),
]
UNIT1_ONLY = "\n".join(WALK_ANSWER.splitlines()[:4])
COUNT_ONLY = dict(WALK, units={"names": [], "count": 2})
MOVE = {"unit": "Unit 1", "item_text": ITEMS[1], "answer": None, "comment": "",
        "move_to_unit": "Unit 2", "source": "device", "quote": "no that was unit two",
        "voice_note_id": None, "answered_at": "09:09:00", "asked_kind": "confirm_no"}


def test_r44_an_entered_unit_he_said_nothing_about_is_a_section_all_not_checked(run):
    meta, sections, _ = run(walk=COUNT_ONLY, lines=ENTERED_LINES, walk_answer=UNIT1_ONLY)
    assert _titles(sections) == ["Inspection Details", U1, U2, "Summary", "Anything else"]
    by = {s["title"]: s for s in sections}
    assert by[U2]["paragraphs"][2:] == ["%s |  | %s |  | " % (it, checklist.NOT_COVERED)
                                        for it in ITEMS]
    assert meta["walk"]["summary"]["rows"][1] == ["Unit 2"] + ["Not checked"] * 3


def test_r44_a_move_to_a_unit_known_only_by_entering_it_applies(run):
    meta, sections, _ = run(walk=dict(COUNT_ONLY, answers=[MOVE]), lines=ENTERED_LINES,
                            walk_answer=UNIT1_ONLY)
    w = meta["walk"]
    assert w["unmatched_answers"] == []
    assert w["summary"]["rows"] == [["Unit 1", "Yes", "Not checked", "Not checked"],
                                    ["Unit 2", "Not checked", "No", "Not checked"]]
    assert walk_report.NOT_APPLIED not in _titles(sections)


def test_r44_an_on_site_answer_that_cannot_be_applied_is_shown_in_the_report(run):
    lost = [dict(ANSWER_ROW, unit="Unit 9"),
            dict(MOVE, item_text=ITEMS[0], move_to_unit="Unit 2")]      # Unit 1 item 1 is a Yes
    meta, sections, _ = run(walk=dict(WALK, answers=lost))
    assert _titles(sections) == ["Inspection Details", "General", U1, U2, U3, "Summary",
                                 walk_report.NOT_APPLIED, "Anything else"]
    assert walk_report.NOT_APPLIED == "On-site answers not applied"
    by = {s["title"]: s for s in sections}
    assert by[walk_report.NOT_APPLIED]["paragraphs"] == [
        "Unit 9 — Nogs fitted at 800 centres: Yes (on site, 09:09) — unit not in the walk",
        "Unit 1 — Studs straight and plumb: move to Unit 2 (on site, 09:09) — "
        "moved item is not a No"]
    assert len(meta["walk"]["unmatched_answers"]) == 2


# ---- no walk: today's report ------------------------------------------------------------

# The prompt and the sections the renderer got, as the worker wrote them before
# the walk branch existed (captured on 44f6a273). Re-captured 2026-11-13: only the
# checklist shape sentence changed (spec 2026-11-13 D2/D4), checked by running the
# test against the old checklist.py.
NO_WALK_SNAPSHOT = "4b87420c017f4c3e70b0a5329dbaf27b4ae812c4b7a12d1db986cca0e7b3d4b9"


def _fingerprint(sections, prompts, meta):
    return hashlib.sha256(json.dumps(
        {"prompts": prompts, "sections": sections,
         "meta": {k: v for k, v in meta.items() if k != "promptChars"}},
        sort_keys=True, default=str).encode("utf-8")).hexdigest()


def test_no_walk_key_is_todays_report(run):
    main = ("### Inspection Details\nFraming walk.\n\n### Framing Checklist\n"
            "Item no | Answer | Comment | Responsible | Due | Evidence\n---|---|---|---|---|---\n"
            "1 | Yes | Studs fine | | | the studs are all straight here\n\n"
            "### Anything else\nNothing here.\n")
    meta, sections, prompts = run(main_answer=main)
    assert _titles(sections) == ["Inspection Details", "Framing Checklist", "Anything else"]
    assert len(prompts) == 1 and "Checklist items:\n1. Studs straight and plumb" in prompts[0]
    assert "walk" not in meta and list(meta["checklists"]) == ["Framing Checklist"]
    assert _fingerprint(sections, prompts, meta) == NO_WALK_SNAPSHOT


def test_two_units_all_goods_in_one_long_line_both_place_in_the_report(run):
    """D2 (TEST smoke 2026-11-06): one long turn timed 09:01:10, inside Unit 1's
    stretch, holds both units' all-goods. The entered marks' quotes place Unit
    2's by where it was said, so it is not dropped OUTSIDE."""
    lines = [(9, 1, 10, "now in unit one. everything else in this one is good. now in unit "
                        "two. everything else in this one is all good.")]
    walk = dict(WALK, entered=[
        {"unit": "Unit 1", "at_s": "09:01:05", "quote": "now in unit one"},
        {"unit": "Unit 2", "at_s": "09:03:05", "quote": "now in unit two"}])
    answer = "\n".join([
        "Unit | Item no | Answer | Comment | Responsible | Due | Evidence",
        "---|---|---|---|---|---|---",
        "Unit 1 | ALL | Yes | | | | everything else in this one is good",
        "Unit 2 | ALL | Yes | | | | everything else in this one is all good",
    ])
    meta, _, _ = run(walk=walk, lines=lines, walk_answer=answer)
    assert meta["walk"]["summary"]["rows"] == [["Unit 1", "Yes", "Yes", "Yes"],
                                               ["Unit 2", "Yes", "Yes", "Yes"]]


# ---- on-site corrections reach the prose (spec 2026-11-13 D3) ------------------

CORRECTIONS = "Corrections the inspector gave on site after the recording"


def _main_prompt(prompts):
    return next(p for p in prompts if IS_WALK not in p)


def test_a_move_and_a_read_back_override_reach_the_main_prompt_and_a_fill_does_not(run):
    move = dict(ANSWER_ROW, answer=None, move_to_unit="Unit 2", comment="that was unit two",
                asked_kind="confirm_no")
    override = dict(ANSWER_ROW, item_text=ITEMS[0], answer="No", comment="one stud bowed",
                    asked_kind="confirm_no")
    fill = dict(ANSWER_ROW, item_text=ITEMS[2], asked_kind="not_checked", comment="lintels fine")
    _, _, prompts = run(walk=dict(WALK, answers=[move, override, fill]))
    main = _main_prompt(prompts)
    block = main[main.index(CORRECTIONS):]
    assert "- Unit 1 -- %s: moved to Unit 2 (that was unit two)" % ITEMS[1] in block
    assert "- Unit 1 -- %s: No (one stud bowed)" % ITEMS[0] in block
    assert ITEMS[2] not in block
    assert main.index("## Transcript") < main.index(CORRECTIONS)     # in the data region
    assert not any(CORRECTIONS in p for p in prompts if IS_WALK in p)


def test_no_answers_or_no_walk_means_no_corrections_block(run):
    for walk in (WALK, dict(WALK, answers="garbage"), None):
        _, _, prompts = run(walk=walk)
        assert not any(CORRECTIONS in p for p in prompts)


def test_the_corrections_block_is_capped_and_one_line_each():
    import walk_report
    rows = [dict(ANSWER_ROW, answer="No", asked_kind="confirm_no",
                 comment="x\n" * 300) for _ in range(40)]
    block = walk_report.corrections_block(rows)
    lines = [ln for ln in block.splitlines() if ln.startswith("- ")]
    assert len(lines) == walk_report.MAX_CORRECTIONS == 30
    assert all(len(ln) <= walk_report.MAX_CORRECTION_CHARS for ln in lines)
    assert walk_report.corrections_block([]) == "" and walk_report.corrections_block(None) == ""
