"""A checklist is answered only from the recording, and never guessed.

Owner, 2026-09-30: inspection checklists (Penina, BICL) are fixed question
lists; an answer counts only if the words that answer it were said, and an
item nobody addressed stays EMPTY -- "Yes" on an item nobody checked is a
false compliance record. The model proposes answers with their evidence; the
code keeps an answer only if that evidence is in the transcript, and rebuilds
the table in the customer's order and wording.

THE test is `an answer whose evidence was never said is dropped, and its item
left blank`.
"""
import datetime as dt
import io

import pytest

import checklist
import lambda_session_report as sr
import report_template as rt

ITEMS = [
    "Is the toilet facility in a clean and sanitary condition?",
    "Is the lunchroom clean and tidy?",
    "Is rubbish cleared?",
    "Are sufficient fire extinguishers provided?",
    "Have evacuation drills been carried out in the last six months?",
]

TRANSCRIPT = "\n".join([
    "[09:01:02] clement (spk_0): Okay, the toilets, uh, they get serviced twice a week so they're fine.",
    "[09:02:10] clement (spk_0): Lunchroom is a bit of a mess actually, needs a tidy up by Friday.",
    "[09:03:40] clement (spk_0): Rubbish, contractor to remove the excess material.",
])


def model_section(rows):
    lines = ["Item no | Answer | Comment | Responsible | Due | Evidence", "---|---|---|---|---|---"] + rows
    return sr._prose_sections("### Site Amenities\n" + "\n".join(lines) + "\n")[0]


# ---- THE test -----------------------------------------------------------------

def test_THE_an_answer_whose_evidence_was_never_said_is_dropped_and_its_item_left_blank():
    sec = model_section([
        "2 | No | Needs a tidy up | Dom | Friday | Lunchroom is a bit of a mess actually",
        "5 | Yes | Drill done | Dom | | we ran the evacuation drill last month",   # never said
    ])
    paragraphs, refs, report = checklist.rebuild(sec, ITEMS, TRANSCRIPT)
    rows = [p.split(" | ") for p in paragraphs[2:]]
    assert [r[0] for r in rows] == ITEMS, "every item, in the customer's order and words"
    assert rows[1][1:] == ["No", "Needs a tidy up", "Dom", "Friday"]
    assert rows[4][1:] == ["", checklist.NOT_COVERED, "", ""], "the invented answer is gone"
    assert report["answered"] == 1
    assert report["dropped"] == [{"item": 5, "reason": "evidence not in the transcript"}]


# ---- what an answer must be ----------------------------------------------------

def test_items_nobody_addressed_stay_blank():
    paragraphs, _, report = checklist.rebuild(model_section([]), ITEMS, TRANSCRIPT)
    assert all(p.endswith(" |  | " + checklist.NOT_COVERED + " |  | ") for p in paragraphs[2:])
    assert report["answered"] == 0


@pytest.mark.parametrize("answer,kept", [("Yes", "Yes"), ("y", "Yes"), ("NO", "No"),
                                         ("n/a", "N/A"), ("Not applicable", "N/A")])
def test_answers_are_normalised(answer, kept):
    sec = model_section(["1 | %s | | | | the toilets, uh, they get serviced twice a week" % answer])
    assert checklist.rebuild(sec, ITEMS, TRANSCRIPT)[0][2].split(" | ")[1] == kept


@pytest.mark.parametrize("row,reason", [
    ("1 | Probably | | | | they get serviced twice a week", "answer is not Yes, No or N/A"),
    ("9 | Yes | | | | they get serviced twice a week", "no such item"),
    ("1 | Yes | | | | serviced", "evidence not in the transcript"),       # too short to count
])
def test_a_bad_row_is_dropped_with_its_reason(row, reason):
    _, _, report = checklist.rebuild(model_section([row]), ITEMS, TRANSCRIPT)
    assert report["dropped"][0]["reason"] == reason and report["answered"] == 0


def test_an_item_answered_twice_keeps_the_first():
    sec = model_section(["3 | Yes | | | | contractor to remove the excess material",
                         "3 | No | | | | contractor to remove the excess material"])
    paragraphs, _, report = checklist.rebuild(sec, ITEMS, TRANSCRIPT)
    assert paragraphs[4].split(" | ")[1] == "Yes"
    assert report["dropped"] == [{"item": 3, "reason": "answered twice"}]


def test_evidence_forgives_case_punctuation_and_a_dropped_filler():
    words = checklist._words(TRANSCRIPT)
    assert checklist.evidence_found("the toilets they get serviced twice a week", words)
    assert checklist.evidence_found("LUNCHROOM is a bit of a mess, actually.", words)
    assert not checklist.evidence_found("the toilets were inspected by the council", words)


# A sentence the speaker said across two turns (or two recording chunks) is one
# sentence: the "[time] name (spk):" heads between them were never said.
# Measured 2026-10-21 on TEST: the weather item, answered Yes by the model, was
# dropped in 6 of 7 runs because its quote ran over a "[10:56:18]" head.

