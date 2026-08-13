"""lambda_extract_session._dedup_batch_window_repeats — collapses the SAME audio
transcribed twice by overlapping batch windows.

Distinct from _dedup_turn_boundaries (test_chunk_boundary_dedup.py), which trims
an exact leading word-run at a device seam and is gated on abs_end. Neither
mechanism survives batching: the recogniser re-decodes the same speech
differently in each window, and a batch-rebased turn may carry no usable
abs_end. The cases below are taken from a real batched session
(sid15770a…, 2026-08-13) where 921 near-duplicate pairs survived the seam pass.

The load-bearing test in this file is the no-op one: this pass must not touch a
legacy, VAD or unbatched session.
"""
import os
from datetime import datetime, timedelta

import pytest

# Same import-time env dance as test_chunk_boundary_dedup.py — the module reads
# these once, and this file may import it first depending on collection order.
os.environ.setdefault("AWS_ACCESS_KEY_ID", "testing")
os.environ.setdefault("AWS_SECRET_ACCESS_KEY", "testing")
os.environ.setdefault("AWS_DEFAULT_REGION", "ap-southeast-2")
os.environ.setdefault("ANTHROPIC_API_KEY", "sk-ant-test-dummy-key")

les = pytest.importorskip("lambda_extract_session")

_BASE = datetime(2026, 8, 13, 13, 25, 0)


def T(text, start_s, speaker="spk_0", end_s=None):
    """A turn with NO abs_end unless asked — that is the batched shape, and the
    pass must work without one."""
    t = {
        "speaker": speaker,
        "text": text,
        "abs_start": _BASE + timedelta(seconds=start_s),
        "abs_start_str": (_BASE + timedelta(seconds=start_s)).strftime("%H:%M:%S"),
    }
    if end_s is not None:
        t["abs_end"] = _BASE + timedelta(seconds=end_s)
    return t


def texts(turns):
    return [t["text"] for t in turns]


# --- the real failure modes -------------------------------------------------

def test_collapses_a_re_decode_that_differs_by_one_word():
    # Verbatim from the session: an exact word-run match finds nothing to trim
    # here, because the difference sits in the middle of the run.
    a = T("Well, they said they want it for defense security.", 245)
    b = T("Well, they said they wanted it for defense security.", 245)
    out, stats = les._dedup_batch_window_repeats([a, b])
    assert len(out) == 1
    assert stats["dropped"] == 1


def test_collapses_when_one_copy_has_a_leading_filler():
    # "You know, " only on one side, so the leading run differs at word 1.
    a = T("Like, like we're gonna use this, right?", 473)
    b = T("You know, like, like we're gonna use this, right?", 473)
    out, _ = les._dedup_batch_window_repeats([a, b])
    assert texts(out) == ["You know, like, like we're gonna use this, right?"]


def test_keeps_the_longer_decode_and_the_original_position():
    # Windows clip the same utterance at different points; the fuller one wins,
    # and it keeps the FIRST copy's timestamp so ordering does not shift.
    a = T("Reality capture. He doesn't do reality capture, right? So now he's got like", 481)
    b = T("Reality capture. He doesn't do reality capture, right? So now he's got like a factory, like the-", 481)
    tail = T("Something else entirely that nobody said twice at all.", 600)
    out, _ = les._dedup_batch_window_repeats([a, b, tail])
    assert len(out) == 2
    assert out[0]["text"].endswith("a factory, like the-")
    assert out[0]["abs_start"] == a["abs_start"]
    assert out[1]["text"] == tail["text"]


def test_works_with_no_abs_end_at_all():
    # The seam pass is gated on abs_end; 201 byte-identical adjacent pairs
    # survived it for exactly this reason. This pass must not need one.
    a = T("The information the PM and the ELT wants is hidden in the CM.", 100)
    b = T("The information the PM and the ELT wants is hidden in the CM.", 100)
    assert "abs_end" not in a
    out, stats = les._dedup_batch_window_repeats([a, b])
    assert len(out) == 1 and stats["dropped"] == 1


def test_reports_what_it_removed():
    a = T("Well, they said they want it for defense security.", 245)
    b = T("Well, they said they wanted it for defense security.", 245)
    _, stats = les._dedup_batch_window_repeats([a, b])
    assert stats["dropped"] == 1
    assert stats["chars_saved"] == len(a["text"])


# --- the guards -------------------------------------------------------------

def test_two_defects_sharing_a_tail_are_never_collapsed():
    # THE case this pass must not get wrong. These score 0.891 — inside the
    # 0.857-0.980 range measured for genuine re-decodes — so the ratio cannot
    # tell them apart. They open on different subjects, and that is what saves
    # them. Collapsing these loses a defect report off a site walk.
    a = T("The door on level one is damaged and needs replacing.", 0)
    b = T("The window on level three is damaged and needs replacing.", 1)
    out, _ = les._dedup_batch_window_repeats([a, b])
    assert len(out) == 2


