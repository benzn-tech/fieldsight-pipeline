"""Unit: the gateway carries the previous turn, and refuses to carry junk.

STEP 2 of the continuity spec, and deliberately INERT: the gateway forwards
`history` and the agent counts it. Nothing retrieves with it yet. That ordering
exists because the device half and the retrieval half are owned by different
people, and an inert forward can be shipped and observed in production logs
before either of them commits to a shape.

Three things are pinned here, none of which is "continuity works".

ABSENT STAYS ABSENT. Every caller today sends no history, and
`test_forwards_mode_voice_audio_and_caller_sub` in the sibling file asserts the
forwarded payload EQUALS a four-key dict. A key added unconditionally -- even as
`[]` -- turns that assertion red and, worse, teaches the agent that "no history"
and "an empty conversation" are the same message. They are not: one is an old
client, the other is a user who just cleared their history.

MALFORMED IS DROPPED, NOT REJECTED. A device that ships a serialisation bug must
lose its *memory*, not its *voice*. Returning 400 here would take hands-free Ask
offline on every device running that build, to protect a feature that is not
wired up yet. So bad turns are discarded and the count is logged -- the trace is
how anyone finds out.

THE CAPS ARE ON THE GATEWAY. This body already carries up to 1.5 MB of base64
audio and is forwarded to a Lambda with a 6 MB synchronous payload ceiling. An
unbounded history field is the one part of this request a client controls
without limit.

The screen path is out of scope by the spec (§8) and is asserted here, because
`ask_question` and `ask_voice` build their payloads field by field and the usual
way this repo breaks is by fixing one of two implementations.
"""
import json
import os

import pytest

os.environ.setdefault("AWS_ACCESS_KEY_ID", "testing")
os.environ.setdefault("AWS_SECRET_ACCESS_KEY", "testing")
os.environ.setdefault("AWS_DEFAULT_REGION", "ap-southeast-2")

fapi = pytest.importorskip("lambda_fieldsight_api",
                           reason="requires boto3 (installed in CI)")

# Reuse the sibling file's fake rather than a second copy of it: two fakes that
# drift is how a gateway test starts passing against a shape the gateway no
# longer sends.
sibling = pytest.importorskip(
    "tests.unit.test_lambda_fieldsight_api_ask_voice")
WORKER_CALLER = sibling.WORKER_CALLER
wire = sibling.wire


def _payload(fake):
    return fake.calls[0]["Payload"]


TURN = {"question": "how is level three going",
        "answer": "The slab pour finished on Tuesday."}


def test_no_history_sends_no_key(monkeypatch):
    """THE compatibility test. Not `history: []` -- absent. Every device in the
    field today sends nothing, and an empty list is a different statement."""
    fake = wire(monkeypatch)
    fapi.ask_voice({"audio": "QUJD"}, WORKER_CALLER)
    assert "history" not in _payload(fake)


def test_an_empty_history_also_sends_no_key(monkeypatch):
    """A device that has history support but an empty conversation. There is
    nothing to carry, so carrying an empty list only gives the agent a field to
    misread."""
    fake = wire(monkeypatch)
    fapi.ask_voice({"audio": "QUJD", "history": []}, WORKER_CALLER)
    assert "history" not in _payload(fake)


def test_one_turn_is_forwarded_verbatim(monkeypatch):
    fake = wire(monkeypatch)
    fapi.ask_voice({"audio": "QUJD", "history": [TURN]}, WORKER_CALLER)
    assert _payload(fake)["history"] == [TURN]


def test_only_question_and_answer_survive(monkeypatch):
    """A turn is two strings. Anything else the device happens to keep locally --
    timestamps, audio keys, ids, a site name -- is not forwarded, because this
    field ends up inside an LLM prompt and a passthrough is how unreviewed
    client data gets there."""
    fake = wire(monkeypatch)
    fapi.ask_voice({"audio": "QUJD", "history": [
        dict(TURN, at="2026-09-12T10:00:00Z", audio_key="voice_ask/x.wav",
             caller_sub="sub-someone-else")]}, WORKER_CALLER)
    assert _payload(fake)["history"] == [TURN]


def test_the_oldest_turns_are_dropped_first(monkeypatch):
    """When the cap bites, the RECENT turns are the ones a follow-up refers to.
    Keeping the head would mean a user's fourth follow-up is answered against
    the conversation they had ten minutes ago."""
    fake = wire(monkeypatch)
    turns = [{"question": "q%d" % i, "answer": "a%d" % i} for i in range(20)]
    fapi.ask_voice({"audio": "QUJD", "history": turns}, WORKER_CALLER)
    kept = _payload(fake)["history"]
    assert len(kept) == fapi.MAX_VOICE_HISTORY_TURNS
    assert kept[-1] == {"question": "q19", "answer": "a19"}
    assert kept[0]["question"] == "q%d" % (20 - fapi.MAX_VOICE_HISTORY_TURNS)


