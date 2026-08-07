"""Unit: a recorder's spoken prompt is not a participant.

Recorders play announcements — "recording started", "please stop recording" —
and any device within earshot records them. The transcriber has no way to know
a machine said it, so they arrive as ordinary speaker turns with speaker
labels. In one real 70-minute session there were five; in the densest five
minutes `spk_2` and `spk_3` were largely machine audio, which is how that
session's artifact came to report `speaker_count: 4` with at least one device
counted as a person.

It gets worse exactly as the product gets better: with multi-device grouping
every device announces start and stop and every nearby device hears it, so
these grow with the square of the crew size.

The properties pinned here:

1. **Every sentence must match, and never as a substring.** "we should stop
   recording now, mate" is a person talking about the recorder; so is
   "Recording stopped. I'll redo that bit.", which opens with the exact prompt
   text and then says the thing worth keeping. A substring filter — or a
   first-sentence-only one — deletes both. This is the property most likely to
   be broken by a later "improvement", so it is tested from several directions.
2. **Filtered before `speaker_count`.** Filtering only at prompt-render time
   would leave the device in the count, and `speaker_count == 1` is a gate
   item-writer uses to resolve a self-referential responsible party to a name.
3. **The phrases are recorded in the artifact.** The app's prompt audio
   (`res/raw/recording_started.mp3` and siblings) was staged on 2026-08-07 and
   ships as of GrandTime PR #13; the wording can still change. The
   filter therefore also reports what it met — a filter whose misses are
   invisible cannot be tuned.
4. **A bad override is loud.** A typo in the env pattern list turns the filter
   off, which reads as "the announcements came back" and sends the next person
   to the transcriber (CLAUDE.md BUG-40).
"""
import os

import pytest

os.environ.setdefault("AWS_ACCESS_KEY_ID", "testing")
os.environ.setdefault("AWS_SECRET_ACCESS_KEY", "testing")
os.environ.setdefault("AWS_DEFAULT_REGION", "ap-southeast-2")
os.environ.setdefault("S3_BUCKET", "test-bucket")
os.environ.setdefault("ANTHROPIC_API_KEY", "sk-ant-test-dummy-key")

import lambda_extract_session as les


# ------------------------------------------------------------------
# What must be removed
# ------------------------------------------------------------------

# The app's four voice lines, verbatim from GrandTime PR #13 (merged 2026-08-07,
# wired and verified in the release apk). These are the strings that will
# actually be spoken on site, so they are the ones that matter — an earlier
# version of this file guessed at the wording and missed two of the four,
# because both are multi-sentence and the matcher was whole-turn only.
REAL_VOICE_LINES = [
    "Recording started.",
    "Recording stopped.",
    "Recording stopped. Has the meeting ended? Check the screen.",
    "Meeting ended. Recording stopped.",
]


@pytest.mark.parametrize("text", REAL_VOICE_LINES)
def test_every_real_voice_line_is_filtered(text):
    assert les.is_device_announcement(text), text


@pytest.mark.parametrize("text", [
    "Recording stopped.",
    "Has the meeting ended?",
    "Meeting ended.",
])
def test_prompt_sentences_are_caught_when_split_at_the_pauses(text):
    """The voice lines have audible pauses between sentences, so a transcriber
    may well emit them as separate turns rather than one. Each sentence has to
    stand on its own.

    "Check the screen." is deliberately absent: a person can say exactly that on
    a site and it carries no recording vocabulary, so it is only ever dropped as
    part of a turn whose other sentences name the recorder."""
    assert les.is_device_announcement(text), text


def test_a_bare_companion_sentence_is_kept():
    assert not les.is_device_announcement("Check the screen.")


