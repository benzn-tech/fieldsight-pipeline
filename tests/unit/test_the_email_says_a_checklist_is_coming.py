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
    assert ("Heard “steel inspections for the stair core” (17:15) — no checklist in "
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
    assert 'final_email_ctx["checks"] = email_checks(stored_inspections, ' in src
    assert 'checks=artifact.get("checks"), open_url=' in open(fin.__file__, encoding="utf-8").read()


# ---- an unmatched check says where its words went (owner, 2026-10-09) ---------------

STEEL = {"name": "steel inspections for the stair core", "kind": "steel", "template_name": None,
         "start_at": "17:15:26", "end_at": "17:15:42",
         "segments": [{"from": "17:15:26", "to": "17:15:42"}]}
PREPOUR = {"name": "pre-pour check", "kind": "pre-pour", "template_name": "Pre-pour",
           "start_at": "17:12:52", "end_at": "17:17:33"}
TOPICS_1006 = [{"topic_title": "Formwork Prep and Cleanliness", "time_range": "17:12 – 17:14"},
               {"topic_title": "Stair Core Steel Check", "time_range": "17:14 – 17:14"},
               {"topic_title": "Slab Rebar and Cover", "time_range": "17:14 – 17:16"}]


def test_THE_the_topic_is_found_by_its_title_not_its_minute_stamp():
    """TEST 2026-10-06: the check ran 17:15:26-17:15:42; its topic was stamped
    17:14 and "Slab Rebar and Cover" spanned the minute."""
    checks = iw.email_checks([PREPOUR, STEEL], TOPICS_1006)
    assert checks[1]["topic"] == "Stair Core Steel Check"
    assert "topic" not in checks[0], "a matched check has its report"
    line = fin._check_lines(checks)[1]
    assert line.endswith("What you said is kept under “Stair Core Steel Check” on your "
                         "Timeline and in the daily report.")


def test_without_a_titled_topic_the_minute_decides_and_two_is_none():
    one = [{"topic_title": "Slab Rebar and Cover", "time_range": "17:14 – 17:16"},
           {"topic_title": "Scaffold", "time_range": "17:10 – 17:11"}]
    assert iw.email_checks([STEEL], one)[0]["topic"] == "Slab Rebar and Cover"
    two = one + [{"topic_title": "Edge Protection", "time_range": "17:15 – 17:15"}]
    assert "topic" not in iw.email_checks([STEEL], two)[0], "two candidates: say nothing"


def test_no_topic_no_promise():
    line = fin._check_lines(iw.email_checks([STEEL], []))[0]
    assert line.endswith("so no checklist report was made.")


def test_item_writer_hands_the_topics_over():
    src = open(iw.__file__, encoding="utf-8").read()
    assert 'email_checks(stored_inspections, extraction.get("topics"))' in src
