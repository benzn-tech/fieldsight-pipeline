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
    assert rows[4][1:] == ["", "", "", ""], "the invented answer is gone"
    assert report["answered"] == 1
    assert report["dropped"] == [{"item": 5, "reason": "evidence not in the transcript"}]


# ---- what an answer must be ----------------------------------------------------

def test_items_nobody_addressed_stay_blank():
    paragraphs, _, report = checklist.rebuild(model_section([]), ITEMS, TRANSCRIPT)
    assert all(p.endswith(" |  |  |  | ") for p in paragraphs[2:])
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
    assert [c.text for c in table.rows[4].cells][1:] == ["", "", "", ""], "item 4 left blank"
