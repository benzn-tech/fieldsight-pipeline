"""Unit: the question a day could not answer has to survive the database.

`lambda_meeting_minutes` used to append open questions to the topic summary::

    summary += ' Open questions: ' + '; '.join(questions)

That is how they reached Aurora at all -- as prose, inside another field. The
report's owner objected to exactly that gluing on 2026-09-10 ("这种按时间段的表达
方式不清晰"), so the report now carries them as their own key and renders them as
their own section.

The moment it did, `topics` became where they were lost. That table had a column
for a summary, for actions, for safety observations, for photos -- and nowhere
for a question. The Timeline reads Aurora, not the report file, so a day's
questions were visible before the change and gone after it. Fixing the report by
deleting information from the timeline is not a fix, which is what migration
0055 and these tests are about.

`render_report_shape` matters twice over: on an authority-flip day it is not a
view of the report, it IS the report, so a key it does not name never reaches
`report_sections` and the Open Questions section is empty on most days of the
year. That serializer is a fixed allowlist and has already silently dropped a
field this way once (`mention_count`).
"""
import re

import pytest

import lambda_org_api as org
from repositories import topics

SITE_ID = "11111111-1111-1111-1111-111111111111"


def _row(**over):
    """The DB row shape org-api serializes from — same fixture as
    test_org_api_serves_sections."""
    base = {
        "id": "t-1", "site_id": SITE_ID, "site_name": "Alpha", "user_name": "Ada L",
        "category": "progress", "title": "Level 3", "summary": "Walked level three.",
        "time_range": "08:00 – 08:15", "participants": ["Ada L"],
        "action_items": [], "safety_observations": [], "findings": [], "photos": [],
    }
    base.update(over)
    return base


def _insert_sql():
    src = open(topics.__file__, encoding="utf-8").read()
    m = re.search(r'INSERT INTO topics \((.*?)\) "\s*\n\s*f"VALUES \((.*?)\)', src, re.S)
    assert m, "could not find the topics INSERT"
    return m.group(1), m.group(2)


def test_the_column_exists_in_a_migration():
    import pathlib
    sql = (pathlib.Path(topics.__file__).parents[1] / "migrations"
           / "0055_topic_open_questions.sql").read_text(encoding="utf-8")
    assert "ADD COLUMN IF NOT EXISTS open_questions jsonb" in sql


def test_the_insert_binds_one_placeholder_per_column():
    """The fake connection in this suite records SQL and does not parse it, so
    an off-by-one between the column list and the placeholder list is green
    here and a `ProgrammingError` in production. Count them."""
    cols, holders = _insert_sql()
    n_cols = len([c for c in cols.replace('f"', "").replace('"', "").split(",") if c.strip()])
    n_holders = holders.count("%s")
    assert n_cols == n_holders, (n_cols, n_holders)


def test_the_insert_names_open_questions():
    cols, _ = _insert_sql()
    assert "open_questions" in cols


def test_both_read_column_lists_carry_it():
    """There are two: `_TOPIC_COLS` for single-table reads and
    `_TOPIC_COLS_JOINED` for the joined ones that feed `render_report_shape`.
    `evidence` is in the first and not the second, and that asymmetry is exactly
    how a column becomes invisible on the path that matters."""
    assert "open_questions" in topics._TOPIC_COLS
    assert "t.open_questions" in topics._TOPIC_COLS_JOINED


def test_ingest_passes_the_report_key_through():
    src = open(org.__file__.replace("lambda_org_api", "lambda_ingest"), encoding="utf-8").read()
    assert "open_questions=t.get(\"open_questions\")" in src


@pytest.mark.parametrize("stored,expected", [
    (["Is 3604 a 150?"], ["Is 3604 a 150?"]),
    ([], []),
    (None, []),
])
def test_the_shaped_report_carries_them(stored, expected):
    """A shaped topic is what `report_sections` reads on an authority-flip day."""
    shaped = org.render_report_shape([_row(open_questions=stored)], None,
                                     "2026-09-11", "Ben_UCPK")
    assert shaped["topics"][0]["open_questions"] == expected


def test_the_section_is_built_from_the_shaped_report():
    """End of the chain: what the reader sees."""
    shaped = org.render_report_shape(
        [_row(open_questions=["Is 3604 a 150 or a 200?"])], None,
        "2026-09-11", "Ben_UCPK")
    titles = [s["title"] for s in shaped.get("sections", [])]
    assert "Open Questions" in titles, titles
    section = [s for s in shaped["sections"] if s["title"] == "Open Questions"][0]
    assert any("3604" in str(x) for x in section["items"])
