"""handoff-sync plan Part 2 (§5-§10): the brief and the extraction stop
talking past each other.

Two real defects, both from the same email (session `d740cd5f...`,
2026-09-11), fixed by ONE rule applied in two directions:

  §5.1 the same commitment appeared twice -- once as a brief action row, once
  again as a topic row, because topic rows were decided from the extraction
  ALONE, blind to what the brief had already said. Fixed by §7.1/§7.2:
  coverage by TIME, not text -- a topic a brief task's `at` falls inside is
  not emitted as a topic row.

  §5.2 a dated commitment the extraction caught ("PS4 for the Port Com SR
  study...") was silently dropped because the brief won outright and had no
  such task. Fixed by §7.3/§7.4: back-fill every due-dated extraction action
  item that is not textually represented in the brief, appended after the
  brief's own rows.

§9's four worked examples are pinned verbatim below, against the real
2026-09-11 session's actual text.
"""
import pytest

fin = pytest.importorskip("lambda_session_finalize", reason="requires boto3 (installed in CI)")
iw = pytest.importorskip("lambda_item_writer", reason="requires psycopg (installed in CI)")

SID = "d740cd5f"


# ---- §7.1/§7.2: coverage by time --------------------------------------------

def test_a_topic_covered_by_an_at_inside_its_range_gets_no_topic_row():
    assert fin._topic_covered("13:40 – 13:41", ["13:40:43"]) is True


def test_an_at_exactly_at_the_range_start_covers_it():
    """Both ends inclusive (§7.1) -- the start boundary."""
    assert fin._topic_covered("13:40 – 13:41", ["13:40:00"]) is True


def test_an_at_exactly_at_the_range_end_covers_it():
    """Both ends inclusive (§7.1) -- the end boundary."""
    assert fin._topic_covered("13:40 – 13:41", ["13:41:00"]) is True


def test_an_at_one_second_past_the_end_does_not_cover_it():
    assert fin._topic_covered("13:40 – 13:41", ["13:41:01"]) is False


def test_an_at_one_second_before_the_start_does_not_cover_it():
    assert fin._topic_covered("13:40 – 13:41", ["13:39:59"]) is False


def test_an_unparsable_range_is_never_covered():
    for bad in ("", None, "garbage", "not a time"):
        assert fin._topic_covered(bad, ["13:40:43"]) is False


def test_a_brief_task_with_no_at_covers_nothing():
    assert fin._topic_covered("13:40 – 13:41", [None, ""]) is False


def test_only_one_of_several_brief_tasks_needs_to_land_inside():
    assert fin._topic_covered("09:00 – 09:20", ["08:00:00", "09:05:00", "20:00:00"]) is True


# ---- §7.4: the Jaccard "represented" test -----------------------------------

def test_the_jaccard_threshold_and_stop_list_are_one_named_constant():
    """Not inlined twice: `_is_represented` must actually consult the module
    constants, not a private copy -- proven by moving the threshold and
    watching the same call flip."""
    assert fin.BRIEF_MATCH_JACCARD_THRESHOLD == 0.30
    assert "the" in fin.BRIEF_MATCH_STOP_WORDS and "a" in fin.BRIEF_MATCH_STOP_WORDS


def test_moving_the_threshold_constant_changes_the_verdict(monkeypatch):
    text = "aaaone aaatwo aaathree aaafour aaafive"
    brief = ["aaaone aaatwo aaathree bbbone bbbtwo bbbthree bbbfour bbbfive"]  # overlap = 0.30 exactly
    assert fin._is_represented(text, brief) is False       # a tie: NOT represented
    monkeypatch.setattr(fin, "BRIEF_MATCH_JACCARD_THRESHOLD", 0.0, raising=False)
    assert fin._is_represented(text, brief) is True         # same inputs, lowered bar