def test_same_observation_on_two_levels_is_kept():
    a = T("Level 2 ceiling tiles are all coming out this week.", 0)
    b = T("Level 4 ceiling tiles are all coming out this week.", 2)
    out, _ = les._dedup_batch_window_repeats([a, b])
    assert len(out) == 2


def test_same_instruction_to_two_trades_is_kept():
    a = T("The electricians need to cap that pipe before Friday.", 0)
    b = T("The plumbers need to cap that pipe before Friday.", 1)
    out, _ = les._dedup_batch_window_repeats([a, b])
    assert len(out) == 2


def test_a_re_decode_that_respells_a_number_is_deliberately_missed():
    # "one hundred and fifty" against "150" scores 0.789, under the ratio floor,
    # so this duplicate survives. Documented rather than fixed: the thresholds
    # that would catch it are the ones that eat the defect pair above.
    a = T("We need to follow up about the one hundred and fifty lumens units.", 10)
    b = T("We need to follow up about the 150 lumens units.", 10)
    out, _ = les._dedup_batch_window_repeats([a, b])
    assert len(out) == 2

def test_no_op_on_a_sequential_unbatched_session():
    # Legacy / VAD / unbatched turns never start within 3s of each other with
    # 85% of their words shared. Byte-for-byte unchanged.
    turns = [T("Morning pour on level two is finished.", 0),
             T("Morning pour on level two is finished.", 90),
             T("The rebar inspection is booked for Thursday.", 150)]
    out, stats = les._dedup_batch_window_repeats(turns)
    assert texts(out) == texts(turns)
    assert stats == {"dropped": 0, "chars_saved": 0}


def test_a_genuine_repetition_further_apart_is_kept():
    # A person really can say the same sentence twice — just not inside 3
    # seconds. The time gate, not the ratio, is what protects them.
    a = T("So we need to move the ducting before they drill.", 0)
    b = T("So we need to move the ducting before they drill.", 20)
    out, _ = les._dedup_batch_window_repeats([a, b])
    assert len(out) == 2


def test_different_speakers_are_never_collapsed():
    # Two people agreeing in the same words is a real exchange, not a re-decode.
    a = T("Yeah, the fire rating on that wall is wrong.", 5, speaker="spk_0")
    b = T("Yeah, the fire rating on that wall is wrong.", 5, speaker="spk_1")
    out, _ = les._dedup_batch_window_repeats([a, b])
    assert len(out) == 2


def test_short_turns_are_left_alone():
    # Backchannel repeats all over a transcript; collapsing them would be an
    # opinion about the conversation, not a fix to the recogniser.
    turns = [T("Mm-hmm.", 0), T("Mm-hmm.", 1), T("Yeah.", 2), T("Yeah.", 2)]
    out, stats = les._dedup_batch_window_repeats(turns)
    assert len(out) == 4 and stats["dropped"] == 0


def test_merely_similar_sentences_are_not_collapsed():
    a = T("The door on level one is damaged and needs replacing.", 0)
    b = T("The window on level three is damaged and needs replacing.", 2)
    out, _ = les._dedup_batch_window_repeats([a, b])
    assert len(out) == 2


def test_does_not_mutate_the_caller_s_turns():
    a = T("Reality capture. He doesn't do reality capture, right? So now he's got", 0)
    b = T("Reality capture. He doesn't do reality capture, right? So now he's got a factory.", 0)
    before = a["text"]
    les._dedup_batch_window_repeats([a, b])
    assert a["text"] == before


def test_empty_and_single_are_safe():
    assert les._dedup_batch_window_repeats([])[0] == []
    one = [T("Just the one turn, nothing to compare it against.", 0)]
    assert les._dedup_batch_window_repeats(one)[0] == one


def test_a_turn_with_no_abs_start_is_passed_through():
    a = {"speaker": "spk_0", "text": "A turn the normaliser could not time at all."}
    out, _ = les._dedup_batch_window_repeats([a])
    assert out == [a]


def test_collapses_a_run_of_four_copies():
    # Verbatim from the session at 13:44:45 — four windows, one sentence, four
    # decodes differing only in punctuation and a stray "uh". All arrive on the
    # same second, which is why the time gate can be as tight as it is.
    copies = [
        T("Oh. So yeah, I'll upload some information. But Heidi, I saw at my "
          "Riccarton, uh, clinic when I went with my-", 1185, speaker="spk_1"),
        T("Oh. So yeah, I'll upload some information. But, uh, Heidi, I saw at my "
          "Riccarton, uh, clinic when I went with my-", 1185, speaker="spk_1"),
        T("Oh. So yeah, I'll upload some information. But, uh, Heidi, I saw at my "
          "Riccarton, uh, clinic when I went with my-", 1185, speaker="spk_1"),
        T("Oh, so yeah, I'll upload some information. But Heidi I saw at my "
          "Riccarton, uh, clinic when I went with my-", 1185, speaker="spk_1"),
    ]
    out, stats = les._dedup_batch_window_repeats(copies)
    assert len(out) == 1
    assert stats["dropped"] == 3
