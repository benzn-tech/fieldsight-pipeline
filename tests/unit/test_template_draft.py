"""Everything decided about a read form is decided here, in code (spec 2026-10-16 s3).

The model's output is data: a label it left on, a section it split across a page break,
a 400-character "item" -- each is either fixed in code or flagged for the person, never
fixed by asking the model nicely.
"""
import json
import pathlib

import pytest

import checklist
import report_template
import template_draft

FIXTURES = pathlib.Path(__file__).resolve().parents[1] / "fixtures" / "template_digitise"


def _texts(section):
    return [i["text"] for i in section["items"]]


def test_list_labels_are_stripped_from_titles_and_items():
    out = template_draft.clean([{"title": "8. Electrical",
                                 "items": ["1. Leads tagged", "a) RCDs used", "ii) Cables tidy",
                                           "1.2 Boards closed", "2 workers present"]}])
    s = out["sections"][0]
    assert s["title"] == "Electrical"
    assert _texts(s) == ["Leads tagged", "RCDs used", "Cables tidy", "Boards closed",
                         "2 workers present"]
    assert all(i["flags"] == [] for i in s["items"])


def test_a_label_still_there_after_stripping_is_flagged_not_guessed():
    s = template_draft.clean([{"title": "T", "items": ["1. a) Ladder footed"]}])["sections"][0]
    assert s["items"][0] == {"text": "a) Ladder footed", "flags": ["label_kept"]}


def test_empty_items_and_sections_with_no_items_are_dropped():
    out = template_draft.clean([{"title": "Empty", "items": ["", "   ", None, 7]},
                                {"title": "Real", "items": ["  Gate   locked "]},
                                "not a section"])
    assert [s["title"] for s in out["sections"]] == ["Real"]
    assert _texts(out["sections"][0]) == ["Gate locked"]
    assert out["counts"] == {"sections": 1, "items": 1}


def test_a_missing_title_becomes_checklist():
    out = template_draft.clean([{"title": "", "items": ["Gate locked"]}])
    assert out["sections"][0]["title"] == template_draft.DEFAULT_TITLE


def test_an_item_over_the_limit_is_cut_and_flagged():
    long = "x" * (checklist.MAX_ITEM_CHARS + 1)
    item = template_draft.clean([{"title": "T", "items": [long]}])["sections"][0]["items"][0]
    assert len(item["text"]) == checklist.MAX_ITEM_CHARS
    assert item["flags"] == ["truncated"]


def test_a_title_over_the_limit_is_cut_and_flagged():
    s = template_draft.clean([{"title": "T" * 121, "items": ["i"]}])["sections"][0]
    assert len(s["title"]) == report_template.MAX_TITLE_CHARS
    assert "truncated" in s["flags"]


def test_121_items_split_into_two_sections_each_flagged():
    items = ["Item %d" % n for n in range(checklist.MAX_ITEMS + 1)]
    out = template_draft.clean([{"title": "Big", "items": items}])
    assert [s["title"] for s in out["sections"]] == ["Big (1)", "Big (2)"]
    assert [len(s["items"]) for s in out["sections"]] == [checklist.MAX_ITEMS, 1]
    assert all(s["flags"] == ["split_for_limit"] for s in out["sections"])


def test_near_duplicate_titles_apart_are_flagged():
    out = template_draft.clean([{"title": "Compressed Gasses", "items": ["a"]},
                                {"title": "Ladders", "items": ["b"]},
                                {"title": "Compressed Gases", "items": ["c"]}])
    flags = [s["flags"] for s in out["sections"]]
    assert flags == [["near_duplicate_section"], [], ["near_duplicate_section"]]


def test_the_same_heading_twice_in_a_row_is_a_split_section():
    out = template_draft.clean([{"title": "Electrical", "items": ["a"]},
                                {"title": "Electrical", "items": ["b"]}])
    assert [s["flags"] for s in out["sections"]] == [["split_section"], ["split_section"]]


def test_more_than_forty_sections_is_flagged_for_the_person_to_merge():
    raw = [{"title": "Section %03d xyz" % n, "items": ["i"]}
           for n in range(report_template.MAX_SECTIONS + 1)]
    out = template_draft.clean(raw)
    assert out["counts"]["sections"] == report_template.MAX_SECTIONS + 1
    assert "too_many_sections" in out["flags"]


def test_a_draft_too_long_to_save_is_flagged():
    raw = [{"title": "Part %d" % n, "items": ["%d %s" % (k, "y" * 280) for k in range(120)]}
           for n in range(3)]
    assert "too_long" in template_draft.clean(raw)["flags"]


@pytest.mark.parametrize("name,n_sections,n_items", [("bicl", 11, 47), ("penina", 19, 115)])
def test_the_ground_truth_passes_through_untouched_and_unflagged(name, n_sections, n_items):
    truth = json.load(open(FIXTURES / f"truth_{name}.json", encoding="utf-8"))
    out = template_draft.clean(truth["sections"])
    assert out["counts"] == {"sections": n_sections, "items": n_items}
    assert [s["title"] for s in out["sections"]] == [s["title"] for s in truth["sections"]]
    assert [_texts(s) for s in out["sections"]] == [s["items"] for s in truth["sections"]]
    assert out["flags"] == []
    assert all(s["flags"] == [] for s in out["sections"])
    assert all(i["flags"] == [] for s in out["sections"] for i in s["items"])
