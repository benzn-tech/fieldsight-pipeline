"""Unit: the clips are cut from the file the timestamps belong to.

The defect this guards is the most expensive one in this corner of the repo and it is
completely silent: after batching, a transcript's timeline belongs to the BATCH wav, while
the device's own upload for the same chunk is 30 seconds. Cutting from the upload makes
every turn past 30 s produce an empty clip, `MIN_TURN_S` drops them, and the measurement
quietly becomes "the first 30 seconds of each batch". On 2026-09-10 that used 30 s of a
114 s recording and nothing about the result looked wrong.
"""
import io
import os
import struct
import sys
import wave

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.join(ROOT, "scripts", "labelling"))

try:
    import cut_turns as ct
except ImportError as exc:  # pragma: no cover - the message IS the point
    raise AssertionError(f"scripts/labelling/cut_turns.py is not importable: {exc}")


def wav_bytes(seconds, rate=16000):
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(rate)
        w.writeframes(struct.pack("<%dh" % int(rate * seconds),
                                  *([0] * int(rate * seconds))))
    return buf.getvalue()


def duration(raw):
    with wave.open(io.BytesIO(raw), "rb") as w:
        return w.getnframes() / float(w.getframerate())


def test_the_segment_key_is_derived_by_swapping_not_by_rebuilding():
    """Every attempt to reconstruct a key from its parts has fetched the wrong file: the
    stem carries each chunk's own timestamp and a `_bnN` batch marker."""
    assert ct._segment_key(
        "transcripts/Ben_UCPK2/2026-09-10/x_c0000_bn2_off0.0_to58.0_srcwav.json"
    ) == "audio_segments/Ben_UCPK2/2026-09-10/x_c0000_bn2_off0.0_to58.0_srcwav.wav"


def test_a_window_past_the_end_returns_none_rather_than_an_empty_clip():
    """The signature of cutting from the wrong file. An empty wav would be marked
    'unusable' by a listener and vanish into the results as ordinary attrition."""
    raw = wav_bytes(30)
    assert ct.cut_wav(raw, 100.0, 105.0) is None
    assert ct.cut_wav(raw, 5.0, 9.0) is not None


def test_a_long_turn_is_trimmed_from_its_start():
    """A listener needs enough to recognise a voice and no more, and the opening of a turn
    is where the speaker change happens -- the tail is where the next speaker bleeds in."""
    raw = wav_bytes(120)
    clip = ct.cut_wav(raw, 10.0, 90.0)
    assert abs(duration(clip) - ct.MAX_CLIP_S) < 0.05


def test_a_window_running_past_the_end_is_truncated_not_refused():
    """A final turn whose end_time rounds past the file is ordinary, not the defect."""
    clip = ct.cut_wav(wav_bytes(10), 8.0, 14.0)
    assert clip is not None and abs(duration(clip) - 2.0) < 0.05


def test_turns_under_the_floor_never_become_clips():
    """The matcher refuses them whatever the scores say, so a label on one could never
    appear in any ROC."""
    doc = {"results": {"audio_segments": [
        {"start_time": "0.0", "end_time": "2.0", "speaker_label": "spk_0", "transcript": "no"},
        {"start_time": "2.0", "end_time": "9.0", "speaker_label": "spk_0", "transcript": "yes"},
    ]}}
    turns = ct.turns_from_transcript(doc)
    assert [t["text"] for t in turns] == ["yes"]


def test_a_provider_without_audio_segments_still_yields_turns():
    """ElevenLabs and everything added later normalise to `items` and stop there. A reader
    that requires audio_segments shows an EMPTY transcript for a recording that transcribed
    perfectly -- which is exactly what happened the day the ASR provider changed."""
    doc = {"results": {"items": [
        {"start_time": "0.0", "end_time": "4.0", "speaker_label": "spk_0",
         "alternatives": [{"content": "hello"}]},
        {"start_time": "4.0", "end_time": "8.0", "speaker_label": "spk_0",
         "alternatives": [{"content": "there"}]},
    ]}}
    assert ct.turns_from_transcript(doc), "items-only transcripts produced no turns"


def test_a_quiet_clip_is_brought_up_so_it_can_be_labelled_at_all():
    """Measured on the first real cut, 2026-09-23: peaks across sixteen clips ran 973 to
    27562 out of 32768, a 28x spread. A listener given those raw marks the quiet half
    'unusable' and the dataset ends up describing loud recordings only, with nothing
    anywhere saying so -- a sampling bias the tool introduced that looks exactly like a
    property of the material."""
    quiet = struct.pack("<8h", *([300, -300] * 4))
    out = ct.normalise_for_listening(quiet)
    peak = max(abs(v) for v in struct.unpack("<8h", out))
    assert peak > 20000, "a quiet clip was left unhearable"


def test_a_loud_clip_is_left_alone():
    """Gain is only ever applied upward; scaling a clip down would be changing material the
    listener is meant to judge for no reason."""
    loud = struct.pack("<4h", *[30000, -30000, 30000, -30000])
    assert ct.normalise_for_listening(loud) == loud


def test_silence_is_not_amplified_into_something_that_sounds_like_audio():
    """Multiplying nothing by a large number is how a microphone fault becomes convincing
    noise -- and a silent capture is a known unfixed defect here, not a rarity."""
    silence = struct.pack("<8h", *([0] * 8))
    assert ct.normalise_for_listening(silence) == silence
    assert ct.normalise_for_listening(b"") == b""
