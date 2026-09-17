"""handoff-sync plan Part 2 (§5-§10), then superseded on §5.1/§7.1 by the
2026-09-18 spec "the brief says where a task came from".

Two real defects, both from the same email (session `d740cd5f...`,
2026-09-11):

  §5.1 the same commitment appeared twice -- once as a brief action row, once
  again as a topic row, because topic rows were decided from the extraction
  ALONE, blind to what the brief had already said. Fixed twice: first by
  coverage-by-TIME (a brief task's `at` falling inside a topic's
  `time_range`), which was measured on a real TEST session to suppress
  UNRELATED content (the Papakura case -- see the module docstring note in
  `lambda_session_finalize`); then by coverage-by-TEXT (`_is_represented`
  applied to a topic row's own text). Both were a GUESS about whether two
  documents, written separately, meant the same thing. **This file's §5.1
  section now tests the FINAL fix (2026-09-18)**: the extraction's topic
  rows are not consulted for this at all any more. When a brief is used, its
  own sections that produced no task become the "nobody promised anything"
  rows directly -- `_sunk_rows_from_brief`, keyed on the `section` field the
  brief itself attaches to each task. No cross-document text matching is
  needed because the model that wrote both halves in one pass already knows
  the answer, and `session_brief.validate_task_sections` makes a wrong answer
  (a hallucinated title) checkable.

  §5.2 a dated commitment the extraction caught ("PS4 for the Port Com SR
  study...") was silently dropped because the brief won outright and had no
  such task. Fixed by §7.3/§7.4 and UNCHANGED by the 2026-09-18 spec, which
  explicitly keeps this safety net: back-fill every due-dated extraction
  action item that is not textually represented in the brief, appended after
  the brief's own rows.

§9's worked examples 2-4 (back-fill) are pinned verbatim below, against the
real 2026-09-11 session's actual text. Worked example 1 (§5.1, coverage) is
now tested against `_sunk_rows_from_brief` instead of against
`request_todos`' topic rows, which are no longer rendered when a brief wins.
"""
import pytest

fin = pytest.importorskip("lambda_session_finalize", reason="requires boto3 (installed in CI)")
iw = pytest.importorskip("lambda_item_writer", reason="requires psycopg (installed in CI)")

SID = "d740cd5f"


# ---- §7.4: the Jaccard "represented" test -----------------------------------
#
# The ONLY text-similarity primitive left in the module, and it decides only
# back-fill now (an extraction action item not said by the brief -> kept,
# appended after the brief's own rows). It used to also decide suppression (a
# topic row already said by the brief -> dropped); that direction is retired
# by the 2026-09-18 spec in favour of `_sunk_rows_from_brief` reading the
# brief's own `section` link -- see `_rows_from_brief_or_request`.

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


# ---- §9 worked example 1 (superseded 2026-09-18): coverage by SECTION ------
#
# The original coverage-by-time rule suppressed a topic whenever a brief
# task's `at` fell inside the topic's `time_range`. Measured on TEST
# (2026-09-18) that rule ate content it should not have: a real brief's 8
# tasks carried only THREE distinct `at` values, and the extraction's topics
# were 2-minute windows that overlapped those three values -- so "Papakura is
# mid-program and progressing well" was suppressed by a task about meeting
# KCD at the Icehouse office, though nothing in the email mentioned Papakura.
# Time cannot separate topics that overlap in time when real conversation
# does. A same-day text-matching fix (`_is_represented` on a topic row's own
# text) replaced it, and was itself replaced by THIS file's current form: the
# extraction's topic rows are not consulted for coverage at all any more --
# `_sunk_rows_from_brief` reads the brief's own `section`/`sections` link
# directly, which the model that wrote both halves in one pass actually
# knows, rather than re-guessing it from two documents afterwards.

PAPAKURA_SECTION = {"title": "Papakura Progress and Modulars", "bullets": [
    {"text": "Papakura is mid-program with good positioning and progressing well."}]}
ORMISTON_SECTION = {"title": "Ormiston College 360 Inspections", "bullets": [
    {"text": "Speaker hopes to visit the MPI site with DeAndre."}]}
ORMISTON_TASK_TEXT = ("Visit MPI site with DeAndre to review open-space usage pattern "
                      "after Ormiston College 360 inspections.")


def test_a_section_with_no_covering_task_is_sunk():
    brief = {"sections": [PAPAKURA_SECTION], "tasks": []}
    rows = fin._sunk_rows_from_brief(brief)
    assert [r["text"] for r in rows] == [
        "Papakura Progress and Modulars — Papakura is mid-program with good "
        "positioning and progressing well."]


