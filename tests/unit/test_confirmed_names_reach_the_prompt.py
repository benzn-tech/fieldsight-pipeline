"""A confirmed speaker name is scoped to ONE transcript call, not to the session.

`spk_0` in one call and `spk_0` in the next are usually different people -- that is
the entire reason the anonymous re-bind exists, and it was measured: speaker labels
are about 55% pure across a session. So the map from label to name is keyed on
`(source_filename, speaker_label)`, the same key the names are stored under and the
same key the re-bind uses.

A session-level `{"spk_0": "Ben"}` map would be smaller, would look right in a demo
recorded in one call, and would put one person's name on another person's words for
most of a real meeting. That is the defect this file exists to prevent, and it is
not visible from either half alone: the producer would be writing names it believes
are correct, and the consumer would be rendering them faithfully.

The lambda that builds the prompt has NO database (no VpcConfig, no PGHOST), so the
names ride the `extraction_requests/` artifact written by the in-VPC org-api -- the
same channel, in the opposite direction, that carries `speaker_turns` out.
"""
import json

import pytest

import lambda_extract_session as ex


def _turn(fn, spk, text, at="09:00:00"):
    return {"source_filename": fn, "speaker": spk, "text": text, "abs_start_str": at}


CALL_A = "ben_2026-08-27_11-06-35_sid93_c0000.json"
CALL_B = "ben_2026-08-27_11-08-03_sid93_c0003.json"


# --------------------------------------------------------------------------
# Rendering
# --------------------------------------------------------------------------

def test_the_same_label_in_two_calls_can_be_two_people():
    """The whole point. Both calls have a `spk_0`; they are different people, and
    only the (file, label) pair can tell them apart."""
    turns = [_turn(CALL_A, "spk_0", "I will order the doors"),
             _turn(CALL_B, "spk_0", "I will chase the supplier")]
    names = {(CALL_A, "spk_0"): "Ben", (CALL_B, "spk_0"): "Mark"}
    text, _ = ex.render_transcript(turns, names=names)
    assert "Ben: I will order the doors" in text
    assert "Mark: I will chase the supplier" in text


def test_a_label_with_no_confirmed_name_keeps_its_label():
    """Half a session named is the normal case. The unnamed half must still say
    what it is rather than borrowing the nearest name."""
    turns = [_turn(CALL_A, "spk_0", "first"), _turn(CALL_A, "spk_1", "second")]
    text, _ = ex.render_transcript(turns, names={(CALL_A, "spk_0"): "Ben"})
    assert "Ben: first" in text
    assert "spk_1: second" in text


def test_a_name_confirmed_in_one_call_does_not_leak_into_another():
    """The failure a session-level map produces, stated as a test."""
    turns = [_turn(CALL_B, "spk_0", "someone else speaking")]
    text, _ = ex.render_transcript(turns, names={(CALL_A, "spk_0"): "Ben"})
    assert "Ben" not in text
    assert "spk_0: someone else speaking" in text


def test_no_names_renders_exactly_as_before():
    """Every extraction that predates this path, and every one the finalize sweep
    still triggers, must be byte-identical."""
    turns = [_turn(CALL_A, "spk_0", "hello")]
    assert ex.render_transcript(turns, names=None)[0] == ex.render_transcript(turns)[0]
    assert "spk_0: hello" in ex.render_transcript(turns)[0]


# --------------------------------------------------------------------------
# The prompt
# --------------------------------------------------------------------------

def test_the_model_is_told_the_names_were_confirmed_by_a_person():
    prompt, _ = ex.build_extraction_prompt(
        "Ben_UCPK", "2026-08-27", "sid93", [_turn(CALL_A, "spk_0", "hi")], 1,
        speaker_names={(CALL_A, "spk_0"): "Ben"})
    assert "CONFIRMED BY A PERSON" in prompt