@pytest.mark.parametrize("text", [
    "Please stop recording.",              # observed verbatim, 2026-08-07 02:10
    "please stop recording",
    "Recording started.",
    "Recording stopped",
    "The recording has started.",
    "recording has stopped",
    "Start recording",
    "Stopped recording.",
    "The meeting has ended.",
    "Meeting ended",
    "End of meeting.",
])
def test_device_announcements_are_recognised(text):
    assert les.is_device_announcement(text), text


def test_punctuation_and_case_do_not_matter():
    """The same prompt comes back as "Please stop recording." or "please stop
    recording" depending on engine and run. Neither spelling is the interesting
    part, and pinning literals would make the filter engine-specific."""
    for variant in ("PLEASE STOP RECORDING!!", "please  stop   recording",
                    "Please, stop recording."):
        assert les.is_device_announcement(variant), variant


# ------------------------------------------------------------------
# What must NOT be removed — the expensive direction to get wrong
# ------------------------------------------------------------------

@pytest.mark.parametrize("text", [
    "We should stop recording now, mate.",
    "Can you start recording when the pour begins?",
    "I stopped recording before we got to the stair reinforcement, sorry.",
    "The meeting ended up going an hour over because of the steel.",
    "Recording started late so the first bit about the scaffold tags is missing.",
    "Tell him to stop recording the wrong bay and get the east elevation.",
    # The one a first-sentence-only rule would have deleted: it opens with the
    # exact prompt text and then says the thing worth keeping.
    "Recording stopped. I'll redo that bit.",
    "Check the screen on the hoist, it's throwing a fault.",
])
def test_people_talking_about_recording_are_kept(text):
    """A person discussing the recorder is content, often content about a gap in
    the record — the single most useful thing they could say."""
    assert not les.is_device_announcement(text), text


def test_a_long_turn_is_never_an_announcement():
    """The length guard is the second half of the whole-turn promise: prompts
    are short and fixed, so anything long is a person."""
    long_turn = "please stop recording " * 10
    assert len(long_turn) > les.DEVICE_ANNOUNCEMENT_MAX_CHARS
    assert not les.is_device_announcement(long_turn)


def test_empty_and_missing_text_are_not_announcements():
    assert not les.is_device_announcement("")
    assert not les.is_device_announcement(None)


# ------------------------------------------------------------------
# The filter, and what it reports
# ------------------------------------------------------------------

def _turn(speaker, text):
    return {"abs_start_str": "09:00:00", "speaker": speaker, "text": text}


def test_filtering_removes_the_announcements_and_keeps_the_rest():
    turns = [
        _turn("spk_0", "The scaffold tags need checking before the pour."),
        _turn("spk_2", "Recording started."),
        _turn("spk_1", "Yeah, I'll get Dave onto it this arvo."),
        _turn("spk_3", "Please stop recording."),
    ]
    kept, stats = les.filter_device_announcements(turns)

    assert [t["speaker"] for t in kept] == ["spk_0", "spk_1"]
    assert stats["removed"] == 2
    assert stats["texts"] == ["Please stop recording.", "Recording started."]


def test_a_speaker_who_only_ever_announced_leaves_the_speaker_count():
    """This is the damage the roadmap measured: speaker_count 4 with a device
    among them. speaker_count == 1 is a gate item-writer uses to resolve a
    self-referential responsible party to a real name, so an inflated count
    silently turns a resolvable name into a guess."""
    turns = [
        _turn("spk_0", "We're on the east elevation this morning."),
        _turn("spk_1", "Recording started."),
        _turn("spk_1", "Recording stopped."),
    ]
    kept, _ = les.filter_device_announcements(turns)
    assert len({t["speaker"] for t in kept}) == 1


def test_the_distinct_phrases_are_reported_not_just_a_count():
    """The wording the recorders will use is not settled — res/raw/*.mp3 was
    can change wording without telling the backend. The count alone would
    not tell anyone what to add to the pattern list."""
    turns = [_turn("spk_1", "Recording started."), _turn("spk_1", "Recording started.")]
    _, stats = les.filter_device_announcements(turns)
    assert stats["removed"] == 2
    assert stats["texts"] == ["Recording started."], "distinct, not repeated"