def test_a_tie_at_the_threshold_is_not_represented():
    """Exactly 0.30 overlap by construction (3 shared / 10 union tokens) --
    plan §7.4's named boundary case, decided towards keeping the row."""
    text = "aaaone aaatwo aaathree aaafour aaafive"
    brief_text = "aaaone aaatwo aaathree bbbone bbbtwo bbbthree bbbfour bbbfive"
    assert fin._is_represented(text, [brief_text]) is False


def test_an_empty_token_set_is_not_represented():
    assert fin._is_represented("!!! *** ...", ["Chase the delivery"]) is False
    assert fin._is_represented("Chase the delivery", ["!!! *** ..."]) is False


def test_stop_words_and_short_tokens_do_not_count_towards_overlap():
    """'to be signed by' is all stop-words-or-short; without the filter this
    pair would share more than it should."""
    assert fin._is_represented("to be or at a an", ["to be or at a an"]) is False


def test_exactly_030_by_a_second_independent_construction_is_not_represented():
    """Parity ruling (2026-09-18): "Represented iff best overlap is strictly >
    0.30." Built the way the ruling names it -- a 7-token text and a 6-token
    text sharing 3 tokens, union 10, overlap 3/10 = 0.30 exactly -- as a
    second, independent proof alongside the 5/8/3/10 construction above."""
    text = "aone atwo athree afour afive asix aseven"
    brief_text = "aone atwo athree bfour bfive bsix"
    assert fin._is_represented(text, [brief_text]) is False


# ---- range parsing: en dash / em dash / plain hyphen, HH:MM means :00 ------

def test_an_em_dash_range_separator_is_accepted():
    """Parity ruling point 1 (acceptance set): en dash, em dash or a plain
    hyphen, with or without surrounding spaces."""
    assert fin._parse_hms_range("13:40—13:41") == (49200, 49260)   # en dash
    assert fin._parse_hms_range("13:40—13:41") == fin._parse_hms_range("13:40—13:41")
    assert fin._parse_hms_range("13:40 — 13:41") == (49200, 49260)      # em dash
    assert fin._parse_hms_range("13:40 - 13:41") == (49200, 49260)      # plain hyphen


def test_an_hh_mm_endpoint_means_hh_mm_00_not_hh_mm_59():
    """Parity ruling point 2: no invented seconds."""
    assert fin._parse_hms("13:41") == 13 * 3600 + 41 * 60
    assert fin._parse_hms_range("13:40 – 13:41") == (13 * 3600 + 40 * 60, 13 * 3600 + 41 * 60)


def test_a_range_with_a_nonstandard_separator_is_unparsable():
    assert fin._parse_hms_range("13:40 to 13:41") is None
    assert fin._parse_hms_range("13:40..13:41") is None
    assert fin._parse_hms_range("garbage") is None
    assert fin._parse_hms_range("") is None
    assert fin._parse_hms_range(None) is None


# ---- §9 worked example 1: coverage suppresses a duplicated topic row -------

def test_worked_example_1_ormiston_topic_is_covered_and_dropped(monkeypatch):
    monkeypatch.setattr(fin, "SESSION_BRIEF", True, raising=False)
    brief_action_rows = [
        {"text": "Visit MPI site with DeAndre to see comprehensive open-space "
                 "usage pattern.", "responsible": None, "due": None, "at": "13:40:43"}]
    request_todos = [
        {"text": "Order steel", "responsible": "Neil", "due": None, "kind": "action"},
        {"text": "Ormiston College 360 Inspections — Speaker hopes to visit the "
                 "MPI site with DeAndre.", "responsible": None, "due": None,
         "kind": "topic", "topic_range": "13:40 – 13:41"},
    ]
    out = fin._rows_from_brief_or_request(
        {"kind": "final", "sessionId": SID, "folder": "F", "date": "D"}, request_todos,
        poll_brief=lambda *a: {"tasks": [1], "open_todos": brief_action_rows})
    texts = [r["text"] for r in out]
    assert "Ormiston College 360 Inspections" not in " ".join(texts)
    assert any("MPI site" in t for t in texts)   # the brief's own row still there