def test_a_long_turn_is_truncated_not_discarded(monkeypatch):
    """A four-minute answer is still the thing the follow-up is about. Dropping
    the turn would lose the context entirely; trimming it loses the tail."""
    fake = wire(monkeypatch)
    long_answer = "x" * (fapi.MAX_VOICE_HISTORY_CHARS * 3)
    fapi.ask_voice({"audio": "QUJD",
                    "history": [{"question": "q", "answer": long_answer}]},
                   WORKER_CALLER)
    kept = _payload(fake)["history"]
    assert len(kept) == 1
    assert kept[0]["question"] == "q"
    assert len(kept[0]["answer"]) == fapi.MAX_VOICE_HISTORY_CHARS


@pytest.mark.parametrize("bad", [
    "a string",                       # not a list
    {"question": "q", "answer": "a"}, # one turn, unwrapped
    42,
])
def test_a_malformed_history_does_not_break_the_ask(monkeypatch, bad):
    """FAILS SOFT, deliberately. The device loses its memory, never its voice:
    a 400 here would take hands-free Ask offline across a whole app build to
    protect a feature that is not wired up yet."""
    fake = wire(monkeypatch)
    res = fapi.ask_voice({"audio": "QUJD", "history": bad}, WORKER_CALLER)
    assert res["statusCode"] == 200
    assert "history" not in _payload(fake)


@pytest.mark.parametrize("bad_turn", [
    None, "q", 7, [], {}, {"question": "q"}, {"answer": "a"},
    {"question": "", "answer": "a"}, {"question": "q", "answer": ""},
    {"question": None, "answer": "a"}, {"question": 1, "answer": 2},
])
def test_a_bad_turn_is_dropped_and_the_good_ones_are_kept(monkeypatch, bad_turn):
    """Per-turn, not all-or-nothing. One corrupted entry must not erase a
    conversation that is otherwise intact."""
    fake = wire(monkeypatch)
    fapi.ask_voice({"audio": "QUJD", "history": [bad_turn, TURN]}, WORKER_CALLER)
    assert _payload(fake)["history"] == [TURN]


def test_a_history_of_nothing_but_junk_sends_no_key(monkeypatch):
    """Not an empty list -- absent, same as a client that sent nothing. "I sent
    you five turns and you kept none" and "I sent you nothing" must reach the
    agent as the same message, or the count in the log means two things."""
    fake = wire(monkeypatch)
    fapi.ask_voice({"audio": "QUJD", "history": [None, {}, "x"]}, WORKER_CALLER)
    assert "history" not in _payload(fake)


def test_the_screen_path_does_not_gain_history(monkeypatch):
    """Out of scope by the spec, and asserted because fixing one of two
    implementations is this repo's oldest failure. The screen Ask has its own
    payload builder; continuity there is a separate decision with a visible
    transcript already on the page."""
    fake = wire(monkeypatch)
    fapi.ask_question({"question": "how is level three", "history": [TURN]},
                      WORKER_CALLER)
    assert "history" not in _payload(fake)


def test_history_cannot_smuggle_a_caller(monkeypatch):
    """Identity comes from the Cognito authorizer, never the body. A history
    entry carrying `caller_sub` must not survive into the payload -- the ACL
    downstream reads that field."""
    fake = wire(monkeypatch)
    fapi.ask_voice({"audio": "QUJD",
                    "history": [{"question": "q", "answer": "a",
                                 "caller_sub": "sub-admin"}]},
                   WORKER_CALLER)
    sent = _payload(fake)
    assert sent["caller_sub"] == WORKER_CALLER["sub"]
    assert all("caller_sub" not in t for t in sent["history"])


def test_the_whole_history_is_bounded_in_bytes(monkeypatch):
    """Turn count and per-field length are two caps; their product is the one
    that matters. This body already carries 1.5 MB of base64 audio toward a
    6 MB synchronous invoke ceiling, and history is the part a client sets
    without limit."""
    fake = wire(monkeypatch)
    turns = [{"question": "q" * 5000, "answer": "a" * 5000} for _ in range(50)]
    fapi.ask_voice({"audio": "QUJD", "history": turns}, WORKER_CALLER)
    sent = json.dumps(_payload(fake)["history"])
    assert len(sent) <= (fapi.MAX_VOICE_HISTORY_TURNS
                         * fapi.MAX_VOICE_HISTORY_CHARS * 2) + 1024
