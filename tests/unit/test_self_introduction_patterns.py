"""Unit: the self-introduction detector (`src/self_introduction.py`).

Pure pattern matching, no I/O. See the design doc's "Review outcome" and the plan's
"Spec corrections" for why each rule below is shaped the way it is.
"""
import transcript_utils


def _item(content, speaker="spk_0", start="0.0", end="0.5", kind="pronunciation"):
    it = {"type": kind, "alternatives": [{"content": content}]}
    if kind == "pronunciation":
        it.update({"start_time": start, "end_time": end, "speaker_label": speaker})
    return it


def _turns_from_sentence(sentence, speaker="spk_0", source_filename="Ben1_2026-09-29_09-00-00_sid" + "a" * 32 + ".json", turn_seconds=6.0):
    """Build turns the way `assemble_session_turns` does: `normalize_transcript`
    output (keys: speaker, text, start_sec, end_sec) plus `source_filename` --
    never a hand-built {"speaker_label": ...} dict, which is exactly the shape
    that let a bug reading the wrong key pass silently before (spec correction 1).
    """
    words = sentence.split(" ")
    items = []
    t = 0.0
    step = turn_seconds / max(len(words), 1)
    for w in words:
        items.append(_item(w, speaker=speaker, start=f"{t:.2f}", end=f"{t + step:.2f}"))
        t += step
    transcript_data = {"results": {"transcripts": [{"transcript": sentence}], "items": items}}
    normalized = transcript_utils.normalize_transcript(transcript_data, source_filename)
    turns = [dict(turn, source_filename=source_filename) for turn in normalized["speaker_turns"]]
    return turns


def test_reads_speaker_not_speaker_label_from_assemble_session_turns_output():
    """The contract test: feed real `normalize_transcript` output (key `speaker`,
    no `speaker_label` anywhere in the input) and confirm the detector still finds
    the intro and emits `speaker_label` on its way OUT. A detector that reads
    `t["speaker_label"]` on the way in gets None/KeyError here, not silence on a
    hand-built double that happened to agree with it.
    """
    import self_introduction
    turns = _turns_from_sentence("Hi, this is Petros from Cassidy")
    assert "speaker_label" not in turns[0]
    assert "speaker" in turns[0]
    results = self_introduction.find(turns)
    assert len(results) == 1
    assert results[0]["speaker_label"] == "spk_0"


# ---------------------------------------------------------------------------
# Positives
# ---------------------------------------------------------------------------

def _find_one(sentence, **kw):
    import self_introduction
    turns = _turns_from_sentence(sentence, **kw)
    results = self_introduction.find(turns)
    assert len(results) == 1, f"expected one hit for {sentence!r}, got {results}"
    return results[0]


def test_hi_this_is_x_from_company():
    r = _find_one("Hi, this is Petros from Cassidy")
    assert r["heard_name"] == "Petros"
    assert r["company_name"] == "Cassidy"


def test_my_name_is_two_token_name():
    r = _find_one("my name is Sam Yu")
    assert r["heard_name"] == "Sam Yu"


def test_my_names_contraction():
    r = _find_one("my name's Ben")
    assert r["heard_name"] == "Ben"


def test_im_x_comma_from_company():
    r = _find_one("I'm Petros, from Cassidy")
    assert r["heard_name"] == "Petros"
    assert r["company_name"] == "Cassidy"


def test_i_am_x_with_company():
    r = _find_one("I am Petros with Fletcher")
    assert r["heard_name"] == "Petros"
    assert r["company_name"] == "Fletcher"


def test_introduce_myself_precedes_im_x_with_no_following_context():
    r = _find_one("let me introduce myself, I'm Petros Pan")
    assert r["heard_name"] == "Petros Pan"


def test_gday_this_is_x():
    r = _find_one("G'day, this is Dave")
    assert r["heard_name"] == "Dave"


def test_mandarin_wojiao():
    r = _find_one("我叫王小明")
    assert r["heard_name"] == "王小明"


def test_mandarin_wojiao_spaced():
    r = _find_one("我 叫 王 小 明")
    assert r["heard_name"] == "王小明"