# ---- §9 worked example 2: PS4 dated commitment is back-filled -------------

PS4_ITEM_TEXT = "PS4 for the Port Com SR study to be signed by January next year"
QA_TASK_TEXT = ("Start QA pour checks and create report on Port Com concrete "
                "once 28-day cure ends in about three weeks")


def test_worked_example_2_ps4_is_not_represented_and_gets_backfilled(monkeypatch):
    monkeypatch.setattr(fin, "SESSION_BRIEF", True, raising=False)
    # Confirms the plan's own approximation ("best overlap ... ~= 0.1") against
    # this repo's actual tokenizer, before trusting the behaviour it drives.
    assert fin._is_represented(PS4_ITEM_TEXT, [QA_TASK_TEXT]) is False

    brief_action_rows = [
        {"text": QA_TASK_TEXT, "responsible": "Sam", "due": "Mon", "at": "09:10:00"}]
    request_todos = [
        {"text": PS4_ITEM_TEXT, "responsible": None, "due": "2027-01-31",
         "kind": "action", "topic_range": "10:00 – 10:05"},
    ]
    out = fin._rows_from_brief_or_request(
        {"kind": "final", "sessionId": SID, "folder": "F", "date": "D"}, request_todos,
        poll_brief=lambda *a: {"tasks": [1], "open_todos": brief_action_rows})
    texts = [r["text"] for r in out]
    assert QA_TASK_TEXT in texts
    assert PS4_ITEM_TEXT in texts, "a due-dated, unmatched extraction item must be back-filled"
    assert texts.index(QA_TASK_TEXT) < texts.index(PS4_ITEM_TEXT), (
        "back-filled rows come AFTER the brief's own rows (§7.3)")


# ---- §9 worked example 3: a paraphrase IS represented, not duplicated -----

CONCRETE_EXTRACTION_TEXT = ("Concrete QA pour checks and report to be started "
                             "three weeks later after cure")


def test_worked_example_3_the_paraphrase_is_represented_and_not_backfilled(monkeypatch):
    monkeypatch.setattr(fin, "SESSION_BRIEF", True, raising=False)
    assert fin._is_represented(CONCRETE_EXTRACTION_TEXT, [QA_TASK_TEXT]) is True

    brief_action_rows = [
        {"text": QA_TASK_TEXT, "responsible": "Sam", "due": "Mon", "at": "09:10:00"}]
    request_todos = [
        {"text": CONCRETE_EXTRACTION_TEXT, "responsible": None, "due": "2026-10-09",
         "kind": "action", "topic_range": "10:00 – 10:05"},
    ]
    out = fin._rows_from_brief_or_request(
        {"kind": "final", "sessionId": SID, "folder": "F", "date": "D"}, request_todos,
        poll_brief=lambda *a: {"tasks": [1], "open_todos": brief_action_rows})
    texts = [r["text"] for r in out]
    assert CONCRETE_EXTRACTION_TEXT not in texts, (
        "already said, in different words, by the brief -- must NOT double up")


# ---- §9 worked example 4: no due date -> never back-filled, match or not --

def test_worked_example_4_an_undated_unmatched_item_is_not_backfilled(monkeypatch):
    monkeypatch.setattr(fin, "SESSION_BRIEF", True, raising=False)
    brief_action_rows = [
        {"text": QA_TASK_TEXT, "responsible": "Sam", "due": "Mon", "at": "09:10:00"}]
    request_todos = [
        {"text": "Someone should tidy the site office", "responsible": None, "due": None,
         "kind": "action", "topic_range": "10:00 – 10:05"},
    ]
    out = fin._rows_from_brief_or_request(
        {"kind": "final", "sessionId": SID, "folder": "F", "date": "D"}, request_todos,
        poll_brief=lambda *a: {"tasks": [1], "open_todos": brief_action_rows})
    texts = [r["text"] for r in out]
    assert "Someone should tidy the site office" not in texts