def test_nothing_removed_reports_cleanly():
    turns = [_turn("spk_0", "All good on the south side.")]
    kept, stats = les.filter_device_announcements(turns)
    assert kept == turns and stats == {"removed": 0, "texts": []}


# ------------------------------------------------------------------
# Configuration
# ------------------------------------------------------------------

def test_patterns_are_overridable_without_a_deploy(monkeypatch):
    monkeypatch.setenv("DEVICE_ANNOUNCEMENT_PATTERNS",
                       '["battery low", "(please\\\\s+)?stop\\\\s+recording"]')
    assert les.is_device_announcement("Battery low.")
    assert les.is_device_announcement("Please stop recording.")
    assert not les.is_device_announcement("Recording started."), (
        "an override replaces the defaults rather than adding to them")


def test_an_empty_override_list_means_defaults_not_disabled(monkeypatch):
    """`[]` is exactly what both deploy workflows send when the repo variable is
    unset — SAM's --parameter-overrides rejects a bare "Key=" with an empty
    value, so the fallback has to be a non-empty token. Reading `[]` as "filter
    nothing" would silently disable the feature on every stack that has not set
    the variable, which is all of them."""
    monkeypatch.setenv("DEVICE_ANNOUNCEMENT_PATTERNS", "[]")
    assert les.is_device_announcement("Recording started.")


def test_the_pattern_env_var_is_declared_in_the_template():
    """The docstring above says "overridable without a code deploy". That is
    only true if CloudFormation owns the variable: a value set on the live
    function is erased by the next reconcile, which is the trap the template
    comments for TRANSCRIPT_TEXT_LIMIT and NormaliseAudio were written to
    prevent. Pinned because the claim and the wiring can drift apart silently."""
    import os.path
    root = os.path.join(os.path.dirname(__file__), "..", "..")
    with open(os.path.join(root, "src", "template.yaml"), encoding="utf-8") as fh:
        template = fh.read()
    assert "DEVICE_ANNOUNCEMENT_PATTERNS: !Ref DeviceAnnouncementPatterns" in template
    assert "DeviceAnnouncementPatterns:" in template
    for wf in ("deploy.yml", "deploy-prod.yml"):
        with open(os.path.join(root, ".github", "workflows", wf), encoding="utf-8") as fh:
            assert "DeviceAnnouncementPatterns=" in fh.read(), wf


def test_a_malformed_override_falls_back_loudly(monkeypatch, caplog):
    """Silent fallback would read as "the announcements came back" and send the
    next person to the transcriber (BUG-40)."""
    caplog.set_level("WARNING")
    monkeypatch.setenv("DEVICE_ANNOUNCEMENT_PATTERNS", "not json at all")
    assert les.is_device_announcement("Recording started."), "defaults still apply"
    assert any("DEVICE_ANNOUNCEMENT_PATTERNS" in r.getMessage() for r in caplog.records)


def test_a_wrong_shaped_override_falls_back_loudly(monkeypatch, caplog):
    caplog.set_level("WARNING")
    monkeypatch.setenv("DEVICE_ANNOUNCEMENT_PATTERNS", '{"a": 1}')
    assert les.is_device_announcement("Recording started.")
    assert any("DEVICE_ANNOUNCEMENT_PATTERNS" in r.getMessage() for r in caplog.records)


# ------------------------------------------------------------------
# Wiring
# ------------------------------------------------------------------

def test_the_filter_runs_before_speaker_count_is_taken():
    import inspect
    src = inspect.getsource(les.extract_session)
    assert src.index("filter_device_announcements") < src.index("'speaker_count'"), (
        "filtering after the count leaves the device in it")


def test_the_extraction_records_what_was_filtered():
    import inspect
    src = inspect.getsource(les)
    assert "'device_announcements': announcement_stats" in src
