"""The confirmation email says a spoken check's checklist is coming (owner,
2026-10-06): the email goes out before the report is written, and without this
the recorder learns about it only from the web or the bell.
"""
import pytest

fin = pytest.importorskip("lambda_session_finalize")
iw = pytest.importorskip("lambda_item_writer")

CHECKS = [{"check": "concrete pre-pour check for the level two slab",
           "template": "Concrete Pre-pour Inspection Checklist", "from": "17:12", "to": "17:17"},
          {"check": "steel inspections for the stair core", "template": None,
           "from": "17:15", "to": "17:15"}]


def test_THE_the_email_names_the_checklist_being_filled_and_the_unmatched_check():
    _, text, html = fin.build_confirmation_email(date="2026-10-06", time_range="17:12–17:17",
                                                 open_todos=[], checks=CHECKS)
    assert ("Checklist: Concrete Pre-pour Inspection Checklist for “concrete pre-pour check "
            "for the level two slab” (17:12–17:17) is being filled in — it will be ready in "
            "FieldSight in a few minutes.") in text
    assert ("Heard “steel inspections for the stair core” (17:15–17:15) — no checklist in "
            "your Library matches it, so no checklist report was made.") in text
    assert "Concrete Pre-pour Inspection Checklist" in html


def test_check_text_is_escaped_in_the_html():
    _, _, html = fin.build_confirmation_email(open_todos=[], checks=[
        {"check": "<script>x</script>", "template": None}])
    assert "<script>" not in html and "&lt;script&gt;" in html


def test_no_checks_leaves_the_email_as_it_was():
    assert fin.build_confirmation_email(open_todos=[], checks=None) == \
        fin.build_confirmation_email(open_todos=[])


def test_item_writer_hands_the_checks_to_the_email():
    assert iw.email_checks([{"name": "pre-pour", "template_name": "Pre-pour", "start_at": "17:12:52",
                             "end_at": "17:17:33"}]) == [
        {"check": "pre-pour", "template": "Pre-pour", "from": "17:12", "to": "17:17"}]
    src = open(iw.__file__, encoding="utf-8").read()
    assert 'final_email_ctx["checks"] = email_checks(stored_inspections)' in src
    assert 'checks=artifact.get("checks"))' in open(fin.__file__, encoding="utf-8").read()
