"""Unit: the decisions the extractor already makes have to survive the database.

`lambda_extract_session`'s own schema has asked the model for `decisions` --
`{decision, rationale, decided_by}` -- since it was written. Nothing ever stored
them. `lambda_org_api` said so in place::

    "key_decisions": [],                    # D3: v1, decisions table deferred

Across the 120 extraction artifacts measured on 2026-09-07, 81 of 274 topics
(30 %) carried at least one, 91 items in total, including phased-delivery
commitments with dates. Every one was written to S3 and then dropped.

What makes this worth fixing cheaply is that nothing downstream needs building.
`report_sections._decisions` already renders a Decisions section from
`topic["key_decisions"]`; `chunking.py` already folds that key into the RAG
chunk text; `lambda_ask_agent.format_report_for_prompt` already reads it. All
three have been fed a hardcoded empty list -- and on an authority-flip day
`render_report_shape` is not a view of the report, it IS the report, so that
empty list is the whole truth for most days of the prod year.

Shape, which is the one thing that must not be got wrong: the COLUMN stores the
model's objects, because the spec's position is that `rationale` and
`decided_by` are stored but not sent in v1, and a jsonb column can hold them at
no cost. The PAYLOAD is plain strings, because `topic-card.js:283-287` passes
each entry straight to React as a child -- an object there raises "Objects are
not valid as a React child" and the whole card stops rendering. The first draft
of the 2026-09-07 spec proposed objects in the payload and would have BROKEN the
section it set out to fill.
"""
import re

import pytest

import lambda_org_api as org
from repositories import topics
from tests.unit.test_lambda_ingest import wired      # noqa: F401  (fixture)

SITE_ID = "11111111-1111-1111-1111-111111111111"

DECISION = "Door replacement in two phases: floors 1-3 by next Tuesday"


def _row(**over):
    """The DB row shape org-api serializes from — same fixture as
    test_open_questions_reach_the_timeline."""
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
           / "0057_topic_decisions.sql").read_text(encoding="utf-8")
    assert "ADD COLUMN IF NOT EXISTS decisions jsonb" in sql


def test_the_insert_binds_one_placeholder_per_column():
    """The fake connection in this suite records SQL and does not parse it, so
    an off-by-one between the column list and the placeholder list is green
    here and a `ProgrammingError` in production. Adding a column is exactly
    when that happens. Count them."""
    cols, holders = _insert_sql()
    n_cols = len([c for c in cols.replace('f"', "").replace('"', "").split(",") if c.strip()])
    n_holders = holders.count("%s")
    assert n_cols == n_holders, (n_cols, n_holders)


def test_the_insert_names_decisions():
    cols, _ = _insert_sql()
    assert "decisions" in cols


def test_both_read_column_lists_carry_it():
    """There are two: `_TOPIC_COLS` for single-table reads and
    `_TOPIC_COLS_JOINED` for the joined ones that feed `render_report_shape`.
    `evidence` is in the first and not the second, and that asymmetry is exactly
    how a column becomes invisible on the path that matters."""
    assert "decisions" in topics._TOPIC_COLS
    assert "t.decisions" in topics._TOPIC_COLS_JOINED


def test_ingest_passes_the_report_key_through(wired):
    """Driven, not grepped. The report path spells it `key_decisions` — that is
    what `lambda_report_generator` emits and what `report_sections` reads."""
    import json
    import lambda_ingest as ing
    from tests.unit.test_lambda_ingest import FakeS3, REPORT_KEY, make_report

    report = make_report()
    report["topics"][0]["key_decisions"] = [DECISION]
    wired.setattr(ing, "_s3_client", FakeS3({REPORT_KEY: json.dumps(report)}))
    captured = []
    wired.setattr(
        ing.topics, "upsert_topic",
        lambda conn, site_id, report_date, title, **kw:
            captured.append(kw) or {"id": "topic-uuid-0"},
    )

    ing.ingest_report("2026-03-02", "Jarley_Trainor", REPORT_KEY)

    assert captured, "the report's topics must reach upsert_topic"
    assert captured[0]["decisions"] == [DECISION]


@pytest.mark.parametrize("stored,expected", [
    ([DECISION], [DECISION]),
    ([], []),
    (None, []),
])
def test_the_shaped_report_carries_them(stored, expected):
    """A shaped topic is what `report_sections` reads on an authority-flip day."""
    shaped = org.render_report_shape([_row(decisions=stored)], None,
                                     "2026-09-11", "Ben_UCPK")
    assert shaped["topics"][0]["key_decisions"] == expected


def test_the_payload_flattens_stored_objects_to_strings():
    """The column keeps what the model said; the payload may not.

    `topic-card.js:283-287` maps each `key_decisions` entry straight into an
    `<li>` as a React child. An object raises "Objects are not valid as a React
    child" and the card stops rendering — so the serializer, not the writer, is
    where the shape narrows. Both existing producers of this key
    (`lambda_meeting_minutes`, `lambda_report_generator.py:182`) emit strings.
    """
    shaped = org.render_report_shape(
        [_row(decisions=[{"decision": DECISION,
                          "rationale": "Access to level 4 is blocked",
                          "decided_by": "Site manager"}])],
        None, "2026-09-11", "Ben_UCPK")
    assert shaped["topics"][0]["key_decisions"] == [DECISION]


def test_a_stored_object_without_a_decision_is_dropped():
    """A blank renders as a bullet with nothing in it."""
    shaped = org.render_report_shape(
        [_row(decisions=[{"rationale": "why"}, {"decision": ""}, {"decision": DECISION}])],
        None, "2026-09-11", "Ben_UCPK")
    assert shaped["topics"][0]["key_decisions"] == [DECISION]


def test_the_section_is_built_from_the_shaped_report():
    """End of the chain: what the reader sees. This section has existed in
    `report_sections` the whole time, fed by the hardcoded empty list."""
    shaped = org.render_report_shape([_row(decisions=[DECISION])], None,
                                     "2026-09-11", "Ben_UCPK")
    titles = [s["title"] for s in shaped.get("sections", [])]
    assert "Decisions" in titles, titles
    section = [s for s in shaped["sections"] if s["title"] == "Decisions"][0]
    assert any("Door replacement" in str(x) for x in section["items"])
