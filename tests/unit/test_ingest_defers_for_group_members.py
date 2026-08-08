"""Unit: the nightly ingest defers for group MEMBERS too (Phase C, Task 8).

Without this the feature un-does itself every night at 05:00 NZ.

The merge deletes each member's own topics, which empties
`extractions/{member}/{date}/` as far as Aurora is concerned. The authority-flip
defer test asks exactly that prefix, so it goes false for every joiner, and the
nightly branch then DELETES the extraction prefix and writes report-sourced
topics in its place — the duplicates the merge removed, back by morning, with
the merged record still sitting there beside them.

The lead is covered for free: the merged artifact lives under the lead's own
prefix. Only the joiners need the second clause, which is precisely why this is
easy to ship broken — a single-device test and a two-device test where the lead
is the one you check both pass.
"""
import pytest

ing = pytest.importorskip("lambda_ingest", reason="requires psycopg (installed in CI)")

GID = "a" * 32
MERGED = f"extractions/Lead_Folder/2026-08-07/grp{GID}.json"


@pytest.fixture(autouse=True)
def _flag_on(monkeypatch):
    # The module reads the env at import time; every test here except the
    # flag-off one is about behaviour when the feature is enabled.
    monkeypatch.setattr(ing, "ENABLE_GROUP_MERGE", True)


def test_a_joiner_with_no_solo_topics_still_defers(monkeypatch):
    monkeypatch.setattr(ing.topics, "has_topics_for_source_prefix",
                        lambda conn, prefix: False)          # solo topics deleted by the merge
    monkeypatch.setattr(ing, "_merged_keys_for", lambda conn, uid, d: [MERGED])
    monkeypatch.setattr(ing.topics, "has_topics_for_source", lambda conn, key: True)
    assert ing._should_defer(object(), "u-1", "Sam_UCPK", "2026-08-07") is True


def test_the_lead_defers_on_the_plain_prefix_alone(monkeypatch):
    # The merged artifact lives under the LEAD's prefix, so the original test
    # already answers for it. Pinned so nobody "simplifies" the first clause
    # away on the grounds that the union covers everything.
    monkeypatch.setattr(ing.topics, "has_topics_for_source_prefix",
                        lambda conn, prefix: True)
    monkeypatch.setattr(ing, "_merged_keys_for",
                        lambda conn, uid, d: pytest.fail("should short-circuit"))
    assert ing._should_defer(object(), "u-1", "Lead_Folder", "2026-08-07") is True


def test_a_genuinely_empty_day_still_ingests(monkeypatch):
    # The dangerous false positive. Deferring on a day with nothing silently
    # drops that day's report topics — the user sees an empty timeline and
    # nothing anywhere says why.
    monkeypatch.setattr(ing.topics, "has_topics_for_source_prefix",
                        lambda conn, prefix: False)
    monkeypatch.setattr(ing, "_merged_keys_for", lambda conn, uid, d: [])
    assert ing._should_defer(object(), "u-1", "Ben_UCPK", "2026-08-07") is False


def test_a_merged_key_whose_topics_are_gone_does_not_defer(monkeypatch):
    # A merged record that was re-ingested away, or a group marked merged whose
    # write never landed. Deferring on it would leave the day with no items at
    # all from either source.
    monkeypatch.setattr(ing.topics, "has_topics_for_source_prefix",
                        lambda conn, prefix: False)
    monkeypatch.setattr(ing, "_merged_keys_for", lambda conn, uid, d: [MERGED])
    monkeypatch.setattr(ing.topics, "has_topics_for_source", lambda conn, key: False)
    assert ing._should_defer(object(), "u-1", "Sam_UCPK", "2026-08-07") is False


def test_a_failed_group_lookup_falls_back_to_todays_behaviour(monkeypatch):
    # This runs in the nightly batch over every user. An exception here would
    # abort the run for everyone, so a broken enrichment degrades to the plain
    # prefix test rather than taking the night down.
    monkeypatch.setattr(ing.topics, "has_topics_for_source_prefix",
                        lambda conn, prefix: False)

    def boom(conn, uid, d):
        raise RuntimeError("db down")
    monkeypatch.setattr(ing, "_merged_keys_for", boom)
    assert ing._should_defer(object(), "u-1", "Sam_UCPK", "2026-08-07") is False


def test_the_flag_being_off_skips_the_group_clause(monkeypatch):
    monkeypatch.setattr(ing, "ENABLE_GROUP_MERGE", False)
    monkeypatch.setattr(ing.topics, "has_topics_for_source_prefix",
                        lambda conn, prefix: False)
    monkeypatch.setattr(ing, "_merged_keys_for",
                        lambda conn, uid, d: pytest.fail("group clause ran with the flag off"))
    assert ing._should_defer(object(), "u-1", "Sam_UCPK", "2026-08-07") is False
