"""Unit: an audio-event tag is not something a person said.

ElevenLabs `scribe_v2` annotates non-speech it hears — `[background noise]`,
`[laughs]`, `[outro jingle]`, `[objects clattering]`, and in Chinese audio
`[话筒碰撞声]`, `[吸气声]`. AWS Transcribe never emitted these, so they arrived
with the 2026-08-07 provider switch and flow straight into the extraction
prompt, the rolling summary and the confirmation email as ordinary speech.

The properties pinned here:

1. **Match the bracket FORM, not a phrase list.** The vocabulary is open and
   unstable — the same event came back as `[点击鼠标]` and `[鼠标点击]` in one
   evaluation, and the set differs per language. A phrase table would be
   permanently behind; the shape is the invariant.
2. **A mixed turn keeps its words.** `[background noise] So you've got to
   rearrange these fences` is a real sentence with a tag stuck to it. Dropping
   the turn would delete site conversation, which is the opposite of the point.
3. **Stripping happens BEFORE the announcement filter.** `[background noise]
   Recording started.` only becomes recognisable as a device announcement once
   the tag is gone — `is_device_announcement` matches whole sentences, so the
   mixed turn would otherwise survive as speech.
4. **A tag-only turn disappears entirely**, rather than becoming an empty turn
   that downstream code has to defend against.
5. **Real brackets in speech are not eaten wholesale.** The pattern is bounded,
   so an unclosed `[` cannot swallow the rest of a turn.
"""
import os

os.environ.setdefault("AWS_ACCESS_KEY_ID", "testing")
os.environ.setdefault("AWS_SECRET_ACCESS_KEY", "testing")
os.environ.setdefault("AWS_DEFAULT_REGION", "ap-southeast-2")
os.environ.setdefault("S3_BUCKET", "test-bucket")
os.environ.setdefault("ANTHROPIC_API_KEY", "sk-ant-test-dummy-key")

import lambda_extract_session as ex  # noqa: E402


def _turn(text, start=0.0):
    return {"text": text, "abs_start": start, "speaker": "spk_0"}


# ---- the form, not the vocabulary ----------------------------------------

def test_english_and_chinese_tags_both_go():
    """No phrase list is consulted, so an unseen language costs nothing."""
    turns, stats = ex.filter_audio_event_tags([
        _turn("[background noise]", 0.0),
        _turn("[话筒碰撞声]", 1.0),
        _turn("[outro jingle]", 2.0),
    ])
    assert turns == []
    assert stats["removed"] == 3


def test_an_invented_tag_nobody_has_seen_still_goes():
    turns, _ = ex.filter_audio_event_tags([_turn("[distant helicopter]")])
    assert turns == []


# ---- a mixed turn keeps its words ----------------------------------------

def test_mixed_turn_keeps_the_speech():
    turns, stats = ex.filter_audio_event_tags([
        _turn("[background noise] So you've got to rearrange these fences")
    ])
    assert len(turns) == 1
    assert turns[0]["text"] == "So you've got to rearrange these fences"
    assert stats["stripped"] == 1


def test_tag_in_the_middle_leaves_one_space():
    turns, _ = ex.filter_audio_event_tags([
        _turn("That's the crack [objects clattering] now")
    ])
    assert turns[0]["text"] == "That's the crack now"


def test_a_turn_with_no_tags_is_returned_unchanged():
    original = _turn("There's no steel. They're reinforcing the stairs.")
    turns, stats = ex.filter_audio_event_tags([original])
    assert turns[0]["text"] == "There's no steel. They're reinforcing the stairs."
    assert stats["removed"] == 0 and stats["stripped"] == 0


# ---- ordering against the announcement filter -----------------------------

def test_tag_stripping_lets_the_announcement_filter_see_the_prompt():
    """The ordering property. `is_device_announcement` matches whole sentences,
    so a tag glued to the prompt hides it. Stripping first is what makes the
    existing filter work on ElevenLabs output at all."""
    mixed = "[background noise] Recording started."
    assert ex.is_device_announcement(mixed) is False        # hidden by the tag
    stripped, _ = ex.filter_audio_event_tags([_turn(mixed)])
    assert ex.is_device_announcement(stripped[0]["text"]) is True


# ---- bounded, so a stray bracket cannot eat a turn ------------------------

def test_unclosed_bracket_is_left_alone():
    turns, stats = ex.filter_audio_event_tags([
        _turn("[unclosed and then a long stretch of real conversation follows")
    ])
    assert turns[0]["text"].startswith("[unclosed")
    assert stats["removed"] == 0 and stats["stripped"] == 0


def test_an_overlong_bracket_run_is_not_treated_as_a_tag():
    long_tag = "[" + "x" * 200 + "]"
    turns, _ = ex.filter_audio_event_tags([_turn(long_tag + " real words")])
    assert long_tag in turns[0]["text"]


# ---- the filter reports what it met --------------------------------------

def test_stats_record_the_distinct_tags_seen():
    """Same reason the announcement filter reports its phrases: the vocabulary
    is not settled, so the filter is also the instrument that tells us what is
    actually arriving."""
    _, stats = ex.filter_audio_event_tags([
        _turn("[background noise]", 0.0),
        _turn("[background noise] and then we talked", 1.0),
        _turn("[laughs]", 2.0),
    ])
    assert stats["tags"] == ["[background noise]", "[laughs]"]


# ---- it is wired into assembly, not just available ------------------------

def test_assembly_applies_it_before_announcements(monkeypatch):
    """A tag-only turn and a tag+prompt turn both vanish through the real
    assembly path; the sentence in between survives."""
    monkeypatch.setattr(ex, "_dedup_turn_boundaries", lambda t: t)
    turns = [
        _turn("[background noise]", 0.0),
        _turn("[background noise] Recording started.", 1.0),
        _turn("[laughs] We changed how we were breaking into the panel.", 2.0),
    ]
    kept, _ = ex.filter_audio_event_tags(turns)
    kept, _ = ex.filter_device_announcements(kept)
    assert [t["text"] for t in kept] == [
        "We changed how we were breaking into the panel."
    ]