def test_an_unnamed_session_is_not_told_anything_about_confirmation():
    """Claiming names are confirmed over a transcript that still says spk_0 would
    teach the model to read the recogniser's labels as identities -- the opposite
    of true, and a licence to invent."""
    prompt, _ = ex.build_extraction_prompt(
        "Ben_UCPK", "2026-08-27", "sid93", [_turn(CALL_A, "spk_0", "hi")], 1)
    assert "CONFIRMED BY A PERSON" not in prompt


# --------------------------------------------------------------------------
# The artifact
# --------------------------------------------------------------------------

class _FakeS3:
    def __init__(self, body):
        self._body = body

    def get_object(self, Bucket, Key):
        class _B:
            def __init__(self, b):
                self._b = b

            def read(self):
                return self._b
        return {"Body": _B(self._body)}


def _parse(monkeypatch, payload):
    monkeypatch.setattr(ex, "s3", lambda: _FakeS3(json.dumps(payload).encode()))
    return ex.parse_final_request("bucket", "extraction_requests/x.json")


BASE = {"userFolder": "Ben_UCPK", "date": "2026-08-27", "sessionBase": "sid" + "a" * 32}




def test_the_artifact_is_keyed_into_pairs(monkeypatch):
    parsed = _parse(monkeypatch, dict(BASE, speakerNames=[
        {"source_filename": CALL_A, "speaker_label": "spk_0", "display_name": "Ben"},
        {"source_filename": CALL_B, "speaker_label": "spk_0", "display_name": "Mark"}]))
    assert parsed.speaker_names == {(CALL_A, "spk_0"): "Ben", (CALL_B, "spk_0"): "Mark"}


def test_an_artifact_without_names_asks_for_the_old_behaviour(monkeypatch):
    assert _parse(monkeypatch, BASE).speaker_names is None
    assert _parse(monkeypatch, dict(BASE, speakerNames=[])).speaker_names is None


@pytest.mark.parametrize("row", [
    {"speaker_label": "spk_0", "display_name": "Ben"},                    # no file
    {"source_filename": CALL_A, "display_name": "Ben"},                   # no label
    {"source_filename": CALL_A, "speaker_label": "spk_0"},                # no name
    {"source_filename": CALL_A, "speaker_label": "spk_0", "display_name": "  "},
    "not a dict",
])
def test_an_unusable_row_is_dropped_and_said_out_loud(monkeypatch, caplog, row):
    """A name that does not arrive is a name the user typed and will not see.
    Silence here presents as 'renaming does nothing' -- the complaint this whole
    path exists to answer."""
    caplog.set_level("WARNING")
    good = {"source_filename": CALL_B, "speaker_label": "spk_1", "display_name": "Mark"}
    parsed = _parse(monkeypatch, dict(BASE, speakerNames=[row, good]))
    assert parsed.speaker_names == {(CALL_B, "spk_1"): "Mark"}
    assert "dropped 1" in caplog.text


def test_every_row_unusable_is_not_a_silent_success(monkeypatch, caplog):
    caplog.set_level("WARNING")
    parsed = _parse(monkeypatch, dict(BASE, speakerNames=[{"display_name": "Ben"}]))
    assert parsed.speaker_names is None
    assert "dropped 1" in caplog.text


def test_the_handler_hands_the_names_to_the_extraction(monkeypatch):
    """The seam. The parser can key them perfectly and the renderer can use them
    perfectly while the hop between drops the field -- which is exactly how
    label inheritance shipped as unreachable code that reported success."""
    seen = {}

    def _fake(bucket, user_folder, date, session_base, **kw):
        seen.update(kw)
        return "ok"

    monkeypatch.setattr(ex, "extract_session", _fake)
    monkeypatch.setattr(ex, "s3", lambda: _FakeS3(json.dumps(dict(
        BASE, speakerNames=[{"source_filename": CALL_A, "speaker_label": "spk_0",
                             "display_name": "Ben"}])).encode()))
    monkeypatch.setattr(ex, "S3_BUCKET", "bucket")
    ex.lambda_handler({"Records": [{"s3": {"object": {
        "key": "extraction_requests/sid" + "a" * 32 + ".json"}}}]}, None)
    assert seen.get("speaker_names") == {(CALL_A, "spk_0"): "Ben"}
