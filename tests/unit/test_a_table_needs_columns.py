"""Two things a measured before/after on a real template turned up.

Batch 1 wired `kind` up and was compared against its own noise floor: three runs
of the same template under the new code, to find out how much of a before/after
difference is just sampling. The model is not deterministic and temperature does
not make it so, so without that floor neither a difference nor the absence of
one means anything.

The BEFORE document sat 0.13 further from the three AFTERs than they sat from
each other. Outside the floor, so: caused by the change. Two causes, both real:

1. `Open Actions` had `kind: "table"` and a description reading, in full,
   `Outstanding tasks`. Asked for "a markdown table" and given no columns, the
   model invented ONE -- a single column headed `Open Actions`, holding the two
   action lines that had read perfectly well as sentences the day before. The
   control that was supposed to stop people writing formatting into
   descriptions had made the document worse than leaving it unwired.

2. The same template had `Photos` with `kind: "photos"`, saved months ago. The
   validation that shipped WITH the wiring refused that value -- so the
   customer's own live template could no longer be SAVED, and the error named a
   section they had not touched. Refusing it protected nothing: the lookup
   already ignored it, so it never reached the prompt either way.

THE test is `a table section with no columns of its own asks for the house
columns`. The second is pinned by `a body that already says photos still
saves`.

The house columns are not invented here. They are what a customer already sees
in the two places this product prints a task table: the stop-recording email
(AGENDA ITEM / ASSIGNED / DUE DATE) and the Actions table in every generated
document (Action / Owner / When).
"""
import json

import pytest

rt = pytest.importorskip("report_template")

SCOPE = {"folder": "Ben_UCPK2", "date": "2026-09-23", "from": "00:00", "to": "23:59",
         "recordings": 3}


def body(section):
    return {"sections": [section],
            "catch_all": {"key": "other", "title": "Anything else", "purpose": "Rest."},
            "excluded_subjects": [], "style": []}


def prompt(section):
    return rt.render_prompt(body(section), SCOPE, [], "x", source=rt.SOURCE_LIBRARY)


def shape_line(section):
    for line in prompt(section).splitlines():
        if line.startswith('- Write "'):
            return line
    return ""


# The section as it actually sits in the customer's daily report.
LIVE_TABLE_SECTION = {"key": "open-actions", "title": "Open Actions",
                      "purpose": "Outstanding tasks", "kind": "table"}


# ---- THE test ---------------------------------------------------------------

def test_THE_test_a_table_with_no_columns_of_its_own_asks_for_the_house_columns():
    line = shape_line(LIVE_TABLE_SECTION)
    assert "Item | Assigned | Due" in line, \
        "asking for 'a table' and nothing else is what produced a one-column table"
    assert "in this order" in line


def test_a_section_that_names_its_columns_gets_those():
    section = dict(LIVE_TABLE_SECTION,
                   columns=["Task", "Trade", "Blocked by", "Needed by"])
    line = shape_line(section)
    assert "Task | Trade | Blocked by | Needed by" in line
    assert "Item | Assigned" not in line, "the house set is a fallback, not a floor"


def test_the_columns_a_person_typed_are_not_an_instruction_channel():
    """They are names in a list, and they arrive as names in a list. Nothing
    about them opens a second way to write sentences at the model: whatever is
    in them is printed between pipes, in the order given, and the rest of the
    sentence around them is ours."""
    section = dict(LIVE_TABLE_SECTION, columns=["Task"])
    line = shape_line(section)
    assert line.count("One row per item") == 1
    assert "pipes between columns" in line


def test_too_many_columns_is_refused_and_a_long_name_too():
    assert rt.validate_body(body(dict(LIVE_TABLE_SECTION,
                                      columns=["c%d" % i for i in range(20)])))
    assert rt.validate_body(body(dict(LIVE_TABLE_SECTION,
                                      columns=["x" * (rt.MAX_TITLE_CHARS + 1)])))
    assert rt.validate_body(body(dict(LIVE_TABLE_SECTION, columns="Task, Owner")))


def test_empty_or_blank_columns_fall_back_rather_than_asking_for_nothing():
    for cols in ([], ["", "  "]):
        line = shape_line(dict(LIVE_TABLE_SECTION, columns=cols))
        assert "Item | Assigned | Due" in line


def test_the_other_kinds_are_untouched_by_all_this():
    assert 'as a list, one item per line' in shape_line(
        dict(LIVE_TABLE_SECTION, kind="list"))
    assert 'as a list of figures' in shape_line(dict(LIVE_TABLE_SECTION, kind="kpi"))
    assert shape_line(dict(LIVE_TABLE_SECTION, kind="narrative")) == ""


# ---- the template that could not be saved -----------------------------------

def test_a_body_that_already_says_photos_still_saves():
    """The customer's own live template, refused for a value they never typed
    into a section they never touched. Nothing was protected by refusing it:
    the kind lookup ignores `photos`, so it reached the prompt neither before
    nor after."""
    section = {"key": "photos", "title": "Photos",
               "purpose": "Site progress photos", "kind": "photos"}
    assert rt.validate_body(body(section)) is None


def test_photos_still_reaches_the_prompt_as_nothing():
    """Accepting it must not mean giving it behaviour it does not have. The
    generated path inserts no images; a Photos section is a heading, and what
    the model writes under it."""
    section = {"key": "photos", "title": "Photos",
               "purpose": "Site progress photos", "kind": "photos"}
    p = prompt(section)
    assert "photos" not in p.split("## Sections")[0]
    assert '- Write "Photos"' not in p
    assert "Site progress photos" in p, "its description is still the model's brief"


def test_a_kind_nobody_has_ever_stored_is_still_refused():
    """Grandfathering one value is not opening the enum."""
    assert rt.validate_body(body(dict(LIVE_TABLE_SECTION, kind="gantt")))


def test_the_whole_live_daily_report_body_validates():
    """Reconstructed from the request artifact that generated the reports being
    compared -- the exact shape that stopped saving."""
    live = {"sections": [
        {"key": "daily-summary", "title": "Daily Summary", "kind": "narrative",
         "purpose": "List every topic discussed today as a numbered list."},
        {"key": "workforce", "title": "Workforce", "kind": "kpi",
         "purpose": "Labour numbers on site"},
        {"key": "key-decisions", "title": "Key Decisions", "kind": "list",
         "purpose": "Decisions affecting programme or cost"},
        {"key": "open-actions", "title": "Open Actions", "kind": "table",
         "purpose": "Outstanding tasks"},
        {"key": "photos", "title": "Photos", "kind": "photos",
         "purpose": "Site progress photos"},
    ],
        "catch_all": {"key": "other", "title": "Anything else",
                      "purpose": "Anything not covered above."},
        "excluded_subjects": [], "style": []}
    assert rt.validate_body(live) is None
