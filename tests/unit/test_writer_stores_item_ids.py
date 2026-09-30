"""Task 5 (extractor-declares-item-continuity plan, 2026-09-30): the item writer stores
the extractor's item_id on all four child tables and cleans malformed/duplicated ids to
NULL before insert.

Two groups:

  1. `lambda_ingest._map_action_items` -- the whitelist trap (spec §5): this dict literal
     rebuilds a NEW dict with a fixed key set, so item_id must be listed explicitly or it
     is silently dropped on the way to `topics.upsert_topic`'s action_items INSERT.
  2. `lambda_item_writer.write_extraction_items` calls `item_continuity.clean_item_ids`
     once, before the per-topic insert loop, and logs at WARNING only when it actually
     cleaned something -- mirrors the FakeConn/FakeS3 harness of
     tests/unit/test_lambda_item_writer.py (imported, not re-defined).

Real-Postgres coverage (every child table actually stores the value and
list_for_carry_forward reads it back) lives in
tests/integration/test_item_id_inserts.py.
"""
import json

import pytest

ing = pytest.importorskip("lambda_ingest", reason="requires psycopg (installed in CI)")
iw = pytest.importorskip("lambda_item_writer", reason="requires psycopg (installed in CI)")

from tests.unit.test_lambda_item_writer import (  # noqa: E402
    EXTRACTION_KEY, FakeS3, make_extraction,
)
from tests.unit.test_lambda_item_writer import wired  # noqa: E402,F401  (pytest fixture)


# ---------------------------------------------------------------------------
# _map_action_items -- the whitelist trap
# ---------------------------------------------------------------------------

def test_map_action_items_keeps_item_id():
    out = ing._map_action_items(
        [{"action": "Fix door", "item_id": "11111111-1111-4111-8111-111111111111"}])
    assert out[0]["item_id"] == "11111111-1111-4111-8111-111111111111"


def test_map_action_items_without_item_id_is_unchanged():
    out = ing._map_action_items([{"action": "Fix door"}])
    assert out[0].get("item_id") is None


# ---------------------------------------------------------------------------
# write_extraction_items -- calls clean_item_ids once, before the topic loop
# ---------------------------------------------------------------------------

def test_writer_calls_clean_item_ids_with_the_extraction_topics(wired):
    seen = []
    wired.setattr(iw.item_continuity, "clean_item_ids",
                 lambda topics: seen.append(topics) or 0)

    iw.write_extraction_items("2026-07-06", "Jarley_Trainor", EXTRACTION_KEY)

    assert len(seen) == 1
    assert seen[0] == make_extraction()["topics"]


def test_writer_logs_warning_when_ids_were_cleaned(wired, caplog):
    wired.setattr(iw.item_continuity, "clean_item_ids", lambda topics: 2)

    with caplog.at_level("WARNING"):
        iw.write_extraction_items("2026-07-06", "Jarley_Trainor", EXTRACTION_KEY)

    assert "item_id" in caplog.text
    assert "2" in caplog.text
    assert EXTRACTION_KEY in caplog.text


def test_writer_stays_silent_when_nothing_was_cleaned(wired, caplog):
    wired.setattr(iw.item_continuity, "clean_item_ids", lambda topics: 0)

    with caplog.at_level("WARNING"):
        iw.write_extraction_items("2026-07-06", "Jarley_Trainor", EXTRACTION_KEY)

    assert "item_id" not in caplog.text


def test_writer_passes_item_id_from_extraction_through_to_upsert_topic(wired):
    """End-to-end through the REAL _map_action_items (not stubbed): an item_id the
    extractor put on an action item reaches topics.upsert_topic's action_items kwarg."""
    extraction = make_extraction()
    extraction["topics"][0]["action_items"] = [
        {"action": "Order more hard hats", "responsible": "Bob",
         "item_id": "22222222-2222-4222-8222-222222222222"},
    ]
    wired.setattr(iw, "_s3_client", FakeS3({EXTRACTION_KEY: json.dumps(extraction)}))
    captured = []
    wired.setattr(
        iw.topics, "upsert_topic",
        lambda conn, site_id, report_date, title, **kw:
            captured.append(kw) or {"id": "topic-uuid-0"},
    )

    iw.write_extraction_items("2026-07-06", "Jarley_Trainor", EXTRACTION_KEY)

    assert captured[0]["action_items"][0]["item_id"] == "22222222-2222-4222-8222-222222222222"