def test_a_quote_said_across_a_turn_boundary_is_found():
    t = checklist.transcript_words(TRANSCRIPT)
    assert checklist.evidence_found(
        "they get serviced twice a week so they're fine. Lunchroom is a bit of a mess", t)
    assert not checklist.evidence_found(
        "they get serviced twice a week so they're fine. clement spk 0 Lunchroom", t), \
        "the head is not speech, so a quote that includes it is not in the recording"


def test_a_quote_across_a_chunk_seam_in_the_check_done_format_is_found():
    text = ("[10:55:40] Weather looks okay for Thursday, but we have got covers on standby ju-\n"
            "[10:56:18] We have got covers on standby just in case. Vibrators, two on site.")
    assert checklist.evidence_found(
        "Weather looks okay for Thursday, but we have got covers on standby ju- "
        "We have got covers on standby just in case.", checklist.transcript_words(text))


def test_only_the_head_is_dropped_a_colon_in_speech_stays():
    assert checklist.transcript_words("[09:01:02] Note: the gate is locked") == \
        ["note", "the", "gate", "is", "locked"]
    assert checklist.transcript_words("[09:01:02 – 09:01:09] Ben Lin (spk_1): Note: the gate") == \
        ["note", "the", "gate"]


def test_rebuild_keeps_an_answer_whose_quote_spans_two_turns():
    sec = model_section(["1 | Yes | Serviced | | | they get serviced twice a week so they're fine. "
                         "Lunchroom is a bit of a mess"])
    _p, _r, report = checklist.rebuild(sec, ITEMS, TRANSCRIPT)
    assert report["answered"] == 1 and report["dropped"] == []


def test_a_header_row_starting_with_a_hash_is_not_read_as_a_heading():
    # The model's habit of numbering a table "# | ..." split the section in two.
    sec = sr._prose_sections("### Site Amenities\n# | Answer | Evidence\n---|---|---\n1 | Yes | x\n")
    assert len(sec) == 1 and sec[0]["title"] == "Site Amenities" and len(sec[0]["paragraphs"]) == 3


# ---- the whole section ------------------------------------------------------------

TEMPLATE = {"sections": [
    {"title": "Summary", "purpose": "The day."},
    {"title": "Site Amenities", "purpose": "Amenities check.", "kind": "checklist", "items": ITEMS}],
    "catch_all": {"title": "Anything else", "purpose": "Rest."}}


def test_a_checklist_the_model_left_out_is_added_blank_before_the_catch_all():
    prose = sr._prose_sections("### Summary\nA day.\n\n### Anything else\nNothing here.\n")
    reports = checklist.apply(prose, TEMPLATE, TRANSCRIPT)
    assert [s["title"] for s in prose] == ["Summary", "Site Amenities", "Anything else"]
    assert reports["Site Amenities"]["omitted_by_model"] is True
    assert len(prose[1]["paragraphs"]) == 2 + len(ITEMS)


def test_a_rows_topic_tag_survives_the_rebuild_for_its_photographs():
    sec = model_section(["2 | No | Tidy up | Dom | Friday | Lunchroom is a bit of a mess actually [t3]"])
    _, refs, _ = checklist.rebuild(sec, ITEMS, TRANSCRIPT)
    assert refs[2 + 1] == ["t3"] and refs[2] == []


# ---- the prompt and the door --------------------------------------------------------

SCOPE = {"folder": "F", "date": "2026-09-30", "from": "00:00", "to": "23:59", "recordings": 1}


def test_the_items_are_numbered_inside_the_customer_region_and_the_shape_is_ours():
    p = rt.render_prompt(TEMPLATE, SCOPE, [], "x", source=rt.SOURCE_LIBRARY)
    fence = p[p.index(rt.FENCE_BEGIN):p.index(rt.FENCE_END)]
    assert "Checklist items:\n1. Is the toilet facility" in fence
    shape = p[p.index(rt.FENCE_END):]
    assert "Item no | Answer | Comment | Responsible | Due | Evidence" in shape
    assert "do not guess" in shape


@pytest.mark.parametrize("items,err", [(None, "at least one item"), ([], "at least one item"),
                                       (["ok", " "], "item 2 is empty"),
                                       (["x" * 301], "longer than 300")])
def test_a_checklist_without_proper_items_is_refused_at_the_door(items, err):
    body = {"sections": [{"title": "Check", "purpose": "p", "kind": "checklist", "items": items}],
            "catch_all": {"title": "Anything else", "purpose": "Rest."},
            "excluded_subjects": [], "style": []}
    assert err in (rt.validate_body(body) or "")


# ---- end to end through the worker ---------------------------------------------------

