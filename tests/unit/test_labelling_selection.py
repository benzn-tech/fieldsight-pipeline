"""Unit: the labelling pack tells the truth about the sample it is.

The pack exists to produce an ROC, and an ROC computed from a sample that quietly fell short
of its own quotas is indistinguishable from a good one. So the two things worth pinning are
(a) that a session spread over many chunk files counts once, and (b) that a shortfall is
reported rather than absorbed.
"""
import os
import sys

import pytest

sys.path.insert(0, os.path.join(
    os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))),
    "scripts", "labelling"))

#: The rule, in one line, stated where a future whole-repo audit will look for it:
#: **importorskip is for third-party dependencies an environment may legitimately lack
#: (psycopg, onnxruntime, docx, yaml); a module from this repository is imported directly,
#: so a missing one fails.** Full reasoning in
#: `tests/unit/test_no_cross_company_voice_identity.py`, where the same rule was arrived at
#: by shipping a guard that had never once run.
try:
    import select_turns as sel
except ImportError as exc:  # pragma: no cover - the message IS the point
    # NOT `pytest.importorskip`, which is this suite's house style. That helper exists for a
    # module a test environment may legitimately lack (psycopg, onnxruntime, docx). This one
    # is a file in this repository, reached through the `sys.path` line above — so the only
    # way the import fails is that the path is wrong or the file moved, and either of those
    # should be red. Skipped, the whole file reports as a passing suite while none of it has
    # run, which is how 84 tests in this repo went unexecuted in CI for weeks.
    raise AssertionError(f"scripts/labelling/select_turns.py is not importable: {exc}")


def key(folder, date, device="Benl1", sid="a" * 32, chunk="c0000", time="09-00-00"):
    return (f"transcripts/{folder}/{date}/"
            f"{device}_{date}_{time}_sid{sid}_{chunk}_srcwav.json")


def test_a_legacy_recording_is_not_a_candidate():
    """No `sid` means the turns cannot be grouped into a session at all. Guessing one reads
    another meeting's turns as this one's — the same refusal `turn_name_overlay` makes."""
    assert sel.parse_key(
        "transcripts/Ben_UCPK2/2026-08-13/RealPTT_2026-08-13_12-18-34.json") is None
    assert sel.parse_key("reports/2026-08-13/Ben/daily_report.json") is None
    assert sel.parse_key("") is None


def test_one_meeting_of_fourteen_chunks_counts_once():
    """The defect this was written against: counting FILES reported '23 sessions' for four
    meetings, and every quota check then passed on a sample that could not support them."""
    rows = [sel.parse_key(key("Ben_UCPK2", "2026-08-13", chunk=f"c{i:04d}"))
            for i in range(14)]
    picked = sel.select([r for r in rows if r])
    assert len(picked) == 1


def test_one_person_on_two_devices_is_two_entries_of_one_session():
    """The clause that makes this a cross-CHANNEL sample rather than a cross-session one.
    Two devices in one meeting separate 'session' from 'channel', which is precisely what
    the 2026-08-30 measurement could not do."""
    rows = [sel.parse_key(key("Ben_UCPK2", "2026-08-13", device="Benl1")),
            sel.parse_key(key("Ben_UCPK2", "2026-08-13", device="Benl2"))]
    picked = sel.select(rows)
    assert len({p["device"] for p in picked}) == 2
    assert len({p["session"] for p in picked}) == 1


def test_a_sample_that_falls_short_says_so_in_the_words_of_the_rule():
    """Silence here would produce a number that looks exactly like a good one."""
    rows = [sel.parse_key(key("Ben_UCPK2", "2026-08-13"))]
    missing = sel.shortfalls(sel.select([r for r in rows if r]))
    joined = " ".join(missing)
    assert "sessions" in joined
    assert "folders" in joined
    assert "days" in joined
    assert "two devices" in joined


