"""handoff-sync plan Part 2 (§5-§10): the brief and the extraction stop
talking past each other.

Two real defects, both from the same email (session `d740cd5f...`,
2026-09-11), fixed by ONE rule applied in two directions:

  §5.1 the same commitment appeared twice -- once as a brief action row, once
  again as a topic row, because topic rows were decided from the extraction
  ALONE, blind to what the brief had already said. Originally fixed by
  coverage-by-TIME (§7.1/§7.2 as first written): a topic a brief task's `at`
  fell inside was not emitted as a topic row. That rule was itself replaced
  by coverage-by-TEXT (2026-09-18, this file's current form) after a second
  real session showed time cannot separate topics that overlap in time --
  see the module docstring note in `lambda_session_finalize` and §7.1 of the
  plan for the measured Papakura case that forced the change.

  §5.2 a dated commitment the extraction caught ("PS4 for the Port Com SR
  study...") was silently dropped because the brief won outright and had no
  such task. Fixed by §7.3/§7.4: back-fill every due-dated extraction action
  item that is not textually represented in the brief, appended after the
  brief's own rows.

§9's worked examples are pinned verbatim below, against the real
2026-09-11 and 2026-09-18 sessions' actual text.
"""
import pytest

fin = pytest.importorskip("lambda_session_finalize", reason="requires boto3 (installed in CI)")
iw = pytest.importorskip("lambda_item_writer", reason="requires psycopg (installed in CI)")

SID = "d740cd5f"


# ---- §7.4: the Jaccard "represented" test -----------------------------------
#
# This is now the ONLY text-similarity primitive in the module: it decides
# both back-fill (an extraction action item not said by the brief -> kept)
# and suppression (a topic row already said by the brief -> dropped). One
# function, one threshold, both directions -- see `_rows_from_brief_or_request`.

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


# ---- §9 worked example 1 (superseded 2026-09-18): suppression by TEXT ------
#
# The original coverage-by-time rule suppressed a topic whenever a brief
# task's `at` fell inside the topic's `time_range`. Measured on TEST
# (2026-09-18) that rule ate content it should not have: a real brief's 8
# tasks carried only THREE distinct `at` values, and the extraction's topics
# were 2-minute windows that overlapped those three values -- so "Papakura is
# mid-program and progressing well" was suppressed by a task about meeting
# KCD at the Icehouse office, though nothing in the email mentioned Papakura.
# Time cannot separate topics that overlap in time when real conversation
# does. Suppression is now decided the same way back-fill always was: by
# `_is_represented` on the row's own text against the brief's task texts.

# Eight representative brief task texts from that session's shape (module
# docstring: KCD Meeting, Ormiston College, Papakura -- three 2-minute topic
# windows sharing only three distinct `at` values across 8 tasks). None of
# these mentions Papakura in any form.
EIGHT_BRIEF_TASK_TEXTS = [
    "Meet KCD at the Icehouse office to review the current programme sequence.",
    "Confirm KCD meeting agenda items before the site walk on Friday.",
    "Visit MPI site with DeAndre to review open-space usage pattern after "
    "Ormiston College 360 inspections.",
    "Follow up with KCD on the revised construction timeline.",
    "Order additional steel reinforcement for the northern block.",
    "Chase the electrical subcontractor for updated wiring diagrams.",
    "Schedule a follow-up call with the client project manager.",
    "Confirm delivery dates for the precast panels with the supplier.",
]

PAPAKURA_TOPIC_TEXT = ("Papakura Progress and Modulars — Papakura is mid-program with "
                       "good positioning and progressing well.")
ORMISTON_TOPIC_TEXT = ("Ormiston College 360 Inspections — Speaker hopes to visit the "
                       "MPI site with DeAndre.")
ORMISTON_BRIEF_TEXT = ("Visit MPI site with DeAndre to review open-space usage pattern "
                       "after Ormiston College 360 inspections.")


def test_papakura_topic_not_represented_in_any_of_the_eight_tasks_is_kept():
    """The measured false-suppression this change exists to fix: nothing in
    the 8 real-shaped brief tasks textually says Papakura, so the topic row
    must survive."""
    assert fin._is_represented(PAPAKURA_TOPIC_TEXT, EIGHT_BRIEF_TASK_TEXTS) is False


def test_ormiston_topic_represented_by_its_matching_task_is_suppressed():
    """The case the rule SHOULD still catch: a brief task that does say the
    same thing, in different words, still suppresses the topic row."""
    assert fin._is_represented(ORMISTON_TOPIC_TEXT, [ORMISTON_BRIEF_TEXT]) is True


def test_worked_example_1_papakura_topic_is_kept_ormiston_topic_is_dropped(monkeypatch):
    """End-to-end through `_rows_from_brief_or_request`: the brief's action
    rows come from the 8-task session; the request's topic rows are Papakura
    (must survive) and Ormiston (must be dropped, represented by the brief's
    own MPI/DeAndre task)."""
    monkeypatch.setattr(fin, "SESSION_BRIEF", True, raising=False)
    brief_action_rows = [{"text": t, "responsible": None, "due": None, "at": "13:40:43"}
                         for t in EIGHT_BRIEF_TASK_TEXTS]
    request_todos = [
        {"text": "Order steel", "responsible": "Neil", "due": None, "kind": "action"},
        {"text": PAPAKURA_TOPIC_TEXT, "responsible": None, "due": None, "kind": "topic"},
        {"text": ORMISTON_TOPIC_TEXT, "responsible": None, "due": None, "kind": "topic"},
    ]
    out = fin._rows_from_brief_or_request(
        {"kind": "final", "sessionId": SID, "folder": "F", "date": "D"}, request_todos,
        poll_brief=lambda *a: {"tasks": [1], "open_todos": brief_action_rows})
    texts = [r["text"] for r in out]
    assert PAPAKURA_TOPIC_TEXT in texts, "not represented in any brief task -- must be kept"
    assert ORMISTON_TOPIC_TEXT not in texts, "represented by the brief's own MPI/DeAndre task"
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
        {"text": PS4_ITEM_TEXT, "responsible": None, "due": "2027-01-31", "kind": "action"},
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
         "kind": "action"},
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
         "kind": "action"},
    ]
    out = fin._rows_from_brief_or_request(
        {"kind": "final", "sessionId": SID, "folder": "F", "date": "D"}, request_todos,
        poll_brief=lambda *a: {"tasks": [1], "open_todos": brief_action_rows})
    texts = [r["text"] for r in out]
    assert "Someone should tidy the site office" not in texts


# ---- the log line stays ONE line and names both new numbers --------------

def test_the_log_line_names_suppressed_and_backfilled_counts(monkeypatch, caplog):
    monkeypatch.setattr(fin, "SESSION_BRIEF", True, raising=False)
    brief_action_rows = [
        {"text": QA_TASK_TEXT, "responsible": "Sam", "due": "Mon", "at": "09:10:00"}]
    request_todos = [
        # represented by QA_TASK_TEXT (paraphrase) -> suppressed
        {"text": CONCRETE_EXTRACTION_TEXT, "responsible": None, "due": None, "kind": "topic"},
        {"text": PS4_ITEM_TEXT, "responsible": None, "due": "2027-01-31",
         "kind": "action"},  # unmatched, dated -> back-filled
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
