"""A summary holds no photographs, and no tag ever reaches the page.

Owner review, TEST 2026-10-01 (Ben_Lin_test2, template v4):
- the Daily Summary took the inspection photographs; the owner wants the
  overview free of them and the photographs in the sections with detail;
- "[t2]" printed in the checklist's Comment column -- a tag at the end of a
  CELL was not recognised, only one at the end of a line;
- the "Inspections" section was the Visitors module, whose wording excludes
  the day's own inspections, so it said "Nothing here.";
- the checklist's Comment / Responsible / Due had no meaning given.

THE test is `a topic named only in the summary is captioned at the end, not
put in the summary`.
"""
import checklist
import lambda_session_report as sr
import report_modules as rm

TEMPLATE = {"sections": [{"title": "Daily Summary", "purpose": "p"},
                         {"title": "Quality", "purpose": "p"}]}


def test_THE_a_topic_named_only_in_the_summary_is_captioned_at_the_end():
    secs = sr._prose_sections("### Daily Summary\nWalked level 1. [t2]\n\n"
                              "### Quality\n- Nothing logged.\n\n### Anything else\nNothing here.\n")
    _, _, orphaned = sr._place_photos(secs, {"t2": ["p1", "p2"]},
                                      no_photos=sr._summary_titles(TEMPLATE),
                                      captions={"t2": "Level 1 inspection (13:26 - 13:28)"})
    assert "photos_after" not in secs[0] and "photo_streams" not in secs[0]
    assert orphaned == 2
    last = secs[-1]
    assert last["paragraphs"][-1] == "Photos: Level 1 inspection (13:26 - 13:28)"
    assert last["photos_after"][len(last["paragraphs"]) - 1] == ["p1", "p2"]


def test_a_topic_named_in_the_summary_and_elsewhere_goes_elsewhere_even_as_prose():
    secs = sr._prose_sections("### Daily Summary\n- Walked level 1. [t2]\n\n"
                              "### Quality\nLevel 1 slab checked. [t2]\n")
    sr._place_photos(secs, {"t2": ["p"]}, no_photos=["Daily Summary"])
    assert "photos_after" not in secs[0] and secs[1]["photos_after"] == {0: ["p"]}


def test_summary_titles_are_the_module_and_anything_called_a_summary_or_overview():
    tpl = {"sections": [{"title": "Daily Summary"}, {"title": "Day at a glance", "module": {"key": "summary"}},
                        {"title": "Project Overview"}, {"title": "Summary of costs incurred"},
                        {"title": "Safety"}]}
    assert sr._summary_titles(tpl) == ["Daily Summary", "Day at a glance", "Project Overview",
                                       "Summary of costs incurred"]


def test_a_tag_at_the_end_of_a_cell_is_taken_off_the_page():
    text, refs = sr._line_refs("Is Level 0 inspected? | Yes | Ground floor inspected [t2] | |")
    assert text == "Is Level 0 inspected? | Yes | Ground floor inspected | |" and refs == ["t2"]


def test_a_tag_inside_a_sentence_is_still_left_alone():
    assert sr._line_refs("Section [t1] of the contract applies.") == \
        ("Section [t1] of the contract applies.", [])


def test_the_checklist_comment_column_carries_the_tagless_text():
    sec = sr._prose_sections(
        "### Checks\nItem no | Answer | Comment | Responsible | Due | Evidence\n---|---|---|---|---|---\n"
        "1 | Yes | Ground floor inspected [t2] | | | carried out the ground floor inspection\n")[0]
    paragraphs, refs, _ = checklist.rebuild(sec, ["Is Level 0 inspected?"],
                                            "we carried out the ground floor inspection today")
    assert paragraphs[2] == "Is Level 0 inspected? | Yes | Ground floor inspected |  | "
    assert refs[2] == ["t2"], "the photograph still knows its row"


def test_inspections_is_a_module_and_visitors_no_longer_claims_them():
    by = {m["key"]: m for m in rm.STANDARD}
    assert by["inspections"]["kind"] == "table"
    assert by["inspections"]["columns"] == ["Area", "What was checked", "Result", "Follow-up"]
    assert by["visitors"]["title"] == "Site Visitors"


def test_the_checklist_says_what_comment_responsible_and_due_are():
    s = checklist.shape_sentence()
    assert "for a No, what is wrong" in s
    assert "Responsible and Due are who will put right what is wrong and by when" in s