def test_a_sample_that_meets_every_quota_reports_nothing():
    """Guard the guard: if `shortfalls` could never return empty, the test above would pass
    against a function that always complains, and the pack would be un-shippable."""
    rows = []
    # six sessions, three people, spread over more than three weeks
    for i, (folder, date) in enumerate([
            ("Ben_UCPK2", "2026-08-01"), ("Ben_UCPK2", "2026-08-14"),
            ("Mike_UCPK", "2026-08-02"), ("Mike_UCPK", "2026-08-20"),
            ("Leo_UCPK", "2026-08-03"), ("Leo_UCPK", "2026-08-28")]):
        rows.append(sel.parse_key(key(folder, date, sid=chr(ord("a") + i) * 32)))
    # and one of them on a second device, which is the clause with teeth
    rows.append(sel.parse_key(key("Ben_UCPK2", "2026-08-14", device="Benl2",
                                  sid="b" * 32)))
    missing = sel.shortfalls(sel.select(rows))
    # The hand-check note is not a shortfall: a folder is not a person and no amount of
    # metadata can settle that, so it is always emitted once the folder quota is met.
    assert [m for m in missing if not m.startswith("CHECK BY HAND")] == []
    assert any(m.startswith("CHECK BY HAND") for m in missing), (
        "the folder-is-not-a-person warning disappeared; nine folders read as nine people "
        "on prod, and five of them were one man")


def test_the_pack_is_the_same_pack_on_a_second_run():
    """A pack whose contents shift between runs cannot be compared with the run before it,
    and comparing runs is the whole method — 93.5% of one earlier measurement's apparent
    effect turned out to be run-to-run jitter."""
    rows = [sel.parse_key(key("Ben_UCPK2", "2026-08-13", chunk=f"c{i:04d}"))
            for i in range(6)]
    rows += [sel.parse_key(key("Mike_UCPK", "2026-08-02", sid="b" * 32))]
    a = sel.select([r for r in rows if r])
    b = sel.select(list(reversed([r for r in rows if r])))
    assert a == b


def test_turns_below_the_attribution_floor_are_never_asked_about():
    """The matcher refuses a turn under three seconds whatever the scores say, so a label on
    one could never appear in any ROC — it would be effort spent on rows the system cannot
    produce."""
    picked = sel.select([sel.parse_key(key("Ben_UCPK2", "2026-08-13"))])
    assert picked[0]["min_turn_s"] == sel.MIN_TURN_S == 3.0


def test_a_folder_prefix_with_underscores_is_still_read():
    """The bug this file exists to have caught, found against real prod keys 2026-09-23.

    Legacy RealPTT names carry one bare token (`Benl1_2026-03-20_...`); the app writes the
    FOLDER as the prefix and folders have underscores (`ben_ucpk2_...`, `petros_pan_...`).
    A device pattern of `[A-Za-z0-9]+` matches `ben`, then demands a date where `ucpk2_` is,
    and returns None.

    It failed in the safe direction, which is why it survived: the scan found ONE session
    across thirteen folders and months of recordings, and reported a quota shortfall -- a
    number indistinguishable from "there is not enough material yet".
    """
    row = sel.parse_key(
        "transcripts/Ben_UCPK2/2026-09-22/"
        "ben_ucpk2_2026-09-22_15-32-00_sid" + "2" * 32 + "_c0000_srcwav.json")
    assert row is not None, "the current app naming convention is unreadable"
    assert row["device"] == "ben_ucpk2"
    assert row["date"] == "2026-09-22"

    legacy = sel.parse_key(
        "transcripts/Jarley_Trainor/2026-03-20/"
        "Benl1_2026-03-20_12-18-34_sid" + "3" * 32 + "_c0000_srcwav.json")
    assert legacy is not None and legacy["device"] == "Benl1", "the old convention broke"


def test_the_prefix_never_swallows_the_date():
    """`.+?` is non-greedy and anchored on the date, so a filename carrying two date-shaped
    runs still takes the first as the recording date rather than the last."""
    row = sel.parse_key(
        "transcripts/X/2026-09-22/a_b_2026-09-22_15-32-00_sid" + "4" * 32 + "_c0.json")
    assert row["date"] == "2026-09-22"
    assert row["device"] == "a_b"