@pytest.fixture
def run(monkeypatch):
    def go(answer):
        monkeypatch.setattr(sr.transcript_window, "select_keys",
                            lambda *a, **k: [(dt.datetime(2026, 9, 30, 9, 0), "t/x.json")])
        monkeypatch.setattr(sr.transcript_window, "assemble", lambda *a, **k: [
            {"at": dt.datetime(2026, 9, 30, 9, 0), "line": line} for line in TRANSCRIPT.split("\n")])
        monkeypatch.setattr(sr.llm_utils, "call_llm", lambda prompt, **kw: (answer, None))
        monkeypatch.setattr(sr.llm_utils, "active_model", lambda **k: "m")
        artifact = {"requestId": "r", "folder": "Clement", "date": "2026-09-30",
                    "sessionId": "sid" + "a" * 32, "resultKey": "x", "title": "Inspection",
                    "generate": {"templateId": "u", "templateVersion": 1, "templateName": "Site inspection",
                                 "templateBody": TEMPLATE, "templateSource": "library"},
                    "window": {"from": "00:00", "to": "23:59"}, "excludedTopics": [],
                    "content": {"date": "2026-09-30", "participants": ["Clement"], "topics": []}}
        return sr._generate_document(artifact)
    return go


def test_the_worker_records_what_each_answer_stood_on(run):
    answer = ("### Summary\nAmenities checked.\n\n### Site Amenities\n"
              "# | Answer | Comment | Responsible | Due | Evidence\n---|---|---|---|---|---\n"
              "1 | Yes | Serviced twice weekly | | | they get serviced twice a week\n"
              "4 | Yes | | | | there are plenty of extinguishers\n\n"
              "### Anything else\nNothing here.\n")
    buf, meta = run(answer)
    report = meta["checklists"]["Site Amenities"]
    assert report["answered"] == 1 and report["evidence"] == {1: "they get serviced twice a week"}
    assert report["dropped"] == [{"item": 4, "reason": "evidence not in the transcript"}]
    assert buf is not None
    from docx import Document
    d = Document(io.BytesIO(buf.getvalue()))
    table = d.tables[0]
    assert [c.text for c in table.rows[0].cells] == checklist.DOC_COLUMNS
    assert [c.text for c in table.rows[1].cells] == [ITEMS[0], "Yes", "Serviced twice weekly", "", ""]
    assert [c.text for c in table.rows[4].cells][1:] == ["", checklist.NOT_COVERED, "", ""], \
        "item 4: no answer, and it says why"


# ---- the gaps: what the inspector did not do -----------------------------------

ITEMS_G = ["Formwork braced | plumb", "Edge protection installed",
           "Cover to steel checked", "Pour booked", "Hand rails on stair"]
TRANSCRIPT_G = ("formwork is braced and plumb all good. "
                "edge protection is not installed on the north side, Mike will fix it by friday. "
                "stair rails not applicable today")


def gaps_rows():
    # Item 1 Yes with evidence; item 2 No with evidence; item 3 No WITHOUT
    # evidence (dropped); item 4 not mentioned; item 5 N/A with evidence.
    return [
        "1 | Yes | | | | formwork is braced and plumb",
        "2 | No | rail missing north side | Mike | friday | edge protection is not installed on the north side",
        "3 | No | | | | cover was never measured at all",
        "5 | N/A | | | | stair rails not applicable today",
    ]


def test_THE_gaps_are_the_blank_items_and_the_no_answers():
    sec = model_section(gaps_rows())
    _, _, report = checklist.rebuild(sec, ITEMS_G, TRANSCRIPT_G)
    gaps = report["gaps"]
    assert [g["n"] for g in gaps["not_covered"]] == [3, 4]
    assert [g["text"] for g in gaps["not_covered"]] == ["Cover to steel checked", "Pour booked"]
    assert len(gaps["failed"]) == 1
    f = gaps["failed"][0]
    assert f["n"] == 2 and f["text"] == "Edge protection installed"
    assert f["comment"] == "rail missing north side" and f["responsible"] == "Mike"
    assert f["due"] == "friday"


def test_a_gap_keeps_the_customers_wording_even_with_a_pipe():
    _, _, report = checklist.rebuild(model_section([]), ITEMS_G, TRANSCRIPT_G)
    assert report["gaps"]["not_covered"][0]["text"] == "Formwork braced | plumb"
    assert len(report["gaps"]["not_covered"]) == 5 and report["gaps"]["failed"] == []


def test_a_checklist_the_model_left_out_has_every_item_not_covered():
    tpl = {"sections": [{"title": "Pre-pour", "kind": "checklist", "items": ITEMS_G}],
           "catch_all": {"title": "Other"}}
    prose = [{"title": "Other", "paragraphs": [], "line_refs": []}]
    reports = checklist.apply(prose, tpl, TRANSCRIPT_G)
    assert [g["n"] for g in reports["Pre-pour"]["gaps"]["not_covered"]] == [1, 2, 3, 4, 5]
