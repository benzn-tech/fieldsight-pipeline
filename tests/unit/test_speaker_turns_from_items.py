"""Unit: speaker turns derived from word items.

`results.audio_segments` is an AWS Transcribe field — it exists only when
Transcribe's own diarization ran. Every other provider normalizes to
`transcripts` + `items` and stops there.

The reader for the transcript viewer required audio_segments, so on the day
test switched to ElevenLabs it rendered an EMPTY transcript for recordings that
had transcribed perfectly. Reports and topics were correct the whole time,
because extraction reads `items` — which is what made the failure look like an
ASR problem rather than a reader problem.
"""
from transcript_utils import speaker_turns_from_items


def _item(content, speaker="spk_0", start="0.0", end="0.5", kind="pronunciation"):
    it = {"type": kind, "alternatives": [{"content": content}]}
    if kind == "pronunciation":
        it.update({"start_time": start, "end_time": end, "speaker_label": speaker})
    return it


def test_consecutive_words_from_one_speaker_are_one_turn():
    turns = speaker_turns_from_items({"items": [
        _item("Check", start="0.0", end="0.4"),
        _item("the", start="0.4", end="0.5"),
        _item("scaffold", start="0.5", end="1.0"),
    ]})
    assert len(turns) == 1
    assert turns[0]["transcript"] == "Check the scaffold"
    assert turns[0]["start_time"] == "0.0"
    assert turns[0]["end_time"] == "1.0"


def test_a_change_of_speaker_starts_a_new_turn():
    turns = speaker_turns_from_items({"items": [
        _item("Morning", speaker="spk_0"),
        _item("Morning", speaker="spk_1"),
        _item("all", speaker="spk_1"),
    ]})
    assert [t["speaker_label"] for t in turns] == ["spk_0", "spk_1"]
    assert turns[1]["transcript"] == "Morning all"


def test_chinese_is_not_space_separated():
    """The product records Chinese and English in the same sentence. Joining
    every token with a space is right for one and visibly broken for the other:
    "我 现 在 在 剪" is the same words and an unusable transcript."""
    turns = speaker_turns_from_items({"items": [
        _item("我"), _item("现在"), _item("在"), _item("剪"),
    ]})
    assert turns[0]["transcript"] == "我现在在剪"


def test_a_bilingual_sentence_keeps_both_conventions():
    turns = speaker_turns_from_items({"items": [
        _item("We"), _item("need"), _item("的"), _item("是"), _item("scaffold"),
    ]})
    assert turns[0]["transcript"] == "We need的是scaffold"


def test_punctuation_attaches_without_a_space_and_starts_no_turn():
    """Transcribe emits punctuation as its own item with no speaker and no
    timing. Treated as a word it would both insert a space before the comma and
    open a phantom turn."""
    turns = speaker_turns_from_items({"items": [
        _item("Right", start="0.0", end="0.3"),
        _item(",", kind="punctuation"),
        _item("next", speaker="spk_0", start="0.4", end="0.7"),
    ]})
    assert len(turns) == 1
    assert turns[0]["transcript"] == "Right, next"


def test_an_item_with_no_speaker_label_still_produces_a_turn():
    """A provider may omit speaker labels entirely. One unlabelled turn is a
    usable transcript; dropping the text is not."""
    turns = speaker_turns_from_items({"items": [
        {"type": "pronunciation", "start_time": "0.0", "end_time": "0.5",
         "alternatives": [{"content": "Solo"}]},
    ]})
    assert turns == [{"speaker_label": "spk_0", "transcript": "Solo",
                      "start_time": "0.0", "end_time": "0.5"}]


def test_no_items_yields_no_turns():
    assert speaker_turns_from_items({}) == []
    assert speaker_turns_from_items({"items": []}) == []


def test_blank_content_is_dropped_rather_than_becoming_an_empty_turn():
    turns = speaker_turns_from_items({"items": [
        _item("   "), _item("Real"),
    ]})
    assert len(turns) == 1 and turns[0]["transcript"] == "Real"
