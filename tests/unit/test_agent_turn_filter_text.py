"""The report generator has no speaker turns, only a flat string per transcript file.

The other three consumers hand the filter `speaker_turns` with per-turn `abs_start`. The report
generator does not: it calls `parse_transcribe_json` and keeps `full_text`. So the same decision
has to be reachable at sentence granularity, with the file's own time span standing in for the
per-turn timestamp.

Deliberately the SAME containment test underneath -- the segmentation differs, the matching
rule does not. Two fuzzy matchers that are supposed to agree eventually do not.
"""
from datetime import datetime, timedelta

from agent_turn_filter import AgentAnswer, filter_agent_text

NOON = datetime(2026, 8, 12, 12, 13, 21)
ANSWER = "The concrete pour is scheduled for Monday with a weather contingency to Wednesday."


def _answers(*rows):
    return [AgentAnswer(at_local=at, text=txt) for at, txt in rows]


def test_the_agent_sentence_is_removed_and_the_rest_survives():
    text = ("Scaffold needs checking before Monday. "
            "The concrete pour is scheduled for Monday with the weather contingency to Wednesday. "
            "We are short two brackets.")
    out, stats = filter_agent_text(text, _answers((NOON, ANSWER)), NOON, 30.0)
    assert "concrete pour is scheduled" not in out
    assert "Scaffold needs checking" in out
    assert "short two brackets" in out
    assert stats["removed"] == 1


def test_a_file_outside_the_window_is_untouched():
    """The whole point of keeping a time condition even at this coarser resolution: a person
    saying the same thing an hour later is a person reporting a fact."""
    text = "The concrete pour is scheduled for Monday with the weather contingency to Wednesday."
    out, stats = filter_agent_text(text, _answers((NOON, ANSWER)), NOON + timedelta(hours=2), 30.0)
    assert out == text
    assert stats["removed"] == 0


def test_a_file_that_merely_overlaps_the_window_still_matches():
    """The answer plays across a chunk boundary, so the file can start before `at` and still
    contain the playback."""
    text = "The concrete pour is scheduled for Monday with the weather contingency to Wednesday."
    out, _ = filter_agent_text(text, _answers((NOON, ANSWER)), NOON - timedelta(seconds=20), 30.0)
    assert "concrete pour" not in out


def test_unrelated_speech_in_the_window_survives():
    text = "Can you grab the level off the ute before we start."
    out, stats = filter_agent_text(text, _answers((NOON, ANSWER)), NOON, 30.0)
    assert out == text
    assert stats["removed"] == 0


def test_a_short_sentence_is_never_removed():
    """Same floor as the turn-level matcher: 'Monday.' is inside almost any answer, and eating
    a one-word human reply is worse than missing a short echo."""
    text = "Monday. We need six more brackets and two rows of building wrap."
    out, _ = filter_agent_text(text, _answers((NOON, ANSWER)), NOON, 30.0)
    assert out.startswith("Monday.")


def test_no_answers_returns_the_text_unchanged():
    text = "Anything at all."
    out, stats = filter_agent_text(text, [], NOON, 30.0)
    assert out == text and stats["removed"] == 0


def test_empty_text_is_safe():
    out, stats = filter_agent_text("", _answers((NOON, ANSWER)), NOON, 30.0)
    assert out == "" and stats["removed"] == 0


def test_a_file_with_no_timestamp_is_left_alone():
    """`extract_timestamp_from_filename` can return None. Matching on text alone at file
    granularity would strip a whole report's worth of sentences."""
    text = "The concrete pour is scheduled for Monday with the weather contingency to Wednesday."
    out, stats = filter_agent_text(text, _answers((NOON, ANSWER)), None, 30.0)
    assert out == text and stats["removed"] == 0


def test_chinese_sentences_are_segmented_and_matched():
    """Splitting on '.' alone never fires on Chinese, which ends sentences with U+3002."""
    answer = "混凝土浇筑安排在周一，天气备用窗口延到周三。"
    text = "脚手架需要检查。混凝土浇筑安排在周一，天气备用窗口延到周三。我们还缺两个支架。"
    out, stats = filter_agent_text(text, _answers((NOON, answer)), NOON, 30.0)
    assert "混凝土浇筑安排在周一" not in out
    assert "脚手架需要检查" in out
    assert stats["removed"] == 1
