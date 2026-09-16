"""The template is data: a section plan plus house style. This test pins what the
renderer turns that data into, because the prompt is the product here -- a silently
dropped section reads as a model failure, not a rendering one."""
import json
import pathlib

import pytest

import report_template


TEMPLATE = {
    "template_id": "personal-meeting",
    "version": 3,
    "name": "Personal Meeting Notes",
    "sections": [
        {"key": "what", "title": "What this was", "purpose": "Who was in it."},
        {"key": "actions", "title": "Actions", "purpose": "One line per action."},
    ],
    "catch_all": {"key": "other", "title": "Anything else", "purpose": "What still matters."},
    "excluded_subjects": [],
    "style": ["One sentence per item."],
}
SCOPE = {"folder": "Ben_UCPK2", "date": "2026-09-10", "from": "09:00", "to": "11:30",
         "recordings": 70}


def test_the_builtin_template_is_the_one_the_owner_adopted():
    tpl = report_template.load_template("personal-meeting", 3)
    assert tpl["version"] == 3
    assert [s["key"] for s in tpl["sections"]] == ["what", "decided", "open", "actions"]
    assert tpl["catch_all"]["key"] == "other"
    assert tpl["style"], "v3 carries house style; without it the record runs long"


def test_an_unknown_template_is_refused_not_defaulted():
    with pytest.raises(report_template.TemplateNotFound):
        report_template.load_template("personal-meeting", 99)
    with pytest.raises(report_template.TemplateNotFound):
        report_template.load_template("../secrets", 3)


def test_every_section_reaches_the_prompt_in_order_with_its_purpose():
    p = report_template.render_prompt(TEMPLATE, SCOPE, [], "[09:00:00] Ben: morning")
    assert p.index("### What this was") < p.index("### Actions") < p.index("### Anything else")
    assert "Who was in it." in p
    assert "## House style" in p and "One sentence per item." in p


def test_the_action_list_is_given_as_data_not_left_to_the_model():
    items = [
        {"action": "Chase the H1 statement", "owner": "Ben", "deadline": "2026-09-12"},
        {"action": "Send the roofing prices", "owner": None, "deadline": None},
    ]
    p = report_template.render_prompt(TEMPLATE, SCOPE, items, "[09:00:00] Ben: morning")
    assert "Chase the H1 statement" in p and "Ben" in p and "2026-09-12" in p
    assert "no owner recorded" in p and "no date" in p
    assert "Do not invent an owner or a date" in p


def test_no_placeholder_survives_rendering():
    p = report_template.render_prompt(TEMPLATE, SCOPE, [], "[09:00:00] Ben: morning")
    head = p.split("## The recording")[0]
    assert "{" not in head and "}" not in head


def test_excluded_subjects_become_a_leave_out_instruction():
    tpl = dict(TEMPLATE, excluded_subjects=[{"label": "commercial", "covers": "rates, margin"}])
    p = report_template.render_prompt(tpl, SCOPE, [], "x")
    assert "## Leave out" in p and "rates, margin" in p


def test_the_scope_is_stated_so_the_model_knows_what_it_did_not_see():
    p = report_template.render_prompt(TEMPLATE, SCOPE, [], "x")
    assert "Ben_UCPK2" in p and "2026-09-10" in p and "09:00" in p and "11:30" in p
    assert "70" in p