# ---- §8: topic_range is carried by the item-writer's row builders --------

def test_final_email_rows_carry_the_topic_range():
    art = {"topics": [{"time_range": "09:00 – 09:20",
                       "action_items": [{"action": "Order steel", "responsible": "Neil"}]}]}
    rows = iw._final_email_rows(art, "2026-09-18")
    assert rows[0]["topic_range"] == "09:00 – 09:20"


def test_topic_rows_carry_the_topic_range():
    art = {"topics": [{"topic_title": "Site walk", "summary": "Poured the slab.",
                       "time_range": "10:00 – 10:05", "action_items": []}]}
    rows = iw._topic_rows(art)
    assert rows[0]["topic_range"] == "10:00 – 10:05"


def test_clean_todos_carries_topic_range_through_the_rebuild():
    """The exact trap that once dropped `kind` (module docstring): `_clean_todos`
    rebuilds every row from scratch, so a new key that isn't explicitly copied
    is silently lost."""
    out = fin._clean_todos([{"text": "x", "responsible": None, "due": None,
                             "kind": "topic", "topic_range": "09:00 – 09:20"}])
    assert out[0]["topic_range"] == "09:00 – 09:20"


def test_clean_todos_defaults_a_missing_topic_range_to_none():
    out = fin._clean_todos([{"text": "x", "responsible": None, "due": None}])
    assert out[0]["topic_range"] is None


# ---- the log line stays ONE line and names both new numbers --------------

def test_the_log_line_names_suppressed_and_backfilled_counts(monkeypatch, caplog):
    monkeypatch.setattr(fin, "SESSION_BRIEF", True, raising=False)
    brief_action_rows = [
        {"text": QA_TASK_TEXT, "responsible": "Sam", "due": "Mon", "at": "09:10:00"}]
    request_todos = [
        {"text": "Site walk — poured the slab.", "responsible": None, "due": None,
         "kind": "topic", "topic_range": "09:05 – 09:15"},   # covered by 09:10:00 -> suppressed
        {"text": PS4_ITEM_TEXT, "responsible": None, "due": "2027-01-31",
         "kind": "action", "topic_range": "10:00 – 10:05"},  # unmatched, dated -> back-filled
    ]
    with caplog.at_level("INFO"):
        fin._rows_from_brief_or_request(
            {"kind": "final", "sessionId": SID, "folder": "F", "date": "D"}, request_todos,
            poll_brief=lambda *a: {"tasks": [1], "open_todos": brief_action_rows})
    lines = [r.message for r in caplog.records if "email action rows from the brief" in r.message]
    assert len(lines) == 1, "exactly one log line"
    line = lines[0]
    assert line == (
        "finalize: d740cd5f email action rows from the brief (2 row(s), 1 "
        "back-filled from the extraction), 0 topic row(s) kept from the "
        "request (1 suppressed as covered by a brief task)")


def test_an_impossible_clock_is_unparsable_not_a_range():
    """A shape like "25:00" or "09:99" is not a clock, so it is not a range.

    Plan §7.1: anything that is not two parsable clock times never covers a
    topic. The regex alone accepts these, and fieldsight-ui's
    `parseClockSeconds` rejects them -- so without the bounds check the two
    surfaces disagreed: an out-of-range `time_range` from an upstream
    arithmetic slip would have suppressed a topic row in the email while
    Preview & copy kept it. Found by executing both parsers on the same
    inputs, not by reading them."""
    import lambda_session_finalize as f
    for bad in ("25:00", "09:99", "99:99", "12:00:60"):
        assert f._parse_hms(bad) is None, bad
    for good, secs in (("00:00", 0), ("23:59", 86340), ("13:41:07", 49267)):
        assert f._parse_hms(good) == secs, good
    assert f._parse_hms_range("25:00 – 26:00") is None
    assert f._topic_covered("25:00 – 26:00", ["25:30"]) is False
