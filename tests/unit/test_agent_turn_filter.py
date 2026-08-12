"""The Ask agent's spoken answer gets recorded by the meeting it is answering.

Measured on prod 2026-08-12: the operator asked SP-Ask about the concrete pour, the device
played the answer aloud, the running recording picked it up, and it became a finding on the
daily timeline reading as a fact confirmed on site. Nobody confirmed it -- the agent read it
back out of the index it had been built from.

That closes a loop: extraction -> report_chunks -> RAG -> agent answers aloud -> recorded ->
transcription -> extraction. One statement said once becomes N mutually corroborating findings,
all from the same original utterance, and nothing in the report shows they share a source.
"""
from datetime import datetime, timedelta

from agent_turn_filter import AgentAnswer, filter_agent_turns


def _turn(text, at, speaker="spk_1"):
    return {"speaker": speaker, "text": text,
            "abs_start": at, "abs_end": at + timedelta(seconds=6)}


NOON = datetime(2026, 8, 12, 12, 13, 21)

# Verbatim from the incident: voice_ask_log.answer, and the two turns it produced.
ANSWER = "The concrete pour is scheduled for Monday with a weather contingency to Wednesday."
HEARD = "The concrete pour is scheduled for Monday with the weather contingency to Wednesday."
FRAGMENT = "The concrete pour is scheduled"


def _answers(*rows):
    return [AgentAnswer(at_local=at, text=txt) for at, txt in rows]


def test_the_measured_case_is_matched():
    """One word differs -- 'with a' vs 'with the'. The transcript is a re-transcription of TTS
    audio through a room microphone, so it is never character-identical."""
    turns = [_turn(HEARD, NOON + timedelta(seconds=4))]
    out, stats = filter_agent_turns(turns, _answers((NOON, ANSWER)))
    assert out[0]["from_agent"] is True
    assert stats["matched"] == 1


def test_a_fragment_of_the_answer_is_matched():
    """The measured tail: diarisation split one played answer, and the second piece is 5 tokens
    of a 16-token sentence. A symmetric similarity ratio scores that around 0.4 and would leave
    half the agent's sentence in the record -- first playback filtered, fragment kept.

    Containment is what makes this work: is the TURN inside the ANSWER, not are they alike.
    """
    turns = [_turn(FRAGMENT, NOON + timedelta(seconds=25))]
    out, _ = filter_agent_turns(turns, _answers((NOON, ANSWER)))
    assert out[0]["from_agent"] is True


def test_a_person_talking_inside_the_window_is_untouched():
    """People talk right after asking. Time alone must never be enough."""
    turns = [_turn("Can you grab the level off the ute", NOON + timedelta(seconds=5), "spk_0")]
    out, stats = filter_agent_turns(turns, _answers((NOON, ANSWER)))
    assert "from_agent" not in out[0]
    assert stats["matched"] == 0


def test_a_person_quoting_the_agent_much_later_is_untouched():
    """Someone repeating the answer an hour on is a person reporting a fact, and deleting it
    would remove the only human record of it. Text alone must never be enough either."""
    turns = [_turn(HEARD, NOON + timedelta(hours=1))]
    out, _ = filter_agent_turns(turns, _answers((NOON, ANSWER)))
    assert "from_agent" not in out[0]


def test_playback_starts_after_the_answer_is_produced():
    """`at` is stamped when the answer text is generated, before the audio has even reached the
    device, and a long answer plays for tens of seconds. The window is two-sided and generous
    on the late side for that reason."""
    turns = [_turn(HEARD, NOON + timedelta(seconds=40))]
    out, _ = filter_agent_turns(turns, _answers((NOON, ANSWER)))
    assert out[0]["from_agent"] is True


def test_a_turn_slightly_before_the_stamp_still_matches():
    """The device clock can sit a little behind the server's. Being strict on the early side
    would drop matches for a skew nobody can see."""
    turns = [_turn(HEARD, NOON - timedelta(seconds=3))]
    out, _ = filter_agent_turns(turns, _answers((NOON, ANSWER)))
    assert out[0]["from_agent"] is True


def test_chinese_answers_match():
    """`[^0-9a-z]` normalisation erases CJK entirely -- every character becomes empty, so any
    two Chinese strings compare equal AND any Chinese turn has zero tokens. This repo has
    shipped that bug twice. Qwen answers Chinese questions in Chinese, so an English-only
    suite proves nothing about the path most likely to break."""
    answer = "混凝土浇筑安排在周一，天气备用窗口延到周三。"
    turns = [_turn("混凝土浇筑安排在周一 天气备用窗口延到周三", NOON + timedelta(seconds=4))]
    out, _ = filter_agent_turns(turns, _answers((NOON, answer)))
    assert out[0]["from_agent"] is True


def test_two_different_chinese_sentences_do_not_match():
    """The other half of the CJK bug: normalisation that erases the characters makes every
    Chinese string equal to every other one."""
    answer = "混凝土浇筑安排在周一。"
    turns = [_turn("脚手架需要在周五之前检查", NOON + timedelta(seconds=4))]
    out, _ = filter_agent_turns(turns, _answers((NOON, answer)))
    assert "from_agent" not in out[0]


def test_a_very_short_turn_is_not_matched_on_coincidence():
    """Below the token floor, containment is meaningless: 'yes' is contained in almost any
    answer. The floor is what stops the filter eating one-word human replies."""
    turns = [_turn("Monday", NOON + timedelta(seconds=4))]
    out, _ = filter_agent_turns(turns, _answers((NOON, ANSWER)))
    assert "from_agent" not in out[0]


def test_no_sidecar_entries_changes_nothing():
    turns = [_turn(HEARD, NOON), _turn("Scaffold check on Friday", NOON, "spk_0")]
    out, stats = filter_agent_turns(turns, [])
    assert all("from_agent" not in t for t in out)
    assert stats["matched"] == 0


def test_duplicate_sidecar_entries_count_once():
    """The audit hop is an at-least-once `Event` invoke, so the same answer can be written
    twice. A duplicate must not double-count the match or mark the turn twice."""
    turns = [_turn(HEARD, NOON + timedelta(seconds=4))]
    out, stats = filter_agent_turns(turns, _answers((NOON, ANSWER), (NOON, ANSWER)))
    assert out[0]["from_agent"] is True
    assert stats["matched"] == 1


def test_an_answer_that_matched_nothing_is_counted():
    """A device whose clock is out matches nothing, silently. This codebase has a shipped
    instance of a wall clock 12 hours wrong, and a skewed device would look exactly like a
    session where nobody asked anything. The unmatched count is the only way it is ever
    noticed, so it is a requirement, not telemetry."""
    turns = [_turn("Scaffold check on Friday", NOON, "spk_0")]
    _, stats = filter_agent_turns(turns, _answers((NOON, ANSWER)))
    assert stats["answers_with_no_match"] == 1


def test_turns_without_timestamps_are_left_alone():
    """A transcript with no diarisation collapses to a single turn with no abs_start. Matching
    on text alone there would delete the entire session."""
    turns = [{"speaker": "unknown", "text": HEARD}]
    out, _ = filter_agent_turns(turns, _answers((NOON, ANSWER)))
    assert "from_agent" not in out[0]


def test_the_original_turn_list_is_not_mutated():
    """Callers rebuild turns from raw JSON on every path; a filter that mutates its input makes
    two consumers disagree depending on call order."""
    original = _turn(HEARD, NOON + timedelta(seconds=4))
    turns = [original]
    filter_agent_turns(turns, _answers((NOON, ANSWER)))
    assert "from_agent" not in original