def test_a_section_named_by_a_task_is_not_sunk():
    brief = {"sections": [ORMISTON_SECTION],
             "tasks": [{"text": ORMISTON_TASK_TEXT,
                       "section": "Ormiston College 360 Inspections"}]}
    assert fin._sunk_rows_from_brief(brief) == []


def test_worked_example_1_papakura_is_sunk_ormiston_is_not(monkeypatch):
    """End-to-end through `_rows_from_brief_or_request`, replaying the same
    real-session shape the original worked example used: Papakura produced no
    task and must appear as a sunk row; Ormiston produced the MPI/DeAndre
    task and must NOT appear twice."""
    monkeypatch.setattr(fin, "SESSION_BRIEF", True, raising=False)
    brief = {
        "sections": [PAPAKURA_SECTION, ORMISTON_SECTION],
        "tasks": [{"text": ORMISTON_TASK_TEXT, "at": "13:40:43",
                  "section": "Ormiston College 360 Inspections"}],
        "open_todos": [{"text": ORMISTON_TASK_TEXT, "responsible": None, "due": None,
                        "at": "13:40:43", "section": "Ormiston College 360 Inspections"}],
    }
    request_todos = [
        {"text": "Order steel", "responsible": "Neil", "due": None, "kind": "action"},
    ]
    out = fin._rows_from_brief_or_request(
        {"kind": "final", "sessionId": SID, "folder": "F", "date": "D"}, request_todos,
        poll_brief=lambda *a: brief)
    texts = [r["text"] for r in out]
    assert any(t.startswith("Papakura Progress and Modulars — ") for t in texts), (
        "Papakura's section produced no task -- it must survive as a sunk row")
    assert not any(t.startswith("Ormiston College 360 Inspections — ") for t in texts), (
        "Ormiston's section is covered by the brief's own MPI/DeAndre task -- "
        "it must not ALSO appear as a sunk row")
    assert any("MPI site" in t for t in texts)   # the brief's own row still there
    assert "Order steel" not in texts, (
        "the request's own topic/action rows play no part in coverage any "
        "more -- only back-fill (below) can bring an extraction row back, and "
        "this one has no due date")


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
        {"text": PS4_ITEM_TEXT, "responsible": None, "due": "2027-01-31", "kind": "action"},
    ]
    out = fin._rows_from_brief_or_request(
        {"kind": "final", "sessionId": SID, "folder": "F", "date": "D"}, request_todos,
        poll_brief=lambda *a: {"tasks": [], "open_todos": brief_action_rows})
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
         "kind": "action"},
    ]
    out = fin._rows_from_brief_or_request(
        {"kind": "final", "sessionId": SID, "folder": "F", "date": "D"}, request_todos,
        poll_brief=lambda *a: {"tasks": [], "open_todos": brief_action_rows})
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
         "kind": "action"},
    ]
    out = fin._rows_from_brief_or_request(
        {"kind": "final", "sessionId": SID, "folder": "F", "date": "D"}, request_todos,
        poll_brief=lambda *a: {"tasks": [], "open_todos": brief_action_rows})
    texts = [r["text"] for r in out]
    assert "Someone should tidy the site office" not in texts


# ---- the log line stays ONE line and names both new numbers --------------

def test_the_log_line_names_backfilled_and_sunk_counts(monkeypatch, caplog):
    monkeypatch.setattr(fin, "SESSION_BRIEF", True, raising=False)
    brief = {
        "sections": [PAPAKURA_SECTION],   # produced no task -> 1 sunk row
        "tasks": [{"text": QA_TASK_TEXT, "section": None}],
        "open_todos": [
            {"text": QA_TASK_TEXT, "responsible": "Sam", "due": "Mon", "at": "09:10:00"}],
    }
    request_todos = [
        {"text": PS4_ITEM_TEXT, "responsible": None, "due": "2027-01-31",
         "kind": "action"},  # unmatched, dated -> back-filled
    ]
    with caplog.at_level("INFO"):
        fin._rows_from_brief_or_request(
            {"kind": "final", "sessionId": SID, "folder": "F", "date": "D"}, request_todos,
            poll_brief=lambda *a: brief)
    lines = [r.message for r in caplog.records if "email action rows from the brief" in r.message]
    assert len(lines) == 1, "exactly one log line"
    line = lines[0]
    assert line == (
        "finalize: d740cd5f email action rows from the brief (2 row(s), 1 "
        "back-filled from the extraction), 1 sunk row(s) from the brief's own "
        "sections with no task (the extraction's topic rows are not rendered "
        "when a brief is used)")