def test_mandarin_wodemingzi_shi():
    r = _find_one("我的名字是李雷")
    assert r["heard_name"] == "李雷"


def test_mandarin_woshi_with_company():
    r = _find_one("我是张伟，来自中建")
    assert r["heard_name"] == "张伟"
    assert r["company_name"] == "中建"


# ---------------------------------------------------------------------------
# Negatives -- named for the false positive each one guards against
# ---------------------------------------------------------------------------

def _find_none(sentence, **kw):
    import self_introduction
    turns = _turns_from_sentence(sentence, **kw)
    results = self_introduction.find(turns)
    assert results == [], f"expected no hit for {sentence!r}, got {results}"


def test_introducing_someone_else_with_no_greeting_is_not_first_person():
    _find_none("This is Benny from Performance")


def test_im_going_is_not_a_name():
    _find_none("I'm going to check the slab")


def test_im_sure_is_not_a_name():
    _find_none("I'm sure it's fine")


def test_im_here_with_someone_else_is_not_a_name():
    _find_none("I'm here with Dave")


def test_this_is_level_2_east_is_a_place_not_a_name():
    _find_none("Hi, this is Level 2 east")


def test_this_is_block_c_is_a_place_not_a_name():
    _find_none("Morning, this is Block C")


def test_this_is_cassidy_construction_is_a_company_not_a_name():
    """Decision 3 (owner, 2026-09-29): reject a company-word suffix as a name."""
    _find_none("Hello, this is Cassidy Construction")


def test_im_gib_fixing_has_no_following_context():
    _find_none("I'm Gib fixing all day")


def test_mandarin_woshi_shuo_is_not_first_person_naming():
    _find_none("我是说这个不行")


def test_mandarin_wojiao_ta_is_a_pronoun_not_a_name():
    _find_none("我叫他过来")


def test_turn_shorter_than_min_turn_sec_is_skipped():
    import self_introduction
    turns = _turns_from_sentence("Hi I'm Petros", turn_seconds=2.5)
    assert self_introduction.find(turns) == []


def test_same_speaker_introducing_twice_in_one_file_yields_one_result():
    import self_introduction
    filename = "Ben1_2026-09-29_09-00-00_sid" + "b" * 32 + ".json"
    first = _turns_from_sentence("Hi, this is Petros from Cassidy",
                                 source_filename=filename, turn_seconds=6.0)
    second = _turns_from_sentence("Hi, this is Petros again",
                                  source_filename=filename, turn_seconds=6.0)
    # Second turn starts after the first, same speaker/file.
    for t in second:
        t["start_sec"] += 100.0
        t["end_sec"] += 100.0
    results = self_introduction.find(first + second)
    assert len(results) == 1
    assert results[0]["heard_name"] == "Petros"


# ---- reported and example speech (measured 2026-09-30, prod + TEST transcripts) ----------
# Five of nine hits over 30 days were somebody else's words. Each sentence below is the
# real transcript text, trimmed; none of them is the speaker introducing themselves.

import pytest as _pytest
import self_introduction  # noqa: E402 -- the tests above import it locally


@_pytest.mark.parametrize("text", [
    'So he\'s like, uh, "I\'m Aaron Arnold from Colliers. Uh, I\'m the, uh, I\'m the',
    'ask them to say, introduce myself, "I\'m," blah, blah, blah. "I\'m Will from '
    'Cassidy Construction," for example.',
    "Or what me? 'Cause you just know my name is Camp. And then I'm like,",
    '"Hi, my name is Jesse. I\'m sharing breakfast at ten AM this Friday."',
    'introduce each of them and say, "I\'m Ben from DBC or from somewhere, somewhere else,',
])
def test_somebody_elses_words_are_not_an_introduction(text):
    assert self_introduction._detect(text) is None


@_pytest.mark.parametrize("text,name", [
    ("Morning. Morning, mate. I'm James. How are you doing?", "James"),
    ("Hey, Sam. This is Ben from FieldSight AI company. Can tell you", "Ben"),
])
def test_real_introductions_still_count(text, name):
    assert self_introduction._detect(text)["heard_name"] == name
